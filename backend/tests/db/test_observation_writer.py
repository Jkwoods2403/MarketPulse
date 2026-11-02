"""write_versions against PostgreSQL, inside one rolled-back test transaction.

Uses the real recorded FRED fixtures where possible; other data is synthetic and labelled.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from fred_mock import fixture
from marketpulse.core.versions import ObservationVersion
from marketpulse.db.models import DataQualityIssue, IngestionRun, Observation, Series, Source
from marketpulse.ingestion.observation_writer import (
    OBSERVATION_LOCK_NAMESPACE,
    write_versions,
)
from marketpulse.sources.fred.parse import parse_observations
from marketpulse.sources.fred.versions import to_versions, vintage_instant

T1 = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=1)


@pytest.fixture
def series_id(session: Session) -> int:
    source = Source(
        code="fred_test",
        name="FRED (test)",
        homepage_url="https://example.invalid",
        license="attribution_required",
    )
    session.add(source)
    session.flush()
    series = Series(
        source_id=source.id,
        source_series_id="GDPC1",
        code="real_gdp_test",
        name="Real Gross Domestic Product",
        category="macro",
        frequency="quarterly",
        period_convention="quarter",
        units="Billions of Chained 2017 Dollars",
        original_source="U.S. Bureau of Economic Analysis",
        reference_url="https://example.invalid/GDPC1",
        max_staleness=timedelta(days=125),
        vintage_basis="source_vintage",
    )
    session.add(series)
    session.flush()
    return series.id


def new_run(session: Session) -> int:
    run = IngestionRun(job="test.observations")
    session.add(run)
    session.flush()
    return run.id


def gdp_versions() -> list[ObservationVersion]:
    pages = [parse_observations(fixture(f"observations_gdpc1_2024q1q2_page{n}")) for n in (1, 2)]
    return to_versions(pages[0].observations + pages[1].observations, "quarterly")


def rows(session: Session, series_id: int) -> list[Observation]:
    session.expire_all()
    return list(
        session.scalars(
            select(Observation)
            .where(Observation.series_id == series_id)
            .order_by(Observation.period_start, Observation.vintage_start)
        )
    )


def as_of(session: Session, series_id: int, period: date, instant: datetime) -> Decimal | None:
    """Point-in-time lookup: the value of `period` as known at `instant`."""
    return session.scalar(
        text(
            "SELECT value FROM obs.observations WHERE series_id = :s AND period_start = :p "
            "AND vintage_start <= :t AND (vintage_end IS NULL OR vintage_end > :t)"
        ),
        {"s": series_id, "p": period, "t": instant},
    )


# --- first ingestion ---------------------------------------------------------------------------


def test_first_ingestion_stores_real_revision_history_exactly(
    session: Session, series_id: int
) -> None:
    versions = gdp_versions()
    result = write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=versions,
        retrieved_at=T1,
    )

    assert (result.fetched, result.inserted, result.revised, result.unchanged) == (11, 11, 0, 0)
    stored = rows(session, series_id)
    assert len(stored) == 11
    q1 = [r for r in stored if r.period_start == date(2024, 1, 1)]
    assert [str(r.value) for r in q1] == [
        "22768.866",
        "22749.846",
        "22758.752",
        "23053.545",
        "23082.119",
        "23115.975",
    ]  # verbatim from FRED, no rounding
    assert q1[0].vintage_start == datetime(2024, 4, 25, 5, tzinfo=UTC)  # midnight CDT
    assert all(r.first_retrieved_at == T1 == r.last_confirmed_at for r in stored)
    assert sum(r.vintage_end is None for r in q1) == 1


def test_point_in_time_queries_return_the_value_known_then(
    session: Session, series_id: int
) -> None:
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=gdp_versions(),
        retrieved_at=T1,
    )
    q1 = date(2024, 1, 1)
    assert as_of(session, series_id, q1, vintage_instant(date(2024, 4, 24))) is None  # unpublished
    assert as_of(session, series_id, q1, vintage_instant(date(2024, 5, 1))) == Decimal("22768.866")
    assert as_of(session, series_id, q1, vintage_instant(date(2024, 6, 1))) == Decimal("22749.846")
    assert as_of(session, series_id, q1, T1) == Decimal("23115.975")


def test_missing_values_are_stored_as_null_with_status(session: Session, series_id: int) -> None:
    page = parse_observations(fixture("observations_dgs10_holidays_window"))
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=to_versions(page.observations, "daily"),
        retrieved_at=T1,
    )
    by_day = {r.period_start: r for r in rows(session, series_id)}
    assert len(by_day) == 8  # no rows invented for weekends
    assert by_day[date(2025, 12, 25)].value is None
    assert by_day[date(2025, 12, 25)].status == "missing_at_source"
    assert by_day[date(2026, 1, 1)].value is None
    assert by_day[date(2025, 12, 24)].value == Decimal("4.15")


# --- repeat ingestion --------------------------------------------------------------------------


def test_repeat_with_no_changes_inserts_nothing(session: Session, series_id: int) -> None:
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=gdp_versions(),
        retrieved_at=T1,
    )
    result = write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=gdp_versions(),
        retrieved_at=T2,
    )

    assert (result.inserted, result.revised, result.unchanged) == (0, 0, 11)
    stored = rows(session, series_id)
    assert len(stored) == 11
    assert all(r.first_retrieved_at == T1 and r.last_confirmed_at == T2 for r in stored)


# --- revisions ---------------------------------------------------------------------------------

Q3 = date(2026, 7, 1)  # synthetic period


def v(start: datetime, end: datetime | None, value: str | None) -> ObservationVersion:
    return ObservationVersion(
        Q3,
        date(2026, 9, 30),
        start,
        end,
        Decimal(value) if value else None,
        "ok" if value else "missing_at_source",
    )


A, B, C = (
    vintage_instant(date(2026, 10, 29)),
    vintage_instant(date(2026, 11, 26)),
    vintage_instant(date(2026, 12, 23)),
)


def test_revision_closes_old_version_and_inserts_new(session: Session, series_id: int) -> None:
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=[v(A, None, "24000.5")],
        retrieved_at=T1,
    )
    result = write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=[v(A, B, "24000.5"), v(B, None, "24010.25")],
        retrieved_at=T2,
    )

    assert (result.inserted, result.revised, result.unchanged) == (1, 1, 0)
    first, second = rows(session, series_id)
    assert (first.value, first.vintage_end, first.first_retrieved_at) == (Decimal("24000.5"), B, T1)
    assert first.last_confirmed_at == T2
    assert (second.value, second.vintage_start, second.vintage_end) == (
        Decimal("24010.25"),
        B,
        None,
    )


def test_successive_revisions_keep_full_history(session: Session, series_id: int) -> None:
    run = new_run(session)
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=run,
        versions=[v(A, None, "1.0")],
        retrieved_at=T1,
    )
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=run,
        versions=[v(A, B, "1.0"), v(B, None, "1.1")],
        retrieved_at=T2,
    )
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=run,
        versions=[v(A, B, "1.0"), v(B, C, "1.1"), v(C, None, None)],
        retrieved_at=T2 + timedelta(days=30),
    )
    history = rows(session, series_id)
    assert [(r.value, r.status) for r in history] == [
        (Decimal("1.0"), "ok"),
        (Decimal("1.1"), "ok"),
        (None, "missing_at_source"),
    ]
    assert [r.vintage_end for r in history] == [B, C, None]


def test_incremental_fetch_with_cut_start_confirms_and_revises(
    session: Session, series_id: int
) -> None:
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=[v(A, None, "1.0")],
        retrieved_at=T1,
    )
    cs = vintage_instant(date(2026, 11, 20))  # fetch window starts after A
    result = write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=[v(cs, B, "1.0"), v(B, None, "1.1")],
        retrieved_at=T2,
        coverage_start=cs,
    )
    assert (result.inserted, result.revised, result.unchanged) == (1, 1, 0)
    first, second = rows(session, series_id)
    assert (first.vintage_start, first.vintage_end) == (A, B)  # original start preserved
    assert (second.vintage_start, second.vintage_end) == (B, None)


# --- conflicts and quality issues -----------------------------------------------------------


def test_conflicting_value_is_recorded_and_history_untouched(
    session: Session, series_id: int
) -> None:
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=[v(A, None, "1.0")],
        retrieved_at=T1,
    )
    run = new_run(session)
    result = write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=run,
        versions=[v(A, None, "2.0")],
        retrieved_at=T2,
    )

    assert result.has_errors and result.periods_skipped == 1
    (only,) = rows(session, series_id)
    assert (only.value, only.last_confirmed_at) == (Decimal("1.0"), T1)  # not even "confirmed"
    issue = session.scalars(
        select(DataQualityIssue).where(DataQualityIssue.ingestion_run_id == run)
    ).one()
    assert (issue.check_name, issue.severity, issue.period_start) == ("value_conflict", "error", Q3)
    assert "1.0" in issue.detail and "2.0" in issue.detail


def test_vanished_version_is_kept_and_warned(session: Session, series_id: int) -> None:
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=[v(A, B, "1.0"), v(B, None, "1.1")],
        retrieved_at=T1,
    )
    run = new_run(session)
    result = write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=run,
        versions=[v(B, None, "1.1")],
        retrieved_at=T2,
    )
    assert not result.has_errors
    assert len(rows(session, series_id)) == 2
    assert [i.check_name for i in result.issues] == ["vanished_version"]


# --- locking and validation --------------------------------------------------------------------


def test_series_advisory_lock_is_held_until_transaction_ends(
    session: Session, series_id: int
) -> None:
    write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=[v(A, None, "1.0")],
        retrieved_at=T1,
    )
    held = session.scalar(
        text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND classid = :ns AND objid = :sid AND pid = pg_backend_pid()"
        ),
        {"ns": OBSERVATION_LOCK_NAMESPACE, "sid": series_id},
    )
    assert held == 1


def test_unknown_series_is_rejected(session: Session) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        write_versions(
            session,
            series_id=999_999,
            ingestion_run_id=new_run(session),
            versions=[],
            retrieved_at=T1,
        )


def test_naive_retrieval_time_is_rejected(session: Session, series_id: int) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        write_versions(
            session,
            series_id=series_id,
            ingestion_run_id=new_run(session),
            versions=[],
            retrieved_at=datetime(2026, 11, 1),  # noqa: DTZ001  (naive on purpose)
        )


def test_empty_input_changes_nothing(session: Session, series_id: int) -> None:
    result = write_versions(
        session,
        series_id=series_id,
        ingestion_run_id=new_run(session),
        versions=[],
        retrieved_at=T1,
    )
    assert (result.fetched, result.inserted) == (0, 0)
    assert session.scalar(select(func.count()).select_from(Observation)) == 0
