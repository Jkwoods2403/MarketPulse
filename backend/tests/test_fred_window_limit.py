"""FRED's real 2,000-vintage-date limit, reproduced from the live incident of 2026-11-01.

During the first live backfill, 5 of 12 series failed with HTTP 400 "There are 2001 vintage
dates in the specified real-time period". Diagnosis against the live API showed FRED counts a
window's start and end dates as extra vintage dates when they are not vintage dates themselves.
The simulator (tests/fred_sim.py) now applies that rule; these tests reproduce the incident.

Vintage dates here are synthetic business days arranged like DGS30's real history: the 2,000th
vintage date is a Friday, followed by a weekend gap.
"""

import random
from datetime import date, timedelta

import pytest
from pydantic import SecretStr

from fred_mock import TEST_KEY, UnlimitedLimiter
from fred_sim import FakeFred, SimRow, SimSeries, effective_vintage_count
from marketpulse.sources.fred.client import FredAPIError, FredClient
from marketpulse.sources.fred.windows import BOUNDARY_ALLOWANCE, RealtimeWindow, plan_windows
from marketpulse.sources.fred.windows import (
    effective_vintage_count as effective_vintage_count_planner,
)
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
    """A daily-series-like history: each vintage date publishes one new observation that stays
    current, so long-lived values span window boundaries (and must be rejoined)."""

    def __init__(self, vintages: list[date]) -> None:
        super().__init__()
        self.series["DGS30"] = SimSeries(
            {"id": "DGS30"},
            [SimRow(v.isoformat(), v.isoformat(), "9999-12-31", "7.75") for v in vintages],
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


# --- the incident, reproduced with the planning used on 2026-11-01 ----------------------------


def old_planning(vintages: list[date]) -> list[RealtimeWindow]:
    """The planning in commit 42af1a4: up to 2,000 *listed* vintage dates per window, each ending
    the day before the next. Equivalent to today's planner with no boundary allowance
    (limit 2,000 + 2), kept here only to reproduce the incident."""
    return plan_windows(vintages, limit=2000 + BOUNDARY_ALLOWANCE)


def test_old_planning_reproduces_the_live_failure() -> None:
    first = old_planning(VINTAGES)[0]
    assert first.end == VINTAGES[2000] - DAY  # the Monday holiday, not a vintage date
    assert sum(first.start <= v <= first.end for v in VINTAGES) == 2000  # looks fine...
    assert effective_vintage_count(ISO, first.start.isoformat(), first.end.isoformat()) == 2001
    with pytest.raises(FredAPIError, match="There are 2001 vintage dates"):
        fetch(first)  # ...but FRED rejects it, exactly as in the live run


# --- the fix -----------------------------------------------------------------------------------


def test_new_planner_windows_are_all_accepted() -> None:
    windows = plan_windows(VINTAGES)
    assert len(windows) == 2
    for window in windows:
        assert (
            effective_vintage_count(ISO, window.start.isoformat(), window.end.isoformat()) <= 2000
        )
        fetch(window)  # accepted by the simulator applying FRED's observed rule


def test_fetch_history_over_incident_history_returns_every_value_exactly_once() -> None:
    with client_for(_VintageOnlySim(VINTAGES)) as client:
        windows, rows = client.fetch_history("DGS30")
    assert windows[0].start == VINTAGES[0] and windows[-1].end == date(9999, 12, 31)
    assert len(rows) == len(VINTAGES)  # window splits rejoined: no duplicates, nothing lost
    assert [(r.observation_date, r.realtime_start) for r in rows] == [(v, v) for v in VINTAGES]
    assert all(r.is_current for r in rows)


@pytest.mark.parametrize("gap_start", [False, True])
def test_new_planner_handles_incremental_start_in_a_gap(gap_start: bool) -> None:
    start = VINTAGES[2000] - DAY if gap_start else VINTAGES[1]
    windows = plan_windows(VINTAGES, start=start)
    for window in windows:
        fetch(window)


def test_planner_rule_matches_the_simulated_fred_rule() -> None:
    rng = random.Random(7)
    for _ in range(300):
        a = rng.randrange(len(VINTAGES) - 1)
        b = rng.randrange(a, len(VINTAGES))
        window = RealtimeWindow(
            VINTAGES[a] - timedelta(days=rng.choice([0, 0, 1, 2])),
            VINTAGES[b] + timedelta(days=rng.choice([0, 0, 1, 2])),
        )
        assert effective_vintage_count_planner(window, VINTAGES) == effective_vintage_count(
            ISO, window.start.isoformat(), window.end.isoformat()
        )
