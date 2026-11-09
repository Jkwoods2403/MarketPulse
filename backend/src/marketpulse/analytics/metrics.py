"""Calculated metrics from a series' current observations (Phase 4).

Pure functions over observations the caller has already restricted to **current versions**
(vintage_end IS NULL): no revision or superseded version ever enters a calculation. Inputs are
exact decimals as published; nothing is interpolated, carried forward, or filled in.

Methodology (also returned with every result, so each number can be reproduced):

- **Period change.** Latest observation with a value vs. the immediately preceding period of the
  same frequency: the week ending exactly 7 days earlier (weekly), the previous calendar month
  (monthly), the previous quarter (quarterly), or the previous published observation no more
  than 7 days earlier (daily; weekends and holidays have no observation). If that period is
  absent or was reported as missing at source, the change is not available.
- **Year over year.** The same period one year earlier: the same month or quarter (monthly,
  quarterly), the week ending within 3 days of the same calendar date (weekly), or the latest
  observation on or before the same calendar date, no more than 7 days earlier (daily).
- **Five-year seasonal range** (weekly and monthly levels with a seasonal pattern). For each of
  the previous five years, the period aligned with the latest one as for year over year. All
  five must have values; otherwise the range is reported as insufficient history. The current
  value's position is (current - min) / (max - min) * 100; below 0 or above 100 means it is
  outside the five-year range.
- **Trailing five-year extremes.** Minimum and maximum of every observation whose period ends
  within the five years up to the latest period end; requires stored history covering the
  whole window.

Change style follows the units: series measured in percent (interest rates, spreads,
unemployment, utilization) change in percentage points (and basis points for interest rates);
percent changes of percentages are not computed. Levels and indexes change in their own units
and in percent. Percent changes are rounded to two decimals; absolute changes are exact.
"""

import calendar
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

PERCENT_PLACES = Decimal("0.01")
POSITION_PLACES = Decimal("0.1")
WEEKLY_ALIGNMENT_DAYS = 3
DAILY_LOOKBACK_DAYS = 7
RANGE_YEARS = 5

Frequency = Literal["daily", "weekly", "monthly", "quarterly"]
ChangeStyle = Literal["level", "percentage_points"]
MetricKind = Literal["period_change", "year_over_year", "seasonal_range", "trailing_extremes"]
MetricStatus = Literal["ok", "insufficient_history", "missing_comparison"]


@dataclass(frozen=True)
class Obs:
    period_start: date
    period_end: date
    source_period: str | None
    value: Decimal | None  # None: reported as missing at source
    status: str


@dataclass(frozen=True)
class MetricPlan:
    """Which calculations are meaningful for a series."""

    period_change: bool = True
    year_over_year: bool = True
    seasonal_range: bool = False
    trailing_extremes: bool = False
    basis_points: bool = False  # also express percentage-point changes in basis points


@dataclass
class Metric:
    kind: MetricKind
    label: str
    status: MetricStatus
    methodology: str
    current: Obs
    reason: str | None = None
    comparison: Obs | None = None
    absolute_change: Decimal | None = None
    change_units: str | None = None
    basis_points: Decimal | None = None
    percent_change: Decimal | None = None
    percent_note: str | None = None
    window: list[Obs] = field(default_factory=list)  # range/extremes: the observations used
    minimum: Obs | None = None
    maximum: Obs | None = None
    position_percent: Decimal | None = None
    position: Literal["below", "within", "above"] | None = None


def change_style(units: str) -> ChangeStyle:
    return "percentage_points" if "percent" in units.lower() else "level"


def _shift_years(day: date, years: int) -> date:
    year = day.year - years
    return date(year, day.month, min(day.day, calendar.monthrange(year, day.month)[1]))


def _shift_months(first_of_month: date, months: int) -> date:
    index = first_of_month.year * 12 + first_of_month.month - 1 - months
    return date(index // 12, index % 12 + 1, 1)


def _days_in_month(day: date) -> int:
    return calendar.monthrange(day.year, day.month)[1]


def _rounded(value: Decimal, places: Decimal) -> Decimal:
    return value.quantize(places, rounding=ROUND_HALF_EVEN)


class _Index:
    """Lookups over current observations, keyed the way each frequency labels its periods."""

    def __init__(self, observations: Sequence[Obs]) -> None:
        self.ordered = sorted(observations, key=lambda o: o.period_end)
        self.by_start = {o.period_start: o for o in self.ordered}
        self.by_end = {o.period_end: o for o in self.ordered}

    def latest_with_value(self) -> Obs | None:
        return next((o for o in reversed(self.ordered) if o.value is not None), None)

    def nearest_end(self, target: date, tolerance_days: int) -> Obs | None:
        best = None
        for offset in range(tolerance_days + 1):
            for candidate in (target - timedelta(days=offset), target + timedelta(days=offset)):
                if candidate in self.by_end:
                    best = self.by_end[candidate]
                    break
            if best is not None:
                return best
        return None

    def latest_on_or_before(self, target: date, lookback_days: int) -> Obs | None:
        for offset in range(lookback_days + 1):
            obs = self.by_end.get(target - timedelta(days=offset))
            if obs is not None and obs.value is not None:
                return obs
        return None


def _aligned(index: _Index, frequency: Frequency, current: Obs, years: int) -> Obs | None:
    """The period `years` years before `current`, aligned as documented above."""
    if frequency in ("monthly", "quarterly"):
        return index.by_start.get(_shift_years(current.period_start, years))
    if frequency == "weekly":
        return index.nearest_end(_shift_years(current.period_end, years), WEEKLY_ALIGNMENT_DAYS)
    return index.latest_on_or_before(_shift_years(current.period_end, years), DAILY_LOOKBACK_DAYS)


def _change(
    metric: Metric,
    comparison: Obs,
    values: tuple[Decimal, Decimal],  # (current, comparison), both checked non-missing
    units: str,
    style: ChangeStyle,
    plan: MetricPlan,
) -> Metric:
    now, then = values
    metric.comparison = comparison
    metric.absolute_change = now - then
    if style == "percentage_points":
        metric.change_units = "percentage points"
        if plan.basis_points:
            metric.basis_points = metric.absolute_change * 100
        metric.percent_note = "Not computed: the series is already a percentage."
    else:
        metric.change_units = units
        if then == 0:
            metric.percent_note = "Undefined: the comparison value is zero."
        else:
            metric.percent_change = _rounded(
                metric.absolute_change / abs(then) * 100, PERCENT_PLACES
            )
    return metric


def _missing(metric: Metric, what: str, comparison: Obs | None) -> Metric:
    metric.status = "missing_comparison"
    metric.comparison = comparison
    metric.reason = (
        f"{what} was reported as missing at source"
        if comparison is not None
        else f"no stored observation for {what}"
    )
    return metric


def period_change(
    index: _Index, frequency: Frequency, current: Obs, units: str, plan: MetricPlan
) -> Metric:
    style = change_style(units)
    if frequency == "weekly":
        label, target = "Week over week", current.period_end - timedelta(days=7)
        comparison = index.by_end.get(target)
        what = f"the week ending {target.isoformat()}"
        method = "Latest week vs. the week ending exactly 7 days earlier."
    elif frequency == "monthly":
        target = _shift_months(current.period_start, 1)
        label, comparison = "Month over month", index.by_start.get(target)
        what = f"{target:%Y-%m}"
        method = "Latest month vs. the previous calendar month."
    elif frequency == "quarterly":
        target = _shift_months(current.period_start, 3)
        label, comparison = "Quarter over quarter", index.by_start.get(target)
        what = f"the quarter starting {target.isoformat()}"
        method = "Latest quarter vs. the previous quarter (change in the level, not annualized)."
    else:
        label = "Change from previous observation"
        comparison = index.latest_on_or_before(
            current.period_end - timedelta(days=1), DAILY_LOOKBACK_DAYS - 1
        )
        what = f"a published observation in the {DAILY_LOOKBACK_DAYS} days before"
        method = (
            "Latest observation vs. the previous published observation no more than "
            f"{DAILY_LOOKBACK_DAYS} days earlier (no observation on weekends and holidays)."
        )
    metric = Metric(
        kind="period_change", label=label, status="ok", methodology=method, current=current
    )
    if current.value is None or comparison is None or comparison.value is None:
        return _missing(metric, what, comparison)
    _change(metric, comparison, (current.value, comparison.value), units, style, plan)
    if frequency == "monthly" and style == "level":
        days_now, days_then = (
            _days_in_month(current.period_start),
            _days_in_month(comparison.period_start),
        )
        if days_now != days_then:
            metric.methodology += (
                f" The months have {days_then} and {days_now} days; monthly totals are not "
                "adjusted for the difference."
            )
    return metric


def year_over_year(
    index: _Index, frequency: Frequency, current: Obs, units: str, plan: MetricPlan
) -> Metric:
    method = {
        "weekly": (
            "Latest week vs. the week ending within 3 days of the same calendar date one year "
            "earlier."
        ),
        "monthly": "Latest month vs. the same month one year earlier.",
        "quarterly": "Latest quarter vs. the same quarter one year earlier.",
        "daily": (
            "Latest observation vs. the latest observation on or before the same calendar date "
            f"one year earlier, no more than {DAILY_LOOKBACK_DAYS} days before it."
        ),
    }[frequency]
    metric = Metric(
        kind="year_over_year",
        label="Year over year",
        status="ok",
        methodology=method,
        current=current,
    )
    comparison = _aligned(index, frequency, current, 1)
    if current.value is None or comparison is None or comparison.value is None:
        if index.ordered and index.ordered[0].period_end > _shift_years(current.period_end, 1):
            metric.status = "insufficient_history"
            metric.reason = "less than one year of stored history"
            return metric
        return _missing(metric, "the same period one year earlier", comparison)
    return _change(
        metric, comparison, (current.value, comparison.value), units, change_style(units), plan
    )


def seasonal_range(index: _Index, frequency: Frequency, current: Obs) -> Metric:
    alignment = (
        "the week ending within 3 days of the same calendar date"
        if frequency == "weekly"
        else "the same month"
    )
    metric = Metric(
        kind="seasonal_range",
        label="Five-year seasonal range",
        status="ok",
        methodology=(
            f"Minimum and maximum of {alignment} in each of the previous {RANGE_YEARS} years "
            "(the current year excluded). Position = (current - min) / (max - min) x 100."
        ),
        current=current,
    )
    window = [_aligned(index, frequency, current, k) for k in range(1, RANGE_YEARS + 1)]
    found = [(o, o.value) for o in window if o is not None and o.value is not None]
    metric.window = [o for o, _ in found]
    if len(found) < RANGE_YEARS:
        metric.status = "insufficient_history"
        metric.reason = f"{len(found)} of {RANGE_YEARS} prior years have a value for this period"
        return metric
    _extremes(metric, current, found)
    return metric


def trailing_extremes(index: _Index, current: Obs) -> Metric:
    start = _shift_years(current.period_end, RANGE_YEARS)
    metric = Metric(
        kind="trailing_extremes",
        label="Five-year low and high",
        status="ok",
        methodology=(
            f"Lowest and highest of every observation whose period ends after {start.isoformat()} "
            f"(the {RANGE_YEARS} years up to the latest period)."
        ),
        current=current,
    )
    if not index.ordered or index.ordered[0].period_end > start:
        metric.status = "insufficient_history"
        metric.reason = f"stored history does not cover the full {RANGE_YEARS} years"
        return metric
    window = [
        (o, o.value)
        for o in index.ordered
        if start < o.period_end <= current.period_end and o.value is not None
    ]
    _extremes(metric, current, window)
    return metric


def _extremes(metric: Metric, current: Obs, window: Sequence[tuple[Obs, Decimal]]) -> None:
    """Set min, max, and the current value's position within them."""
    low_obs, low = min(window, key=lambda item: (item[1], item[0].period_end))
    high_obs, high = max(window, key=lambda item: (item[1], item[0].period_end))
    metric.minimum, metric.maximum = low_obs, high_obs
    if current.value is None:  # pragma: no cover  (calculate() only passes valued observations)
        return
    now = current.value
    metric.position = "below" if now < low else "above" if now > high else "within"
    if high == low:
        metric.reason = "The range has zero width; position is undefined."
        return
    metric.position_percent = _rounded((now - low) / (high - low) * 100, POSITION_PLACES)


def calculate(
    observations: Sequence[Obs], frequency: Frequency, units: str, plan: MetricPlan
) -> tuple[Obs | None, list[Metric]]:
    """(the observation the metrics describe, the metrics). Current versions only."""
    index = _Index(observations)
    current = index.latest_with_value()
    if current is None:
        return None, []
    metrics: list[Metric] = []
    if plan.period_change:
        metrics.append(period_change(index, frequency, current, units, plan))
    if plan.year_over_year:
        metrics.append(year_over_year(index, frequency, current, units, plan))
    if plan.seasonal_range and frequency in ("weekly", "monthly"):
        metrics.append(seasonal_range(index, frequency, current))
    if plan.trailing_extremes:
        metrics.append(trailing_extremes(index, current))
    return current, metrics
