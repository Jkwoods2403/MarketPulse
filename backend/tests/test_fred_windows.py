"""Real-time window planning. Vintage date lists are synthetic."""

from datetime import date, timedelta
from itertools import pairwise

import pytest

from marketpulse.sources.fred import MAX_VINTAGES_PER_REQUEST, REALTIME_OPEN_END
from marketpulse.sources.fred.windows import RealtimeWindow, plan_windows


def days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def vintages_in(window: RealtimeWindow, vintages: list[date]) -> int:
    return sum(window.start <= v <= window.end for v in vintages)


def assert_contiguous_cover(windows: list[RealtimeWindow], start: date) -> None:
    assert windows[0].start == start
    assert windows[-1].end == REALTIME_OPEN_END
    for prev, nxt in pairwise(windows):
        assert nxt.start == prev.end + timedelta(days=1)
        assert prev.start <= prev.end


def test_default_limit_matches_fred_documentation() -> None:
    assert MAX_VINTAGES_PER_REQUEST == 2000


def test_few_vintages_fit_in_one_window() -> None:
    v = days(date(2019, 2, 4), 404)
    assert plan_windows(v) == [RealtimeWindow(date(2019, 2, 4), REALTIME_OPEN_END)]


@pytest.mark.parametrize(
    ("n", "expected_windows"),
    [(1, 1), (1999, 1), (2000, 1), (2001, 2), (4000, 2), (4001, 3), (5141, 3)],
)
def test_window_count_and_limit_at_boundaries(n: int, expected_windows: int) -> None:
    v = days(date(2005, 6, 28), n)
    windows = plan_windows(v)

    assert len(windows) == expected_windows
    assert_contiguous_cover(windows, v[0])
    assert all(vintages_in(w, v) <= 2000 for w in windows)
    assert sum(vintages_in(w, v) for w in windows) == n  # every vintage covered exactly once


def test_exact_boundaries_for_a_small_limit() -> None:
    v = days(date(2026, 1, 1), 7)  # Jan 1..7
    assert plan_windows(v, max_vintages=3) == [
        RealtimeWindow(date(2026, 1, 1), date(2026, 1, 3)),
        RealtimeWindow(date(2026, 1, 4), date(2026, 1, 6)),
        RealtimeWindow(date(2026, 1, 7), REALTIME_OPEN_END),
    ]


def test_sparse_vintages_leave_no_gaps_between_windows() -> None:
    v = [date(1990, 1, 1), date(1995, 6, 1), date(2001, 3, 15), date(2010, 1, 1)]
    windows = plan_windows(v, max_vintages=2)
    assert windows == [
        RealtimeWindow(date(1990, 1, 1), date(2001, 3, 14)),
        RealtimeWindow(date(2001, 3, 15), REALTIME_OPEN_END),
    ]


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


def test_rejects_nothing_to_plan_and_invalid_limit() -> None:
    with pytest.raises(ValueError, match="nothing to plan"):
        plan_windows([])
    with pytest.raises(ValueError, match="max_vintages"):
        plan_windows([date(2026, 1, 1)], max_vintages=0)


def test_window_request_params() -> None:
    assert RealtimeWindow(date(2005, 6, 28), REALTIME_OPEN_END).as_params() == {
        "realtime_start": "2005-06-28",
        "realtime_end": "9999-12-31",
    }
