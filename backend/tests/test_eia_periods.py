from datetime import date

import pytest

from marketpulse.sources.eia.periods import PeriodError, period_bounds


@pytest.mark.parametrize("convention", ["business_day", "calendar_day"])
def test_daily(convention: str) -> None:
    assert period_bounds("2026-10-27", "daily", convention) == (date(2026, 10, 27),) * 2


def test_weekly_period_is_the_week_ending_friday() -> None:
    assert period_bounds("2026-10-23", "weekly", "week_ending_friday") == (
        date(2026, 10, 17),
        date(2026, 10, 23),
    )


@pytest.mark.parametrize("not_friday", ["2026-10-22", "2026-10-24", "2026-10-25"])
def test_weekly_period_must_fall_on_the_conventions_weekday(not_friday: str) -> None:
    with pytest.raises(PeriodError, match="weeks end on Friday"):
        period_bounds(not_friday, "weekly", "week_ending_friday")


@pytest.mark.parametrize("convention", ["week_ending_funday", "month", "friday", ""])
def test_weekly_needs_a_valid_week_ending_convention(convention: str) -> None:
    with pytest.raises(PeriodError, match="week_ending"):
        period_bounds("2026-10-23", "weekly", convention)


@pytest.mark.parametrize(
    ("period", "end"),
    [
        ("2026-08", date(2026, 8, 31)),
        ("2024-02", date(2024, 2, 29)),
        ("2025-02", date(2025, 2, 28)),
        ("1997-01", date(1997, 1, 31)),
    ],
)
def test_monthly(period: str, end: date) -> None:
    start, stop = period_bounds(period, "monthly", "month")
    assert start == end.replace(day=1) and stop == end


@pytest.mark.parametrize(
    ("period", "frequency"),
    [
        ("2026-10-23", "monthly"),
        ("2026-13", "monthly"),
        ("2026-00", "monthly"),
        ("26-08", "monthly"),
        ("2026-08", "daily"),
        ("2026/10/27", "daily"),
        ("2026-02-30", "daily"),
        ("2026-10-23T00:00", "weekly"),
    ],
)
def test_malformed_periods_are_refused(period: str, frequency: str) -> None:
    convention = {"monthly": "month", "daily": "business_day", "weekly": "week_ending_friday"}
    with pytest.raises(PeriodError):
        period_bounds(period, frequency, convention[frequency])


@pytest.mark.parametrize("frequency", ["quarterly", "annual", "four-week-average", "hourly"])
def test_unverified_frequencies_are_refused(frequency: str) -> None:
    with pytest.raises(PeriodError, match="not been verified"):
        period_bounds("2026-01-01", frequency, "quarter")
