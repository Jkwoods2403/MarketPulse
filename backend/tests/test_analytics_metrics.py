"""Calculated metrics (pure): changes, alignment, ranges, and every refusal to calculate."""

import calendar
from datetime import date, timedelta
from decimal import Decimal

import pytest

from marketpulse.analytics.metrics import Metric, MetricPlan, Obs, calculate, change_style

D = Decimal
SEASONAL = MetricPlan(seasonal_range=True)
TRAILING = MetricPlan(trailing_extremes=True)
RATE = MetricPlan(basis_points=True)


def weekly(end: date, value: str | None) -> Obs:
    return Obs(
        end - timedelta(days=6),
        end,
        end.isoformat(),
        None if value is None else D(value),
        "missing_at_source" if value is None else "final",
    )


def monthly(year: int, month: int, value: str | None) -> Obs:
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return Obs(
        start,
        end,
        f"{year}-{month:02d}",
        None if value is None else D(value),
        "missing_at_source" if value is None else "final",
    )


def quarterly(year: int, quarter: int, value: str) -> Obs:
    start = date(year, 3 * quarter - 2, 1)
    end_month = 3 * quarter
    end = date(year, end_month, calendar.monthrange(year, end_month)[1])
    return Obs(start, end, f"{year}-Q{quarter}", D(value), "final")


def daily(day: date, value: str) -> Obs:
    return Obs(day, day, day.isoformat(), D(value), "final")


def weekly_history(last_end: date, weeks: int, value: str = "100") -> list[Obs]:
    return [weekly(last_end - timedelta(days=7 * k), value) for k in range(weeks)]


def by_kind(metrics: list[Metric]) -> dict[str, Metric]:
    return {m.kind: m for m in metrics}


LAST = date(2026, 10, 30)  # a Friday (EIA storage week ending)


# --- change style ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("units", "style"),
    [
        ("Percent", "percentage_points"),
        ("percent per annum", "percentage_points"),
        ("Billion cubic feet", "level"),
        ("Index Jan 2006=100", "level"),
    ],
)
def test_change_style_follows_units(units: str, style: str) -> None:
    assert change_style(units) == style


# --- weekly ------------------------------------------------------------------------------------


def test_week_over_week_and_year_over_year_are_exact() -> None:
    obs = weekly_history(LAST, 60, "3500")
    obs = [
        o for o in obs if o.period_end not in (LAST, LAST - timedelta(days=7), date(2025, 10, 31))
    ]
    obs += [
        weekly(LAST, "3915"),
        weekly(LAST - timedelta(days=7), "3882"),
        weekly(date(2025, 10, 31), "3800"),
    ]
    current, metrics = calculate(obs, "weekly", "Billion cubic feet", MetricPlan())
    assert current is not None and current.period_end == LAST
    m = by_kind(metrics)
    wow, yoy = m["period_change"], m["year_over_year"]
    assert (wow.label, wow.status, wow.absolute_change) == ("Week over week", "ok", D("33"))
    assert wow.comparison is not None and wow.comparison.period_end == date(2026, 10, 23)
    assert wow.change_units == "Billion cubic feet"
    assert wow.percent_change == D("0.85")  # 33 / 3882 = 0.8500...
    # 2025-10-31 is one day after the same calendar date (2025-10-30): within +-3 days.
    assert yoy.comparison is not None and yoy.comparison.period_end == date(2025, 10, 31)
    assert (yoy.absolute_change, yoy.percent_change) == (D("115"), D("3.03"))


def test_exact_decimals_are_preserved() -> None:
    obs = [weekly(LAST - timedelta(days=7), "421.153"), weekly(LAST, "421.2")]
    _, metrics = calculate(obs, "weekly", "Million barrels", MetricPlan(year_over_year=False))
    assert metrics[0].absolute_change == D("0.047")
    assert metrics[0].current.value == D("421.2")


def test_week_over_week_requires_the_week_exactly_seven_days_earlier() -> None:
    obs = [weekly(LAST - timedelta(days=14), "1"), weekly(LAST, "2")]  # a skipped week
    _, metrics = calculate(obs, "weekly", "Bcf", MetricPlan(year_over_year=False))
    wow = metrics[0]
    assert wow.status == "missing_comparison"
    assert wow.absolute_change is None and wow.percent_change is None
    assert wow.reason == "no stored observation for the week ending 2026-10-23"


def test_comparison_missing_at_source_is_not_filled() -> None:
    obs = [weekly(LAST - timedelta(days=7), None), weekly(LAST, "2")]
    _, metrics = calculate(obs, "weekly", "Bcf", MetricPlan(year_over_year=False))
    wow = metrics[0]
    assert wow.status == "missing_comparison"
    assert wow.comparison is not None and wow.comparison.value is None
    assert wow.reason == "the week ending 2026-10-23 was reported as missing at source"
    assert wow.absolute_change is None


def test_latest_missing_at_source_falls_back_to_latest_valued_observation() -> None:
    obs = [
        weekly(LAST - timedelta(days=14), "1"),
        weekly(LAST - timedelta(days=7), "2"),
        weekly(LAST, None),
    ]
    current, metrics = calculate(obs, "weekly", "Bcf", MetricPlan(year_over_year=False))
    assert current is not None and current.period_end == LAST - timedelta(days=7)
    assert metrics[0].absolute_change == D("1")


def test_no_values_at_all_produces_no_metrics() -> None:
    assert calculate([], "weekly", "Bcf", SEASONAL) == (None, [])
    assert calculate([weekly(LAST, None)], "weekly", "Bcf", SEASONAL) == (None, [])


def test_year_over_year_with_less_than_a_year_of_history() -> None:
    _, metrics = calculate(weekly_history(LAST, 30), "weekly", "Bcf", MetricPlan())
    yoy = by_kind(metrics)["year_over_year"]
    assert yoy.status == "insufficient_history"
    assert yoy.reason == "less than one year of stored history"
    assert yoy.absolute_change is None


def test_year_over_year_gap_in_long_history_is_missing_not_insufficient() -> None:
    obs = [o for o in weekly_history(LAST, 80) if abs((o.period_end - date(2025, 10, 30)).days) > 3]
    _, metrics = calculate(obs, "weekly", "Bcf", MetricPlan())
    yoy = by_kind(metrics)["year_over_year"]
    assert yoy.status == "missing_comparison"
    assert yoy.reason == "no stored observation for the same period one year earlier"


def test_weekly_alignment_does_not_reach_beyond_three_days() -> None:
    # Only a week ending 4 days after the anniversary exists: not aligned, not used.
    obs = [weekly(date(2025, 10, 20), "1"), weekly(date(2025, 11, 3), "1"), weekly(LAST, "2")]
    _, metrics = calculate(obs, "weekly", "Bcf", MetricPlan(period_change=False))
    assert metrics[0].status == "missing_comparison"


def test_weekly_alignment_prefers_the_closest_week() -> None:
    obs = [weekly(date(2025, 10, 28), "5"), weekly(date(2025, 11, 1), "7"), weekly(LAST, "9")]
    # Anniversary 2025-10-30: both are 2 days away; the earlier candidate is checked first.
    _, metrics = calculate(obs, "weekly", "Bcf", MetricPlan(period_change=False))
    assert metrics[0].comparison is not None
    assert metrics[0].comparison.period_end == date(2025, 10, 28)


def test_division_by_zero_gives_no_percent_change() -> None:
    obs = [weekly(LAST - timedelta(days=7), "0"), weekly(LAST, "5")]
    _, metrics = calculate(obs, "weekly", "Million barrels", MetricPlan(year_over_year=False))
    wow = metrics[0]
    assert (wow.status, wow.absolute_change, wow.percent_change) == ("ok", D("5"), None)
    assert wow.percent_note == "Undefined: the comparison value is zero."


def test_percent_change_uses_the_magnitude_of_a_negative_base() -> None:
    obs = [weekly(LAST - timedelta(days=7), "-4"), weekly(LAST, "-2")]
    _, metrics = calculate(obs, "weekly", "Bcf", MetricPlan(year_over_year=False))
    assert metrics[0].percent_change == D("50.00")  # rose by half of the base's magnitude


def test_percent_change_is_rounded_half_even_to_two_places() -> None:
    obs = [weekly(LAST - timedelta(days=7), "8"), weekly(LAST, "8.0010")]
    _, metrics = calculate(obs, "weekly", "Bcf", MetricPlan(year_over_year=False))
    assert metrics[0].percent_change == D("0.01")  # 0.0125 -> 0.01


# --- percent-unit series -----------------------------------------------------------------------


def test_percent_series_change_in_percentage_points_without_percent_change() -> None:
    obs = [weekly(LAST - timedelta(days=7), "90.1"), weekly(LAST, "91.4")]
    _, metrics = calculate(obs, "weekly", "Percent", MetricPlan(year_over_year=False))
    m = metrics[0]
    assert (m.absolute_change, m.change_units) == (D("1.3"), "percentage points")
    assert m.percent_change is None and m.basis_points is None
    assert m.percent_note == "Not computed: the series is already a percentage."


def test_daily_rate_changes_in_basis_points_from_previous_business_day() -> None:
    friday, monday = date(2026, 10, 30), date(2026, 11, 2)
    obs = [daily(friday, "4.19"), daily(monday, "4.23")]
    _, metrics = calculate(obs, "daily", "Percent", RATE)
    m = by_kind(metrics)["period_change"]
    assert m.comparison is not None and m.comparison.period_end == friday
    assert (m.absolute_change, m.basis_points) == (D("0.04"), D("4.00"))


def test_daily_previous_observation_is_limited_to_seven_days() -> None:
    obs = [daily(date(2026, 10, 20), "4"), daily(date(2026, 11, 2), "5")]
    _, metrics = calculate(obs, "daily", "Percent", RATE)
    assert by_kind(metrics)["period_change"].status == "missing_comparison"


def test_daily_year_over_year_uses_latest_on_or_before_anniversary() -> None:
    # 2025-11-02 is a Sunday: the Friday before (2025-10-31) is used, not the Monday after.
    obs = [
        daily(date(2025, 10, 31), "4.00"),
        daily(date(2025, 11, 3), "9.99"),
        daily(date(2026, 11, 2), "4.25"),
    ]
    _, metrics = calculate(obs, "daily", "Percent", RATE)
    yoy = by_kind(metrics)["year_over_year"]
    assert yoy.comparison is not None and yoy.comparison.period_end == date(2025, 10, 31)
    assert yoy.basis_points == D("25.00")


# --- monthly and quarterly ---------------------------------------------------------------------


def test_month_over_month_notes_unequal_month_lengths() -> None:
    obs = [monthly(2026, 1, "3100"), monthly(2026, 2, "2800")]
    _, metrics = calculate(obs, "monthly", "Billion cubic feet", MetricPlan(year_over_year=False))
    m = metrics[0]
    assert (m.label, m.absolute_change) == ("Month over month", D("-300"))
    assert "The months have 31 and 28 days" in m.methodology
    assert "not adjusted" in m.methodology


def test_month_over_month_equal_lengths_has_no_note() -> None:
    obs = [monthly(2026, 7, "1"), monthly(2026, 8, "2")]  # 31 and 31 days
    _, metrics = calculate(obs, "monthly", "Bcf", MetricPlan(year_over_year=False))
    assert "days" not in metrics[0].methodology


def test_month_over_month_compares_previous_calendar_month_across_year_end() -> None:
    obs = [monthly(2025, 12, "10"), monthly(2026, 1, "12")]
    _, metrics = calculate(obs, "monthly", "Bcf", MetricPlan(year_over_year=False))
    assert metrics[0].comparison is not None
    assert metrics[0].comparison.source_period == "2025-12"


def test_monthly_skipped_month_is_missing_not_bridged() -> None:
    obs = [monthly(2026, 6, "10"), monthly(2026, 8, "12")]
    _, metrics = calculate(obs, "monthly", "Bcf", MetricPlan(year_over_year=False))
    assert metrics[0].status == "missing_comparison"
    assert metrics[0].reason == "no stored observation for 2026-07"


def test_monthly_year_over_year_matches_the_same_month() -> None:
    obs = [monthly(2025, 9, "100"), monthly(2025, 10, "1"), monthly(2026, 9, "110")]
    _, metrics = calculate(obs, "monthly", "Bcf", MetricPlan(period_change=False))
    assert metrics[0].comparison is not None and metrics[0].comparison.source_period == "2025-09"
    assert metrics[0].percent_change == D("10.00")


def test_quarter_over_quarter_is_not_annualized() -> None:
    obs = [
        quarterly(2026, 1, "24000.5"),
        quarterly(2026, 2, "24200.0"),
        quarterly(2025, 2, "23500.0"),
    ]
    _, metrics = calculate(obs, "quarterly", "Billions of Dollars", MetricPlan())
    m = by_kind(metrics)
    assert m["period_change"].label == "Quarter over quarter"
    assert "not annualized" in m["period_change"].methodology
    assert m["period_change"].absolute_change == D("199.5")
    assert m["year_over_year"].comparison is not None
    assert m["year_over_year"].comparison.source_period == "2025-Q2"


def test_seasonal_range_is_not_calculated_for_daily_or_quarterly() -> None:
    obs = [quarterly(2026, 1, "1"), quarterly(2026, 2, "2")]
    _, metrics = calculate(obs, "quarterly", "Index", SEASONAL)
    assert "seasonal_range" not in by_kind(metrics)


# --- seasonal range ----------------------------------------------------------------------------


def storage_years(values: list[str]) -> list[Obs]:
    """Weeks ending near Oct 30 in 2021..2025 with the given values, plus 2026-10-30 = 3915."""
    ends = [
        date(2021, 10, 29),
        date(2022, 10, 28),
        date(2023, 10, 27),
        date(2024, 11, 1),
        date(2025, 10, 31),
    ]
    return [weekly(e, v) for e, v in zip(ends, values, strict=True)] + [weekly(LAST, "3915")]


def test_five_year_seasonal_range_and_position() -> None:
    obs = storage_years(["3700", "3580", "3779", "3932", "3882"])
    _, metrics = calculate(obs, "weekly", "Bcf", SEASONAL)
    r = by_kind(metrics)["seasonal_range"]
    assert r.status == "ok"
    assert [o.period_end.year for o in r.window] == [2025, 2024, 2023, 2022, 2021]
    assert r.minimum is not None and r.minimum.value == D("3580")
    assert r.maximum is not None and r.maximum.value == D("3932")
    assert r.position == "within"
    assert r.position_percent == D("95.2")  # 335 / 352 * 100 = 95.17
    assert "current year excluded" in r.methodology


def test_seasonal_range_flags_values_outside_the_range() -> None:
    obs = storage_years(["4000", "4100", "4200", "4300", "4400"])
    r = by_kind(calculate(obs, "weekly", "Bcf", SEASONAL)[1])["seasonal_range"]
    assert (r.position, r.position_percent) == ("below", D("-21.2"))


def test_seasonal_range_requires_all_five_years() -> None:
    obs = storage_years(["1", "2", "3", "4", "5"])
    obs = [o for o in obs if o.period_end.year != 2023]
    r = by_kind(calculate(obs, "weekly", "Bcf", SEASONAL)[1])["seasonal_range"]
    assert r.status == "insufficient_history"
    assert r.reason == "4 of 5 prior years have a value for this period"
    assert r.minimum is None and r.position is None


def test_seasonal_range_year_missing_at_source_counts_as_absent() -> None:
    obs = storage_years(["1", "2", "3", "4", "5"])
    obs = [weekly(o.period_end, None) if o.period_end.year == 2022 else o for o in obs]
    r = by_kind(calculate(obs, "weekly", "Bcf", SEASONAL)[1])["seasonal_range"]
    assert r.status == "insufficient_history"


def test_zero_width_range_has_no_position_percent() -> None:
    obs = storage_years(["3915"] * 5)
    r = by_kind(calculate(obs, "weekly", "Bcf", SEASONAL)[1])["seasonal_range"]
    assert (r.position, r.position_percent) == ("within", None)
    assert r.reason == "The range has zero width; position is undefined."


def test_monthly_seasonal_range_uses_the_same_month() -> None:
    obs = [monthly(y, 8, str(100 + y - 2021)) for y in range(2021, 2026)]
    obs += [monthly(y, 9, "999") for y in range(2021, 2026)]  # adjacent month: never used
    obs.append(monthly(2026, 8, "102"))
    r = by_kind(calculate(obs, "monthly", "Bcf", SEASONAL)[1])["seasonal_range"]
    assert r.status == "ok"
    assert {o.source_period for o in r.window} == {f"{y}-08" for y in range(2021, 2026)}
    assert r.position_percent == D("50.0")


def test_leap_day_alignment_uses_february_28() -> None:
    leap = date(2028, 2, 29)
    obs = [daily(date(2027, 2, 26), "1"), daily(date(2027, 2, 28), "2"), daily(leap, "3")]
    _, metrics = calculate(obs, "daily", "Index", MetricPlan(period_change=False))
    assert metrics[0].comparison is not None
    assert metrics[0].comparison.period_end == date(2027, 2, 28)


# --- trailing extremes -------------------------------------------------------------------------


def test_trailing_five_year_extremes() -> None:
    obs = [
        weekly(LAST - timedelta(days=7 * k), "350" if k == 100 else str(400 + (k % 50)))
        for k in range(300)
    ]
    r = by_kind(calculate(obs, "weekly", "Million barrels", TRAILING)[1])["trailing_extremes"]
    assert r.status == "ok"
    assert r.minimum is not None and r.minimum.value == D("350")
    assert r.maximum is not None and r.maximum.value == D("449")
    assert r.current.value == D("400")
    assert r.position == "within"


def test_trailing_extremes_need_full_coverage() -> None:
    r = by_kind(calculate(weekly_history(LAST, 200), "weekly", "Mbbl", TRAILING)[1])[
        "trailing_extremes"
    ]
    assert r.status == "insufficient_history"
    assert r.reason == "stored history does not cover the full 5 years"


def test_trailing_extremes_ignore_observations_outside_the_window() -> None:
    obs = weekly_history(LAST, 270, "500")
    obs = [o for o in obs if o.period_end != LAST - timedelta(days=7 * 265)]
    obs.append(weekly(LAST - timedelta(days=7 * 265), "1"))  # older than five years
    r = by_kind(calculate(obs, "weekly", "Mbbl", TRAILING)[1])["trailing_extremes"]
    assert r.minimum is not None and r.minimum.value == D("500")
