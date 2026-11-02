"""Real-time window planning. Vintage date lists are synthetic.

The invariant that matters is FRED's *effective* count (listed vintage dates + 1 for a
non-vintage start + 1 for a non-vintage end), not merely the number of listed vintage dates;
checking only the latter is what missed the live incident of 2026-11-01.
"""

import random
from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from itertools import pairwise

import pytest

from marketpulse.sources.fred import MAX_VINTAGES_PER_REQUEST, REALTIME_OPEN_END
from marketpulse.sources.fred.windows import (
    BOUNDARY_ALLOWANCE,
    RealtimeWindow,
    effective_vintage_count,
    plan_windows,
)


def days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def vintages_in(window: RealtimeWindow, vintages: list[date]) -> int:
    return sum(window.start <= v <= window.end for v in vintages)


def assert_valid_plan(
    windows: list[RealtimeWindow],
    vintages: list[date],
    start: date,
    limit: int = MAX_VINTAGES_PER_REQUEST,
) -> None:
    """Contiguous, non-overlapping, full coverage, every vintage once, FRED's limit respected."""
    assert windows[0].start == start
    assert windows[-1].end == REALTIME_OPEN_END
    for prev, nxt in pairwise(windows):
        assert prev.start <= prev.end
        assert nxt.start == prev.end + timedelta(days=1)  # no gap, no overlap
    relevant = [v for v in vintages if v >= start]
    listed = set(relevant)
    counts = [bisect_right(relevant, w.end) - bisect_left(relevant, w.start) for w in windows]
    assert sum(counts) == len(relevant)  # every vintage date in exactly one window
    assert len(set(windows)) == len(windows)  # no duplicate windows
    for w, inside in zip(windows, counts, strict=True):  # FRED's effective count (independent)
        assert inside + (w.start not in listed) + (w.end not in listed) <= limit


def test_default_limit_matches_fred_documentation() -> None:
    assert MAX_VINTAGES_PER_REQUEST == 2000
    assert BOUNDARY_ALLOWANCE == 2  # FRED may count both a non-vintage start and end


def test_effective_count_rule() -> None:
    v = [date(2014, 4, 17), date(2014, 4, 18), date(2014, 4, 22)]
    assert effective_vintage_count(RealtimeWindow(v[0], v[1]), v) == 2
    assert effective_vintage_count(RealtimeWindow(v[0], date(2014, 4, 21)), v) == 3  # end in gap
    assert effective_vintage_count(RealtimeWindow(date(2014, 4, 16), v[1]), v) == 3  # start in gap
    assert effective_vintage_count(RealtimeWindow(date(2014, 4, 16), date(2014, 4, 21)), v) == 4
    assert effective_vintage_count(RealtimeWindow(v[2], REALTIME_OPEN_END), v) == 2


def test_few_vintages_fit_in_one_window() -> None:
    v = days(date(2019, 2, 4), 404)
    assert plan_windows(v) == [RealtimeWindow(date(2019, 2, 4), REALTIME_OPEN_END)]


@pytest.mark.parametrize(
    ("n", "expected_windows"),
    [(1, 1), (1998, 1), (1999, 2), (2000, 2), (3996, 2), (3997, 3), (5141, 3)],
)
def test_window_count_and_limit_at_boundaries(n: int, expected_windows: int) -> None:
    v = weekdays(date(2005, 6, 28), n)
    windows = plan_windows(v)
    assert len(windows) == expected_windows
    assert_valid_plan(windows, v, v[0])
    assert all(vintages_in(w, v) <= 1998 for w in windows)


def test_exact_boundaries_for_a_small_limit() -> None:
    v = days(date(2026, 1, 1), 7)  # Jan 1..7; limit 5 -> 3 listed per window
    assert plan_windows(v, limit=5) == [
        RealtimeWindow(date(2026, 1, 1), date(2026, 1, 3)),
        RealtimeWindow(date(2026, 1, 4), date(2026, 1, 6)),
        RealtimeWindow(date(2026, 1, 7), REALTIME_OPEN_END),
    ]


def test_sparse_vintages_leave_no_gaps_between_windows() -> None:
    v = [date(1990, 1, 1), date(1995, 6, 1), date(2001, 3, 15), date(2010, 1, 1)]
    windows = plan_windows(v, limit=4)  # 2 listed per window
    assert windows == [
        RealtimeWindow(date(1990, 1, 1), date(2001, 3, 14)),
        RealtimeWindow(date(2001, 3, 15), REALTIME_OPEN_END),
    ]
    assert_valid_plan(windows, v, v[0], limit=4)


@pytest.mark.parametrize("seed", range(200))
def test_randomized_plans_are_always_valid(seed: int) -> None:
    """Random vintage histories (gaps of 1-10 days), random sizes, random incremental starts
    (sometimes on a vintage date, sometimes in a gap, sometimes before or after all dates)."""
    rng = random.Random(seed)
    v, d = [], date(1990, 1, 1) + timedelta(days=rng.randrange(365))
    for _ in range(rng.choice([1, 5, 1997, 1998, 1999, 2000, 2001, 4000, rng.randrange(1, 7000)])):
        v.append(d)
        d += timedelta(days=rng.choice([1, 1, 1, 1, 3, 4, rng.randrange(1, 11)]))
    limit = rng.choice([MAX_VINTAGES_PER_REQUEST, MAX_VINTAGES_PER_REQUEST, 3, 10, 57])
    start = rng.choice(
        [
            None,
            rng.choice(v),
            rng.choice(v) - timedelta(days=1),
            v[0] - timedelta(days=30),
            v[-1] + timedelta(days=5),
        ]
    )
    windows = plan_windows(v, start=start, limit=limit)
    assert_valid_plan(windows, v, start or v[0], limit=limit)


def test_incremental_start_counts_only_later_vintages() -> None:
    v = days(date(2005, 6, 28), 5141)
    since = v[-10]
    windows = plan_windows(v, start=since)
    assert windows == [RealtimeWindow(since, REALTIME_OPEN_END)]


def test_incremental_start_between_vintages() -> None:
    v = [date(2026, 1, 1), date(2026, 1, 10), date(2026, 1, 20)]
    assert plan_windows(v, start=date(2026, 1, 5)) == [
        RealtimeWindow(date(2026, 1, 5), REALTIME_OPEN_END)
    ]


def test_start_after_all_vintages_or_with_no_vintages_gives_one_open_window() -> None:
    start = date(2026, 11, 1)
    assert plan_windows([date(2026, 1, 1)], start=start) == [
        RealtimeWindow(start, REALTIME_OPEN_END)
    ]
    assert plan_windows([], start=start) == [RealtimeWindow(start, REALTIME_OPEN_END)]


@pytest.mark.parametrize(
    "v", [[date(2026, 1, 2), date(2026, 1, 1)], [date(2026, 1, 1), date(2026, 1, 1)]]
)
def test_rejects_unsorted_or_duplicate_vintages(v: list[date]) -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        plan_windows(v)


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_rejects_nothing_to_plan_and_limits_without_room_for_boundaries(limit: int) -> None:
    with pytest.raises(ValueError, match="nothing to plan"):
        plan_windows([])
    with pytest.raises(ValueError, match="limit must be at least 3"):
        plan_windows([date(2026, 1, 1)], limit=limit)


def test_window_request_params() -> None:
    assert RealtimeWindow(date(2005, 6, 28), REALTIME_OPEN_END).as_params() == {
        "realtime_start": "2005-06-28",
        "realtime_end": "9999-12-31",
    }
