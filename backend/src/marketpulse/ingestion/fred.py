"""End-to-end FRED ingestion: catalog -> FRED API -> raw payloads -> versioned observations.

Per series, inside one tracked ingestion run (`ops.ingestion_runs`, job "fred.observations"):

1. Resolve the series in the version-controlled catalog and in ref.series (identity must match).
2. Open the run, then choose the mode: a full backfill if nothing is stored yet (or --full),
   otherwise an incremental fetch starting at the newest stored vintage date (one-day overlap,
   harmless because writes are idempotent). The stored-data read happens inside the run, so a
   database error there fails only this series and is recorded on its run.
3. Fetch FRED series metadata and run the catalog drift check. Error-level drift stops the
   series before any observations are requested (recorded as data-quality issues + failed run).
4. Fetch all real-time windows and pages (<= 2,000 vintage dates per window). Every successful
   response is saved to data/raw and recorded in ops.raw_payloads *before* it is parsed, in its
   own committed transaction, so the record of what FRED returned survives any later failure.
5. Convert to provider-neutral versions and write them in one all-or-nothing transaction under
   the series lock (Phase C writer).

Series are independent: a failure is recorded on that series' run and the job moves on.
Configuration problems (no API key, catalog not synced, unknown series code) stop the job before
any network request.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from marketpulse.core.config import Settings
from marketpulse.db.enums import RunStatus
from marketpulse.db.models import DataQualityIssue, Observation, Series, Source
from marketpulse.ingestion.raw_store import RawPayloadStore, record_raw_payload
from marketpulse.ingestion.runs import IngestionRunTracker, describe_error
from marketpulse.sources.catalog import Catalog, SeriesSpec, load_catalog
from marketpulse.sources.fred.client import DATASET_OBSERVATIONS, FredClient, ResponseHook
from marketpulse.sources.fred.drift import check_catalog_drift
from marketpulse.sources.fred.versions import FRED_TIMEZONE, to_versions, vintage_instant
from marketpulse.sources.http import FetchResult

logger = logging.getLogger(__name__)

JOB_NAME = "fred.observations"
SOURCE_CODE = "fred"
MODE_UNKNOWN = "unknown"  # the mode is chosen inside the run; a run that fails first has none

ClientFactory = Callable[[ResponseHook], FredClient]


class FredIngestionConfigError(RuntimeError):
    """The job cannot start (bad series selection, catalog not synced, missing credentials)."""


class CatalogDriftError(RuntimeError):
    """FRED's metadata no longer matches the catalog; the series is not ingested."""


@dataclass
class SeriesOutcome:
    code: str
    fred_series_id: str
    status: str  # RunStatus value
    run_id: int | None
    mode: str
    fetched: int = 0
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0
    issues: int = 0
    error: str | None = None


@dataclass
class FredIngestionSummary:
    outcomes: list[SeriesOutcome] = field(default_factory=list)

    @property
    def failed(self) -> list[SeriesOutcome]:
        return [o for o in self.outcomes if o.status == RunStatus.FAILED.value]

    @property
    def partial(self) -> list[SeriesOutcome]:
        return [o for o in self.outcomes if o.status == RunStatus.PARTIAL.value]

    @property
    def exit_code(self) -> int:
        """0 all succeeded; 1 any series failed; 2 none failed but some were partial."""
        if self.failed:
            return 1
        return 2 if self.partial else 0


class RawResponseRecorder:
    """`on_response` hook: save each raw FRED response and record it for the current run."""

    def __init__(
        self, session_factory: sessionmaker[Session], store: RawPayloadStore, source_id: int
    ) -> None:
        self._factory = session_factory
        self._store = store
        self._source_id = source_id
        self.run_id: int | None = None
        self.observation_retrieved_at: list[datetime] = []
        self.saved = 0

    def start_run(self, run_id: int) -> None:
        self.run_id = run_id
        self.observation_retrieved_at = []
        self.saved = 0

    def __call__(self, dataset: str, result: FetchResult) -> None:
        if self.run_id is None:
            raise RuntimeError("raw response received outside an ingestion run")
        stored = self._store.save(SOURCE_CODE, dataset, result)
        with self._factory() as session, session.begin():
            record_raw_payload(
                session,
                ingestion_run_id=self.run_id,
                source_id=self._source_id,
                result=result,
                stored=stored,
            )
        self.saved += 1
        if dataset == DATASET_OBSERVATIONS:
            self.observation_retrieved_at.append(result.retrieved_at)


def default_client_factory(settings: Settings) -> ClientFactory:
    def factory(hook: ResponseHook) -> FredClient:
        return FredClient.from_settings(settings, on_response=hook)

    return factory


def select_series(catalog: Catalog, codes: Sequence[str] | None) -> list[SeriesSpec]:
    available = {spec.code: spec for spec in catalog.series_for(SOURCE_CODE)}
    if not codes:
        return list(available.values())
    unknown = sorted(set(codes) - set(available))
    if unknown:
        raise FredIngestionConfigError(
            f"unknown FRED series code(s) {unknown}; available: {sorted(available)}"
        )
    return [available[code] for code in dict.fromkeys(codes)]


def _resolve_database_rows(
    session_factory: sessionmaker[Session], specs: Sequence[SeriesSpec]
) -> tuple[int, dict[str, int]]:
    with session_factory() as session:
        source = session.scalars(select(Source).where(Source.code == SOURCE_CODE)).one_or_none()
        if source is None:
            raise FredIngestionConfigError(
                "source 'fred' is not in the database; run `marketpulse catalog sync` first"
            )
        rows = {
            s.code: s
            for s in session.scalars(
                select(Series).where(Series.code.in_([spec.code for spec in specs]))
            )
        }
        problems = []
        for spec in specs:
            row = rows.get(spec.code)
            if row is None:
                problems.append(f"{spec.code}: not in ref.series")
            elif row.source_id != source.id or row.source_series_id != spec.source_series_id:
                problems.append(f"{spec.code}: database identity differs from the catalog")
            elif not row.is_active:
                problems.append(f"{spec.code}: inactive in ref.series")
        if problems:
            raise FredIngestionConfigError(
                "catalog and database are out of sync (run `marketpulse catalog sync`): "
                + "; ".join(problems)
            )
        return source.id, {code: row.id for code, row in rows.items()}


def _latest_vintage_date(session_factory: sessionmaker[Session], series_id: int) -> date | None:
    with session_factory() as session:
        latest = session.scalar(
            select(func.max(Observation.vintage_start)).where(Observation.series_id == series_id)
        )
    return latest.astimezone(FRED_TIMEZONE).date() if latest is not None else None


def _record_issues(
    session_factory: sessionmaker[Session],
    *,
    run_id: int,
    series_id: int,
    check_name: str,
    messages: Sequence[str],
    severity: str,
) -> None:
    if not messages:
        return
    with session_factory() as session, session.begin():
        session.add_all(
            DataQualityIssue(
                ingestion_run_id=run_id,
                series_id=series_id,
                check_name=check_name,
                detail=message,
                severity=severity,
            )
            for message in messages
        )


def ingest_fred(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    series_codes: Sequence[str] | None = None,
    full: bool = False,
    client_factory: ClientFactory | None = None,
    catalog: Catalog | None = None,
) -> FredIngestionSummary:
    """Ingest the selected FRED series (default: all in the catalog). Never raises per series."""
    catalog = catalog or load_catalog()
    specs = select_series(catalog, series_codes)
    source_id, series_ids = _resolve_database_rows(session_factory, specs)
    if settings.fred_api_key is None and client_factory is None:
        raise FredIngestionConfigError("FRED_API_KEY is not set (add it to .env)")

    recorder = RawResponseRecorder(
        session_factory, RawPayloadStore(settings.marketpulse_data_dir), source_id
    )
    summary = FredIngestionSummary()
    with (client_factory or default_client_factory(settings))(recorder) as client:
        for spec in specs:
            summary.outcomes.append(
                _ingest_series(
                    client,
                    recorder,
                    session_factory,
                    spec=spec,
                    source_id=source_id,
                    series_id=series_ids[spec.code],
                    full=full,
                )
            )
    return summary


def _ingest_series(
    client: FredClient,
    recorder: RawResponseRecorder,
    session_factory: sessionmaker[Session],
    *,
    spec: SeriesSpec,
    source_id: int,
    series_id: int,
    full: bool,
) -> SeriesOutcome:
    fred_id = spec.source_series_id
    tracker = IngestionRunTracker(
        session_factory,
        job=JOB_NAME,
        source_id=source_id,
        params={
            "series_id": series_id,
            "series_code": spec.code,
            "fred_series_id": fred_id,
        },
    )
    outcome = SeriesOutcome(
        code=spec.code, fred_series_id=fred_id, status="", run_id=None, mode=MODE_UNKNOWN
    )
    try:
        with tracker as run:
            run_id = run.run_id
            if run_id is None:  # pragma: no cover  (always set by __enter__)
                raise RuntimeError("ingestion run was not created")
            outcome.run_id = run_id
            recorder.start_run(run_id)

            # Inside the run: a database error here fails this series only, on its own run.
            latest = None if full else _latest_vintage_date(session_factory, series_id)
            outcome.mode = "incremental" if latest is not None else "full"
            run.params.update(
                mode=outcome.mode,
                realtime_start=latest.isoformat() if latest else None,
            )

            metadata = client.series_metadata(fred_id)
            run.params["source_last_updated"] = metadata.last_updated.isoformat()
            drift = check_catalog_drift(spec, metadata)
            _record_issues(
                session_factory,
                run_id=run_id,
                series_id=series_id,
                check_name="catalog_drift",
                messages=drift.warnings,
                severity="warning",
            )
            if not drift.ok:
                _record_issues(
                    session_factory,
                    run_id=run_id,
                    series_id=series_id,
                    check_name="catalog_drift",
                    messages=drift.errors,
                    severity="error",
                )
                outcome.issues += len(drift.errors)
                raise CatalogDriftError("; ".join(drift.errors))
            outcome.issues += len(drift.warnings)

            windows, observations = client.fetch_history(fred_id, start=latest)
            run.params["windows"] = len(windows)
            run.params["raw_payloads"] = recorder.saved
            versions = to_versions(observations, spec.frequency.value)
            retrieved_at = max(recorder.observation_retrieved_at, default=datetime.now(UTC))
            result = run.write(
                series_id=series_id,
                versions=versions,
                retrieved_at=retrieved_at,
                coverage_start=vintage_instant(latest) if latest is not None else None,
            )
            outcome.issues += len(result.issues)
    except Exception as exc:
        outcome.error = describe_error(exc)
        logger.warning("FRED ingestion failed for %s: %s", spec.code, outcome.error)
    finally:
        recorder.run_id = None

    counts = tracker.counts
    outcome.status = tracker.status.value if tracker.status else RunStatus.FAILED.value
    outcome.fetched, outcome.inserted = counts.fetched, counts.inserted
    outcome.revised, outcome.unchanged = counts.revised, counts.unchanged
    logger.info(
        "FRED %s (%s): %s, %d fetched, %d inserted, %d revised, %d unchanged",
        spec.code,
        outcome.mode,
        outcome.status,
        counts.fetched,
        counts.inserted,
        counts.revised,
        counts.unchanged,
    )
    return outcome
