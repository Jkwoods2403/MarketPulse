"""The database itself enforces the lineage invariants (ADR-0007), not just application code.

The rows here are synthetic test records for exercising constraints, not market data.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from marketpulse.db.models import IngestionRun, Observation, Series, Source

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


@pytest.fixture
def series(session: Session) -> Series:
    source = Source(
        code="test_source",
        name="Test Source (synthetic)",
        homepage_url="https://example.invalid",
        license="public_domain",
    )
    session.add(source)
    session.flush()
    row = Series(
        source_id=source.id,
        source_series_id="TEST1",
        code="test_series",
        name="Synthetic test series",
        category="test",
        frequency="daily",
        period_convention="day",
        units="Index",
        max_staleness=timedelta(days=4),
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def run(session: Session) -> IngestionRun:
    row = IngestionRun(job="test.job")
    session.add(row)
    session.flush()
    return row


def observation(series: Series, run: IngestionRun, **overrides: Any) -> Observation:
    fields: dict[str, Any] = {
        "series_id": series.id,
        "period_start": date(2026, 1, 2),
        "period_end": date(2026, 1, 2),
        "value": Decimal("1.2345"),
        "status": "ok",
        "vintage_start": T0,
        "first_retrieved_at": T0,
        "last_confirmed_at": T0,
        "ingestion_run_id": run.id,
    }
    fields.update(overrides)
    return Observation(**fields)


def assert_rejected(session: Session, row: object) -> None:
    session.add(row)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_valid_observation_is_stored_exactly(
    session: Session, series: Series, run: IngestionRun
) -> None:
    row = observation(series, run, value=Decimal("71.123456789012345"))
    session.add(row)
    session.flush()
    session.refresh(row)
    assert row.value == Decimal("71.123456789012345")  # numeric: no float rounding


def test_null_value_allowed_only_when_missing_at_source(
    session: Session, series: Series, run: IngestionRun
) -> None:
    session.add(observation(series, run, value=None, status="missing_at_source"))
    session.flush()

    assert_rejected(
        session,
        observation(
            series,
            run,
            period_start=date(2026, 1, 3),
            period_end=date(2026, 1, 3),
            value=None,
            status="ok",
        ),
    )


def test_missing_at_source_cannot_carry_a_value(
    session: Session, series: Series, run: IngestionRun
) -> None:
    assert_rejected(session, observation(series, run, status="missing_at_source"))


def test_only_one_current_row_per_series_and_period(
    session: Session, series: Series, run: IngestionRun
) -> None:
    session.add(observation(series, run))
    session.flush()
    assert_rejected(session, observation(series, run, vintage_start=T0 + timedelta(days=1)))


def test_revision_closes_old_vintage_and_adds_new_current_row(
    session: Session, series: Series, run: IngestionRun
) -> None:
    original = observation(series, run)
    session.add(original)
    session.flush()

    revised_at = T0 + timedelta(days=7)
    original.vintage_end = revised_at
    session.flush()
    session.add(observation(series, run, value=Decimal("1.3"), vintage_start=revised_at))
    session.flush()

    rows = session.query(Observation).filter_by(series_id=series.id).all()
    assert len(rows) == 2
    assert sum(r.vintage_end is None for r in rows) == 1


def test_vintage_end_must_follow_vintage_start(
    session: Session, series: Series, run: IngestionRun
) -> None:
    assert_rejected(session, observation(series, run, vintage_end=T0))


def test_period_end_cannot_precede_period_start(
    session: Session, series: Series, run: IngestionRun
) -> None:
    assert_rejected(session, observation(series, run, period_end=date(2026, 1, 1)))


def test_unknown_status_is_rejected(session: Session, series: Series, run: IngestionRun) -> None:
    assert_rejected(session, observation(series, run, status="interpolated"))


def test_unknown_license_is_rejected(session: Session) -> None:
    assert_rejected(
        session,
        Source(code="bad", name="Bad", homepage_url="https://example.invalid", license="free"),
    )


def test_unknown_run_status_is_rejected(session: Session) -> None:
    assert_rejected(session, IngestionRun(job="test.job", status="done"))
