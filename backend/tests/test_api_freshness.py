"""Freshness judgments (no database). Each series is judged against its own limit."""

from datetime import date, timedelta

import pytest

from marketpulse.api.freshness import assess_freshness

TODAY = date(2026, 11, 8)


def test_nothing_stored_is_unavailable() -> None:
    result = assess_freshness(None, timedelta(days=14), TODAY)
    assert result.status == "unavailable"
    assert result.latest_period_end is None
    assert result.age_days is None
    assert result.max_staleness_days == 14


@pytest.mark.parametrize(("age", "status"), [(0, "fresh"), (14, "fresh"), (15, "stale")])
def test_limit_is_inclusive(age: int, status: str) -> None:
    result = assess_freshness(TODAY - timedelta(days=age), timedelta(days=14), TODAY)
    assert result.status == status
    assert result.age_days == age


def test_daily_weekly_and_monthly_limits_differ() -> None:
    """The same 9-day-old period is stale for a daily series but fresh for a weekly one."""
    period_end = TODAY - timedelta(days=9)
    assert assess_freshness(period_end, timedelta(days=5), TODAY).status == "stale"
    assert assess_freshness(period_end, timedelta(days=14), TODAY).status == "fresh"
    assert assess_freshness(period_end, timedelta(days=100), TODAY).status == "fresh"
