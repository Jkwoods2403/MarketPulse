"""FRED's real 2,000-vintage-date limit, reproduced from the live incident of 2026-11-01.

During the first live backfill, 5 of 12 series failed with HTTP 400 "There are 2001 vintage
dates in the specified real-time period". Diagnosis against the live API showed FRED counts a
window's start and end dates as extra vintage dates when they are not vintage dates themselves.
The simulator (tests/fred_sim.py) now applies that rule; these tests reproduce the incident.

Vintage dates here are synthetic business days arranged like DGS30's real history: the 2,000th
vintage date is a Friday, followed by a weekend gap.
"""

from datetime import date, timedelta

import pytest
from pydantic import SecretStr

from fred_mock import TEST_KEY, UnlimitedLimiter
from fred_sim import FakeFred, SimRow, SimSeries, effective_vintage_count
from marketpulse.sources.fred.client import FredAPIError, FredClient
from marketpulse.sources.fred.windows import RealtimeWindow, plan_windows
from marketpulse.sources.http import RetryPolicy

DAY = timedelta(days=1)


def incident_like_vintages(total: int) -> list[date]:
    """`total` weekday vintage dates where the 2,000th is a Friday followed by a long weekend
    (Saturday-Monday without a vintage), like DGS30: 2014-04-18 (Fri) then 2014-04-22 (Tue)."""
    start = date(2005, 6, 28)
    while True:
        days, d = [], start
        while len(days) < total + 1:
            if d.weekday() < 5:
                days.append(d)
            d += DAY
        if days[1999].weekday() == 4:
            del days[2000]  # the Monday after: a holiday with no new vintage
            return days[:total]
        start += DAY


VINTAGES = incident_like_vintages(2100)
ISO = [d.isoformat() for d in VINTAGES]


def client_for(fake: FakeFred) -> FredClient:
    return FredClient(
        SecretStr(TEST_KEY),
        transport=fake.transport(),
        rate_limiter=UnlimitedLimiter(),
        retry=RetryPolicy(max_attempts=1),
        sleep=lambda _: None,
    )


# --- FRED's observed counting rule ------------------------------------------------------------


def test_window_ending_on_the_2000th_vintage_date_counts_2000() -> None:
    assert effective_vintage_count(ISO, ISO[0], ISO[1999]) == 2000


@pytest.mark.parametrize("gap_day", [1, 2, 3])  # Saturday, Sunday, Monday after the Friday
def test_window_ending_in_the_gap_counts_2001(gap_day: int) -> None:
    end = (VINTAGES[1999] + timedelta(days=gap_day)).isoformat()
    assert end not in ISO
    assert effective_vintage_count(ISO, ISO[0], end) == 2001


def test_non_vintage_start_date_also_counts() -> None:
    i = next(i for i in range(1, 100) if VINTAGES[i] - VINTAGES[i - 1] > DAY)
    gap_start = (VINTAGES[i] - DAY).isoformat()
    assert effective_vintage_count(ISO, ISO[i], ISO[i + 1999]) == 2000
    assert effective_vintage_count(ISO, gap_start, ISO[i + 1999]) == 2001
    assert effective_vintage_count(ISO, gap_start, (VINTAGES[i + 1999] + DAY).isoformat()) == 2002


# --- the simulator enforces it like FRED ------------------------------------------------------


class _VintageOnlySim(FakeFred):
    """Serve a vintage-date list directly, independent of observation rows."""

    def __init__(self, vintages: list[date]) -> None:
        super().__init__()
        self.series["DGS30"] = SimSeries(
            {"id": "DGS30"},
            [SimRow("1977-02-15", v.isoformat(), v.isoformat(), "7.75") for v in vintages[:-1]]
            + [SimRow("1977-02-15", vintages[-1].isoformat(), "9999-12-31", "7.75")],
        )


def fetch(window: RealtimeWindow) -> None:
    with client_for(_VintageOnlySim(VINTAGES)) as client:
        client.observations("DGS30", window)


def test_simulator_accepts_window_ending_on_a_vintage_date() -> None:
    fetch(RealtimeWindow(VINTAGES[0], VINTAGES[1999]))  # no exception


def test_simulator_rejects_window_ending_in_a_gap_with_freds_message() -> None:
    end = VINTAGES[1999] + timedelta(days=3)
    with pytest.raises(FredAPIError) as info:
        fetch(RealtimeWindow(VINTAGES[0], end))
    assert info.value.error_message == (
        f"Bad Request.  There are 2001 vintage dates in the specified real-time period: "
        f"{VINTAGES[0]} to {end}.  This exceeds the maximum number of vintage dates allowed "
        "for this file type (2000)."
    )


# --- the incident, reproduced with the planner as it was on 2026-11-01 ------------------------


def test_planner_at_time_of_incident_produced_a_rejected_window() -> None:
    """Commit 42af1a4's planning: up to 2,000 *listed* vintage dates per window, ending the day
    before the next window. With a weekend after the 2,000th vintage, FRED counts 2,001."""
    windows = plan_windows(VINTAGES, max_vintages=2000)
    first = windows[0]
    assert first.end == VINTAGES[2000] - DAY  # Monday, not a vintage date
    assert sum(first.start <= v <= first.end for v in VINTAGES) == 2000  # looks fine...
    assert effective_vintage_count(ISO, first.start.isoformat(), first.end.isoformat()) == 2001
    with pytest.raises(FredAPIError, match="There are 2001 vintage dates"):
        fetch(first)  # ...but FRED rejects it
