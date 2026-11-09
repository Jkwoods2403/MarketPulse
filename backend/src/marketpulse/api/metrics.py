"""GET /metrics: calculated metrics for every permitted series (Phase 4).

Calculations use **current versions only** (the repository's default observation query filters
`vintage_end IS NULL`), from the same licensing-filtered repository as /series, so restricted
series and blocked sources are never calculated. Every value here is derived by Market Pulse,
not published by the source; see marketpulse.analytics.metrics for the methodology.

Which calculations apply is decided per series below; anything not applied is listed in
`not_calculated` with the reason, so the dashboard can say why a metric is absent.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import cast, get_args

from fastapi import APIRouter, Request

from marketpulse.analytics.metrics import Frequency, Metric, MetricPlan, Obs, calculate
from marketpulse.api.repository import SeriesMeta, SeriesRepository
from marketpulse.api.schemas import (
    DATABASE_UNAVAILABLE,
    CalculatedMetric,
    MetricsResponse,
    NotCalculated,
    ObservationRef,
    SeriesMetrics,
)

router = APIRouter(tags=["metrics"])

HISTORY_YEARS = 6  # five-year comparisons plus a year-over-year at the edge of the window
MAX_OBSERVATIONS = 100_000

SEASONAL_LEVEL = MetricPlan(seasonal_range=True)
DAILY_RATE = MetricPlan(basis_points=True)

# Per-series plans. Series not listed get period and year-over-year changes only.
PLANS: dict[str, MetricPlan] = {
    # EIA weekly inventories with a seasonal cycle.
    "l48_gas_storage": MetricPlan(seasonal_range=True, trailing_extremes=True),
    "us_crude_stocks_ex_spr": SEASONAL_LEVEL,
    "us_gasoline_stocks": SEASONAL_LEVEL,
    "us_distillate_stocks": SEASONAL_LEVEL,
    # Policy-driven inventory: a trailing range, not a seasonal one.
    "us_crude_stocks_spr": MetricPlan(trailing_extremes=True),
    # EIA monthly totals: seasonal comparison by calendar month.
    "us_dry_gas_production": SEASONAL_LEVEL,
    "us_lng_exports": SEASONAL_LEVEL,
    # FRED daily interest rates and spread: changes in basis points.
    "ust_3m": DAILY_RATE,
    "ust_2y": DAILY_RATE,
    "ust_10y": DAILY_RATE,
    "ust_30y": DAILY_RATE,
    "ust_10y_2y_spread": DAILY_RATE,
    "fed_funds_effective": DAILY_RATE,
}

# Why a calculation is not offered for a series (shown to users).
NOT_SEASONAL: dict[str, str] = {
    "us_crude_stocks_spr": "SPR levels follow policy releases and purchases, not a seasonal cycle.",
    "us_crude_production": "Not part of this series' metric plan (a production rate).",
    "us_refinery_utilization": "Not part of this series' metric plan.",
}


PREVIOUS = {"daily": "published observation", "monthly": "month", "quarterly": "quarter"}


def _not_calculated(meta: SeriesMeta, plan: MetricPlan) -> list[NotCalculated]:
    out: list[NotCalculated] = []
    if meta.frequency != "weekly":
        previous = PREVIOUS.get(meta.frequency, "observation")
        out.append(
            NotCalculated(
                kind="week_over_week",
                reason=f"The series is {meta.frequency}; it is compared with its previous "
                f"{previous} instead.",
            )
        )
    if not plan.seasonal_range:
        if meta.code in NOT_SEASONAL:
            reason = NOT_SEASONAL[meta.code]
        elif meta.seasonal_adjustment and meta.seasonal_adjustment.startswith("seasonally"):
            reason = "The series is seasonally adjusted; a seasonal range is not meaningful."
        elif meta.frequency == "daily":
            reason = "Daily rates and indexes are not compared by season."
        else:
            reason = "Not part of this series' metric plan."
        out.append(NotCalculated(kind="seasonal_range", reason=reason))
    if "percent" in meta.units.lower():
        out.append(
            NotCalculated(
                kind="percent_change",
                reason="The series is already a percentage; changes are in percentage points.",
            )
        )
    return out


def _ref(obs: Obs | None) -> ObservationRef | None:
    if obs is None:
        return None
    return ObservationRef(
        period_start=obs.period_start,
        period_end=obs.period_end,
        source_period=obs.source_period,
        value=obs.value,
        status=obs.status,
    )


def _metric(m: Metric) -> CalculatedMetric:
    current = _ref(m.current)
    if current is None:  # pragma: no cover  (a metric always has a current observation)
        raise ValueError("metric without a current observation")
    return CalculatedMetric(
        kind=m.kind,
        label=m.label,
        status=m.status,
        reason=m.reason,
        methodology=m.methodology,
        current=current,
        comparison=_ref(m.comparison),
        absolute_change=m.absolute_change,
        change_units=m.change_units,
        basis_points=m.basis_points,
        percent_change=m.percent_change,
        percent_note=m.percent_note,
        window=[ref for o in m.window if (ref := _ref(o)) is not None],
        minimum=_ref(m.minimum),
        maximum=_ref(m.maximum),
        position_percent=m.position_percent,
        position=m.position,
    )


def _history_start(latest: date) -> date:
    try:
        return latest.replace(year=latest.year - HISTORY_YEARS)
    except ValueError:  # 29 February
        return latest.replace(year=latest.year - HISTORY_YEARS, day=28)


def series_metrics(repository: SeriesRepository) -> list[SeriesMetrics]:
    out: list[SeriesMetrics] = []
    for meta, latest in repository.list_series():
        plan = PLANS.get(meta.code, MetricPlan())
        current: Obs | None = None
        metrics: list[Metric] = []
        if latest is not None and meta.frequency in get_args(Frequency):
            page = repository.observations(
                meta,
                start=_history_start(latest.period_start),
                end=None,
                as_of=None,  # current versions only
                limit=MAX_OBSERVATIONS,
                offset=0,
            )
            observations = [
                Obs(o.period_start, o.period_end, o.source_period, o.value, o.status)
                for o in page.observations
            ]
            current, metrics = calculate(
                observations, cast(Frequency, meta.frequency), meta.units, plan
            )
        out.append(
            SeriesMetrics(
                code=meta.code,
                name=meta.name,
                units=meta.units,
                frequency=meta.frequency,
                period_convention=meta.period_convention,
                source=meta.source,
                synthetic=repository.data_mode == "synthetic",
                current=_ref(current),
                metrics=[_metric(m) for m in metrics],
                not_calculated=_not_calculated(meta, plan),
            )
        )
    return out


@router.get("/metrics", response_model=MetricsResponse, responses=DATABASE_UNAVAILABLE)
def list_metrics(request: Request) -> MetricsResponse:
    """Calculated metrics (derived, not published) for every permitted series."""
    repository: SeriesRepository = request.app.state.repository
    clock: Callable[[], datetime] = request.app.state.clock
    return MetricsResponse(
        data_mode=repository.data_mode,
        generated_at=clock().astimezone(UTC),
        series=series_metrics(repository),
    )
