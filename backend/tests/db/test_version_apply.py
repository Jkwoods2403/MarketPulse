"""Shared apply helper (ingestion.version_apply) against PostgreSQL. SYNTHETIC values."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from marketpulse.db.models import IngestionRun, Observation, Series, Source
from marketpulse.ingestion.version_apply import (
    ConcurrentModificationError,
    close_versions,
    confirm_versions,
)

T1 = datetime(2026, 10, 5, tzinfo=UTC)
T2 = T1 + timedelta(days=1)


@pytest.fixture
def stored(session: Session) -> tuple[int, int, int]:
    """(current id, closed id, run id) for one synthetic series."""
    source = Source(
        code="apply_test",
        name="Test",
        homepage_url="https://example.invalid",
        license="public_domain",
    )
    session.add(source)
    session.flush()
    series = Series(
        source_id=source.id,
        source_series_id="APPLY",
        code="apply_test",
        name="Synthetic",
        category="energy",
        frequency="weekly",
        period_convention="week_ending",
        units="Synthetic",
        original_source="Synthetic",
        reference_url="https://example.invalid",
        max_staleness=timedelta(days=14),
        vintage_basis="first_seen",
    )
    run = IngestionRun(job="test.apply")
    session.add_all([series, run])
    session.flush()

    def row(period: date, end: datetime | None) -> Observation:
        return Observation(
            series_id=series.id,
            period_start=period,
            period_end=period,
            value=Decimal("1"),
            status="ok",
            vintage_start=T1 - timedelta(days=1),
            vintage_end=end,
            first_retrieved_at=T1,
            last_confirmed_at=T1,
            ingestion_run_id=run.id,
        )

    current, closed = row(date(2026, 1, 2), None), row(date(2026, 1, 9), T1)
    session.add_all([current, closed])
    session.flush()
    return current.id, closed.id, run.id


def test_closing_a_version_that_is_not_current_raises(
    session: Session, stored: tuple[int, int, int]
) -> None:
    _, closed, _ = stored
    with pytest.raises(ConcurrentModificationError, match="not open"):
        close_versions(session, [(closed, T2)])


def test_close_without_confirmation_leaves_last_confirmed_at(
    session: Session, stored: tuple[int, int, int]
) -> None:
    current, _, _ = stored
    close_versions(session, [(current, T2)])
    session.expire_all()
    row = session.get(Observation, current)
    assert row is not None and (row.vintage_end, row.last_confirmed_at) == (T2, T1)


def test_current_only_confirmation_sets_run_and_checks_every_row(
    session: Session, stored: tuple[int, int, int]
) -> None:
    current, closed, run = stored
    confirm_versions(session, [current], confirmed_at=T2, run_id=run, current_only=True)
    session.expire_all()
    row = session.get(Observation, current)
    assert row is not None and (row.last_confirmed_at, row.last_confirmed_run_id) == (T2, run)

    with pytest.raises(ConcurrentModificationError, match="1 of 2"):  # closed row refused
        confirm_versions(
            session,
            [current, closed],
            confirmed_at=T2 + timedelta(1),
            run_id=run,
            current_only=True,
        )
    with pytest.raises(ConcurrentModificationError, match="0 of 1"):  # already confirmed then
        confirm_versions(session, [current], confirmed_at=T2, run_id=run, current_only=True)


def test_plain_confirmation_also_confirms_closed_versions(
    session: Session, stored: tuple[int, int, int]
) -> None:
    """FRED semantics: provider vintages that ended are still confirmed as reported."""
    current, closed, _ = stored
    confirm_versions(session, [current, closed], confirmed_at=T2)
    session.expire_all()
    confirmed = session.execute(
        select(Observation.last_confirmed_at, Observation.last_confirmed_run_id).where(
            Observation.id.in_([current, closed])
        )
    ).all()
    assert [tuple(r) for r in confirmed] == [(T2, None), (T2, None)]  # no run id: FRED path
