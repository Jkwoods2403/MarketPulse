"""Plan real-time request windows that respect FRED's 2,000-vintage-dates-per-request limit.

FRED's *effective* count (observed live on 2026-11-01, not stated in its documentation):

    vintage dates listed inside [start, end]
    + 1 if `start` is not itself a vintage date
    + 1 if `end` is not itself a vintage date

so a window holding exactly 2,000 listed vintage dates is rejected ("There are 2001 vintage
dates ...") when its end falls in a weekend/holiday gap. The first live backfill failed for 5 of
12 series because of this (see tests/test_fred_window_limit.py).

Windows are contiguous and non-overlapping, together covering [start, 9999-12-31]. Each window
holds at most `limit - BOUNDARY_ALLOWANCE` listed vintage dates, so its effective count stays
within `limit` whatever its boundaries are:

    vintages:  v1 ... v1998 | v1999 ... v3996 | v3997 ... vN
    windows:  [start, v1999-1] [v1999, v3997-1] [v3997, 9999-12-31]

Every plan is checked against the counting rule before it is returned. Because windows split the
real-time axis, a value that did not change can come back as two adjacent pieces at a boundary;
`merge.merge_window_splits` rejoins them.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from marketpulse.sources.fred import MAX_VINTAGES_PER_REQUEST, REALTIME_OPEN_END

# FRED may count a window's start and its end as one extra vintage date each.
BOUNDARY_ALLOWANCE = 2


@dataclass(frozen=True)
class RealtimeWindow:
    start: date
    end: date  # inclusive; REALTIME_OPEN_END for the last window

    def as_params(self) -> dict[str, str]:
        return {"realtime_start": self.start.isoformat(), "realtime_end": self.end.isoformat()}


def effective_vintage_count(window: RealtimeWindow, vintage_dates: Sequence[date]) -> int:
    """How many vintage dates FRED counts for `window` (observed rule, see module docstring)."""
    ordered = sorted(set(vintage_dates))
    return _effective_count(window, ordered, set(ordered))


def _effective_count(window: RealtimeWindow, ordered: Sequence[date], listed: set[date]) -> int:
    inside = bisect_right(ordered, window.end) - bisect_left(ordered, window.start)
    return inside + (window.start not in listed) + (window.end not in listed)


def plan_windows(
    vintage_dates: Sequence[date],
    *,
    start: date | None = None,
    limit: int = MAX_VINTAGES_PER_REQUEST,
) -> list[RealtimeWindow]:
    """Split [start, open end] into contiguous windows FRED will accept.

    `limit` is FRED's effective per-request limit. `vintage_dates` must be strictly increasing.
    `start` defaults to the first vintage date (a full backfill); pass a later date for an
    incremental update, in which case only vintage dates on or after `start` count.
    """
    per_window = limit - BOUNDARY_ALLOWANCE
    if per_window < 1:
        raise ValueError(f"limit must be at least {BOUNDARY_ALLOWANCE + 1}")
    for earlier, later in pairwise(vintage_dates):
        if later <= earlier:
            raise ValueError(f"vintage dates must be strictly increasing: {earlier} then {later}")
    if not vintage_dates and start is None:
        raise ValueError("no vintage dates and no start date: nothing to plan")

    window_start = start if start is not None else vintage_dates[0]
    relevant = [d for d in vintage_dates if d >= window_start]
    if not relevant:
        return [RealtimeWindow(window_start, REALTIME_OPEN_END)]

    # Chunk starts: the window start, then every `per_window`-th relevant vintage after it.
    chunk_starts = [
        window_start,
        *(relevant[i] for i in range(per_window, len(relevant), per_window)),
    ]
    windows = [
        RealtimeWindow(begin, next_begin - timedelta(days=1))
        for begin, next_begin in pairwise(chunk_starts)
    ]
    windows.append(RealtimeWindow(chunk_starts[-1], REALTIME_OPEN_END))

    listed = set(relevant)
    for window in windows:  # self-check: never hand FRED a window it will reject
        counted = _effective_count(window, relevant, listed)
        if counted > limit:  # pragma: no cover  (guaranteed by the allowance)
            raise RuntimeError(f"planned window {window} counts {counted} > {limit} vintage dates")
    return windows
