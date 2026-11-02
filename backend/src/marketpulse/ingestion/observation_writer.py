"""Persist observation versions for one series inside the caller's transaction.

Concurrency and consistency:
1. A transaction-scoped PostgreSQL advisory lock is taken for the series
   (`pg_advisory_xact_lock(namespace, series_id)`). Concurrent writers for the same series queue
   behind it; different series proceed in parallel. The lock is released automatically at
   commit or rollback, so a crashed writer cannot leave it held.
2. Stored history is read *after* the lock is acquired, so the plan is based on data no other
   writer can change until this transaction ends.
3. Changes are applied in an order that never violates the "one current version per period"
   constraint: closes, then confirms, then inserts.
4. Nothing is committed here. The caller commits once, so a failure anywhere rolls back every
   change for the series (no partially written history).

Database constraints remain the final guard: an unexpected conflict raises instead of being
skipped.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from sqlalchemy import Table, bindparam, insert, or_, select, text, update
from sqlalchemy.orm import Session

from marketpulse.core.versions import ObservationVersion
from marketpulse.db.models import DataQualityIssue, Observation, Series
from marketpulse.ingestion.versions import ERROR, PlannedIssue, StoredVersion, plan_changes

# First key of the two-key advisory lock: identifies "observation writes" ("MP" in ASCII).
OBSERVATION_LOCK_NAMESPACE = 0x4D50

_INSERT_BATCH = 5_000


class ConcurrentModificationError(RuntimeError):
    """A row changed underneath the writer despite the lock (should never happen)."""


@dataclass
class WriteResult:
    fetched: int = 0  # incoming versions considered
    inserted: int = 0  # new versions added
    revised: int = 0  # stored current versions closed by a newer version
    unchanged: int = 0  # stored versions confirmed as still reported
    periods_skipped: int = 0  # periods left untouched because of an error-level issue
    issues: list[PlannedIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == ERROR for issue in self.issues)


def lock_series(session: Session, series_id: int) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :series_id)"),
        {"namespace": OBSERVATION_LOCK_NAMESPACE, "series_id": series_id},
    )


def load_stored_versions(
    session: Session, series_id: int, coverage_start: datetime | None = None
) -> list[StoredVersion]:
    o = Observation
    query = select(
        o.id, o.period_start, o.period_end, o.vintage_start, o.vintage_end, o.value, o.status
    ).where(o.series_id == series_id)
    if coverage_start is not None:
        # Versions that ended before the fetched range cannot be affected.
        query = query.where(or_(o.vintage_end.is_(None), o.vintage_end > coverage_start))
    return [StoredVersion(*row) for row in session.execute(query)]


def write_versions(
    session: Session,
    *,
    series_id: int,
    ingestion_run_id: int,
    versions: Sequence[ObservationVersion],
    retrieved_at: datetime,
    coverage_start: datetime | None = None,
) -> WriteResult:
    """Lock the series, compare `versions` with stored history, and apply the plan.

    Runs in the caller's transaction and does not commit.
    """
    if retrieved_at.tzinfo is None:
        raise ValueError("retrieved_at must be timezone-aware")
    if session.get(Series, series_id) is None:
        raise ValueError(f"series id {series_id} does not exist")

    lock_series(session, series_id)
    stored = load_stored_versions(session, series_id, coverage_start)
    plan = plan_changes(stored, versions, coverage_start=coverage_start)
    table = cast(Table, Observation.__table__)

    for stored_id, vintage_end in plan.closes:
        result = session.execute(
            update(table)
            .where(table.c.id == stored_id, table.c.vintage_end.is_(None))
            .values(vintage_end=vintage_end, last_confirmed_at=retrieved_at)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise ConcurrentModificationError(f"observation {stored_id} was not open when closing")

    if plan.confirms:
        session.execute(
            update(table)
            .where(table.c.id.in_(bindparam("ids", expanding=True)))
            .values(last_confirmed_at=retrieved_at),
            {"ids": plan.confirms},
        )

    rows = [
        {
            "series_id": series_id,
            "period_start": v.period_start,
            "period_end": v.period_end,
            "value": v.value,
            "status": v.status,
            "vintage_start": v.vintage_start,
            "vintage_end": v.vintage_end,
            "first_retrieved_at": retrieved_at,
            "last_confirmed_at": retrieved_at,
            "ingestion_run_id": ingestion_run_id,
        }
        for v in plan.inserts
    ]
    for start in range(0, len(rows), _INSERT_BATCH):
        session.execute(insert(table), rows[start : start + _INSERT_BATCH])

    if plan.issues:
        session.add_all(
            DataQualityIssue(
                ingestion_run_id=ingestion_run_id,
                series_id=series_id,
                period_start=issue.period_start,
                check_name=issue.check_name,
                detail=issue.detail,
                severity=issue.severity,
            )
            for issue in plan.issues
        )
    session.flush()

    return WriteResult(
        fetched=len(versions),
        inserted=len(plan.inserts),
        revised=len(plan.closes),
        unchanged=len(plan.confirms),
        periods_skipped=plan.periods_skipped,
        issues=list(plan.issues),
    )
