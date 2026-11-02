"""Compare incoming observation versions with stored history and plan the changes (pure).

Provider-neutral: works on core.versions.ObservationVersion, so FRED, EIA, and later sources
share it. The plan contains only three kinds of change, so history is never destroyed:

- INSERT a version not seen before;
- CLOSE a stored current version (set its vintage_end) when the provider says it was replaced;
- CONFIRM a stored version that the provider still reports unchanged (last_confirmed_at only).

Matching: an incoming version matches the stored version of the same period with the same
vintage_start. For incremental fetches, providers return long-unchanged values with their start
cut to the fetch's lower bound (`coverage_start`); such a row is matched to the stored version
that covers that instant and treated as a continuation, not a new version.

Anything inconsistent is reported as a quality issue instead of being "fixed":
- a stored version whose value or end differs from what the provider now reports (error);
- incoming versions that overlap each other (error);
- a planned result that would overlap or have two current versions (error);
- a stored version the provider no longer reports (warning; the row is kept).
A period with any error is left completely unchanged (all-or-nothing per period).
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise

from marketpulse.core.versions import ObservationVersion

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class StoredVersion:
    """A version already in the database."""

    id: int
    period_start: date
    period_end: date
    vintage_start: datetime
    vintage_end: datetime | None
    value: Decimal | None
    status: str


@dataclass(frozen=True)
class PlannedIssue:
    period_start: date
    check_name: str
    detail: str
    severity: str  # ERROR or WARNING


@dataclass
class ChangePlan:
    inserts: list[ObservationVersion] = field(default_factory=list)
    closes: list[tuple[int, datetime]] = field(default_factory=list)  # (stored id, vintage_end)
    confirms: list[int] = field(default_factory=list)  # stored ids, unchanged
    issues: list[PlannedIssue] = field(default_factory=list)
    periods_skipped: int = 0

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == ERROR for issue in self.issues)


@dataclass(frozen=True)
class _Interval:
    start: datetime
    end: datetime | None


def _same_value(stored: StoredVersion, incoming: ObservationVersion) -> bool:
    # Numeric equality: Decimal("1.0") == Decimal("1.00"). Status must match exactly.
    return stored.status == incoming.status and stored.value == incoming.value


def _overlap_problem(intervals: Iterable[_Interval]) -> str | None:
    ordered = sorted(intervals, key=lambda i: i.start)
    if sum(i.end is None for i in ordered) > 1:
        return "more than one current version"
    for earlier, later in pairwise(ordered):
        if earlier.end is None or earlier.end > later.start:
            return f"version starting {earlier.start} overlaps version starting {later.start}"
    return None


def _covering(stored: Sequence[StoredVersion], instant: datetime) -> StoredVersion | None:
    for version in stored:
        if version.vintage_start < instant and (
            version.vintage_end is None or version.vintage_end > instant
        ):
            return version
    return None


def _in_fetched_range(version: StoredVersion, coverage_start: datetime | None) -> bool:
    if coverage_start is None:
        return True
    return version.vintage_end is None or version.vintage_end > coverage_start


def plan_changes(
    stored: Iterable[StoredVersion],
    incoming: Iterable[ObservationVersion],
    *,
    coverage_start: datetime | None = None,
) -> ChangePlan:
    """Plan the changes needed to record `incoming` on top of `stored`.

    `coverage_start` is the lower bound of the real-time range that was fetched (None for a fetch
    covering all of history). Stored versions entirely before it are out of scope.
    """
    stored_by_period: dict[date, list[StoredVersion]] = defaultdict(list)
    for s in stored:
        stored_by_period[s.period_start].append(s)
    incoming_by_period: dict[date, list[ObservationVersion]] = defaultdict(list)
    for v in incoming:
        incoming_by_period[v.period_start].append(v)

    plan = ChangePlan()
    for period in sorted(set(stored_by_period) | set(incoming_by_period)):
        _plan_period(
            plan,
            period,
            stored_by_period.get(period, []),
            sorted(incoming_by_period.get(period, []), key=lambda v: v.vintage_start),
            coverage_start,
        )
    return plan


def _plan_period(
    plan: ChangePlan,
    period: date,
    stored: list[StoredVersion],
    incoming: list[ObservationVersion],
    coverage_start: datetime | None,
) -> None:
    issues: list[PlannedIssue] = []

    def issue(check: str, detail: str, severity: str = ERROR) -> None:
        issues.append(PlannedIssue(period, check, detail, severity))

    problem = _overlap_problem(_Interval(v.vintage_start, v.vintage_end) for v in incoming)
    if problem:
        issue("incoming_overlap", f"provider returned overlapping versions: {problem}")

    by_start = {s.vintage_start: s for s in stored}
    inserts: list[ObservationVersion] = []
    closes: list[tuple[int, datetime]] = []
    confirms: list[int] = []
    matched: set[int] = set()

    for v in incoming if not issues else []:
        s = by_start.get(v.vintage_start)
        if s is None and coverage_start is not None and v.vintage_start == coverage_start:
            s = _covering(stored, coverage_start)  # start was cut to the fetch boundary
        if s is None:
            inserts.append(v)
            continue
        matched.add(s.id)
        if s.period_end != v.period_end:
            issue(
                "period_end_conflict",
                f"stored period end {s.period_end}, provider reports {v.period_end}",
            )
        elif not _same_value(s, v):
            issue(
                "value_conflict",
                f"version from {s.vintage_start} stored as {s.value} ({s.status}), "
                f"provider now reports {v.value} ({v.status})",
            )
        elif s.vintage_end == v.vintage_end:
            confirms.append(s.id)
        elif s.vintage_end is None and v.vintage_end is not None:
            closes.append((s.id, v.vintage_end))
        else:
            issue(
                "vintage_end_conflict",
                f"version from {s.vintage_start} stored as ending {s.vintage_end}, "
                f"provider reports ending {v.vintage_end}",
            )

    for s in stored:
        if s.id not in matched and _in_fetched_range(s, coverage_start):
            issue(
                "vanished_version",
                f"stored version from {s.vintage_start} ({s.value}) is no longer reported "
                "by the provider; kept unchanged",
                WARNING,
            )

    if not any(i.severity == ERROR for i in issues):
        closed = dict(closes)
        result = [_Interval(s.vintage_start, closed.get(s.id, s.vintage_end)) for s in stored]
        result += [_Interval(v.vintage_start, v.vintage_end) for v in inserts]
        problem = _overlap_problem(result)
        if problem:
            issue("inconsistent_history", f"applying the changes would leave {problem}")

    plan.issues.extend(issues)
    if any(i.severity == ERROR for i in issues):
        plan.periods_skipped += 1
        return
    plan.inserts.extend(inserts)
    plan.closes.extend(closes)
    plan.confirms.extend(confirms)
