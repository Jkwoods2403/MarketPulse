"""Map FRED observation dates to period bounds.

FRED labels each observation with the first day of its period. Only the frequencies whose
convention was verified against live FRED data are supported; others raise instead of
assuming a convention.
"""

import calendar
from datetime import date

SUPPORTED_FREQUENCIES = frozenset({"daily", "monthly", "quarterly"})


class PeriodError(ValueError):
    """An observation date does not fit its series' frequency."""


def period_bounds(obs_date: date, frequency: str) -> tuple[date, date]:
    """Return the inclusive (period_start, period_end) for a FRED observation date."""
    if frequency == "daily":
        return obs_date, obs_date

    if frequency == "monthly":
        if obs_date.day != 1:
            raise PeriodError(f"monthly observation date {obs_date} is not the first of a month")
        last = calendar.monthrange(obs_date.year, obs_date.month)[1]
        return obs_date, obs_date.replace(day=last)

    if frequency == "quarterly":
        if obs_date.day != 1 or obs_date.month not in (1, 4, 7, 10):
            raise PeriodError(f"quarterly observation date {obs_date} does not start a quarter")
        end_month = obs_date.month + 2
        last = calendar.monthrange(obs_date.year, end_month)[1]
        return obs_date, date(obs_date.year, end_month, last)

    raise PeriodError(
        f"frequency {frequency!r} is not supported for FRED yet; its date convention has not "
        f"been verified (supported: {sorted(SUPPORTED_FREQUENCIES)})"
    )
