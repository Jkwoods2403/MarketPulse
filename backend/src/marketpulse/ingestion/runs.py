"""Ingestion run tracking (ops.ingestion_runs).

The run record lives in its *own* transactions, separate from the data it describes:
- on entry, a `running` row is inserted and committed immediately, so an attempt is visible
  even if the process dies;
- on exit, the row is updated and committed with the outcome, even when the data transaction
  was rolled back.

Outcomes:
- succeeded: no exception and no error-level data-quality issues;
- partial:   no exception, but some periods were skipped because of error-level issues;
- failed:    an exception escaped (it is re-raised); the error message is redacted.

A row left `running` means the process was killed mid-run, or the final update failed; step 1.6
will report those.

Finalization failure: if the final update of the run row fails, `status` stays None, `finalized`
stays False, and `finalization_error` holds the (redacted) cause. If the run body had already
raised, that original exception propagates unchanged; otherwise `RunFinalizationError` is
raised. `data_committed` says whether a data transaction of this run had already committed, so
callers never present committed changes as rolled back.
"""

import logging
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from types import TracebackType
from typing import Any, Self

from sqlalchemy import func, update
from sqlalchemy.orm import Session, sessionmaker

from marketpulse import __version__
from marketpulse.core.config import REPO_ROOT
from marketpulse.core.redaction import redact_text
from marketpulse.core.versions import ObservationVersion
from marketpulse.db.enums import RunStatus
from marketpulse.db.models import IngestionRun
from marketpulse.ingestion.observation_writer import WriteResult, write_versions
from marketpulse.ingestion.snapshot_writer import SnapshotWriteResult, write_snapshot
from marketpulse.ingestion.snapshots import SnapshotObservation, SnapshotScope

logger = logging.getLogger(__name__)

MAX_ERROR_MESSAGE_CHARS = 2_000


class RunFinalizationError(RuntimeError):
    """The run's outcome could not be recorded; its row in ops.ingestion_runs stays `running`."""


# Credentials the application reads from the environment. `git` never needs them, so they are
# left out of its environment: every secret Settings reads (all `SecretStr` fields) plus libpq's
# PGPASSWORD. Compared case-insensitively (pydantic-settings reads env names that way).
SUBPROCESS_EXCLUDED_ENV = frozenset(
    {"EIA_API_KEY", "FRED_API_KEY", "POSTGRES_PASSWORD", "PGPASSWORD"}
)


def subprocess_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of `source` (default: this process's environment) without credentials, for child
    processes. Neither `source` nor the process's own environment is modified.
    """
    return {
        name: value
        for name, value in (os.environ if source is None else source).items()
        if name.upper() not in SUBPROCESS_EXCLUDED_ENV
    }


@lru_cache(maxsize=1)
def code_version() -> str:
    """Package version plus the git commit when available (e.g. '0.1.0+790a0e2').

    `git` runs with `subprocess_environment()`: everything it may need (PATH, HOME, ...) but no
    credential, so a provider key in this process's environment never reaches it.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            env=subprocess_environment(),
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return __version__
    return f"{__version__}+{sha}" if sha else __version__


def describe_error(exc: BaseException) -> str:
    message = redact_text(f"{type(exc).__name__}: {exc}")
    if len(message) > MAX_ERROR_MESSAGE_CHARS:
        suffix = " ... (truncated)"
        message = message[: MAX_ERROR_MESSAGE_CHARS - len(suffix)] + suffix
    return message


@dataclass
class RunCounts:
    fetched: int = 0
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0

    def add(
        self, *, fetched: int = 0, inserted: int = 0, revised: int = 0, unchanged: int = 0
    ) -> None:
        self.fetched += fetched
        self.inserted += inserted
        self.revised += revised
        self.unchanged += unchanged


class IngestionRunTracker:
    """Context manager recording one ingestion run.

    with IngestionRunTracker(factory, job="fred.observations", source_id=1,
                             params={"series_code": "ust_10y"}) as run:
        ...  # do the work in a separate data transaction, using run.run_id
        run.counts.add(inserted=10)
        run.mark_partial()  # if error-level quality issues were recorded
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        job: str,
        source_id: int | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        self._factory = session_factory
        self.job = job
        self.source_id = source_id
        self.params = dict(params or {})
        self.counts = RunCounts()
        self.run_id: int | None = None
        self.status: RunStatus | None = None  # set only once the outcome is recorded
        self.intended_status: RunStatus | None = None  # the outcome, recorded or not
        self.finalized = False
        self.finalization_error: str | None = None
        self.data_committed = False  # a data transaction of this run has committed
        self._partial = False

    def mark_partial(self) -> None:
        self._partial = True

    def __enter__(self) -> Self:
        with self._factory() as session, session.begin():
            run = IngestionRun(
                job=self.job,
                source_id=self.source_id,
                params=self.params,
                status=RunStatus.RUNNING.value,
                code_version=code_version(),
            )
            session.add(run)
            session.flush()
            self.run_id = run.id
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None:
            status = RunStatus.FAILED
        elif self._partial:
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.SUCCEEDED
        self.intended_status = status
        try:
            with self._factory() as session, session.begin():
                session.execute(
                    update(IngestionRun)
                    .where(IngestionRun.id == self.run_id)
                    .values(
                        status=status.value,
                        # clock_timestamp(): real time now, not the transaction start.
                        finished_at=func.clock_timestamp(),
                        rows_fetched=self.counts.fetched,
                        rows_inserted=self.counts.inserted,
                        rows_revised=self.counts.revised,
                        rows_unchanged=self.counts.unchanged,
                        error_message=describe_error(exc) if exc is not None else None,
                        # Parameters learned during the run (e.g. windows fetched) are kept.
                        params=self.params,
                    )
                )
        except Exception as final_exc:
            self.finalization_error = describe_error(final_exc)
            committed = (
                "data written by this run was already committed"
                if self.data_committed
                else "no data transaction of this run had committed"
            )
            logger.error(
                "run %s: could not record outcome %r (%s); the run row still says 'running': %s",
                self.run_id,
                status.value,
                committed,
                self.finalization_error,
            )
            if exc is not None:
                return  # the original failure propagates unchanged
            raise RunFinalizationError(
                f"could not record the outcome {status.value!r} of run {self.run_id}; its row "
                f"still says 'running' ({committed}): {self.finalization_error}"
            ) from final_exc
        self.status = status
        self.finalized = True
        # Returning None re-raises any exception.

    def write(
        self,
        *,
        series_id: int,
        versions: Sequence[ObservationVersion],
        retrieved_at: datetime,
        coverage_start: datetime | None = None,
    ) -> WriteResult:
        """Write one series' versions in a single all-or-nothing data transaction for this run.

        Updates this run's counts, and marks it partial if error-level issues were recorded.
        """
        if self.run_id is None:
            raise RuntimeError("the run has not been started (use it as a context manager)")
        with self._factory() as session, session.begin():
            result = write_versions(
                session,
                series_id=series_id,
                ingestion_run_id=self.run_id,
                versions=versions,
                retrieved_at=retrieved_at,
                coverage_start=coverage_start,
            )
        self.data_committed = True
        self.counts.add(
            fetched=result.fetched,
            inserted=result.inserted,
            revised=result.revised,
            unchanged=result.unchanged,
        )
        if result.has_errors:
            self.mark_partial()
        return result

    def write_snapshot(
        self,
        *,
        series_id: int,
        snapshot_at: datetime,
        scope: SnapshotScope,
        period_format: str,
        incoming: Sequence[SnapshotObservation],
        incremental_window_start: str | None = None,
    ) -> SnapshotWriteResult:
        """Apply one first-seen snapshot in a single all-or-nothing data transaction.

        A rejected snapshot commits only its issue rows (no observation changes) and marks the
        run partial; an exception rolls the whole transaction back and fails the run.
        """
        if self.run_id is None:
            raise RuntimeError("the run has not been started (use it as a context manager)")
        with self._factory() as session, session.begin():
            result = write_snapshot(
                session,
                series_id=series_id,
                ingestion_run_id=self.run_id,
                snapshot_at=snapshot_at,
                scope=scope,
                period_format=period_format,
                incoming=incoming,
                incremental_window_start=incremental_window_start,
            )
        if not result.rejected:  # a rejected snapshot commits only its issue rows
            self.data_committed = True
        self.counts.add(
            fetched=result.fetched,
            inserted=result.inserted,
            revised=result.revised,
            unchanged=result.confirmed,
        )
        if result.rejected:
            self.mark_partial()
        return result


def record_series_versions(
    session_factory: sessionmaker[Session],
    *,
    job: str,
    source_id: int,
    series_id: int,
    versions: Sequence[ObservationVersion],
    retrieved_at: datetime,
    coverage_start: datetime | None = None,
    params: Mapping[str, Any] | None = None,
) -> tuple[int, WriteResult]:
    """Write one series' versions in a single data transaction, tracked as one ingestion run.

    The data transaction commits only if the whole write succeeds; any exception rolls it back
    and the run is recorded as failed (the exception is re-raised).
    """
    tracker = IngestionRunTracker(
        session_factory,
        job=job,
        source_id=source_id,
        params={"series_id": series_id, **(params or {})},
    )
    with tracker as run:
        result = run.write(
            series_id=series_id,
            versions=versions,
            retrieved_at=retrieved_at,
            coverage_start=coverage_start,
        )
    if tracker.run_id is None:  # pragma: no cover  (always set by __enter__)
        raise RuntimeError("ingestion run was not created")
    return tracker.run_id, result
