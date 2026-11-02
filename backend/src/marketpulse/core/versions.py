"""Provider-neutral representation of one version of one observation.

Provider adapters (e.g. sources.fred.versions) produce ObservationVersion values; the ingestion
layer compares them with stored history and persists them. Living in `core` lets both layers use
the type without depending on each other.

Semantics:
- [period_start, period_end] is the observed period (inclusive dates).
- [vintage_start, vintage_end) is when this value was the provider's published value; the end
  is exclusive, and None means "still current".
- value is None only when the provider explicitly reported no value (status
  "missing_at_source"); it is never filled, rounded, or interpolated.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

STATUS_OK = "ok"
STATUS_MISSING_AT_SOURCE = "missing_at_source"


@dataclass(frozen=True)
class ObservationVersion:
    period_start: date
    period_end: date
    vintage_start: datetime
    vintage_end: datetime | None
    value: Decimal | None
    status: str

    def __post_init__(self) -> None:
        if self.period_end < self.period_start:
            raise ValueError(
                f"period_end {self.period_end} precedes period_start {self.period_start}"
            )
        if self.vintage_start.tzinfo is None:
            raise ValueError("vintage_start must be timezone-aware")
        if self.vintage_end is not None:
            if self.vintage_end.tzinfo is None:
                raise ValueError("vintage_end must be timezone-aware")
            if self.vintage_end <= self.vintage_start:
                raise ValueError(
                    f"vintage_end {self.vintage_end} is not after "
                    f"vintage_start {self.vintage_start}"
                )
        if (self.value is None) != (self.status == STATUS_MISSING_AT_SOURCE):
            raise ValueError("value must be None exactly when status is 'missing_at_source'")
        if self.value is not None and not self.value.is_finite():
            raise ValueError(f"value must be finite, got {self.value}")

    @property
    def is_current(self) -> bool:
        return self.vintage_end is None
