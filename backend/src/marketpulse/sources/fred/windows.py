"""Plan real-time request windows that respect FRED's 2,000-vintage-dates-per-request limit.

Windows are contiguous and non-overlapping, together covering [start, 9999-12-31]:

    vintages:  v1 ... v2000 | v2001 ... v4000 | v4001 ... vN
    windows:  [start, v2001-1] [v2001, v4001-1] [v4001, 9999-12-31]

Each window contains at most `max_vintages` vintage dates, so every request is accepted.
Because windows split the real-time axis, a value that did not change can come back as two
adjacent pieces at a boundary; `merge.merge_window_splits` rejoins them.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from marketpulse.sources.fred import MAX_VINTAGES_PER_REQUEST, REALTIME_OPEN_END


@dataclass(frozen=True)
class RealtimeWindow:
    start: date
    end: date  # inclusive; REALTIME_OPEN_END for the last window

    def as_params(self) -> dict[str, str]:
        return {"realtime_start": self.start.isoformat(), "realtime_end": self.end.isoformat()}


def plan_windows(
    vintage_dates: Sequence[date],
    *,
    start: date | None = None,
    max_vintages: int = MAX_VINTAGES_PER_REQUEST,
) -> list[RealtimeWindow]:
    """Split [start, open end] into windows of at most `max_vintages` vintage dates each.

    `vintage_dates` must be strictly increasing. `start` defaults to the first vintage date
    (a full backfill); pass a later date for an incremental update, in which case only vintage
    dates on or after `start` count toward the limit.
    """
    if max_vintages < 1:
        raise ValueError("max_vintages must be at least 1")
    for earlier, later in pairwise(vintage_dates):
        if later <= earlier:
            raise ValueError(f"vintage dates must be strictly increasing: {earlier} then {later}")
    if not vintage_dates and start is None:
        raise ValueError("no vintage dates and no start date: nothing to plan")

    window_start = start if start is not None else vintage_dates[0]
    relevant = [d for d in vintage_dates if d >= window_start]
    if not relevant:
        return [RealtimeWindow(window_start, REALTIME_OPEN_END)]

    # Chunk starts: the window start, then every `max_vintages`-th relevant vintage after it.
    chunk_starts = [
        window_start,
        *(relevant[i] for i in range(max_vintages, len(relevant), max_vintages)),
    ]
    windows = [
        RealtimeWindow(begin, next_begin - timedelta(days=1))
        for begin, next_begin in pairwise(chunk_starts)
    ]
    windows.append(RealtimeWindow(chunk_starts[-1], REALTIME_OPEN_END))
    return windows
