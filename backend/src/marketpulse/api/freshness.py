"""Freshness of a series (ADR-0007): is its latest period older than expected?

Each series has a `max_staleness`: the largest expected age of its latest period's end date
just before the next scheduled release, plus a buffer (set per series in the catalog). Daily,
weekly, monthly, and quarterly series therefore have very different limits; freshness never
compares them with one another.
"""

from datetime import date, timedelta

from marketpulse.api.schemas import Freshness


def assess_freshness(
    latest_period_end: date | None, max_staleness: timedelta, reference_date: date
) -> Freshness:
    limit_days = max_staleness.days
    if latest_period_end is None:
        return Freshness(
            status="unavailable",
            reference_date=reference_date,
            latest_period_end=None,
            age_days=None,
            max_staleness_days=limit_days,
        )
    age = (reference_date - latest_period_end).days
    return Freshness(
        status="fresh" if age <= limit_days else "stale",
        reference_date=reference_date,
        latest_period_end=latest_period_end,
        age_days=age,
        max_staleness_days=limit_days,
    )
