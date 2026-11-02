"""Apply one first-seen snapshot of a series inside the caller's transaction (EIA).

The plan comes from the pure planner (`snapshots.plan_snapshot`); this module guards the
inputs against the database and applies the plan. Nothing is committed here: the caller commits
once, so any exception rolls back every change for the series.

Order of work (all after the series' advisory lock, shared with the FRED writer):
1. Label guard: the series must be "first_seen"; the run must exist and still be running.
2. Time guard: `snapshot_at` (Python UTC clock, taken by the job after every page was retrieved
   and validated) must be no earlier than the run's start, no earlier than any cited raw page's
   retrieval, and at most FUTURE_TOLERANCE ahead of the database clock (a future time would
   make every later snapshot look stale). Retries are new runs with a fresh fetch and a fresh
   `snapshot_at`; a time from an earlier attempt fails the run-start check.
3. Provenance guard: every cited raw payload must exist, belong to this run, and come from the
   series' source. (Missing provenance is a planner error; inserts are asserted once more.)
4. Stale-snapshot guard: `snapshot_at` must be later than every stored version start, end, and
   confirmation of the series, closed versions included. A replay, a duplicate application, or
   an out-of-order snapshot is rejected (`stale_snapshot`), never applied.
5. Plan against the current versions, then either
   - REJECTED: record only the plan's issues; no observation or vanished-issue change at all;
   - ACCEPTED: closes (vintage_end = snapshot_at; a replaced value is not "confirmed"), confirms
     (last_confirmed_at = snapshot_at, last_confirmed_run_id = run), inserts (vintage_start =
     first_retrieved_at = last_confirmed_at = snapshot_at, with source_period and
     raw_payload_id), then the tracked "vanished_period" issues and info issues.

Tracked vanished-period issues (one open row per series and period, D1 partial unique index):
- opened on first detection (occurrences = 1, last_detected_* = this snapshot);
- updated on each later detection while open (occurrences + 1, last_detected_* moved; the first
  detection's run, created_at, and detail are kept);
- resolved when an applied snapshot reports the period again (resolved_at = snapshot_at,
  resolved_by_run_id = run); a later disappearance opens a new row.
Open issues outside the snapshot's scope are left untouched.

Known limitation (accepted for D3): a period the provider removes permanently stays stored
(append-only) and counts as vanished in every snapshot covering it, so once the vanishing limit
is exceeded every such snapshot is rejected. The open issue and the rejection are the alert.
Backlog: an audited operator decision to retire a period.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import cast

from sqlalchemy import Table, bindparam, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from marketpulse.db.enums import RunStatus, VintageBasis
from marketpulse.db.models import DataQualityIssue, IngestionRun, Observation, RawPayload, Series
from marketpulse.ingestion.snapshots import (
    PERIOD_FORMATS,
    SnapshotIssue,
    SnapshotObservation,
    SnapshotPlan,
    SnapshotScope,
    StoredSnapshotVersion,
    plan_snapshot,
)
from marketpulse.ingestion.version_apply import (
    close_versions,
    confirm_versions,
    insert_rows,
    lock_series,
    observations_table,
    require_vintage_basis,
)
from marketpulse.ingestion.versions import ERROR, WARNING

FUTURE_TOLERANCE = timedelta(minutes=5)
VANISHED_PERIOD = "vanished_period"
STALE_SNAPSHOT = "stale_snapshot"


class SnapshotTimeError(ValueError):
    """snapshot_at is inconsistent with the run, its raw pages, or the database clock."""


class ProvenanceError(ValueError):
    """A cited raw payload does not exist or does not belong to this run and source."""


@dataclass
class SnapshotWriteResult:
    fetched: int = 0  # observations in the snapshot
    inserted: int = 0  # new versions (new periods + revisions)
    new_periods: int = 0
    revised: int = 0  # current versions closed by a revision
    confirmed: int = 0  # current versions confirmed unchanged
    vanished: int = 0  # stored periods in scope missing from the snapshot
    vanished_opened: int = 0
    vanished_updated: int = 0
    vanished_resolved: int = 0
    rejected: bool = False
    issues: list[SnapshotIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == ERROR for issue in self.issues)


def write_snapshot(
    session: Session,
    *,
    series_id: int,
    ingestion_run_id: int,
    snapshot_at: datetime,
    scope: SnapshotScope,
    period_format: str,
    incoming: Iterable[SnapshotObservation],
    incremental_window_start: str | None = None,
) -> SnapshotWriteResult:
    """Guard, plan, and apply one complete snapshot of a first-seen series. Does not commit."""
    if snapshot_at.tzinfo is None:
        raise ValueError("snapshot_at must be timezone-aware")
    if period_format not in PERIOD_FORMATS:
        raise ValueError(f"unknown period format {period_format!r}")
    incoming = list(incoming)
    series = session.get(Series, series_id)
    if series is None:
        raise ValueError(f"series id {series_id} does not exist")

    lock_series(session, series_id)
    require_vintage_basis(series, VintageBasis.FIRST_SEEN)
    run = session.get(IngestionRun, ingestion_run_id)
    if run is None:
        raise ValueError(f"ingestion run {ingestion_run_id} does not exist")
    if run.status != RunStatus.RUNNING.value:
        raise ValueError(f"ingestion run {ingestion_run_id} is {run.status!r}, not running")
    _check_snapshot_time(session, snapshot_at, run)
    _check_raw_payloads(session, incoming, run, series.source_id, snapshot_at)

    plan = plan_snapshot(
        _load_current_versions(session, series_id),
        incoming,
        snapshot_at=snapshot_at,
        scope=scope,
        period_format=period_format,
        incremental_window_start=incremental_window_start,
    )
    plan = _apply_stale_guard(session, series_id, plan)

    result = SnapshotWriteResult(fetched=len(incoming), issues=list(plan.issues))
    if plan.rejected:
        result.rejected = True
        _add_issues(session, ingestion_run_id, series_id, plan.issues)
        session.flush()
        return result

    close_versions(session, [(stored_id, snapshot_at) for stored_id in plan.closes])
    confirm_versions(
        session,
        plan.confirms,
        confirmed_at=snapshot_at,
        run_id=ingestion_run_id,
        current_only=True,
    )
    insert_rows(
        session,
        insert(observations_table()),
        [_insert_row(o, series_id, ingestion_run_id, snapshot_at) for o in plan.inserts],
    )
    _track_vanished(session, plan, series_id, ingestion_run_id, result)
    result.vanished_resolved = _resolve_vanished(
        session, series_id, ingestion_run_id, snapshot_at, {o.period_start for o in incoming}
    )
    _add_issues(session, ingestion_run_id, series_id, plan.issues)
    session.flush()

    result.inserted = len(plan.inserts)
    result.new_periods = plan.new_periods
    result.revised = len(plan.closes)
    result.confirmed = len(plan.confirms)
    result.vanished = len(plan.vanished)
    return result


# --- guards -------------------------------------------------------------------------------------


def _check_snapshot_time(session: Session, snapshot_at: datetime, run: IngestionRun) -> None:
    db_now = session.scalar(select(func.clock_timestamp()))
    if db_now is not None and snapshot_at > db_now + FUTURE_TOLERANCE:
        raise SnapshotTimeError(
            f"snapshot_at {snapshot_at.isoformat()} is more than {FUTURE_TOLERANCE} ahead of the "
            f"database clock ({db_now.isoformat()})"
        )
    if snapshot_at < run.started_at:
        raise SnapshotTimeError(
            f"snapshot_at {snapshot_at.isoformat()} is before run {run.id} started "
            f"({run.started_at.isoformat()}); retries must take a fresh snapshot time"
        )


def _check_raw_payloads(
    session: Session,
    incoming: list[SnapshotObservation],
    run: IngestionRun,
    source_id: int,
    snapshot_at: datetime,
) -> None:
    cited = {o.raw_payload_id for o in incoming if o.raw_payload_id is not None}
    if not cited:
        return
    rows = session.execute(
        select(
            RawPayload.id,
            RawPayload.ingestion_run_id,
            RawPayload.source_id,
            RawPayload.retrieved_at,
        ).where(RawPayload.id.in_(bindparam("ids", expanding=True))),
        {"ids": sorted(cited)},
    ).all()
    found = {row.id: row for row in rows}
    missing = sorted(cited - found.keys())
    if missing:
        raise ProvenanceError(f"raw payloads {missing} do not exist")
    for row in sorted(found.values(), key=lambda r: r.id):
        if row.ingestion_run_id != run.id:
            raise ProvenanceError(
                f"raw payload {row.id} belongs to run {row.ingestion_run_id}, not run {run.id}"
            )
        if row.source_id != source_id:
            raise ProvenanceError(
                f"raw payload {row.id} is from source {row.source_id}, not the series' source "
                f"{source_id}"
            )
        if row.retrieved_at > snapshot_at:
            raise SnapshotTimeError(
                f"raw payload {row.id} was retrieved at {row.retrieved_at.isoformat()}, after "
                f"snapshot_at {snapshot_at.isoformat()}; take snapshot_at after every page"
            )


def _apply_stale_guard(session: Session, series_id: int, plan: SnapshotPlan) -> SnapshotPlan:
    """Reject the plan if any stored version (closed ones included) is not older than it."""
    o = Observation
    latest = session.scalar(
        select(
            func.max(
                func.greatest(
                    o.vintage_start,
                    func.coalesce(o.vintage_end, o.vintage_start),
                    o.last_confirmed_at,
                )
            )
        ).where(o.series_id == series_id)
    )
    if latest is None or plan.snapshot_at > latest:
        return plan
    if any(issue.check_name == STALE_SNAPSHOT for issue in plan.issues):
        return plan  # the planner already rejected it as stale
    stale = SnapshotIssue(
        None,
        STALE_SNAPSHOT,
        f"snapshot at {plan.snapshot_at.isoformat()} is not newer than stored history "
        f"(latest stored version time or confirmation {latest.isoformat()})",
        ERROR,
    )
    return SnapshotPlan(
        snapshot_at=plan.snapshot_at, issues=[*plan.issues, stale], in_scope=plan.in_scope
    )


# --- reads and writes ---------------------------------------------------------------------------


def _load_current_versions(session: Session, series_id: int) -> list[StoredSnapshotVersion]:
    o = Observation
    query = select(
        o.id,
        o.period_start,
        o.period_end,
        o.source_period,
        o.value,
        o.status,
        o.vintage_start,
        o.vintage_end,
        o.last_confirmed_at,
    ).where(o.series_id == series_id, o.vintage_end.is_(None))
    return [StoredSnapshotVersion(*row) for row in session.execute(query)]


def _insert_row(
    o: SnapshotObservation, series_id: int, run_id: int, snapshot_at: datetime
) -> dict[str, object]:
    # The planner rejects missing provenance; this is the last line of defense.
    if not o.source_period.strip() or o.raw_payload_id is None:
        raise ProvenanceError(f"refusing to insert {o.period_start} without provenance")
    return {
        "series_id": series_id,
        "period_start": o.period_start,
        "period_end": o.period_end,
        "value": o.value,
        "status": o.status,
        "vintage_start": snapshot_at,
        "vintage_end": None,
        "first_retrieved_at": snapshot_at,
        "last_confirmed_at": snapshot_at,
        "ingestion_run_id": run_id,
        "source_period": o.source_period,
        "raw_payload_id": o.raw_payload_id,
        "last_confirmed_run_id": run_id,
    }


def _track_vanished(
    session: Session, plan: SnapshotPlan, series_id: int, run_id: int, result: SnapshotWriteResult
) -> None:
    issue = cast(Table, DataQualityIssue.__table__)
    # Literal predicate, identical to the D1 partial unique index, so PostgreSQL infers it.
    open_vanished = text(f"check_name = '{VANISHED_PERIOD}' AND resolved_at IS NULL")
    for v in plan.vanished:
        statement = (
            pg_insert(issue)
            .values(
                ingestion_run_id=run_id,
                series_id=series_id,
                period_start=v.period_start,
                check_name=VANISHED_PERIOD,
                detail=(
                    f"period {v.source_period!r} (stored value {v.value}) is missing from the "
                    f"snapshot at {plan.snapshot_at.isoformat()}; the stored version is kept "
                    "and not confirmed"
                ),
                severity=WARNING,
                last_detected_at=plan.snapshot_at,
                last_detected_run_id=run_id,
                occurrences=1,
            )
            .on_conflict_do_update(
                index_elements=[issue.c.series_id, issue.c.period_start],
                index_where=open_vanished,
                set_={
                    "last_detected_at": plan.snapshot_at,
                    "last_detected_run_id": run_id,
                    "occurrences": issue.c.occurrences + 1,
                },
            )
            .returning(issue.c.occurrences)
        )
        occurrences = session.execute(statement).scalar_one()
        if occurrences == 1:
            result.vanished_opened += 1
        else:
            result.vanished_updated += 1


def _resolve_vanished(
    session: Session, series_id: int, run_id: int, snapshot_at: datetime, reported: set[date]
) -> int:
    if not reported:
        return 0
    d = DataQualityIssue
    outcome = session.execute(
        update(d)
        .where(
            d.series_id == series_id,
            d.check_name == VANISHED_PERIOD,
            d.resolved_at.is_(None),
            d.period_start.in_(bindparam("periods", expanding=True)),
        )
        .values(resolved_at=snapshot_at, resolved_by_run_id=run_id)
        .execution_options(synchronize_session=False),
        {"periods": sorted(reported)},
    )
    return int(outcome.rowcount)  # type: ignore[attr-defined]


def _add_issues(
    session: Session, run_id: int, series_id: int, issues: Iterable[SnapshotIssue]
) -> None:
    session.add_all(
        DataQualityIssue(
            ingestion_run_id=run_id,
            series_id=series_id,
            period_start=issue.period_start,
            check_name=issue.check_name,
            detail=issue.detail,
            severity=issue.severity,
        )
        for issue in issues
    )
