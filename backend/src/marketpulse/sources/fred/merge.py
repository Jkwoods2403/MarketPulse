"""Rejoin observation periods that were split at real-time window boundaries.

Two rows for the same observation date are merged only when they are *adjacent*
(the second starts the day after the first ends) and carry the *same* value (including both
being missing). Nothing else is altered:
- rows separated by a gap stay separate (no filling);
- exact duplicates (e.g. from overlapping incremental fetches) collapse to one row;
- overlapping periods, or two different values starting on the same day, are inconsistent
  and raise MergeConflictError rather than picking one.
"""

from collections.abc import Iterable
from dataclasses import replace
from datetime import timedelta

from marketpulse.sources.fred import REALTIME_OPEN_END
from marketpulse.sources.fred.parse import FredObservation


class MergeConflictError(ValueError):
    """Observation periods for the same date overlap inconsistently."""

    def __init__(self, conflicts: list[str]) -> None:
        self.conflicts = conflicts
        super().__init__(
            f"{len(conflicts)} conflicting real-time period(s):\n"
            + "\n".join(f"  - {c}" for c in conflicts[:20])
        )


def _adjacent(first: FredObservation, second: FredObservation) -> bool:
    if first.realtime_end == REALTIME_OPEN_END:  # == date.max: nothing can follow it
        return False
    return second.realtime_start == first.realtime_end + timedelta(days=1)


def merge_window_splits(observations: Iterable[FredObservation]) -> list[FredObservation]:
    """Return observations sorted by (date, realtime_start) with window splits rejoined."""
    merged: list[FredObservation] = []
    conflicts: list[str] = []

    # Sort by dates only: values may be None and must never be compared for ordering.
    for obs in sorted(
        observations, key=lambda o: (o.observation_date, o.realtime_start, o.realtime_end)
    ):
        previous = merged[-1] if merged else None
        if previous is None or previous.observation_date != obs.observation_date:
            merged.append(obs)
            continue
        if obs == previous:
            continue  # exact duplicate
        if obs.realtime_start <= previous.realtime_end:
            earlier = f"[{previous.realtime_start}, {previous.realtime_end}]={previous.value}"
            later = f"[{obs.realtime_start}, {obs.realtime_end}]={obs.value}"
            conflicts.append(f"date {obs.observation_date}: {earlier} overlaps {later}")
            continue
        if _adjacent(previous, obs) and obs.value == previous.value:
            merged[-1] = replace(previous, realtime_end=obs.realtime_end)
        else:
            merged.append(obs)

    if conflicts:
        raise MergeConflictError(conflicts)
    return merged
