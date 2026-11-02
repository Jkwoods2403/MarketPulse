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
skipped. Only "source_vintage" series are accepted (label guard); first-seen (EIA) series are
written by `snapshot_writer`. The database operations are shared with it (`version_apply`).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import insert, or_, select
from sqlalchemy.orm import Session

from marketpulse.core.versions import ObservationVersion
from marketpulse.db.enums import VintageBasis
from marketpulse.db.models import DataQualityIssue, Observation, Series
from marketpulse.ingestion.version_apply import (
    OBSERVATION_LOCK_NAMESPACE,
    ConcurrentModificationError,
    VintageBasisMismatchError,
    close_versions,
    confirm_versions,
    insert_rows,
    lock_series,
    observations_table,
    require_vintage_basis,
)
from marketpulse.ingestion.versions import ERROR, PlannedIssue, StoredVersion, plan_changes

__all__ = [
    "OBSERVATION_LOCK_NAMESPACE",
    "ConcurrentModificationError",
    "VintageBasisMismatchError",
    "WriteResult",
    "load_stored_versions",
    "lock_series",
    "write_versions",
]


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
    series = session.get(Series, series_id)
    if series is None:
        raise ValueError(f"series id {series_id} does not exist")
    require_vintage_basis(series, VintageBasis.SOURCE_VINTAGE)

    lock_series(session, series_id)
    stored = load_stored_versions(session, series_id, coverage_start)
    plan = plan_changes(stored, versions, coverage_start=coverage_start)
    table = observations_table()

    close_versions(session, plan.closes, confirmed_at=retrieved_at)
    confirm_versions(session, plan.confirms, confirmed_at=retrieved_at)

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
    insert_rows(session, insert(table), rows)

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
