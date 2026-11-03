"""Point-in-time (as-of) access to stored observations (ADR-0007, ADR-0009).

`series_as_of(session, series_id, as_of)` returns, for each period, the version that was
active at instant `as_of`:

    vintage_start <= as_of  AND  (vintage_end IS NULL OR as_of < vintage_end)

so at exactly a version's vintage_start the new version is returned, and at exactly its
vintage_end the old version is excluded. Nothing with vintage_start > as_of is ever returned, so
results cannot contain information from after `as_of` (no look-ahead).

What a version's timestamps mean depends on the series' vintage basis, which every result
carries:
- "source_vintage" (FRED/ALFRED): the provider's real-time vintages;
- "first_seen" (EIA): when Market Pulse first retrieved the value. EIA publishes no vintages, so
  a period first retrieved after `as_of` is simply absent: history before Market Pulse's first
  ingestion is not reconstructed or invented.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from marketpulse.db.models import Observation, Series


@dataclass(frozen=True)
class PointInTimeObservation:
    period_start: date
    period_end: date
    value: Decimal | None
    status: str
    vintage_start: datetime
    vintage_end: datetime | None
    source_period: str | None


@dataclass(frozen=True)
class PointInTimeSeries:
    series_id: int
    code: str
    vintage_basis: str  # "source_vintage" or "first_seen"
    as_of: datetime
    observations: list[PointInTimeObservation]


class PointInTimeIntegrityError(RuntimeError):
    """More than one version of a period was active at one instant (should never happen)."""


def _series(session: Session, series_id: int) -> Series:
    series = session.get(Series, series_id)
    if series is None:
        raise ValueError(f"series id {series_id} does not exist")
    return series


def series_as_of(
    session: Session,
    series_id: int,
    as_of: datetime,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> PointInTimeSeries:
    """The series as known at `as_of`, optionally limited to periods in [period_start,
    period_end] (by period_start), ordered by period."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    series = _series(session, series_id)
    o = Observation
    query = (
        select(
            o.period_start,
            o.period_end,
            o.value,
            o.status,
            o.vintage_start,
            o.vintage_end,
            o.source_period,
        )
        .where(
            o.series_id == series_id,
            o.vintage_start <= as_of,
            or_(o.vintage_end.is_(None), o.vintage_end > as_of),
        )
        .order_by(o.period_start)
    )
    if period_start is not None:
        query = query.where(o.period_start >= period_start)
    if period_end is not None:
        query = query.where(o.period_start <= period_end)
    observations = [PointInTimeObservation(*row) for row in session.execute(query)]
    seen: set[date] = set()
    for obs in observations:
        if obs.period_start in seen:
            raise PointInTimeIntegrityError(
                f"series {series_id}: two versions of {obs.period_start} active at {as_of}"
            )
        seen.add(obs.period_start)
    return PointInTimeSeries(series_id, series.code, series.vintage_basis, as_of, observations)


def value_as_of(
    session: Session, series_id: int, period_start: date, as_of: datetime
) -> tuple[str, PointInTimeObservation | None]:
    """(vintage basis, the version of one period active at `as_of`, or None if unknown then)."""
    result = series_as_of(
        session, series_id, as_of, period_start=period_start, period_end=period_start
    )
    return result.vintage_basis, (result.observations[0] if result.observations else None)
