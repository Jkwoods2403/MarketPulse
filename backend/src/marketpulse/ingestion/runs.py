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

A row left `running` means the process was killed mid-run; step 1.6 will report those.
"""

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

MAX_ERROR_MESSAGE_CHARS = 2_000


@lru_cache(maxsize=1)
def code_version() -> str:
    """Package version plus the git commit when available (e.g. '0.1.0+790a0e2')."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
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
        self.status: RunStatus | None = None
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
            self.status = RunStatus.FAILED
        elif self._partial:
            self.status = RunStatus.PARTIAL
        else:
            self.status = RunStatus.SUCCEEDED
        with self._factory() as session, session.begin():
            session.execute(
                update(IngestionRun)
                .where(IngestionRun.id == self.run_id)
                .values(
                    status=self.status.value,
                    # clock_timestamp(): real time now, not the transaction start.
                    finished_at=func.clock_timestamp(),
                    rows_fetched=self.counts.fetched,
                    rows_inserted=self.counts.inserted,
                    rows_revised=self.counts.revised,
                    rows_unchanged=self.counts.unchanged,
                    error_message=describe_error(exc) if exc is not None else None,
                )
            )
        # Returning None re-raises any exception.


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
        if run.run_id is None:  # pragma: no cover  (always set by __enter__)
            raise RuntimeError("ingestion run was not created")
        run_id = run.run_id
        with session_factory() as session, session.begin():
            result = write_versions(
                session,
                series_id=series_id,
                ingestion_run_id=run_id,
                versions=versions,
                retrieved_at=retrieved_at,
                coverage_start=coverage_start,
            )
        run.counts.add(
            fetched=result.fetched,
            inserted=result.inserted,
            revised=result.revised,
            unchanged=result.unchanged,
        )
        if result.has_errors:
            run.mark_partial()
    return run_id, result
