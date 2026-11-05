"""Read-only integrity verification of stored observations and their lineage (`marketpulse verify`).

Every check runs inside ONE transaction that is REPEATABLE READ (one consistent snapshot of the
database) and READ ONLY (PostgreSQL refuses any write), and that transaction is rolled back at
the end. Verification never changes the database or any file.

Checks (each finding is an error or a warning; errors mean the stored history breaks an
invariant the writers guarantee, warnings mean something an operator should look at):

- version_overlap: two versions of one period overlap in time (always an error).
- version_gap: a period's versions leave a gap between one version's end and the next start.
  An error for first_seen (EIA) series, whose snapshots always close a version exactly where
  the next begins; a warning for source_vintage (FRED) series, where the provider can withdraw
  an observation and later report it again.
- no_current_version: a period with versions but none current. Error for first_seen series
  (a vanished period keeps its current version), warning for source_vintage series (FRED can
  withdraw an observation).
- written_by_failed_run / confirmed_by_failed_run: a version created or confirmed by a run
  recorded as failed (its data transaction should have rolled back). Errors.
- written_by_unfinalized_run: a version created by a run still marked running (a crash or an
  UNFINALIZED run whose outcome was never recorded). Warning, one finding per run.
- run_source_mismatch: a version created by a run of a different source than its series. Error.
- missing_provenance: a first_seen version without source_period or raw_payload_id. Error.
- provenance_mismatch: a first_seen version whose raw page belongs to another run or source, or
  was retrieved after the version started. Error.
- first_seen_times: a first_seen version whose first retrieval differs from its start, or whose
  last confirmation is before its start or not before its end. Error.
- source_vintage_midnight: a source_vintage (FRED) version boundary that is not midnight US
  Central (FRED vintages are dates, stored as midnight America/Chicago). Error.
- vanished_issue_without_current: an open vanished_period issue for a period with no current
  version (the tracked issue must point at a kept version). Error.
- unredacted_text: a stored raw-payload URL, run error message, run parameter, or issue detail
  that exposes a credential: a credential parameter with a non-empty value other than REDACTED
  (an empty `token=` exposes nothing), or a configured secret anywhere. Error. Always checks
  the whole database (these rows are not all tied to a series). Findings name the table, row,
  and parameter name only, never the value.

With a raw-payload store, two replay checks (ingestion.replay) also re-read the stored raw pages
(hash-verified, read-only) and compare them with the versions they produced:
- replay_eia: each EIA version matches the row of the raw page it cites, and each closed
  version is justified by the closing run's page (its successor's cited page);
- replay_fred: each FRED version appears, unchanged, in the output its creating run
  reconstructs from that run's stored observation pages.
Each replayed version is counted in exactly one state, by this precedence:
ERROR (a supported comparison found a mismatch), then UNVERIFIED (evidence is missing or
incomplete, e.g. a raw file is missing), then UNSUPPORTED (its creation matched, but a later
closure or confirmation by another run was not replayed: FRED records no run for those steps,
and replaying EIA confirmation snapshots is not implemented), else VERIFIED (creation,
provenance, and the whole stored history reconstructed and matched). UNVERIFIED and UNSUPPORTED
are never counted as passed or failed.

Exit codes, by precedence (the first that applies):
  1  any error (an SQL check error, or a replayed version in ERROR);
  2  no errors, but warnings;
  3  no errors or warnings, but replayed versions UNVERIFIED or UNSUPPORTED (not fully verified);
  0  nothing found, and with replay every replayed version VERIFIED.
Without replay, 0 means only that the SQL checks found nothing.

`--series` limits the series-level checks (and replay) to the given catalog codes.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import bindparam, func, select, text, true
from sqlalchemy.orm import Session, sessionmaker

from marketpulse.core.redaction import contains_secret, exposed_credential_parameters
from marketpulse.db.models import Observation, RawPayload, Series
from marketpulse.ingestion.raw_store import RawPayloadStore
from marketpulse.ingestion.replay import ReplayFindings, replay_eia, replay_fred

ERROR = "error"
WARNING = "warning"
MAX_SAMPLES = 5


class VerifyConfigError(RuntimeError):
    """Verification cannot start (e.g. an unknown series code)."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    description: str
    errors: int = 0
    warnings: int = 0
    # Items whose evidence is missing or incomplete (e.g. a raw file is missing).
    unverified: int = 0
    # Items the check has no method to establish (e.g. which FRED run closed a version).
    unsupported: int = 0
    # Items fully checked and matched; None for checks that only report findings (SQL checks).
    verified: int | None = None
    # Findings reported in total (one finding may cover several items); `samples` holds the
    # first MAX_SAMPLES of them: errors, then warnings, unverified, unsupported.
    findings: int = 0
    samples: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.errors:
            return "FAIL"
        if self.warnings:
            return "WARN"
        if self.unverified:
            return "UNVERIFIED"
        return "UNSUPPORTED" if self.unsupported else "ok"


@dataclass
class VerifyReport:
    checks: list[CheckResult] = field(default_factory=list)
    series: int = 0
    versions: int = 0
    raw_payloads: int = 0
    series_codes: tuple[str, ...] | None = None  # None = every series

    @property
    def errors(self) -> int:
        return sum(c.errors for c in self.checks)

    @property
    def warnings(self) -> int:
        return sum(c.warnings for c in self.checks)

    @property
    def unverified(self) -> int:
        return sum(c.unverified for c in self.checks)

    @property
    def unsupported(self) -> int:
        return sum(c.unsupported for c in self.checks)

    @property
    def replayed(self) -> bool:
        """Whether replay checks ran (they are the only checks that count verified items)."""
        return any(c.verified is not None for c in self.checks)

    @property
    def verified(self) -> int:
        return sum(c.verified or 0 for c in self.checks)

    @property
    def exit_code(self) -> int:
        """By precedence: 1 any error; 2 warnings (no errors); 3 no errors or warnings, but some
        items are unverified or unsupported; 0 nothing found (with replay: every replayed
        version verified)."""
        if self.errors:
            return 1
        if self.warnings:
            return 2
        return 3 if self.unverified or self.unsupported else 0


# --- series-level checks (SQL) ------------------------------------------------------------------
# Each query is a constant that yields (severity, detail) rows. `{scope}` is the only placeholder:
# it becomes "AND <column> IN :series_ids" (a bind parameter) or nothing. Details name series
# codes, periods, ids, and times only.

_CHECKS: tuple[tuple[str, str, str, str], ...] = (
    (
        "version_overlap",
        "Two versions of one period overlap in time",
        "o.series_id",
        """
        SELECT 'error', format('%s %s: version %s ends %s but the next starts %s', code,
               period_start, id, coalesce(vintage_end::text, 'never (current)'), next_start)
        FROM (
            SELECT o.id, o.period_start, o.vintage_end, s.code,
                   lead(o.vintage_start) OVER (
                       PARTITION BY o.series_id, o.period_start ORDER BY o.vintage_start
                   ) AS next_start
            FROM obs.observations o JOIN ref.series s ON s.id = o.series_id
            WHERE TRUE {scope}
        ) v
        WHERE next_start IS NOT NULL AND (vintage_end IS NULL OR vintage_end > next_start)
        """,
    ),
    (
        "version_gap",
        "A period's versions leave a gap in time (error for first_seen, warning for "
        "source_vintage)",
        "o.series_id",
        """
        SELECT CASE WHEN vintage_basis = 'first_seen' THEN 'error' ELSE 'warning' END,
               format('%s %s: version %s ends %s, the next starts %s', code, period_start, id,
                      vintage_end, next_start)
        FROM (
            SELECT o.id, o.period_start, o.vintage_end, s.code, s.vintage_basis,
                   lead(o.vintage_start) OVER (
                       PARTITION BY o.series_id, o.period_start ORDER BY o.vintage_start
                   ) AS next_start
            FROM obs.observations o JOIN ref.series s ON s.id = o.series_id
            WHERE TRUE {scope}
        ) v
        WHERE next_start IS NOT NULL AND vintage_end < next_start
        """,
    ),
    (
        "no_current_version",
        "A period has versions but none is current (error for first_seen, warning for "
        "source_vintage)",
        "o.series_id",
        """
        SELECT CASE WHEN vintage_basis = 'first_seen' THEN 'error' ELSE 'warning' END,
               format('%s %s: %s version(s), none current', code, period_start, n)
        FROM (
            SELECT s.code, s.vintage_basis, o.period_start, count(*) AS n
            FROM obs.observations o JOIN ref.series s ON s.id = o.series_id
            WHERE TRUE {scope}
            GROUP BY s.code, s.vintage_basis, o.period_start
            HAVING bool_and(o.vintage_end IS NOT NULL)
        ) p
        """,
    ),
    (
        "written_by_failed_run",
        "A version was created by a run recorded as failed",
        "o.series_id",
        """
        SELECT 'error', format('%s %s: version %s created by failed run %s', s.code,
               o.period_start, o.id, r.id)
        FROM obs.observations o
        JOIN ref.series s ON s.id = o.series_id
        JOIN ops.ingestion_runs r ON r.id = o.ingestion_run_id
        WHERE r.status = 'failed' {scope}
        """,
    ),
    (
        "confirmed_by_failed_run",
        "A version was confirmed by a run recorded as failed",
        "o.series_id",
        """
        SELECT 'error', format('%s %s: version %s confirmed by failed run %s', s.code,
               o.period_start, o.id, r.id)
        FROM obs.observations o
        JOIN ref.series s ON s.id = o.series_id
        JOIN ops.ingestion_runs r ON r.id = o.last_confirmed_run_id
        WHERE r.status = 'failed' {scope}
        """,
    ),
    (
        "written_by_unfinalized_run",
        "Versions were created by a run still marked running (crashed or UNFINALIZED)",
        "o.series_id",
        """
        SELECT 'warning', format('%s: %s version(s) created by run %s, still running since %s',
               s.code, count(*), r.id, r.started_at)
        FROM obs.observations o
        JOIN ref.series s ON s.id = o.series_id
        JOIN ops.ingestion_runs r ON r.id = o.ingestion_run_id
        WHERE r.status = 'running' {scope}
        GROUP BY s.code, r.id, r.started_at
        """,
    ),
    (
        "run_source_mismatch",
        "A version was created by a run of a different source than its series",
        "o.series_id",
        """
        SELECT 'error', format('%s %s: version %s created by run %s of source %s', s.code,
               o.period_start, o.id, r.id, coalesce(r.source_id::text, 'none'))
        FROM obs.observations o
        JOIN ref.series s ON s.id = o.series_id
        JOIN ops.ingestion_runs r ON r.id = o.ingestion_run_id
        WHERE r.source_id IS DISTINCT FROM s.source_id {scope}
        """,
    ),
    (
        "missing_provenance",
        "A first_seen version has no source_period or raw_payload_id",
        "o.series_id",
        """
        SELECT 'error', format('%s %s: version %s has no %s', s.code, o.period_start, o.id,
               concat_ws(' or ',
                   CASE WHEN o.source_period IS NULL OR btrim(o.source_period) = ''
                        THEN 'source_period' END,
                   CASE WHEN o.raw_payload_id IS NULL THEN 'raw_payload_id' END))
        FROM obs.observations o JOIN ref.series s ON s.id = o.series_id
        WHERE s.vintage_basis = 'first_seen'
          AND (o.source_period IS NULL OR btrim(o.source_period) = ''
               OR o.raw_payload_id IS NULL) {scope}
        """,
    ),
    (
        "provenance_mismatch",
        "A first_seen version's raw page belongs to another run or source, or was retrieved "
        "after the version started",
        "o.series_id",
        """
        SELECT 'error', format('%s %s: version %s cites raw payload %s (%s)', s.code,
               o.period_start, o.id, p.id, concat_ws('; ',
                   CASE WHEN p.ingestion_run_id <> o.ingestion_run_id
                        THEN format('run %s, not %s', p.ingestion_run_id, o.ingestion_run_id) END,
                   CASE WHEN p.source_id <> s.source_id
                        THEN format('source %s, not %s', p.source_id, s.source_id) END,
                   CASE WHEN p.retrieved_at > o.vintage_start
                        THEN format('retrieved %s, after %s', p.retrieved_at, o.vintage_start)
                        END))
        FROM obs.observations o
        JOIN ref.series s ON s.id = o.series_id
        JOIN ops.raw_payloads p ON p.id = o.raw_payload_id
        WHERE s.vintage_basis = 'first_seen'
          AND (p.ingestion_run_id <> o.ingestion_run_id OR p.source_id <> s.source_id
               OR p.retrieved_at > o.vintage_start) {scope}
        """,
    ),
    (
        "first_seen_times",
        "A first_seen version's first retrieval differs from its start, or its last "
        "confirmation is outside [start, end)",
        "o.series_id",
        """
        SELECT 'error', format('%s %s: version %s starts %s, first retrieved %s, last '
               'confirmed %s, ends %s', s.code, o.period_start, o.id, o.vintage_start,
               o.first_retrieved_at, o.last_confirmed_at,
               coalesce(o.vintage_end::text, 'never (current)'))
        FROM obs.observations o JOIN ref.series s ON s.id = o.series_id
        WHERE s.vintage_basis = 'first_seen'
          AND (o.first_retrieved_at <> o.vintage_start
               OR o.last_confirmed_at < o.vintage_start
               OR (o.vintage_end IS NOT NULL AND o.last_confirmed_at >= o.vintage_end)) {scope}
        """,
    ),
    (
        "source_vintage_midnight",
        "A source_vintage version boundary is not midnight US Central",
        "o.series_id",
        """
        SELECT 'error', format('%s %s: version %s starts %s, ends %s', s.code, o.period_start,
               o.id, o.vintage_start, coalesce(o.vintage_end::text, 'never (current)'))
        FROM obs.observations o JOIN ref.series s ON s.id = o.series_id
        WHERE s.vintage_basis = 'source_vintage'
          AND ((o.vintage_start AT TIME ZONE 'America/Chicago')::time <> time '00:00'
               OR (o.vintage_end IS NOT NULL
                   AND (o.vintage_end AT TIME ZONE 'America/Chicago')::time <> time '00:00'))
          {scope}
        """,
    ),
    (
        "vanished_issue_without_current",
        "An open vanished_period issue points at a period with no current version",
        "d.series_id",
        """
        SELECT 'error', format('%s %s: open vanished_period issue %s, but no current version',
               s.code, d.period_start, d.id)
        FROM ops.data_quality_issues d JOIN ref.series s ON s.id = d.series_id
        WHERE d.check_name = 'vanished_period' AND d.resolved_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM obs.observations o
              WHERE o.series_id = d.series_id AND o.period_start = d.period_start
                AND o.vintage_end IS NULL)
          {scope}
        """,
    ),
)

# Stored free text that must never hold a credential: (table, id column, text expression).
_TEXT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("ops.raw_payloads", "id", "request_url"),
    ("ops.ingestion_runs", "id", "error_message"),
    ("ops.ingestion_runs", "id", "params::text"),
    ("ops.data_quality_issues", "id", "detail"),
)


def _scope(column: str, series_ids: Sequence[int] | None) -> str:
    return f"AND {column} IN :series_ids" if series_ids is not None else ""


def _run_check(
    session: Session,
    name: str,
    description: str,
    column: str,
    query: str,
    series_ids: Sequence[int] | None,
) -> CheckResult:
    # Only module constants are interpolated (a _CHECKS query and its column name); the series
    # filter values are bind parameters.
    statement = text(
        f"""
        WITH hits(severity, detail) AS ({query.format(scope=_scope(column, series_ids))})
        SELECT severity, detail,
               count(*) FILTER (WHERE severity = 'error') OVER () AS errors,
               count(*) FILTER (WHERE severity = 'warning') OVER () AS warnings
        FROM hits
        ORDER BY severity = 'error' DESC, detail
        LIMIT :max_samples
        """  # noqa: S608
    )
    params: dict[str, object] = {"max_samples": MAX_SAMPLES}
    if series_ids is not None:
        statement = statement.bindparams(bindparam("series_ids", expanding=True))
        params["series_ids"] = list(series_ids)
    rows = session.execute(statement, params).all()
    if not rows:
        return CheckResult(name, description)
    errors, warnings = int(rows[0].errors), int(rows[0].warnings)
    return CheckResult(
        name,
        description,
        errors=errors,
        warnings=warnings,
        findings=errors + warnings,
        samples=tuple(f"{row.severity}: {row.detail}" for row in rows),
    )


def _check_unredacted_text(session: Session, secrets: Iterable[str]) -> CheckResult:
    secret_values = tuple(s for s in secrets if s.strip())
    hits: list[str] = []
    for table, id_column, expression in _TEXT_COLUMNS:
        # Constant table and column names from _TEXT_COLUMNS; no outside input.
        query = f"SELECT {id_column}, {expression} FROM {table} WHERE {expression} IS NOT NULL"  # noqa: S608
        rows = session.execute(text(query))
        where = expression.removesuffix("::text")
        for row_id, value in rows:
            # Only non-empty credential values count: `token=` or `api_key=REDACTED` expose
            # nothing. Names are reported, values never.
            exposed = sorted(set(exposed_credential_parameters(value)))
            if exposed:
                hits.append(
                    f"error: {table}.{where} in row {row_id} has a value for "
                    f"credential parameter(s) {', '.join(exposed)}"
                )
            if contains_secret(value, secret_values):
                hits.append(f"error: {table}.{where} in row {row_id} contains a configured secret")
    return CheckResult(
        "unredacted_text",
        "Stored URLs, run errors and parameters, or issue details expose a credential value",
        errors=len(hits),
        findings=len(hits),
        samples=tuple(hits[:MAX_SAMPLES]),
    )


def _replay_result(name: str, description: str, findings: ReplayFindings) -> CheckResult:
    samples = (
        [f"error: {m}" for m in findings.errors]
        + [f"unverified: {m}" for m in findings.unverified]
        + [f"unsupported: {m}" for m in findings.unsupported]
    )
    return CheckResult(
        name,
        description,
        errors=findings.error_count,
        unverified=findings.unverified_count,
        unsupported=findings.unsupported_count,
        verified=findings.verified_count,
        findings=findings.messages,
        samples=tuple(samples[:MAX_SAMPLES]),
    )


def _resolve_series(session: Session, codes: Sequence[str]) -> list[int]:
    found = {
        code: series_id
        for code, series_id in session.execute(
            select(Series.code, Series.id).where(Series.code.in_(codes))
        )
    }
    unknown = sorted(set(codes) - set(found))
    if unknown:
        raise VerifyConfigError(f"unknown series code(s) {unknown} (not in ref.series)")
    return [found[code] for code in dict.fromkeys(codes)]


def _begin_read_only(session: Session) -> None:
    """One consistent snapshot; PostgreSQL rejects any write in this transaction."""
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


def verify(
    session_factory: sessionmaker[Session],
    *,
    series_codes: Sequence[str] | None = None,
    secrets: Iterable[str] = (),
    raw_store: RawPayloadStore | None = None,
) -> VerifyReport:
    """Run every integrity check in one read-only transaction; never changes anything.

    `secrets` are credential values (e.g. configured API keys) that must appear nowhere in
    stored text; they are compared only, never reported. With `raw_store`, the replay checks
    also re-read the stored raw pages (read-only, hash-verified) and compare them with the
    versions they produced.
    """
    with session_factory() as session:
        try:
            _begin_read_only(session)
            series_ids = _resolve_series(session, series_codes) if series_codes else None
            report = VerifyReport(
                series_codes=tuple(dict.fromkeys(series_codes)) if series_codes else None
            )
            series_filter = Series.id.in_(series_ids) if series_ids is not None else true()
            obs_filter = Observation.series_id.in_(series_ids) if series_ids is not None else true()
            report.series = session.scalar(select(func.count(Series.id)).where(series_filter)) or 0
            report.versions = (
                session.scalar(select(func.count(Observation.id)).where(obs_filter)) or 0
            )
            report.raw_payloads = session.scalar(select(func.count(RawPayload.id))) or 0
            for name, description, column, query in _CHECKS:
                report.checks.append(
                    _run_check(session, name, description, column, query, series_ids)
                )
            report.checks.append(_check_unredacted_text(session, secrets))
            if raw_store is not None:
                report.checks.append(
                    _replay_result(
                        "replay_eia",
                        "Each EIA version matches the row of the stored raw page it cites; "
                        "each closure is justified by the closing run's page",
                        replay_eia(session, raw_store, series_ids),
                    )
                )
                report.checks.append(
                    _replay_result(
                        "replay_fred",
                        "Each FRED version appears, unchanged, in the output its run "
                        "reconstructs from that run's stored raw pages",
                        replay_fred(session, raw_store, series_ids),
                    )
                )
            return report
        finally:
            session.rollback()
