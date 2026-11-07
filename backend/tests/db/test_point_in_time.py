"""Point-in-time queries (analytics.point_in_time) against PostgreSQL, in a rolled-back
transaction. All values are SYNTHETIC."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from marketpulse.analytics.point_in_time import (
    PointInTimeIntegrityError,
    series_as_of,
    value_as_of,
)
from marketpulse.db.models import IngestionRun, Observation, Series, Source

T1 = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=7)
T3 = T2 + timedelta(days=7)
P = date(2026, 9, 1)  # period known from T1, revised at T2
Q = date(2026, 9, 8)  # period first known at T3
TICK = timedelta(microseconds=1)


def make_series(session: Session, code: str, basis: str) -> int:
    source = Source(
        code=f"{code}_src",
        name="Test source",
        homepage_url="https://example.invalid",
        license="public_domain",
        access_requirement="registration_required",
    )
    session.add(source)
    session.flush()
    series = Series(
        source_id=source.id,
        source_series_id=code.upper(),
        code=code,
        name="Synthetic",
        category="energy",
        frequency="weekly",
        period_convention="week_ending_friday",
        units="Synthetic",
        original_source="Synthetic",
        reference_url="https://example.invalid",
        max_staleness=timedelta(days=14),
        vintage_basis=basis,
    )
    session.add(series)
    session.flush()
    return series.id


def version(
    session: Session,
    series_id: int,
    period: date,
    start: datetime,
    end: datetime | None,
    value: str,
) -> None:
    run = IngestionRun(job="test.pit")
    session.add(run)
    session.flush()
    session.add(
        Observation(
            series_id=series_id,
            period_start=period,
            period_end=period + timedelta(days=6),
            value=Decimal(value),
            status="ok",
            vintage_start=start,
            vintage_end=end,
            first_retrieved_at=start,
            last_confirmed_at=start,
            ingestion_run_id=run.id,
            source_period=(period + timedelta(days=6)).isoformat(),
        )
    )
    session.flush()


@pytest.fixture(params=["first_seen", "source_vintage"])
def history(request: pytest.FixtureRequest, session: Session) -> tuple[int, str]:
    """P: 1.0 during [T1, T2), then 2.0 from T2; Q: 5.0 from T3. Same semantics for both bases."""
    basis = request.param
    sid = make_series(session, f"pit_{basis}", basis)
    version(session, sid, P, T1, T2, "1.0")
    version(session, sid, P, T2, None, "2.0")
    version(session, sid, Q, T3, None, "5.0")
    return sid, basis


def values(session: Session, sid: int, at: datetime) -> dict[date, Decimal | None]:
    return {o.period_start: o.value for o in series_as_of(session, sid, at).observations}


def test_nothing_is_known_before_the_first_version(
    session: Session, history: tuple[int, str]
) -> None:
    sid, basis = history
    result = series_as_of(session, sid, T1 - TICK)
    assert (result.observations, result.vintage_basis) == ([], basis)


def test_new_version_is_selected_at_exactly_vintage_start(
    session: Session, history: tuple[int, str]
) -> None:
    sid, _ = history
    assert values(session, sid, T1) == {P: Decimal("1.0")}
    assert values(session, sid, T2 - TICK) == {P: Decimal("1.0")}
    assert values(session, sid, T2) == {P: Decimal("2.0")}  # old version excluded at its end


def test_periods_first_known_later_are_absent_until_then(
    session: Session, history: tuple[int, str]
) -> None:
    sid, _ = history
    assert Q not in values(session, sid, T3 - TICK)
    assert values(session, sid, T3) == {P: Decimal("2.0"), Q: Decimal("5.0")}
    assert values(session, sid, T3 + timedelta(days=365)) == {P: Decimal("2.0"), Q: Decimal("5.0")}


def test_result_carries_the_vintage_basis_and_version_times(
    session: Session, history: tuple[int, str]
) -> None:
    sid, basis = history
    result = series_as_of(session, sid, T2)
    assert result.vintage_basis == basis and result.as_of == T2
    (obs,) = result.observations
    assert (obs.vintage_start, obs.vintage_end, obs.source_period) == (
        T2,
        None,
        (P + timedelta(days=6)).isoformat(),
    )


def test_value_as_of_and_period_filters(session: Session, history: tuple[int, str]) -> None:
    sid, basis = history
    assert value_as_of(session, sid, Q, T2) == (basis, None)  # not yet known
    got_basis, obs = value_as_of(session, sid, P, T2 - TICK)
    assert got_basis == basis and obs is not None and obs.value == Decimal("1.0")
    only_q = series_as_of(session, sid, T3, period_start=Q)
    assert [o.period_start for o in only_q.observations] == [Q]
    only_p = series_as_of(session, sid, T3, period_end=P)
    assert [o.period_start for o in only_p.observations] == [P]


def test_naive_or_unknown_inputs_are_refused(session: Session, history: tuple[int, str]) -> None:
    sid, _ = history
    with pytest.raises(ValueError, match="timezone-aware"):
        series_as_of(session, sid, datetime(2026, 10, 1))  # noqa: DTZ001  (naive on purpose)
    with pytest.raises(ValueError, match="does not exist"):
        series_as_of(session, 999_999, T1)


def test_overlapping_versions_are_reported_not_hidden(session: Session) -> None:
    sid = make_series(session, "pit_broken", "first_seen")
    version(session, sid, P, T1, T3, "1.0")  # overlaps the next version (corrupt history)
    version(session, sid, P, T2, None, "2.0")
    with pytest.raises(PointInTimeIntegrityError, match="two versions"):
        series_as_of(session, sid, T2)
