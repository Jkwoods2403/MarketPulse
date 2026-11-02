"""Map EIA period strings to inclusive (period_start, period_end) dates.

Verified conventions (live API, 2026-11-01): daily and weekly periods are "YYYY-MM-DD" (weekly
dates are the week's last day, a Friday for every catalogued weekly series); monthly periods are
"YYYY-MM". Anything else is refused rather than guessed.
"""

import calendar
import re
from datetime import date, timedelta

DATE_FORMATS = {"daily": "YYYY-MM-DD", "weekly": "YYYY-MM-DD", "monthly": "YYYY-MM"}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class PeriodError(ValueError):
    """An EIA period string does not fit the series' frequency or convention."""


def _day(period: str) -> date:
    if not _DAY.fullmatch(period):
        raise PeriodError(f"expected a YYYY-MM-DD period, got {period!r}")
    try:
        return date.fromisoformat(period)
    except ValueError:
        raise PeriodError(f"invalid date {period!r}") from None


def period_bounds(period: str, frequency: str, period_convention: str) -> tuple[date, date]:
    """Inclusive bounds of an EIA period for a catalogued frequency and period convention."""
    if frequency == "daily":
        day = _day(period)
        return day, day

    if frequency == "weekly":
        end = _day(period)
        prefix = "week_ending_"
        weekday = period_convention.removeprefix(prefix)
        if not period_convention.startswith(prefix) or weekday not in _WEEKDAYS:
            raise PeriodError(
                f"weekly series need a week_ending_<day> convention, got {period_convention!r}"
            )
        if end.weekday() != _WEEKDAYS.index(weekday):
            raise PeriodError(
                f"weekly period {period} is a {end:%A}, but the series' weeks end "
                f"on {weekday.title()}"
            )
        return end - timedelta(days=6), end

    if frequency == "monthly":
        match = _MONTH.fullmatch(period)
        if not match or not 1 <= int(match.group(2)) <= 12:
            raise PeriodError(f"expected a YYYY-MM period, got {period!r}")
        year, month = int(match.group(1)), int(match.group(2))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])

    raise PeriodError(
        f"frequency {frequency!r} is not supported for EIA yet; its period "
        f"format has not been verified (supported: {sorted(DATE_FORMATS)})"
    )
