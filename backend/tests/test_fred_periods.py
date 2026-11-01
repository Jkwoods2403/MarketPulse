from datetime import date

import pytest

from marketpulse.sources.fred.periods import PeriodError, period_bounds


@pytest.mark.parametrize("d", [date(2026, 10, 29), date(2024, 2, 29), date(1962, 1, 2)])
def test_daily_period_is_the_day(d: date) -> None:
    assert period_bounds(d, "daily") == (d, d)


@pytest.mark.parametrize(
    ("d", "end"),
    [
        (date(2026, 9, 1), date(2026, 9, 30)),
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2024, 2, 1), date(2024, 2, 29)),  # leap year
        (date(2023, 2, 1), date(2023, 2, 28)),
        (date(1900, 2, 1), date(1900, 2, 28)),  # century, not a leap year
        (date(2000, 2, 1), date(2000, 2, 29)),  # divisible by 400: leap year
        (date(2026, 12, 1), date(2026, 12, 31)),
    ],
)
def test_monthly_period_runs_to_month_end(d: date, end: date) -> None:
    assert period_bounds(d, "monthly") == (d, end)


@pytest.mark.parametrize(
    ("d", "end"),
    [
        (date(2026, 1, 1), date(2026, 3, 31)),
        (date(2026, 4, 1), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 9, 30)),
        (date(2026, 10, 1), date(2026, 12, 31)),
    ],
)
def test_quarterly_period_runs_to_quarter_end(d: date, end: date) -> None:
    assert period_bounds(d, "quarterly") == (d, end)


@pytest.mark.parametrize("d", [date(2026, 9, 2), date(2026, 9, 30)])
def test_monthly_date_must_be_first_of_month(d: date) -> None:
    with pytest.raises(PeriodError, match="first of a month"):
        period_bounds(d, "monthly")


@pytest.mark.parametrize("d", [date(2026, 2, 1), date(2026, 7, 2), date(2026, 12, 1)])
def test_quarterly_date_must_start_a_quarter(d: date) -> None:
    with pytest.raises(PeriodError, match="does not start a quarter"):
        period_bounds(d, "quarterly")


@pytest.mark.parametrize("frequency", ["weekly", "annual", "hourly", ""])
def test_unverified_frequencies_are_refused_not_guessed(frequency: str) -> None:
    with pytest.raises(PeriodError, match="not been verified"):
        period_bounds(date(2026, 1, 1), frequency)
