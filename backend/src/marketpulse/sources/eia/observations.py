"""Convert validated EIA rows into dated observations (no versioning here).

Each observation keeps EIA's original period string next to the computed period bounds, the
exact Decimal value, and an explicit status: "ok", or "missing_at_source" when EIA reported null.
Vintage/retrieval timestamps are added later by the ingestion layer (snapshot versioning), so
source periods and retrieval times stay distinct.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from marketpulse.core.versions import STATUS_MISSING_AT_SOURCE, STATUS_OK
from marketpulse.sources.eia.parse import EiaRow
from marketpulse.sources.eia.periods import period_bounds
from marketpulse.sources.eia.validate import EiaSeriesExpectation


@dataclass(frozen=True)
class EiaObservation:
    source_period: str  # exactly as EIA reported it
    period_start: date
    period_end: date
    value: Decimal | None
    status: str


def to_observations(rows: Iterable[EiaRow], expected: EiaSeriesExpectation) -> list[EiaObservation]:
    observations = []
    for row in rows:
        start, end = period_bounds(row.period, expected.frequency, expected.period_convention)
        observations.append(
            EiaObservation(
                source_period=row.period,
                period_start=start,
                period_end=end,
                value=row.value,
                status=STATUS_MISSING_AT_SOURCE if row.value is None else STATUS_OK,
            )
        )
    return observations
