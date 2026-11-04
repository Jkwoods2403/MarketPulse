"""Replay stored raw pages and compare them with the versions they produced (read-only).

Used by `marketpulse verify` (ingestion.verify). Raw payloads are read through RawPayloadStore,
which verifies each file against its recorded SHA-256; nothing is written, and database reads
happen in the caller's read-only transaction.

Every replayed version is counted in exactly one of four states, by this precedence (the first
that applies wins):
1. error: a supported comparison found a mismatch, or the raw file is corrupt or tampered with;
2. unverified: evidence the comparison needs is missing or incomplete (the raw file is missing,
   the run that created it has no stored pages, or the version cites no page);
3. unsupported: everything replay can check matched (at least its creation), but a later step
   in its stored history was not replayed: a FRED closure or confirmation (no record says which
   run did it) or an EIA confirmation (replaying confirmation snapshots is not implemented);
4. verified: the version, its provenance, and its whole stored history (creation and any
   closure) were reconstructed from stored raw data and matched.
Unverified and unsupported are never reported as passed or failed, and are kept apart: one
means evidence is missing, the other that the verification method does not cover that step.

EIA (first_seen) creation: every version cites the exact raw page that contained it
(raw_payload_id). The page is re-parsed and its row for the version's source_period must match
the stored version: series facet, period bounds, value, and status. That page IS the creating
run's output for the version, so this compares against what the run saw, not current data.

EIA closure: the snapshot writer closes a version only when a snapshot reports a different
value or status for its period, and in the same transaction inserts the successor with
vintage_start = that snapshot's time = the closed version's vintage_end. Snapshot times strictly
increase per series, and (series, period, vintage_start) is unique, so the closing run is
exactly the run that created the version starting at the closed version's end, and that
successor's cited page is the closing snapshot's page. A closed version is checked against it:
the successor must exist (otherwise the closing run cannot be identified: an error), and the
closing page's row for the period must report a different value or status than the closed
version (otherwise the closure is not justified: an error). A missing closing page makes the
version UNVERIFIED, like a missing creation page.

EIA confirmation: a later snapshot that reports the value unchanged confirms the version (it
moves last_confirmed_at and records last_confirmed_run_id). Replaying that snapshot's pages is
not implemented, so a version confirmed by a later run is UNSUPPORTED, not verified, even though
its creation (and any closure) matched. Only the latest confirming run is recorded.

FRED (source_vintage): versions do not cite pages, but each records the run that created it
(ingestion_run_id), and each run stores its observation pages in its own ops.raw_payloads rows.
A run's output is reconstructed exactly as ingestion built it (parse every observation page,
rejoin window-edge splits, convert to versions), and every version that run created must appear
in that output, matched by (period, vintage start), with the same period end, value, and status.
Its first_retrieved_at must equal the latest retrieval time of the run's observation pages (the
time ingestion stamps on the versions a run inserts).

UNSUPPORTED (FRED): a later run can close a version (set vintage_end after a revision) or
re-confirm it (move last_confirmed_at), and for FRED neither records WHICH run did so; only the
time is stored. A version is therefore unsupported, not verified, when
- its vintage_end is set but its creating run's output had it current (closed later), or
- its last_confirmed_at differs from its first_retrieved_at (confirmed later).
Its creation still matched; only the later step cannot be attributed. When the creating run's
own output had an end, that end is compared like any other field.

Not covered: replay shows that stored history matches what Market Pulse received, not that the
provider's numbers are correct.
"""

import gzip
import zlib
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased

from marketpulse.core.versions import STATUS_MISSING_AT_SOURCE, STATUS_OK
from marketpulse.db.models import Observation, RawPayload, Series, Source
from marketpulse.ingestion.raw_store import (
    RawPayloadIntegrityError,
    RawPayloadStore,
    StoredPayload,
)
from marketpulse.sources.catalog.schema import eia_facet_series
from marketpulse.sources.eia.parse import EiaResponseError, EiaRow, parse_data_page
from marketpulse.sources.eia.periods import PeriodError as EiaPeriodError
from marketpulse.sources.eia.periods import period_bounds as eia_period_bounds
from marketpulse.sources.fred.merge import MergeConflictError, merge_window_splits
from marketpulse.sources.fred.parse import (
    FredObservation,
    FredResponseError,
    parse_observations,
)
from marketpulse.sources.fred.periods import PeriodError as FredPeriodError
from marketpulse.sources.fred.versions import to_versions

MAX_MESSAGES = 5
FRED_OBSERVATION_PAGES = "raw/fred/observations/%"  # RawPayloadStore layout: raw/<src>/<dataset>/


@dataclass
class ReplayFindings:
    """Per-version outcome counts, with a few example messages for each finding kind.

    Counts are versions; `messages` is the number of findings reported (one finding can cover
    several versions, e.g. every version of a run whose page is missing).
    """

    verified_count: int = 0
    error_count: int = 0
    unverified_count: int = 0
    unsupported_count: int = 0
    messages: int = 0
    errors: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    def verified(self, versions: int = 1) -> None:
        self.verified_count += versions

    def error(self, message: str, versions: int = 1) -> None:
        self.error_count += versions
        self._note(self.errors, message)

    def cannot_verify(self, message: str, versions: int = 1) -> None:
        self.unverified_count += versions
        self._note(self.unverified, message)

    def not_supported(self, message: str, versions: int = 1) -> None:
        self.unsupported_count += versions
        self._note(self.unsupported, message)

    def _note(self, kept: list[str], message: str) -> None:
        self.messages += 1
        if len(kept) < MAX_MESSAGES:
            kept.append(message)


class _UnreadablePageError(Exception):
    """A raw page cannot be used: `missing` (unverifiable) or corrupt/tampered (error)."""

    def __init__(self, message: str, *, missing: bool) -> None:
        super().__init__(message)
        self.missing = missing


def _load(store: RawPayloadStore, payload: Any) -> bytes:
    stored = StoredPayload(payload.file_path, payload.sha256, payload.size_bytes)
    try:
        return store.load(stored)
    except FileNotFoundError:
        raise _UnreadablePageError(
            f"raw file for payload {payload.id} is missing", missing=True
        ) from None
    except RawPayloadIntegrityError:
        raise _UnreadablePageError(
            f"raw file for payload {payload.id} does not match its recorded SHA-256",
            missing=False,
        ) from None
    except (OSError, EOFError, zlib.error, gzip.BadGzipFile, ValueError) as exc:
        raise _UnreadablePageError(
            f"raw file for payload {payload.id} is unreadable ({type(exc).__name__})",
            missing=False,
        ) from None


def _status(value: Decimal | None) -> str:
    return STATUS_MISSING_AT_SOURCE if value is None else STATUS_OK


# --- EIA ----------------------------------------------------------------------------------------


ERROR = "error"
UNVERIFIED = "unverified"
_Problem = tuple[str, str] | None  # (ERROR or UNVERIFIED, message), or None when it matched


def replay_eia(
    session: Session, store: RawPayloadStore, series_ids: Sequence[int] | None
) -> ReplayFindings:
    findings = ReplayFindings()
    successor = aliased(Observation)  # the version that starts where this one ends
    query = (
        select(
            Observation.id,
            Observation.period_start,
            Observation.period_end,
            Observation.vintage_end,
            Observation.value,
            Observation.status,
            Observation.source_period,
            Observation.raw_payload_id,
            Observation.ingestion_run_id,
            Observation.first_retrieved_at,
            Observation.last_confirmed_at,
            Observation.last_confirmed_run_id,
            successor.id.label("successor_id"),
            successor.ingestion_run_id.label("closing_run_id"),
            successor.raw_payload_id.label("closing_payload_id"),
            Series.code,
            Series.frequency,
            Series.period_convention,
            Series.source_series_id,
        )
        .join(Series, Series.id == Observation.series_id)
        .join(Source, Source.id == Series.source_id)
        .outerjoin(
            successor,
            and_(
                successor.series_id == Observation.series_id,
                successor.period_start == Observation.period_start,
                successor.vintage_start == Observation.vintage_end,
            ),
        )
        .where(Source.code == "eia")
        .order_by(Observation.raw_payload_id, Observation.id)
    )
    if series_ids is not None:
        query = query.where(Observation.series_id.in_(series_ids))
    versions = session.execute(query).all()
    if not versions:
        return findings

    pages = _EiaPages(
        session,
        store,
        {v.raw_payload_id for v in versions} | {v.closing_payload_id for v in versions},
    )
    grouped: Counter[tuple[str, str]] = Counter()  # (state, message) -> versions
    unsupported: dict[tuple[int, str], set[int | None]] = defaultdict(set)
    unsupported_count: Counter[tuple[int, str]] = Counter()
    for version in versions:
        problems = [_eia_creation(version, pages)]
        if version.vintage_end is not None:
            problems.append(_eia_closure(version, pages))
        found = [problem for problem in problems if problem is not None]
        errors = [message for state, message in found if state == ERROR]
        if errors:
            grouped[(ERROR, errors[0])] += 1
        elif found:
            grouped[(UNVERIFIED, found[0][1])] += 1
        elif _eia_confirmed_later(version):
            key = (version.raw_payload_id, version.code)
            unsupported[key].add(version.last_confirmed_run_id)
            unsupported_count[key] += 1
        else:
            findings.verified()

    for (state, message), n in grouped.items():
        text = message if n == 1 else f"{message} ({n} versions)"
        if state == ERROR:
            findings.error(text, n)
        else:
            findings.cannot_verify(text, n)
    for (payload_id, code), runs in unsupported.items():
        n = unsupported_count[(payload_id, code)]
        listed = ", ".join(str(r) for r in sorted(r for r in runs if r is not None))
        findings.not_supported(
            f"raw payload {payload_id} ({code}): {n} version(s) match the page they cite "
            f"(creation and any closure verified), but were re-confirmed by later run(s) "
            f"{listed or 'unknown'}; replaying confirmation snapshots is not implemented, so "
            "their confirmation history is not verified",
            n,
        )
    return findings


class _EiaPages:
    """Raw EIA pages by payload id, each loaded and parsed at most once (read-only)."""

    def __init__(self, session: Session, store: RawPayloadStore, ids: set[int | None]) -> None:
        wanted = [i for i in ids if i is not None]
        self._store = store
        self._payloads = {
            p.id: p
            for p in session.execute(
                select(
                    RawPayload.id, RawPayload.file_path, RawPayload.sha256, RawPayload.size_bytes
                ).where(RawPayload.id.in_(wanted))
            )
        }
        self._parsed: dict[int, dict[str, list[EiaRow]] | tuple[str, str]] = {}

    def rows(self, payload_id: int) -> dict[str, list[EiaRow]] | tuple[str, str]:
        """The page's rows by source period, or (ERROR or UNVERIFIED, why it is unusable)."""
        if payload_id not in self._parsed:
            self._parsed[payload_id] = self._parse(payload_id)
        return self._parsed[payload_id]

    def _parse(self, payload_id: int) -> dict[str, list[EiaRow]] | tuple[str, str]:
        payload = self._payloads[payload_id]  # the foreign key guarantees it exists
        try:
            rows = parse_data_page(_load(self._store, payload)).rows
        except _UnreadablePageError as exc:
            return (UNVERIFIED if exc.missing else ERROR, str(exc))
        except (EiaResponseError, ValueError) as exc:
            return (ERROR, f"raw payload {payload_id} no longer parses ({type(exc).__name__})")
        by_period: dict[str, list[EiaRow]] = defaultdict(list)
        for row in rows:
            by_period[row.period].append(row)
        return by_period


def _eia_creation(version: Any, pages: _EiaPages) -> _Problem:
    """Compare the version with the row of the page it cites (what its creating run saw)."""
    if version.raw_payload_id is None or not (version.source_period or "").strip():
        return (
            UNVERIFIED,
            f"{version.code} {version.period_start}: version {version.id} cites no raw page or "
            "source period (see missing_provenance)",
        )
    rows = pages.rows(version.raw_payload_id)
    if isinstance(rows, tuple):
        state, why = rows
        return (state, f"{why} (cited by {version.code} versions)")
    return _compare_eia(version, rows.get(version.source_period, []), version.raw_payload_id)


def _eia_closure(version: Any, pages: _EiaPages) -> _Problem:
    """Check a closed version against the closing snapshot's page (its successor's page)."""
    where = (
        f"{version.code} {version.source_period}: version {version.id} closed at "
        f"{version.vintage_end.isoformat()}"
    )
    if version.successor_id is None:
        return (
            ERROR,
            f"{where}, but no version of the period starts then, so the closing run cannot be "
            "identified",
        )
    run, page = version.closing_run_id, version.closing_payload_id
    if page is None:
        return (
            UNVERIFIED,
            f"{where} by run {run}, whose version {version.successor_id} cites no raw page",
        )
    rows = pages.rows(page)
    if isinstance(rows, tuple):
        state, why = rows
        return (state, f"{why} (closing page of {version.code} run {run})")
    matching = rows.get(version.source_period, [])
    if len(matching) != 1:
        return (
            ERROR,
            f"{where} by run {run}, but its page {page} has {len(matching)} rows for the period, "
            "expected 1",
        )
    (row,) = matching
    if row.value == version.value and _status(row.value) == version.status:
        return (
            ERROR,
            f"{where} by run {run}, but its page {page} reports the same value {row.value}; "
            "the closure is not justified",
        )
    return None


def _eia_confirmed_later(version: Any) -> bool:
    """Whether a run other than the creating one confirmed the version after its creation."""
    by_other_run = (
        version.last_confirmed_run_id is not None
        and version.last_confirmed_run_id != version.ingestion_run_id
    )
    return bool(by_other_run or version.last_confirmed_at != version.first_retrieved_at)


def _compare_eia(version: Any, rows: list[EiaRow], page: int) -> _Problem:
    """None when `version` matches its page row, else the mismatch as an error."""
    where = f"{version.code} {version.source_period}: version {version.id}, raw payload {page}"
    if len(rows) != 1:
        return (ERROR, f"{where}: the page has {len(rows)} rows for this period, expected 1")
    (row,) = rows
    problems = []
    facet = eia_facet_series(version.source_series_id)
    if row.series != facet:
        problems.append(f"series {row.series!r}, expected {facet!r}")
    try:
        start, end = eia_period_bounds(row.period, version.frequency, version.period_convention)
    except EiaPeriodError as exc:
        problems.append(f"period does not parse ({exc})")
    else:
        if (start, end) != (version.period_start, version.period_end):
            problems.append(
                f"period {start}..{end}, stored {version.period_start}..{version.period_end}"
            )
    if row.value != version.value or _status(row.value) != version.status:
        problems.append(f"page value {row.value}, stored {version.value} ({version.status})")
    return (ERROR, f"{where}: {'; '.join(problems)}") if problems else None


# --- FRED ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _RunKey:
    run_id: int
    code: str
    frequency: str


def replay_fred(
    session: Session, store: RawPayloadStore, series_ids: Sequence[int] | None
) -> ReplayFindings:
    findings = ReplayFindings()
    query = (
        select(
            Observation.id,
            Observation.ingestion_run_id,
            Observation.period_start,
            Observation.period_end,
            Observation.vintage_start,
            Observation.vintage_end,
            Observation.value,
            Observation.status,
            Observation.first_retrieved_at,
            Observation.last_confirmed_at,
            Series.code,
            Series.frequency,
        )
        .join(Series, Series.id == Observation.series_id)
        .join(Source, Source.id == Series.source_id)
        .where(Source.code == "fred")
        .order_by(Observation.ingestion_run_id, Observation.id)
    )
    if series_ids is not None:
        query = query.where(Observation.series_id.in_(series_ids))

    by_run: dict[_RunKey, list[Any]] = defaultdict(list)
    for version in session.execute(query):
        by_run[_RunKey(version.ingestion_run_id, version.code, version.frequency)].append(version)

    for key, versions in by_run.items():
        run_output = _fred_run_output(session, store, key, len(versions), findings)
        if run_output is None:
            continue
        output, retrieved_at = run_output
        closed_later = confirmed_later = unsupported = 0
        for version in versions:
            built = _compare_fred(findings, key, version, output, retrieved_at)
            if built is None:
                continue  # an error, already reported
            closed = version.vintage_end is not None and built.vintage_end is None
            confirmed = version.last_confirmed_at != version.first_retrieved_at
            if closed or confirmed:
                unsupported += 1
                closed_later += closed
                confirmed_later += confirmed
            else:
                findings.verified()
        if unsupported:
            findings.not_supported(
                f"run {key.run_id} ({key.code}): {unsupported} version(s) match what the run "
                f"created (creation verified), but later runs closed {closed_later} and "
                f"re-confirmed {confirmed_later} of them; FRED does not record which run, so "
                "those steps cannot be replayed",
                unsupported,
            )
    return findings


def _fred_run_output(
    session: Session,
    store: RawPayloadStore,
    key: _RunKey,
    created: int,
    findings: ReplayFindings,
) -> tuple[dict[tuple[date, datetime], Any], datetime] | None:
    """The versions run `key.run_id` built from its stored pages, keyed by (period, start), and
    the latest retrieval time of those pages (the run's stamp on the versions it inserted)."""
    what = f"run {key.run_id} ({created} version(s) of {key.code})"
    pages = session.execute(
        select(
            RawPayload.id,
            RawPayload.file_path,
            RawPayload.sha256,
            RawPayload.size_bytes,
            RawPayload.retrieved_at,
        )
        .where(
            RawPayload.ingestion_run_id == key.run_id,
            RawPayload.file_path.like(FRED_OBSERVATION_PAGES),
        )
        .order_by(RawPayload.id)
    ).all()
    if not pages:
        findings.cannot_verify(f"{what}: the run has no stored observation pages", created)
        return None
    rows: list[FredObservation] = []
    for page in pages:
        try:
            rows.extend(parse_observations(_load(store, page)).observations)
        except _UnreadablePageError as exc:
            if exc.missing:
                findings.cannot_verify(f"{what}: {exc}", created)
            else:
                findings.error(f"{what}: {exc}", created)
            return None
        except (FredResponseError, ValueError) as exc:
            findings.error(
                f"{what}: raw payload {page.id} no longer parses ({type(exc).__name__})", created
            )
            return None
    try:
        built = to_versions(merge_window_splits(rows), key.frequency)
    except (MergeConflictError, FredPeriodError, ValueError) as exc:
        findings.error(f"{what}: its pages no longer assemble ({type(exc).__name__})", created)
        return None
    keys = Counter((v.period_start, v.vintage_start) for v in built)
    if any(n > 1 for n in keys.values()):
        findings.error(f"{what}: its pages yield two versions with one (period, start)", created)
        return None
    return (
        {(v.period_start, v.vintage_start): v for v in built},
        max(page.retrieved_at for page in pages),
    )


def _compare_fred(
    findings: ReplayFindings,
    key: _RunKey,
    version: Any,
    output: dict[tuple[date, datetime], Any],
    retrieved_at: datetime,
) -> Any | None:
    """The run's own version matching `version`, or None after reporting a mismatch."""
    where = f"{key.code} {version.period_start}: version {version.id} of run {key.run_id}"
    built = output.get((version.period_start, version.vintage_start))
    if built is None:
        findings.error(
            f"{where}: not in the run's reconstructed output "
            f"(start {version.vintage_start.isoformat()})"
        )
        return None
    problems = []
    if built.period_end != version.period_end:
        problems.append(f"period end {built.period_end}, stored {version.period_end}")
    if built.value != version.value or built.status != version.status:
        problems.append(
            f"value {built.value} ({built.status}), stored {version.value} ({version.status})"
        )
    # A later run may close a version that was current when created; only an end the creating
    # run itself reported can be compared.
    if built.vintage_end is not None and built.vintage_end != version.vintage_end:
        problems.append(f"end {built.vintage_end.isoformat()}, stored {version.vintage_end}")
    if version.first_retrieved_at != retrieved_at:
        problems.append(
            f"first retrieved {version.first_retrieved_at.isoformat()}, but the run's pages "
            f"were last retrieved {retrieved_at.isoformat()}"
        )
    if problems:
        findings.error(f"{where}: {'; '.join(problems)}")
        return None
    return built
