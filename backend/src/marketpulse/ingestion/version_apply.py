"""Shared database operations for applying planned observation-version changes.

Used by both writers: the FRED writer (provider vintages, `observation_writer`) and the
first-seen snapshot writer (EIA, `snapshot_writer`). Everything here runs inside the caller's
transaction and never commits.

- `lock_series`: a transaction-scoped advisory lock per series. Both writers use the same
  namespace, so any two writers for one series queue behind each other; it is released at
  commit or rollback.
- `require_vintage_basis`: the label guard. A writer refuses a series whose version timestamps
  mean something other than what the writer produces.
- `close_versions`, `confirm_versions`, `insert_rows`: the only three ways history changes.
  Existing rows are never deleted and their values never rewritten; only `vintage_end` (once,
  from NULL) and confirmation fields change.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Insert, Table, bindparam, text, update
from sqlalchemy.orm import Session

from marketpulse.db.enums import VintageBasis
from marketpulse.db.models import Observation, Series

# First key of the two-key advisory lock: identifies "observation writes" ("MP" in ASCII).
OBSERVATION_LOCK_NAMESPACE = 0x4D50

INSERT_BATCH = 5_000


class ConcurrentModificationError(RuntimeError):
    """A row changed underneath the writer despite the lock (should never happen)."""


class VintageBasisMismatchError(ValueError):
    """A writer was given a series whose vintage basis it does not produce."""


def observations_table() -> Table:
    return cast(Table, Observation.__table__)


def lock_series(session: Session, series_id: int) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :series_id)"),
        {"namespace": OBSERVATION_LOCK_NAMESPACE, "series_id": series_id},
    )


def require_vintage_basis(series: Series, expected: VintageBasis) -> None:
    if series.vintage_basis != expected.value:
        raise VintageBasisMismatchError(
            f"series {series.code!r} (id {series.id}) has vintage_basis "
            f"{series.vintage_basis!r}; this writer only writes {expected.value!r} series"
        )


def close_versions(
    session: Session,
    closes: Sequence[tuple[int, datetime]],
    *,
    confirmed_at: datetime | None = None,
) -> None:
    """Set vintage_end on current versions (and last_confirmed_at, when given).

    Each row must still be current; anything else raises ConcurrentModificationError.
    """
    table = observations_table()
    for stored_id, vintage_end in closes:
        values: dict[str, Any] = {"vintage_end": vintage_end}
        if confirmed_at is not None:
            values["last_confirmed_at"] = confirmed_at
        result = session.execute(
            update(table)
            .where(table.c.id == stored_id, table.c.vintage_end.is_(None))
            .values(**values)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise ConcurrentModificationError(f"observation {stored_id} was not open when closing")


def confirm_versions(
    session: Session,
    ids: Sequence[int],
    *,
    confirmed_at: datetime,
    run_id: int | None = None,
    current_only: bool = False,
) -> None:
    """Record that stored versions were reported unchanged.

    With `current_only` (snapshot writes), each row must still be current and last confirmed
    before `confirmed_at`, and `last_confirmed_run_id` is set; a row count other than len(ids)
    raises ConcurrentModificationError. Without it (FRED, which also confirms closed vintages),
    only last_confirmed_at is set.
    """
    if not ids:
        return
    table = observations_table()
    statement = update(table).where(table.c.id.in_(bindparam("ids", expanding=True)))
    values: dict[str, Any] = {"last_confirmed_at": confirmed_at}
    if current_only:
        statement = statement.where(
            table.c.vintage_end.is_(None), table.c.last_confirmed_at < confirmed_at
        )
    if run_id is not None:
        values["last_confirmed_run_id"] = run_id
    result = session.execute(statement.values(**values), {"ids": list(ids)})
    if current_only and result.rowcount != len(ids):  # type: ignore[attr-defined]
        raise ConcurrentModificationError(
            f"confirmed {result.rowcount} of {len(ids)} observations; "  # type: ignore[attr-defined]
            "some were no longer current or already confirmed at this time"
        )


def insert_rows(session: Session, statement: Insert, rows: Sequence[Mapping[str, Any]]) -> None:
    """Execute an INSERT statement built by the caller for `rows`, in batches."""
    for start in range(0, len(rows), INSERT_BATCH):
        session.execute(statement, list(rows[start : start + INSERT_BATCH]))
