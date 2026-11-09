"""Response models of the read-only data API.

Values are exact decimals, serialized as JSON strings ("4.190"), never rounded floats. Every
series carries its source, attribution, license, and freshness, and every observation keeps its
period (what the value describes) separate from its retrieval and vintage times (when Market
Pulse or the provider knew it).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

# "database": real stored observations. "synthetic": generated development fixtures, never
# market data (see marketpulse.dev_app).
DataMode = Literal["database", "synthetic"]
FreshnessStatus = Literal["fresh", "stale", "unavailable"]


class Freshness(BaseModel):
    """Whether newer data should exist, judged from the latest period's end date.

    `fresh`: the latest period ended no more than `max_staleness_days` before
    `reference_date`. `stale`: longer ago. `unavailable`: nothing stored.
    """

    status: FreshnessStatus
    reference_date: date  # the UTC date the judgment was made on
    latest_period_end: date | None
    age_days: int | None  # reference_date - latest_period_end
    max_staleness_days: int


class SourceInfo(BaseModel):
    code: str
    name: str
    attribution: str | None
    terms_url: str | None


class LatestObservation(BaseModel):
    period_start: date
    period_end: date
    source_period: str | None  # the period exactly as the provider labels it, when recorded
    value: Decimal
    status: str
    vintage_start: datetime
    first_retrieved_at: datetime  # when Market Pulse first retrieved this value
    last_confirmed_at: datetime  # when Market Pulse last saw it unchanged


class SeriesSummary(BaseModel):
    code: str
    name: str
    description: str | None
    category: str
    frequency: str
    period_convention: str
    units: str
    seasonal_adjustment: str | None
    original_source: str
    reference_url: str
    vintage_basis: str
    license: str  # the effective license (series override, else the source's)
    source: SourceInfo
    synthetic: bool
    latest: LatestObservation | None
    freshness: Freshness


class SeriesListResponse(BaseModel):
    data_mode: DataMode
    generated_at: datetime
    series: list[SeriesSummary]


class Observation(BaseModel):
    period_start: date
    period_end: date
    source_period: str | None
    value: Decimal | None  # None only with status "missing_at_source"
    status: str
    vintage_start: datetime
    vintage_end: datetime | None


class ObservationsResponse(BaseModel):
    data_mode: DataMode
    series: SeriesSummary
    as_of: datetime | None  # set for a point-in-time (as-of) query
    start: date | None
    end: date | None
    limit: int
    offset: int
    total: int  # observations matching start/end (and as_of), before limit/offset
    observations: list[Observation]


# --- errors -------------------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Body of a 404 or 503 answer. Unknown and not-permitted series get the same 404."""

    detail: str


class ValidationIssue(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ValidationErrorResponse(BaseModel):
    """Body of a 422 answer: a list of issues for malformed parameters (FastAPI's validation),
    or a single message for an inconsistent combination (e.g. start after end)."""

    detail: str | list[ValidationIssue]


# Declared on every route that reads the database (see api.main: DBAPIError -> 503).
DATABASE_UNAVAILABLE: dict[int | str, dict[str, object]] = {
    503: {"model": ErrorResponse, "description": "Database unavailable"}
}


# --- calculated metrics (Phase 4) ---------------------------------------------------------------


class ObservationRef(BaseModel):
    """A current-version observation a calculation used, exactly as stored."""

    period_start: date
    period_end: date
    source_period: str | None
    value: Decimal | None
    status: str


class CalculatedMetric(BaseModel):
    """A value derived by Market Pulse, never published by the source. `methodology` and the
    referenced observations are enough to reproduce it."""

    kind: Literal["period_change", "year_over_year", "seasonal_range", "trailing_extremes"]
    label: str
    status: Literal["ok", "insufficient_history", "missing_comparison"]
    reason: str | None
    methodology: str
    current: ObservationRef
    comparison: ObservationRef | None
    absolute_change: Decimal | None
    change_units: str | None
    basis_points: Decimal | None
    percent_change: Decimal | None  # rounded to 2 decimals
    percent_note: str | None
    window: list[ObservationRef]
    minimum: ObservationRef | None
    maximum: ObservationRef | None
    position_percent: Decimal | None  # (current - min) / (max - min) x 100, 1 decimal
    position: Literal["below", "within", "above"] | None


class NotCalculated(BaseModel):
    kind: str
    reason: str


class SeriesMetrics(BaseModel):
    code: str
    name: str
    units: str
    frequency: str
    period_convention: str
    source: SourceInfo
    synthetic: bool
    calculated: Literal[True] = True  # every value in `metrics` is derived, not observed
    basis: Literal["current_versions"] = "current_versions"
    current: ObservationRef | None
    metrics: list[CalculatedMetric]
    not_calculated: list[NotCalculated]


class MetricsResponse(BaseModel):
    data_mode: DataMode
    generated_at: datetime
    series: list[SeriesMetrics]
