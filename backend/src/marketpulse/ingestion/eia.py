"""End-to-end EIA ingestion: catalog -> EIA API -> raw payloads -> first-seen snapshots.

EIA publishes no vintages, so a series is versioned by what Market Pulse saw and when (ADR-0011):
each complete, validated response is one snapshot at `snapshot_at`. No version is ever dated
before Market Pulse first retrieved it, and no historical vintages are invented.

Per series, inside its own tracked run (`ops.ingestion_runs`, job "eia.observations"):

1. Resolve the series in the catalog and ref.series (identity must match; first_seen basis).
2. Open the run, then choose the mode (inside the run, so a database error here fails only this
   series and is recorded on its run). FULL (nothing stored yet, or --full): the whole history;
   the scope is unbounded, so every stored period is checked for disappearance. INCREMENTAL:
   W = the latest stored period minus the lookback (90 days / 26 weeks / 26 months); the request
   starts at the period immediately before W and rows before W are dropped, so the result never
   depends on whether EIA's `start` is inclusive. The scope starts at W.
3. Fetch every page. Each response is saved to data/raw and recorded in ops.raw_payloads in its
   own committed transaction *before* parsing, so it survives any later failure.
4. Map every row to the raw payload of the page that contained it (matched by redacted URL and
   SHA-256 of the body), and check that page's URL is exactly the request made for this series:
   host, path, route, series facet, frequency, sort, page length, offset, and window start, with
   no other parameters. A page without a recorded payload fails the run.
5. Only then take `snapshot_at` (one timezone-aware UTC instant for the whole snapshot) and apply
   it with the D3 snapshot writer in one all-or-nothing transaction under the series lock.

Outcomes:
- succeeded: applied;
- partial: snapshot rejected; no observation changes;
- failed: any exception before or during the write; no observation changes; raw payloads and
  the run row are kept;
- unfinalized: the run's outcome could not be recorded, so its row still says `running`.
  `observations_committed` says whether the snapshot had already been committed; committed
  changes are never reported as rolled back. Exit code 1, like failed.

Failures that describe what EIA returned for the series (DATA_ERROR_CHECKS, and HTTP 400/404 as
`eia_request_rejected`) also record error-level data-quality issues. Authentication, rate-limit,
server, transport, provenance, and database failures are operational: recorded only in the run's
error message. A retry is a new run with a new fetch and a fresh `snapshot_at`. Series are
independent: one failing does not stop the others. Configuration problems (including a missing,
empty, or blank EIA_API_KEY) stop the job before any network request or run.

Licensing (ADR-0008): a series whose effective catalog license (its `license_override`, else the
source's license) is in BLOCKED_LICENSES is never ingested through the application. Requesting
one by code raises EiaLicenseError before any network request or run; a default (all-series) job
leaves such series out and reports them in `skipped_restricted`. There is no override.

The disappearance threshold (more than 5 periods or more than 1% of periods in scope) is applied
unchanged by the planner; incremental windows only narrow the scope, never the threshold.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from marketpulse.core.config import Settings
from marketpulse.core.redaction import redact_text
from marketpulse.db.enums import RunStatus, VintageBasis
from marketpulse.db.models import DataQualityIssue, Observation, Series, Source
from marketpulse.ingestion.raw_store import RawPayloadStore, record_raw_payload
from marketpulse.ingestion.runs import IngestionRunTracker, RunFinalizationError, describe_error
from marketpulse.ingestion.snapshot_writer import SnapshotTimeError
from marketpulse.ingestion.snapshots import SnapshotObservation, SnapshotScope
from marketpulse.sources.catalog import Catalog, SeriesSpec, load_catalog
from marketpulse.sources.catalog.schema import License
from marketpulse.sources.eia import MAX_ROWS_PER_REQUEST
from marketpulse.sources.eia.client import (
    DATASET_DATA,
    EIA_BASE_URL,
    EiaAPIError,
    EiaClient,
    EiaConfigError,
    EiaFetchedPage,
    ResponseHook,
)
from marketpulse.sources.eia.observations import to_observations
from marketpulse.sources.eia.parse import EiaResponseError
from marketpulse.sources.eia.periods import PeriodError
from marketpulse.sources.eia.validate import (
    EiaDataError,
    EiaEmptyResponseError,
    EiaIncompleteError,
    EiaMetadataError,
    EiaSeriesExpectation,
    EiaWarningError,
    expectation_for,
)
from marketpulse.sources.http import FetchResult, ProviderHTTPError

logger = logging.getLogger(__name__)

JOB_NAME = "eia.observations"
SOURCE_CODE = "eia"

# Approved lookbacks for incremental snapshots (D4).
LOOKBACK_DAYS = 90  # daily
LOOKBACK_WEEKS = 26  # weekly
LOOKBACK_MONTHS = 26  # monthly

ClientFactory = Callable[[ResponseHook], EiaClient]

# Job-level outcome when the run's own outcome could not be recorded. Not a RunStatus: the run
# row itself still says "running".
UNFINALIZED = "unfinalized"
MODE_UNKNOWN = "unknown"  # the mode is chosen inside the run; a run that fails first has none

# Failures that describe what EIA returned for the series. Each also records error-level
# data-quality issues (one per problem). Looked up along the exception's MRO, so a subclass
# without its own entry falls back to its base class's check name.
DATA_ERROR_CHECKS: Mapping[type[Exception], str] = {
    EiaEmptyResponseError: "eia_empty_response",
    EiaIncompleteError: "eia_incomplete",
    EiaWarningError: "eia_warning",
    EiaMetadataError: "eia_metadata",
    EiaDataError: "eia_data_error",
    EiaResponseError: "eia_malformed_response",
    PeriodError: "eia_invalid_row",
}
# HTTP statuses for which EIA refused the request itself. The cause is not guessed: it may be a
# changed route or facet, invalid parameters, or an EIA-side problem. Other HTTP failures (403
# invalid key, 429, 5xx after retries) are operational.
REQUEST_REJECTED = "eia_request_rejected"
REQUEST_REJECTED_STATUSES = frozenset({400, 404})

_EIA_BASE = urlsplit(EIA_BASE_URL)


# Licenses under which a series is never ingested through the application (ADR-0008). Taken from
# the catalog classification, never from a list of series codes.
BLOCKED_LICENSES: frozenset[License] = frozenset({License.PERSONAL_USE_ONLY})


class EiaIngestionConfigError(RuntimeError):
    """The job cannot start (no API key, catalog not synced, unknown series code)."""


class EiaLicenseError(EiaIngestionConfigError):
    """A requested series is licensed in a way that does not permit ingestion."""


class EiaProvenanceError(RuntimeError):
    """A page cannot be tied to exactly one recorded raw payload of this series."""


# --- raw responses ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedPage:
    raw_payload_id: int
    sha256: str  # as recorded in ops.raw_payloads (of the original body)
    retrieved_at: datetime
    url: str


class EiaRawRecorder:
    """`on_response` hook: save each raw EIA response and record it for the current run.

    Separate from the FRED recorder on purpose (approved D4 decision; consolidation is backlog).
    Data pages are indexed by (redacted URL, SHA-256 of the body) for provenance mapping.
    """

    def __init__(
        self, session_factory: sessionmaker[Session], store: RawPayloadStore, source_id: int
    ) -> None:
        self._factory = session_factory
        self._store = store
        self._source_id = source_id
        self.run_id: int | None = None
        self.pages: dict[tuple[str, str], RecordedPage] = {}
        self.saved = 0

    def start_run(self, run_id: int) -> None:
        self.run_id = run_id
        self.pages = {}
        self.saved = 0

    def __call__(self, dataset: str, result: FetchResult) -> None:
        if self.run_id is None:
            raise RuntimeError("raw response received outside an ingestion run")
        stored = self._store.save(SOURCE_CODE, dataset, result)
        with self._factory() as session, session.begin():
            row = record_raw_payload(
                session,
                ingestion_run_id=self.run_id,
                source_id=self._source_id,
                result=result,
                stored=stored,
            )
            raw_payload_id = row.id
        self.saved += 1
        if dataset == DATASET_DATA:
            key = (result.url, stored.sha256)
            if key in self.pages:
                raise EiaProvenanceError(
                    f"the same data page was received twice in run {self.run_id}: {result.url}"
                )
            self.pages[key] = RecordedPage(
                raw_payload_id, stored.sha256, result.retrieved_at, result.url
            )


def map_page_provenance(
    pages: Sequence[EiaFetchedPage],
    recorded: Mapping[tuple[str, str], RecordedPage],
    expected: EiaSeriesExpectation,
    *,
    start: str | None,
    page_size: int,
) -> list[SnapshotObservation]:
    """Convert every page's rows, each tied to the raw payload of the page that contained it.

    `start` (the EIA `start` parameter; None for full history) and `page_size` are what the job
    requested: every page's URL must show exactly that request.
    """
    observations: list[SnapshotObservation] = []
    used: set[int] = set()
    for page in pages:
        entry = recorded.get((page.url, page.sha256))
        if entry is None:
            raise EiaProvenanceError(
                f"{expected.code}: page at offset {page.offset} has no recorded raw payload"
            )
        if entry.raw_payload_id in used:
            raise EiaProvenanceError(
                f"{expected.code}: raw payload {entry.raw_payload_id} matched two pages"
            )
        used.add(entry.raw_payload_id)
        if entry.retrieved_at != page.retrieved_at:
            raise EiaProvenanceError(
                f"{expected.code}: page at offset {page.offset} retrieval time differs from its "
                "recorded raw payload"
            )
        _check_page_request(page, expected, start=start, page_size=page_size)
        for o in to_observations(page.rows, expected):
            observations.append(
                SnapshotObservation(
                    source_period=o.source_period,
                    period_start=o.period_start,
                    period_end=o.period_end,
                    value=o.value,
                    status=o.status,
                    raw_payload_id=entry.raw_payload_id,
                )
            )
    return observations


def _check_page_request(
    page: EiaFetchedPage, expected: EiaSeriesExpectation, *, start: str | None, page_size: int
) -> None:
    """The page's URL must be exactly the request made for this series and window.

    Checked: scheme and host, the exact path (EIA base path + route + "/data"), and every query
    parameter, each present exactly once with the expected value. `start` must be present exactly
    when the job asked for one; `end` and any other parameter are refused. The (redacted)
    `api_key` parameter is allowed, and its value is never inspected or reported.
    """
    parts = urlsplit(page.url)
    params = parse_qs(parts.query, keep_blank_values=True)
    want = {
        "frequency": expected.frequency,
        "data[0]": "value",
        "facets[series][0]": expected.facet_series,
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "length": str(page_size),
        "offset": str(page.offset),
    }
    if start is not None:
        want["start"] = start

    problems = []
    if (parts.scheme, parts.netloc) != (_EIA_BASE.scheme, _EIA_BASE.netloc):
        problems.append(f"host {parts.scheme}://{parts.netloc}")
    path = f"{_EIA_BASE.path.rstrip('/')}/{expected.route}/data"
    if parts.path != path:
        problems.append(f"path {parts.path!r}, expected {path!r}")
    for name, value in want.items():
        got = params.get(name)
        if got != [value]:
            problems.append(f"{name} {got!r}, expected {value!r}")
    unexpected = sorted(set(params) - set(want) - {"api_key"})
    if unexpected:
        problems.append(f"unexpected parameters {unexpected}")  # names only, never values
    if len(params.get("api_key", [])) > 1:
        problems.append("api_key repeated")
    if problems:
        raise EiaProvenanceError(
            f"{expected.code}: page at offset {page.offset} was not requested for this series "
            f"({'; '.join(problems)})"
        )


# --- modes and windows --------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotWindow:
    mode: str  # "full" or "incremental"
    fetch_start: str | None  # EIA `start` parameter
    scope: SnapshotScope
    window_start: str | None  # W: incremental scope start; in full mode, for revision reports


def _month_shift(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def window_start(latest_period: str, frequency: str) -> tuple[str, str]:
    """(W, the period immediately before W) for an incremental snapshot, in EIA's format."""
    if frequency in ("daily", "weekly"):
        latest = date.fromisoformat(latest_period)
        if frequency == "daily":
            w, step = latest - timedelta(days=LOOKBACK_DAYS), timedelta(days=1)
        else:
            w, step = latest - timedelta(weeks=LOOKBACK_WEEKS), timedelta(weeks=1)
        return w.isoformat(), (w - step).isoformat()
    if frequency == "monthly":
        year, month = int(latest_period[:4]), int(latest_period[5:7])
        wy, wm = _month_shift(year, month, -LOOKBACK_MONTHS)
        py, pm = _month_shift(wy, wm, -1)
        return f"{wy:04d}-{wm:02d}", f"{py:04d}-{pm:02d}"
    raise ValueError(f"no lookback defined for frequency {frequency!r}")


def choose_window(latest_period: str | None, frequency: str, *, full: bool) -> SnapshotWindow:
    if latest_period is None:
        return SnapshotWindow("full", None, SnapshotScope(), None)
    w, before = window_start(latest_period, frequency)
    if full:
        return SnapshotWindow("full", None, SnapshotScope(), w)
    return SnapshotWindow("incremental", before, SnapshotScope(start=w), w)


def drop_before(
    observations: Sequence[SnapshotObservation], w: str
) -> tuple[list[SnapshotObservation], int]:
    """Remove observations before W (the boundary period, fetched only for robustness)."""
    kept = [o for o in observations if o.source_period >= w]
    return kept, len(observations) - len(kept)


# --- job ----------------------------------------------------------------------------------------


@dataclass
class SeriesOutcome:
    code: str
    eia_series_id: str
    status: str  # RunStatus value, or UNFINALIZED
    run_id: int | None
    mode: str
    fetched: int = 0
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0
    # Stored periods missing from the snapshot, whether applied or rejected. None = not
    # determined (no snapshot was planned, or it was rejected before periods were compared).
    vanished: int | None = None
    rejected: bool = False
    issues: int = 0
    error: str | None = None  # the run's own failure, if any (redacted)
    observations_committed: bool = False  # the snapshot's observation changes were committed
    finalization_error: str | None = None  # why the run's outcome could not be recorded


@dataclass
class EiaIngestionSummary:
    outcomes: list[SeriesOutcome] = field(default_factory=list)
    # Restricted series left out of a default (all-series) job: code -> effective license.
    skipped_restricted: dict[str, License] = field(default_factory=dict)

    @property
    def failed(self) -> list[SeriesOutcome]:
        return [o for o in self.outcomes if o.status == RunStatus.FAILED.value]

    @property
    def partial(self) -> list[SeriesOutcome]:
        return [o for o in self.outcomes if o.status == RunStatus.PARTIAL.value]

    @property
    def unfinalized(self) -> list[SeriesOutcome]:
        return [o for o in self.outcomes if o.status == UNFINALIZED]

    @property
    def exit_code(self) -> int:
        """0 all succeeded; 1 any series failed or unfinalized; otherwise 2 if any partial."""
        if self.failed or self.unfinalized:
            return 1
        return 2 if self.partial else 0


def default_client_factory(settings: Settings) -> ClientFactory:
    def factory(hook: ResponseHook) -> EiaClient:
        return EiaClient.from_settings(settings, on_response=hook)

    return factory


def effective_license(catalog: Catalog, spec: SeriesSpec) -> License:
    """The series' license override, else its source's license (the catalog classification)."""
    return spec.license_override or catalog.sources[SOURCE_CODE].license


@dataclass(frozen=True)
class SeriesSelection:
    specs: list[SeriesSpec]  # series to ingest, all permitted
    licenses: dict[str, License]  # code -> effective license, for every selected series
    skipped_restricted: dict[str, License]  # blocked series left out of a default selection


def _blocked_message(blocked: Sequence[tuple[SeriesSpec, License]]) -> str:
    listed = "; ".join(
        f"{spec.code} ({license_.value}; original source: {spec.original_source})"
        for spec, license_ in blocked
    )
    return (
        f"restricted EIA series cannot be ingested: {listed}. Their catalog license does not "
        "permit ingestion through Market Pulse; ingesting them requires appropriate licensing "
        "from the original source (see docs/data-sources.md and ADR-0008). Remove them from "
        "the requested series; the other EIA series are unaffected."
    )


def select_series(catalog: Catalog, codes: Sequence[str] | None) -> SeriesSelection:
    """The series to ingest. Restricted series are refused (by code) or left out (default)."""
    available = {spec.code: spec for spec in catalog.series_for(SOURCE_CODE)}
    if codes:
        unknown = sorted(set(codes) - set(available))
        if unknown:
            raise EiaIngestionConfigError(
                f"unknown EIA series code(s) {unknown}; available: {sorted(available)}"
            )
        requested = [available[code] for code in dict.fromkeys(codes)]
    else:
        requested = list(available.values())
    licenses = {spec.code: effective_license(catalog, spec) for spec in requested}
    blocked = [
        (spec, licenses[spec.code]) for spec in requested if licenses[spec.code] in BLOCKED_LICENSES
    ]
    if blocked and codes:
        raise EiaLicenseError(_blocked_message(blocked))
    skipped = {spec.code: license_ for spec, license_ in blocked}
    specs = [spec for spec in requested if spec.code not in skipped]
    return SeriesSelection(specs, {s.code: licenses[s.code] for s in specs}, skipped)


def _require_ingestion_permitted(spec: SeriesSpec, license_: License) -> None:
    """Per-series guard, checked again before a run is opened (defense in depth)."""
    if license_ in BLOCKED_LICENSES:
        raise EiaLicenseError(_blocked_message([(spec, license_)]))


def _resolve_database_rows(
    session_factory: sessionmaker[Session], specs: Sequence[SeriesSpec]
) -> tuple[int, dict[str, int]]:
    with session_factory() as session:
        source = session.scalars(select(Source).where(Source.code == SOURCE_CODE)).one_or_none()
        if source is None:
            raise EiaIngestionConfigError(
                "source 'eia' is not in the database; run `marketpulse catalog sync` first"
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
            elif row.vintage_basis != VintageBasis.FIRST_SEEN.value:
                problems.append(f"{spec.code}: vintage_basis is {row.vintage_basis!r}")
        if problems:
            raise EiaIngestionConfigError(
                "catalog and database are out of sync (run `marketpulse catalog sync`): "
                + "; ".join(problems)
            )
        return source.id, {code: row.id for code, row in rows.items()}


def _latest_stored_period(session_factory: sessionmaker[Session], series_id: int) -> str | None:
    with session_factory() as session:
        return session.scalar(
            select(func.max(Observation.source_period)).where(
                Observation.series_id == series_id, Observation.vintage_end.is_(None)
            )
        )


def failure_diagnostics(exc: BaseException) -> tuple[str, list[str]] | None:
    """(check name, problems) when `exc` describes what EIA returned; None when operational."""
    if isinstance(exc, (EiaAPIError, ProviderHTTPError)):
        if exc.status_code not in REQUEST_REJECTED_STATUSES:
            return None
        message = exc.error_message if isinstance(exc, EiaAPIError) else "no EIA error message"
        return REQUEST_REJECTED, [
            f"EIA rejected the request with HTTP {exc.status_code} ({message}) at {exc.url}"
        ]
    for kind in type(exc).__mro__:
        check = DATA_ERROR_CHECKS.get(kind)
        if check is not None:
            if isinstance(exc, (EiaDataError, EiaResponseError)) and exc.problems:
                return check, list(exc.problems)
            return check, [str(exc)]
    return None


def _record_failure_issues(
    session_factory: sessionmaker[Session], *, run_id: int, series_id: int, exc: BaseException
) -> int:
    """Record error-level issues for a data failure (none for an operational failure).

    Never raises: a failure here is logged and must not hide the failure being diagnosed.
    """
    diagnosis = failure_diagnostics(exc)
    if diagnosis is None:
        return 0
    check, problems = diagnosis
    try:
        with session_factory() as session, session.begin():
            session.add_all(
                DataQualityIssue(
                    ingestion_run_id=run_id,
                    series_id=series_id,
                    check_name=check,
                    detail=redact_text(problem),
                    severity="error",
                )
                for problem in problems
            )
    except Exception as rec_exc:
        logger.error(
            "run %s: could not record %s issues: %s", run_id, check, describe_error(rec_exc)
        )
        return 0
    return len(problems)


def ingest_eia(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    series_codes: Sequence[str] | None = None,
    full: bool = False,
    client_factory: ClientFactory | None = None,
    catalog: Catalog | None = None,
    page_size: int = MAX_ROWS_PER_REQUEST,
) -> EiaIngestionSummary:
    """Ingest the selected EIA series (default: all permitted ones). Never raises per series.

    A series whose license is in BLOCKED_LICENSES raises EiaLicenseError when requested by code,
    and is left out of a default job (listed in `skipped_restricted`), before any request or run.
    """
    key = settings.eia_api_key
    if client_factory is None and (key is None or not key.get_secret_value().strip()):
        raise EiaIngestionConfigError("EIA_API_KEY is not set or is blank (set it in .env)")
    catalog = catalog or load_catalog()
    selection = select_series(catalog, series_codes)
    specs = selection.specs
    if selection.skipped_restricted:
        logger.info(
            "restricted EIA series left out (license does not permit ingestion): %s",
            ", ".join(selection.skipped_restricted),
        )
    try:
        expectations = {spec.code: expectation_for(spec) for spec in specs}
    except ValueError as exc:
        raise EiaIngestionConfigError(str(exc)) from None
    source_id, series_ids = _resolve_database_rows(session_factory, specs)

    recorder = EiaRawRecorder(
        session_factory, RawPayloadStore(settings.marketpulse_data_dir), source_id
    )
    summary = EiaIngestionSummary(skipped_restricted=dict(selection.skipped_restricted))
    try:
        client = (client_factory or default_client_factory(settings))(recorder)
    except EiaConfigError as exc:  # its messages never contain the key
        raise EiaIngestionConfigError(f"EIA client configuration: {exc}") from None
    with client:
        for spec in specs:
            summary.outcomes.append(
                _ingest_series(
                    client,
                    recorder,
                    session_factory,
                    spec=spec,
                    license_=selection.licenses[spec.code],
                    expected=expectations[spec.code],
                    source_id=source_id,
                    series_id=series_ids[spec.code],
                    full=full,
                    page_size=page_size,
                )
            )
    return summary


def _ingest_series(
    client: EiaClient,
    recorder: EiaRawRecorder,
    session_factory: sessionmaker[Session],
    *,
    spec: SeriesSpec,
    license_: License,
    expected: EiaSeriesExpectation,
    source_id: int,
    series_id: int,
    full: bool,
    page_size: int,
) -> SeriesOutcome:
    _require_ingestion_permitted(spec, license_)  # before the run row or any request exists
    tracker = IngestionRunTracker(
        session_factory,
        job=JOB_NAME,
        source_id=source_id,
        params={
            "series_id": series_id,
            "series_code": spec.code,
            "eia_series_id": spec.source_series_id,
            "route": expected.route,
            "facet_series": expected.facet_series,
            "full_requested": full,
        },
    )
    outcome = SeriesOutcome(
        code=spec.code,
        eia_series_id=spec.source_series_id,
        status="",
        run_id=None,
        mode=MODE_UNKNOWN,
    )
    try:
        with tracker as run:
            run_id = run.run_id
            if run_id is None:  # pragma: no cover  (always set by __enter__)
                raise RuntimeError("ingestion run was not created")
            outcome.run_id = run_id
            recorder.start_run(run_id)

            # Inside the run: a database error here fails this series only, on its own run.
            window = choose_window(
                _latest_stored_period(session_factory, series_id), expected.frequency, full=full
            )
            outcome.mode = window.mode
            run.params.update(
                mode=window.mode,
                fetch_start=window.fetch_start,
                scope_start=window.scope.start,
                window_start=window.window_start,
            )

            try:
                try:
                    pages = client.fetch_series_pages(
                        expected, start=window.fetch_start, page_size=page_size
                    )
                finally:
                    run.params["raw_payloads"] = recorder.saved
                run.params["pages"] = len(pages)
                run.params["page_offsets"] = [page.offset for page in pages]
                observations = map_page_provenance(
                    pages,
                    recorder.pages,
                    expected,
                    start=window.fetch_start,
                    page_size=page_size,
                )
            except Exception as exc:
                outcome.issues += _record_failure_issues(
                    session_factory, run_id=run_id, series_id=series_id, exc=exc
                )
                raise
            if window.mode == "incremental" and window.window_start is not None:
                observations, dropped = drop_before(observations, window.window_start)
                run.params["dropped_before_window"] = dropped

            # One instant for the whole snapshot, taken only now: every page has been
            # retrieved, validated, and mapped to its raw payload.
            snapshot_at = datetime.now(UTC)
            latest_page = max((p.retrieved_at for p in pages), default=None)
            if latest_page is not None and snapshot_at < latest_page:
                raise SnapshotTimeError(
                    f"clock went backwards: snapshot_at {snapshot_at.isoformat()} is before the "
                    f"last page's retrieval at {latest_page.isoformat()}"
                )
            run.params["snapshot_at"] = snapshot_at.isoformat()

            result = run.write_snapshot(
                series_id=series_id,
                snapshot_at=snapshot_at,
                scope=window.scope,
                period_format=expected.date_format,
                incoming=observations,
                incremental_window_start=window.window_start,
            )
            run.params["rejected"] = result.rejected
            run.params["vanished"] = result.vanished_detected
            outcome.rejected = result.rejected
            outcome.vanished = result.vanished_detected
            outcome.issues += len(result.issues) + result.vanished_opened + result.vanished_updated
    except RunFinalizationError:
        pass  # the body succeeded; reported from the tracker below
    except Exception as exc:
        outcome.error = describe_error(exc)
        logger.warning("EIA ingestion failed for %s: %s", spec.code, outcome.error)
    finally:
        recorder.run_id = None

    outcome.observations_committed = tracker.data_committed
    outcome.finalization_error = tracker.finalization_error
    if tracker.finalized and tracker.status is not None:
        outcome.status = tracker.status.value
    elif tracker.finalization_error is not None:
        outcome.status = UNFINALIZED
    else:  # the run row could not even be created
        outcome.status = RunStatus.FAILED.value

    counts = tracker.counts
    outcome.fetched, outcome.inserted = counts.fetched, counts.inserted
    outcome.revised, outcome.unchanged = counts.revised, counts.unchanged
    logger.info(
        "EIA %s (%s): %s, %d fetched, %d inserted, %d revised, %d unchanged%s%s",
        spec.code,
        outcome.mode,
        outcome.status,
        counts.fetched,
        counts.inserted,
        counts.revised,
        counts.unchanged,
        " (snapshot rejected)" if outcome.rejected else "",
        (
            f" (observations committed: {outcome.observations_committed})"
            if outcome.status == UNFINALIZED
            else ""
        ),
    )
    return outcome
