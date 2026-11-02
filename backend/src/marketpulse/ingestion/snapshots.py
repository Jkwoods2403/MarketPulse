"""Plan first-seen snapshot versions (pure; no database access).

For sources that publish no vintages (EIA), Market Pulse versions a series by what it saw and
when: each validated, complete response is a *snapshot* taken at one instant, `snapshot_at`,
captured once all pages were retrieved and passed validation. `snapshot_at` is when Market Pulse
saw the values. It is NOT the provider's publication time, and no version is ever dated before
Market Pulse first saw a value.

Compared with the stored current version of each period in the snapshot's scope:
- a period not stored yet           -> INSERT a version starting at snapshot_at;
- the same value and status         -> CONFIRM (no new version; exact Decimal equality, so
                                       "1.0" == "1.00" and a representation-only change does
                                       not create a version);
- a different value or status       -> CLOSE the current version at snapshot_at and INSERT the
  (including value <-> null)           new one starting at snapshot_at;
- a stored current period missing   -> VANISHED: the stored version is kept unchanged and NOT
  from the snapshot                    confirmed; the caller records a tracked warning.

Errors reject the whole snapshot (all-or-nothing per series). A rejected plan carries only its
issues: no inserts, closes, confirms, or vanished periods, so it cannot be applied by mistake.
- the snapshot is not strictly newer than stored history: snapshot_at at or before any stored
  version's start, end, or last confirmation (stale); periods whose stored value conflicts with
  the snapshot at such an instant are also reported (conflicting_snapshot);
- duplicate periods in the snapshot, or more than one current version of a period in storage;
- a period outside the requested scope;
- a scope bound, incremental window start, or source period not in the series' period format
  (scope comparisons are string comparisons, which are only chronological within one format);
- a period whose end or reported period string differs from the stored version;
- missing provenance: an incoming observation without a source period or raw payload page, or a
  stored current version without a source period;
- more than VANISHED_MAX_PERIODS periods, or more than VANISHED_MAX_FRACTION of the stored
  periods in scope, vanished at once (more likely a source problem than real revisions).
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from marketpulse.ingestion.versions import ERROR

INFO = "info"
VANISHED_MAX_PERIODS = 5
VANISHED_MAX_FRACTION = Decimal("0.01")

# Source period formats (as named in sources.eia.periods.DATE_FORMATS). Values in one format
# sort chronologically as strings, which scope and window comparisons rely on.
PERIOD_FORMATS: dict[str, re.Pattern[str]] = {
    "YYYY-MM-DD": re.compile(r"\d{4}-\d{2}-\d{2}"),
    "YYYY-MM": re.compile(r"\d{4}-\d{2}"),
}


@dataclass(frozen=True)
class SnapshotObservation:
    """One validated observation in a snapshot, with the raw page it came from.

    `source_period` and `raw_payload_id` are provenance and required for every new version; a
    missing value is reported by the planner as a `missing_provenance` error.
    """

    source_period: str  # exactly as the provider reported it
    period_start: date
    period_end: date
    value: Decimal | None
    status: str
    raw_payload_id: int | None

    def __post_init__(self) -> None:
        if self.period_end < self.period_start:
            raise ValueError(f"period_end {self.period_end} precedes {self.period_start}")
        if (self.value is None) != (self.status == "missing_at_source"):
            raise ValueError("value must be None exactly when status is 'missing_at_source'")
        if self.value is not None and not self.value.is_finite():
            raise ValueError(f"value must be finite, got {self.value}")


@dataclass(frozen=True)
class StoredSnapshotVersion:
    """A version already in the database (current or closed)."""

    id: int
    period_start: date
    period_end: date
    source_period: str | None
    value: Decimal | None
    status: str
    vintage_start: datetime
    vintage_end: datetime | None
    last_confirmed_at: datetime


@dataclass(frozen=True)
class SnapshotScope:
    """Source periods the snapshot covers completely (inclusive; None = unbounded).

    A full-history reconciliation has no bounds. Bounds are compared as provider period strings
    and must be in the series' period format (checked by the planner).
    """

    start: str | None = None
    end: str | None = None

    def contains(self, source_period: str) -> bool:
        return (self.start is None or source_period >= self.start) and (
            self.end is None or source_period <= self.end
        )

    @property
    def is_full_history(self) -> bool:
        return self.start is None and self.end is None


@dataclass(frozen=True)
class SnapshotIssue:
    period_start: date | None  # None for whole-snapshot problems
    check_name: str
    detail: str
    severity: str  # ERROR rejects the snapshot; INFO is recorded only


@dataclass(frozen=True)
class VanishedPeriod:
    stored_id: int
    period_start: date
    source_period: str
    value: Decimal | None


@dataclass
class SnapshotPlan:
    """The planned changes, or (when rejected) only the issues explaining why."""

    snapshot_at: datetime
    inserts: list[SnapshotObservation] = field(default_factory=list)  # start at snapshot_at
    closes: list[int] = field(default_factory=list)  # stored ids; vintage_end = snapshot_at
    confirms: list[int] = field(default_factory=list)  # stored ids, unchanged
    vanished: list[VanishedPeriod] = field(default_factory=list)
    issues: list[SnapshotIssue] = field(default_factory=list)  # errors reject the snapshot
    new_periods: int = 0
    in_scope: int = 0  # stored current periods inside the scope

    @property
    def rejected(self) -> bool:
        return any(issue.severity == ERROR for issue in self.issues)

    @property
    def revisions(self) -> int:
        return len(self.closes)


def _same(stored: StoredSnapshotVersion, incoming: SnapshotObservation) -> bool:
    # Exact Decimal comparison (no float); status must match exactly.
    return stored.status == incoming.status and stored.value == incoming.value


def plan_snapshot(
    stored: Iterable[StoredSnapshotVersion],
    incoming: Iterable[SnapshotObservation],
    *,
    snapshot_at: datetime,
    scope: SnapshotScope,
    period_format: str,
    incremental_window_start: str | None = None,
) -> SnapshotPlan:
    """Plan how to record one complete snapshot of a series.

    `period_format` is the series' source period format (a key of PERIOD_FORMATS).
    `incremental_window_start` (a source period) marks revisions found before the regular
    incremental window with an info issue, so full-history reconciliations show how far back
    revisions reach.
    """
    if snapshot_at.tzinfo is None:
        raise ValueError("snapshot_at must be timezone-aware")
    pattern = PERIOD_FORMATS.get(period_format)
    if pattern is None:
        raise ValueError(f"unknown period format {period_format!r}")
    plan = _plan(
        list(stored),
        list(incoming),
        snapshot_at=snapshot_at,
        scope=scope,
        pattern=pattern,
        period_format=period_format,
        incremental_window_start=incremental_window_start,
    )
    if plan.rejected:
        # Keep only the diagnosis; nothing in a rejected plan can be applied.
        return SnapshotPlan(snapshot_at=snapshot_at, issues=plan.issues, in_scope=plan.in_scope)
    return plan


def _plan(
    stored: list[StoredSnapshotVersion],
    incoming: list[SnapshotObservation],
    *,
    snapshot_at: datetime,
    scope: SnapshotScope,
    pattern: re.Pattern[str],
    period_format: str,
    incremental_window_start: str | None,
) -> SnapshotPlan:
    plan = SnapshotPlan(snapshot_at=snapshot_at)

    def error(period: date | None, check: str, detail: str) -> None:
        plan.issues.append(SnapshotIssue(period, check, detail, ERROR))

    def well_formed(text: str) -> bool:
        return pattern.fullmatch(text) is not None

    # --- formats and provenance -----------------------------------------------------------------
    for name, bound in (
        ("scope start", scope.start),
        ("scope end", scope.end),
        ("incremental window start", incremental_window_start),
    ):
        if bound is not None and not well_formed(bound):
            error(
                None,
                "scope_format_mismatch",
                f"{name} {bound!r} is not in the series' period format {period_format}",
            )
    for o in incoming:
        if not o.source_period.strip() or o.raw_payload_id is None:
            missing = [
                name
                for name, absent in (
                    ("source_period", not o.source_period.strip()),
                    ("raw_payload_id", o.raw_payload_id is None),
                )
                if absent
            ]
            error(
                o.period_start,
                "missing_provenance",
                f"incoming observation for {o.period_start} has no {' or '.join(missing)}",
            )
        elif not well_formed(o.source_period):
            error(
                o.period_start,
                "source_period_format",
                f"source period {o.source_period!r} is not in the series' period format "
                f"{period_format}",
            )

    # --- stored history -------------------------------------------------------------------------
    current_count = Counter(s.period_start for s in stored if s.vintage_end is None)
    for period, n in sorted(current_count.items()):
        if n > 1:
            error(period, "duplicate_current_version", f"{n} current stored versions of {period}")
    current = {s.period_start: s for s in stored if s.vintage_end is None}
    for s in current.values():
        if s.source_period is None:
            error(s.period_start, "missing_provenance", "stored version has no source period")
        elif not well_formed(s.source_period):
            error(
                s.period_start,
                "source_period_format",
                f"stored source period {s.source_period!r} is not in the series' period format "
                f"{period_format}",
            )

    # --- the snapshot itself --------------------------------------------------------------------
    for period, n in Counter(o.period_start for o in incoming).items():
        if n > 1:
            error(period, "duplicate_period", f"period {period} appears {n} times")
    for text, n in Counter(o.source_period for o in incoming).items():
        if n > 1:
            error(None, "duplicate_period", f"source period {text!r} appears {n} times")
    for o in incoming:
        if not scope.contains(o.source_period):
            error(
                o.period_start,
                "period_outside_scope",
                f"source period {o.source_period!r} is outside the requested scope "
                f"[{scope.start}, {scope.end}]",
            )

    # A snapshot must be strictly newer than everything stored: equal to or earlier than any
    # stored start, end, or confirmation means it was already applied, replayed, or out of order.
    if stored:
        latest = max(
            max(s.vintage_start, s.last_confirmed_at, s.vintage_end or s.vintage_start)
            for s in stored
        )
        if snapshot_at <= latest:
            error(
                None,
                "stale_snapshot",
                f"snapshot at {snapshot_at.isoformat()} is not newer than stored history "
                f"(latest stored version time or confirmation {latest.isoformat()})",
            )
            for o in sorted(incoming, key=lambda o: o.period_start):
                prior = current.get(o.period_start)
                if (
                    prior is not None
                    and prior.last_confirmed_at >= snapshot_at
                    and not _same(prior, o)
                ):
                    error(
                        o.period_start,
                        "conflicting_snapshot",
                        f"stored value {prior.value} ({prior.status}), confirmed at "
                        f"{prior.last_confirmed_at.isoformat()}, differs from this snapshot at the "
                        f"same or an earlier instant ({o.value}, {o.status})",
                    )
    if plan.rejected:
        return plan

    # --- per period -----------------------------------------------------------------------------
    in_scope = {p: s for p, s in current.items() if scope.contains(s.source_period or "")}
    plan.in_scope = len(in_scope)

    seen: set[date] = set()
    for o in sorted(incoming, key=lambda o: o.period_start):
        seen.add(o.period_start)
        existing = current.get(o.period_start)
        if existing is None:
            plan.inserts.append(o)
            plan.new_periods += 1
            continue
        if existing.period_end != o.period_end:
            error(
                o.period_start,
                "period_end_conflict",
                f"stored period end {existing.period_end}, provider now reports {o.period_end}",
            )
            continue
        if existing.source_period != o.source_period:
            error(
                o.period_start,
                "source_period_conflict",
                f"stored source period {existing.source_period!r}, provider now reports "
                f"{o.source_period!r}",
            )
            continue
        if _same(existing, o):
            plan.confirms.append(existing.id)
            continue
        plan.closes.append(existing.id)
        plan.inserts.append(o)
        if incremental_window_start is not None and o.source_period < incremental_window_start:
            plan.issues.append(
                SnapshotIssue(
                    o.period_start,
                    "revision_outside_window",
                    f"revised {existing.value} ({existing.status}) -> {o.value} ({o.status}) "
                    f"before the incremental window start {incremental_window_start!r}",
                    INFO,
                )
            )

    plan.vanished = [
        VanishedPeriod(s.id, p, s.source_period or "", s.value)
        for p, s in sorted(in_scope.items())
        if p not in seen
    ]
    n = len(plan.vanished)
    if n > VANISHED_MAX_PERIODS or n > VANISHED_MAX_FRACTION * plan.in_scope:
        listed = ", ".join(v.source_period for v in plan.vanished[:10])
        error(
            None,
            "vanished_limit",
            f"{n} of {plan.in_scope} stored periods in scope are missing from the snapshot "
            f"(limit: more than {VANISHED_MAX_PERIODS} or more than "
            f"{VANISHED_MAX_FRACTION:%} rejects the snapshot): {listed}",
        )
    return plan
