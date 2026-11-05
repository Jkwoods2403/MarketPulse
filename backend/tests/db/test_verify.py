"""`marketpulse verify`: read-only integrity checks over stored observations and lineage.

Clean data comes from the real FRED and EIA pipelines (simulated providers; all values are
SYNTHETIC or recorded public fixtures). Each corruption test then breaks exactly one invariant
with direct SQL and asserts that exactly the matching check reports it.
"""

import gzip
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from test_eia_ingestion import EXP, STORAGE, add_series, weekly
from test_eia_ingestion import make_client_factory as eia_client_factory
from test_fred_ingestion import SPECS as FRED_SPECS
from test_fred_ingestion import dgs10_rows, gdpc1_rows
from test_fred_ingestion import make_client_factory as fred_client_factory

import marketpulse.ingestion.verify as verify_module
from eia_sim import FakeEia
from fred_mock import TEST_KEY as FRED_KEY
from fred_sim import FakeFred, SimSeries, metadata_for
from marketpulse.cli import main
from marketpulse.core.config import Settings, get_settings
from marketpulse.ingestion.catalog_sync import sync_catalog
from marketpulse.ingestion.eia import ingest_eia
from marketpulse.ingestion.fred import ingest_fred
from marketpulse.ingestion.raw_store import RawPayloadStore
from marketpulse.ingestion.verify import VerifyConfigError, VerifyReport, verify
from marketpulse.sources.catalog import load_catalog
from test_eia_client import KEY as EIA_KEY

CATALOG = load_catalog()
ALL_CHECKS = {name for name, *_ in verify_module._CHECKS} | {"unredacted_text"}

# Every row of every table verification could read, as one fingerprint per table.
CHECKSUMS = (
    "SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '-') FROM obs.observations t",
    "SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '-') "
    "FROM ops.ingestion_runs t",
    "SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '-') FROM ops.raw_payloads t",
    "SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '-') "
    "FROM ops.data_quality_issues t",
    "SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '-') FROM ref.series t",
    "SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '-') FROM ref.sources t",
)

# One local (America/Chicago) day later: still midnight Central, so only the targeted check fires.
END_PLUS_LOCAL_DAY = (
    "UPDATE obs.observations SET vintage_end = "
    "((vintage_end AT TIME ZONE 'America/Chicago') + interval '1 day') AT TIME ZONE "
    "'America/Chicago' WHERE id = :id"
)
START_PLUS_LOCAL_DAY = (
    "UPDATE obs.observations SET vintage_start = "
    "((vintage_start AT TIME ZONE 'America/Chicago') + interval '1 day') AT TIME ZONE "
    "'America/Chicago' WHERE id = :id"
)
START_PLUS_HOUR = (
    "UPDATE obs.observations SET vintage_start = vintage_start + interval '1 hour' WHERE id = :id"
)


@dataclass
class Stored:
    factory: sessionmaker[Session]
    data_dir: Path  # where the pipelines saved their raw pages

    def sql(self, statement: str, **params: Any) -> None:
        with self.factory() as s, s.begin():
            s.execute(text(statement), params)

    def one(self, statement: str, **params: Any) -> Any:
        with self.factory() as s:
            return s.execute(text(statement), params).one()

    def scalar(self, statement: str, **params: Any) -> Any:
        with self.factory() as s:
            return s.scalar(text(statement), params)

    def checksum(self) -> tuple[str, ...]:
        return tuple(self.scalar(query) for query in CHECKSUMS)

    def verify(self, **kwargs: Any) -> VerifyReport:
        return verify(self.factory, **kwargs)

    def count(self, code: str) -> int:
        return int(
            self.scalar(
                "SELECT count(*) FROM obs.observations o JOIN ref.series s ON s.id = o.series_id "
                "WHERE s.code = :code",
                code=code,
            )
        )

    def first_version(self, code: str) -> int:
        """The id of the first stored version of `code`."""
        return int(
            self.scalar(
                "SELECT min(o.id) FROM obs.observations o JOIN ref.series s ON s.id = o.series_id "
                "WHERE s.code = :code",
                code=code,
            )
        )

    def multi_version_period(self, code: str) -> tuple[int, int]:
        """(older id, newer id) of the first period of `code` with two or more versions."""
        row = self.one(
            "SELECT ids[1] AS older, ids[2] AS newer FROM ("
            "  SELECT array_agg(o.id ORDER BY o.vintage_start) AS ids "
            "  FROM obs.observations o JOIN ref.series s ON s.id = o.series_id "
            "  WHERE s.code = :code GROUP BY o.period_start HAVING count(*) >= 2 "
            "  ORDER BY o.period_start LIMIT 1) p",
            code=code,
        )
        return int(row.older), int(row.newer)

    def run_of(self, code: str) -> int:
        """The id of the first run that created versions of `code`."""
        return int(
            self.scalar(
                "SELECT min(o.ingestion_run_id) FROM obs.observations o "
                "JOIN ref.series s ON s.id = o.series_id WHERE s.code = :code",
                code=code,
            )
        )

    def source_id(self, code: str) -> int:
        return int(self.scalar("SELECT id FROM ref.sources WHERE code = :code", code=code))

    def series_id(self, code: str) -> int:
        return int(self.scalar("SELECT id FROM ref.series WHERE code = :code", code=code))


@pytest.fixture
def stored(factory: sessionmaker[Session], tmp_path: Path) -> Iterator[Stored]:
    """FRED ust_10y and real_gdp (with revisions) and EIA l48_gas_storage with one revision."""
    with factory() as s, s.begin():
        sync_catalog(s, CATALOG)
    settings = Settings(
        _env_file=None,
        fred_api_key=SecretStr(FRED_KEY),  # type: ignore[call-arg]
        eia_api_key=SecretStr(EIA_KEY),
        marketpulse_data_dir=tmp_path,
    )
    fred = FakeFred()
    fred.series["DGS10"] = SimSeries(metadata_for(FRED_SPECS["ust_10y"]), dgs10_rows())
    fred.series["GDPC1"] = SimSeries(metadata_for(FRED_SPECS["real_gdp"]), gdpc1_rows())
    summary = ingest_fred(
        settings=settings,
        session_factory=factory,
        series_codes=["ust_10y", "real_gdp"],
        client_factory=fred_client_factory(fred),
    )
    assert summary.exit_code == 0

    eia = FakeEia(EIA_KEY)
    add_series(eia, STORAGE, weekly(10))

    def run_eia() -> None:
        result = ingest_eia(
            settings=settings,
            session_factory=factory,
            series_codes=[STORAGE],
            client_factory=eia_client_factory(eia),
        )
        assert result.exit_code == 0

    run_eia()
    eia.series[EXP[STORAGE].facet_series].rows[9]["value"] = "3999"  # a revision
    run_eia()
    yield Stored(factory, tmp_path)


def findings(report: VerifyReport) -> dict[str, tuple[int, int]]:
    return {c.name: (c.errors, c.warnings) for c in report.checks if c.errors or c.warnings}


def check(report: VerifyReport, name: str) -> verify_module.CheckResult:
    return next(c for c in report.checks if c.name == name)


def only(report: VerifyReport, name: str, errors: int, warnings: int = 0) -> None:
    assert findings(report) == {name: (errors, warnings)}


# --- clean data and read-only guarantees --------------------------------------------------------


def test_data_written_by_the_pipelines_passes_every_check(stored: Stored) -> None:
    report = stored.verify()
    assert findings(report) == {}
    assert report.exit_code == 0
    assert {c.name for c in report.checks} == ALL_CHECKS
    assert all(c.status == "ok" for c in report.checks)
    assert report.versions == stored.scalar("SELECT count(*) FROM obs.observations") > 0
    assert report.series == len(CATALOG.series)
    assert report.raw_payloads == stored.scalar("SELECT count(*) FROM ops.raw_payloads") > 0
    closed_eia = stored.scalar(
        "SELECT count(*) FROM obs.observations WHERE series_id = :s AND vintage_end IS NOT NULL",
        s=stored.series_id(STORAGE),
    )
    assert closed_eia == 1  # the fixture really contains a revised first-seen version


def test_verification_changes_nothing(stored: Stored) -> None:
    stored.sql(START_PLUS_HOUR, id=stored.first_version("ust_10y"))  # so findings are reported
    before = stored.checksum()
    report = stored.verify(secrets=["some-configured-secret"])
    assert report.exit_code == 1
    assert stored.checksum() == before


def test_the_verification_transaction_rejects_writes(
    stored: Stored, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A (hypothetical) check that writes is refused by PostgreSQL, not merely by convention."""
    writing_check = (
        "sneaky_write",
        "A check that tries to write",
        "o.series_id",
        "SELECT 'error', nextval('obs.observations_id_seq')::text",  # even a sequence bump
    )
    monkeypatch.setattr(verify_module, "_CHECKS", (writing_check,))
    before = stored.checksum()
    next_id = stored.scalar("SELECT last_value FROM obs.observations_id_seq")
    with pytest.raises(DBAPIError, match="read-only transaction"):
        stored.verify()
    assert stored.checksum() == before
    assert stored.scalar("SELECT last_value FROM obs.observations_id_seq") == next_id


def test_the_read_only_transaction_refuses_inserts(stored: Stored) -> None:
    before = stored.checksum()
    with stored.factory() as session:
        verify_module._begin_read_only(session)
        with pytest.raises(DBAPIError, match="read-only transaction"):
            session.execute(
                text("INSERT INTO ops.ingestion_runs (job, status) VALUES ('x', 'running')")
            )
        session.rollback()
    assert stored.checksum() == before


def test_verification_of_an_empty_database_finds_nothing(factory: sessionmaker[Session]) -> None:
    report = verify(factory)
    assert (report.exit_code, report.series, report.versions, report.raw_payloads) == (0, 0, 0, 0)


# --- one corruption, one finding ----------------------------------------------------------------


def test_overlapping_versions_are_an_error(stored: Stored) -> None:
    older, _ = stored.multi_version_period("real_gdp")
    stored.sql(END_PLUS_LOCAL_DAY, id=older)
    report = stored.verify()
    only(report, "version_overlap", errors=1)
    assert report.exit_code == 1
    assert "real_gdp" in check(report, "version_overlap").samples[0]


def test_a_gap_is_a_warning_for_fred(stored: Stored) -> None:
    _, newer = stored.multi_version_period("real_gdp")
    stored.sql(START_PLUS_LOCAL_DAY, id=newer)
    report = stored.verify()
    only(report, "version_gap", errors=0, warnings=1)
    assert report.exit_code == 2


def test_a_gap_is_an_error_for_eia(stored: Stored) -> None:
    older, _ = stored.multi_version_period(STORAGE)
    stored.sql(
        # Halfway between start and end (the two test snapshots are well under a second apart).
        "UPDATE obs.observations SET vintage_end = vintage_start + (vintage_end - vintage_start) "
        "/ 2 WHERE id = :id",
        id=older,
    )
    only(stored.verify(), "version_gap", errors=1)


def test_no_current_version_is_a_warning_for_fred(stored: Stored) -> None:
    stored.sql(
        "UPDATE obs.observations SET vintage_end = "
        "((vintage_start AT TIME ZONE 'America/Chicago') + interval '1 day') AT TIME ZONE "
        "'America/Chicago' WHERE id = :id",
        id=stored.first_version("ust_10y"),
    )
    only(stored.verify(), "no_current_version", errors=0, warnings=1)


def test_no_current_version_is_an_error_for_eia(stored: Stored) -> None:
    stored.sql(
        "UPDATE obs.observations SET vintage_end = vintage_start + interval '1 hour' "
        "WHERE id = :id",
        id=stored.first_version(STORAGE),
    )
    only(stored.verify(), "no_current_version", errors=1)


def test_versions_written_by_a_failed_run_are_an_error(stored: Stored) -> None:
    stored.sql(
        "UPDATE ops.ingestion_runs SET status = 'failed' WHERE id = :id",
        id=stored.run_of("ust_10y"),
    )
    only(stored.verify(), "written_by_failed_run", errors=3)  # the three DGS10 versions


def test_versions_confirmed_by_a_failed_run_are_an_error(stored: Stored) -> None:
    stored.sql(
        "INSERT INTO ops.ingestion_runs (job, source_id, status, finished_at) "
        "VALUES ('eia.observations', :source, 'failed', now())",
        source=stored.source_id("eia"),
    )
    stored.sql(
        "UPDATE obs.observations SET last_confirmed_run_id = "
        "(SELECT max(id) FROM ops.ingestion_runs WHERE status = 'failed') WHERE id = :id",
        id=stored.first_version(STORAGE),
    )
    only(stored.verify(), "confirmed_by_failed_run", errors=1)


def test_versions_of_an_unfinalized_run_are_one_warning_per_run(stored: Stored) -> None:
    stored.sql(
        "UPDATE ops.ingestion_runs SET status = 'running', finished_at = NULL WHERE id = :id",
        id=stored.run_of("ust_10y"),
    )
    report = stored.verify()
    only(report, "written_by_unfinalized_run", errors=0, warnings=1)
    assert "3 version(s)" in check(report, "written_by_unfinalized_run").samples[0]


def test_a_run_of_another_source_is_an_error(stored: Stored) -> None:
    eia_run = stored.run_of(STORAGE)
    created = stored.scalar(
        "SELECT count(*) FROM obs.observations WHERE ingestion_run_id = :id", id=eia_run
    )
    stored.sql(
        "UPDATE ops.ingestion_runs SET source_id = :fred WHERE id = :id",
        fred=stored.source_id("fred"),
        id=eia_run,
    )
    only(stored.verify(), "run_source_mismatch", errors=created)


@pytest.mark.parametrize(
    ("statement", "missing"),
    [
        ("UPDATE obs.observations SET raw_payload_id = NULL WHERE id = :id", "raw_payload_id"),
        ("UPDATE obs.observations SET source_period = ' ' WHERE id = :id", "source_period"),
    ],
)
def test_missing_eia_provenance_is_an_error(stored: Stored, statement: str, missing: str) -> None:
    stored.sql(statement, id=stored.first_version(STORAGE))
    report = stored.verify()
    only(report, "missing_provenance", errors=1)
    assert check(report, "missing_provenance").samples[0].endswith(f"has no {missing}")


def test_a_raw_page_of_another_run_and_source_is_an_error(stored: Stored) -> None:
    fred_payload = stored.scalar(
        "SELECT min(id) FROM ops.raw_payloads WHERE source_id = :s", s=stored.source_id("fred")
    )
    stored.sql(
        "UPDATE obs.observations SET raw_payload_id = :p WHERE id = :id",
        p=fred_payload,
        id=stored.first_version(STORAGE),
    )
    report = stored.verify()
    only(report, "provenance_mismatch", errors=1)
    sample = check(report, "provenance_mismatch").samples[0]
    assert "run " in sample and "source " in sample


def test_a_first_retrieval_that_differs_from_the_start_is_an_error(stored: Stored) -> None:
    stored.sql(
        "UPDATE obs.observations SET first_retrieved_at = vintage_start + interval '1 second' "
        "WHERE id = :id",
        id=stored.first_version(STORAGE),
    )
    only(stored.verify(), "first_seen_times", errors=1)


def test_a_confirmation_at_or_after_the_end_is_an_error(stored: Stored) -> None:
    older, _ = stored.multi_version_period(STORAGE)
    stored.sql(
        "UPDATE obs.observations SET last_confirmed_at = vintage_end WHERE id = :id", id=older
    )
    only(stored.verify(), "first_seen_times", errors=1)


def test_a_fred_vintage_off_midnight_is_an_error(stored: Stored) -> None:
    stored.sql(START_PLUS_HOUR, id=stored.first_version("ust_10y"))
    only(stored.verify(), "source_vintage_midnight", errors=1)


def test_an_open_vanished_issue_without_a_current_version_is_an_error(stored: Stored) -> None:
    stored.sql(
        "INSERT INTO ops.data_quality_issues "
        "(ingestion_run_id, series_id, period_start, check_name, detail, severity) "
        "VALUES (:run, :series, :p, 'vanished_period', 'synthetic', 'warning')",
        run=stored.run_of(STORAGE),
        series=stored.series_id(STORAGE),
        p=date(1999, 1, 1),
    )
    only(stored.verify(), "vanished_issue_without_current", errors=1)


def test_a_stored_credential_parameter_is_an_error_and_never_printed(stored: Stored) -> None:
    leaked = "abc123-leaked-key-value"
    stored.sql(
        "UPDATE ops.raw_payloads SET request_url = "
        "replace(request_url, 'api_key=REDACTED', 'api_key=' || :v) "
        "WHERE id = (SELECT min(id) FROM ops.raw_payloads)",
        v=leaked,
    )
    report = stored.verify()
    only(report, "unredacted_text", errors=1)
    (sample,) = check(report, "unredacted_text").samples
    assert "ops.raw_payloads.request_url" in sample and leaked not in sample


def test_a_configured_secret_anywhere_in_stored_text_is_an_error(stored: Stored) -> None:
    secret = "TEST-CONFIGURED-SECRET-0042"
    stored.sql(
        "UPDATE ops.ingestion_runs SET error_message = 'boom ' || :s "
        "WHERE id = (SELECT min(id) FROM ops.ingestion_runs)",
        s=secret,
    )
    assert findings(stored.verify()) == {}  # not a credential *parameter*, so only by value
    report = stored.verify(secrets=[secret, "  "])
    only(report, "unredacted_text", errors=1)
    assert all(secret not in s for c in report.checks for s in c.samples)


def test_samples_are_limited_but_counts_are_complete(stored: Stored) -> None:
    stored.sql(
        "UPDATE ops.ingestion_runs SET status = 'failed' WHERE id = :id",
        id=stored.run_of(STORAGE),
    )
    result = check(stored.verify(), "written_by_failed_run")
    assert result.errors == 10 and len(result.samples) == verify_module.MAX_SAMPLES


# --- scope --------------------------------------------------------------------------------------


def test_series_scope_limits_series_level_checks(stored: Stored) -> None:
    stored.sql(START_PLUS_HOUR, id=stored.first_version("ust_10y"))
    other = stored.verify(series_codes=["real_gdp"])
    assert findings(other) == {} and (other.series, other.series_codes) == (1, ("real_gdp",))
    assert other.versions == stored.count("real_gdp")
    only(stored.verify(series_codes=["ust_10y", "ust_10y"]), "source_vintage_midnight", errors=1)


def test_unknown_series_codes_are_a_configuration_error(stored: Stored) -> None:
    with pytest.raises(VerifyConfigError, match=r"unknown series code\(s\) \['nope'\]"):
        stored.verify(series_codes=["ust_10y", "nope"])


# --- CLI ----------------------------------------------------------------------------------------


@pytest.fixture
def cli(stored: Stored, test_db_name: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Stored]:
    monkeypatch.setenv("POSTGRES_DB", test_db_name)
    monkeypatch.setenv("MARKETPULSE_DATA_DIR", str(stored.data_dir))
    get_settings.cache_clear()
    yield stored
    get_settings.cache_clear()


def test_cli_reports_a_clean_database_with_exit_code_0(
    cli: Stored, capsys: pytest.CaptureFixture[str]
) -> None:
    before = cli.checksum()
    assert main(["verify"]) == 0
    out = capsys.readouterr().out
    assert "version_overlap" in out and "unredacted_text" in out
    assert "0 errors, 0 warnings" in out
    assert "replay: not run" in out and "replay_fred" not in out
    assert "read-only; nothing was changed" in out
    assert cli.checksum() == before


def test_cli_shows_findings_and_exit_codes(cli: Stored, capsys: pytest.CaptureFixture[str]) -> None:
    _, newer = cli.multi_version_period("real_gdp")
    cli.sql(START_PLUS_LOCAL_DAY, id=newer)
    assert main(["verify"]) == 2  # a FRED gap is a warning
    out = capsys.readouterr().out
    assert "version_gap" in out and "WARN" in out and "warning: real_gdp" in out

    cli.sql(START_PLUS_HOUR, id=cli.first_version("ust_10y"))
    assert main(["verify"]) == 1
    assert "FAIL" in capsys.readouterr().out
    assert main(["verify", "--series", "real_gdp"]) == 2  # the error is in another series


def test_cli_unknown_series_is_a_clean_error(
    cli: Stored, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["verify", "--series", "nope"]) == 1
    captured = capsys.readouterr()
    assert "error: unknown series code(s) ['nope']" in captured.out
    assert "Traceback" not in captured.out + captured.err


# --- credential detection (D5.2 phase B refinement) ---------------------------------------------


def set_first_run_error(stored: Stored, message: str) -> None:
    stored.sql(
        "UPDATE ops.ingestion_runs SET error_message = :m "
        "WHERE id = (SELECT min(id) FROM ops.ingestion_runs)",
        m=message,
    )


@pytest.mark.parametrize(
    "message",
    [
        "token= was empty",  # an empty credential parameter exposes nothing
        "GET https://x.example/?api_key=REDACTED&offset=0 failed",  # already redacted
        "monkey=banana turnkey=yes",  # not credential parameters
    ],
)
def test_empty_or_redacted_credential_parameters_are_not_exposures(
    stored: Stored, message: str
) -> None:
    set_first_run_error(stored, message)
    assert findings(stored.verify()) == {}


def test_a_nonempty_credential_value_is_flagged_by_name_only(stored: Stored) -> None:
    set_first_run_error(stored, "login failed: password=hunter2-synthetic token=")
    report = stored.verify()
    only(report, "unredacted_text", errors=1)
    (sample,) = check(report, "unredacted_text").samples
    assert "credential parameter(s) password" in sample  # token= (empty) is not listed
    assert "hunter2" not in sample


# --- replay (D5.2 phase B) ----------------------------------------------------------------------


def files(data_dir: Path) -> dict[str, tuple[int, int, bytes]]:
    """Every file under data_dir with its size, modification time, and bytes."""
    return {
        str(p.relative_to(data_dir)): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(data_dir.rglob("*"))
        if p.is_file()
    }


def replayed(stored: Stored, **kwargs: Any) -> VerifyReport:
    return stored.verify(raw_store=RawPayloadStore(stored.data_dir), **kwargs)


def raw_file(stored: Stored, payload_id: int) -> Path:
    path = stored.scalar("SELECT file_path FROM ops.raw_payloads WHERE id = :p", p=payload_id)
    return stored.data_dir / path


def cited_payload(stored: Stored, version_id: int) -> int:
    return int(
        stored.scalar("SELECT raw_payload_id FROM obs.observations WHERE id = :v", v=version_id)
    )


def versions_citing(stored: Stored, payload_id: int) -> int:
    return int(
        stored.scalar(
            "SELECT count(*) FROM obs.observations WHERE raw_payload_id = :p", p=payload_id
        )
    )


def fred_observation_pages(stored: Stored, code: str) -> list[int]:
    run = stored.run_of(code)
    with stored.factory() as session:  # closed, so no transaction is left holding locks
        return [
            int(page_id)
            for page_id in session.scalars(
                text(
                    "SELECT p.id FROM ops.raw_payloads p WHERE p.ingestion_run_id = :run "
                    "AND p.file_path LIKE 'raw/fred/observations/%' ORDER BY p.id"
                ),
                {"run": run},
            )
        ]


def source_versions(stored: Stored, source: str, codes: tuple[str, ...] | None = None) -> int:
    """Stored versions of `source`, optionally only of the series `codes`."""
    return int(
        stored.scalar(
            "SELECT count(*) FROM obs.observations o JOIN ref.series s ON s.id = o.series_id "
            "JOIN ref.sources src ON src.id = s.source_id WHERE src.code = :source "
            "AND (CAST(:codes AS text[]) IS NULL OR s.code = ANY(CAST(:codes AS text[])))",
            source=source,
            codes=list(codes) if codes is not None else None,
        )
    )


def states(result: verify_module.CheckResult) -> tuple[int, int, int, int]:
    """(verified, errors, unverified, unsupported) of a replay check."""
    assert result.verified is not None
    return result.verified, result.errors, result.unverified, result.unsupported


def assert_each_version_counted_once(stored: Stored, report: VerifyReport) -> None:
    """Every in-scope version is in exactly one replay state (states are disjoint counts)."""
    for name, source in (("replay_eia", "eia"), ("replay_fred", "fred")):
        expected = source_versions(stored, source, report.series_codes)
        assert sum(states(check(report, name))) == expected, name


FRED_ONLY = ["ust_10y", "real_gdp"]  # one ingestion run each: nothing closed/confirmed later


def eia_confirmed_later(stored: Stored) -> int:
    """EIA versions a run other than their creating run confirmed (the fixture's 2nd snapshot)."""
    return int(
        stored.scalar(
            "SELECT count(*) FROM obs.observations o JOIN ref.series s ON s.id = o.series_id "
            "JOIN ref.sources src ON src.id = s.source_id WHERE src.code = 'eia' AND "
            "(o.last_confirmed_run_id <> o.ingestion_run_id "
            " OR o.last_confirmed_at <> o.first_retrieved_at)"
        )
    )


def test_replay_of_pipeline_data_counts_every_version_once(stored: Stored) -> None:
    report = replayed(stored)
    assert {c.name for c in report.checks} == ALL_CHECKS | {"replay_eia", "replay_fred"}
    assert findings(report) == {}
    assert states(check(report, "replay_fred")) == (source_versions(stored, "fred"), 0, 0, 0)
    # The second EIA snapshot re-confirmed every unrevised period: 9 of the 10 first versions.
    later = eia_confirmed_later(stored)
    assert later == 9
    eia = source_versions(stored, "eia")
    assert states(check(report, "replay_eia")) == (eia - later, 0, 0, later)
    assert check(report, "replay_eia").status == "UNSUPPORTED"
    assert (report.unverified, report.unsupported, report.exit_code) == (0, later, 3)
    assert_each_version_counted_once(stored, report)
    assert fred_observation_pages(stored, "real_gdp")  # FRED pages really were replayed


def test_replay_of_fully_attributable_data_is_verified_with_exit_code_0(stored: Stored) -> None:
    report = replayed(stored, series_codes=FRED_ONLY)
    assert findings(report) == {}
    assert all(c.status == "ok" for c in report.checks)
    assert (report.unverified, report.unsupported, report.exit_code) == (0, 0, 0)
    assert report.verified == stored.count("ust_10y") + stored.count("real_gdp")


def test_sql_checks_count_no_verified_items(stored: Stored) -> None:
    report = replayed(stored)
    assert all(c.verified is None for c in report.checks if not c.name.startswith("replay_"))
    assert not stored.verify().replayed and report.replayed


def test_replay_runs_only_when_a_raw_store_is_given(stored: Stored) -> None:
    assert {c.name for c in stored.verify().checks} == ALL_CHECKS


def test_replay_changes_no_file_and_no_row(stored: Stored) -> None:
    stored.sql(
        "UPDATE obs.observations SET value = value + 1 WHERE id = :id",
        id=stored.first_version(STORAGE),
    )  # so the error path runs too
    before_db, before_files = stored.checksum(), files(stored.data_dir)
    assert before_files
    report = replayed(stored)
    assert report.exit_code == 1
    assert stored.checksum() == before_db
    assert files(stored.data_dir) == before_files


def test_eia_value_that_differs_from_its_page_is_an_error(stored: Stored) -> None:
    version = stored.first_version(STORAGE)
    stored.sql("UPDATE obs.observations SET value = value + 1 WHERE id = :id", id=version)
    report = replayed(stored)
    only(report, "replay_eia", errors=1)
    assert "page value 3000, stored 3001" in check(report, "replay_eia").samples[0]


def test_eia_period_label_not_matching_its_page_row_is_an_error(stored: Stored) -> None:
    version = stored.first_version(STORAGE)
    stored.sql(
        "UPDATE obs.observations SET source_period = '1999-01-01' WHERE id = :id", id=version
    )
    report = replayed(stored)
    only(report, "replay_eia", errors=1)
    assert "has 0 rows for this period" in check(report, "replay_eia").samples[0]


def test_a_tampered_eia_page_is_an_error_not_unverified(stored: Stored) -> None:
    payload = cited_payload(stored, stored.first_version(STORAGE))
    raw_file(stored, payload).write_bytes(gzip.compress(b'{"response": {}}'))
    report = replayed(stored)
    only(report, "replay_eia", errors=versions_citing(stored, payload))
    assert "does not match its recorded SHA-256" in check(report, "replay_eia").samples[0]


def test_a_missing_eia_page_is_unverified_not_passed_or_failed(stored: Stored) -> None:
    payload = cited_payload(stored, stored.first_version(STORAGE))
    raw_file(stored, payload).unlink()
    report = replayed(stored)
    assert findings(report) == {}  # no error, no warning ...
    result = check(report, "replay_eia")
    assert (result.status, result.unverified) == ("UNVERIFIED", versions_citing(stored, payload))
    assert (report.unverified, report.exit_code) == (result.unverified, 3)  # ... but not a pass
    assert "is missing" in result.samples[0]


def test_eia_version_without_a_cited_page_is_unverified(stored: Stored) -> None:
    stored.sql(
        "UPDATE obs.observations SET raw_payload_id = NULL WHERE id = :id",
        id=stored.first_version(STORAGE),
    )
    report = replayed(stored)
    only(report, "missing_provenance", errors=1)  # reported as an error by its own check
    assert check(report, "replay_eia").unverified == 1


def test_eia_confirmation_by_a_later_snapshot_is_unsupported_not_verified(stored: Stored) -> None:
    report = replayed(stored, series_codes=[STORAGE])
    result = check(report, "replay_eia")
    assert (result.status, result.errors, result.unverified) == ("UNSUPPORTED", 0, 0)
    assert result.unsupported == eia_confirmed_later(stored)
    # The version the revision closed, and the revision itself, were confirmed by no other run.
    assert result.verified == 2
    (sample,) = result.samples
    assert sample.startswith("unsupported: raw payload")
    assert "(creation and any closure verified)" in sample
    assert "confirmation history is not verified" in sample
    second_run = stored.scalar(
        "SELECT max(ingestion_run_id) FROM obs.observations o "
        "JOIN ref.series s ON s.id = o.series_id WHERE s.code = :code",
        code=STORAGE,
    )
    assert f"later run(s) {second_run};" in sample


def test_eia_confirmation_detected_by_time_alone_is_unsupported(stored: Stored) -> None:
    """A confirmation time after creation means a later confirmation, even with no run id."""
    version = stored.scalar(  # a version no later run confirmed
        "SELECT min(o.id) FROM obs.observations o JOIN ref.series s ON s.id = o.series_id "
        "WHERE s.code = :code AND o.last_confirmed_at = o.first_retrieved_at",
        code=STORAGE,
    )
    stored.sql(
        "UPDATE obs.observations SET last_confirmed_run_id = NULL, "
        "last_confirmed_at = last_confirmed_at + interval '1 second' WHERE id = :id",
        id=version,
    )
    result = check(replayed(stored, series_codes=[STORAGE]), "replay_eia")
    assert (result.unsupported, result.verified) == (eia_confirmed_later(stored), 1)
    assert "later run(s) " in result.samples[0]


def test_an_eia_error_outranks_unsupported_for_one_version(stored: Stored) -> None:
    version = stored.scalar(  # a version confirmed by the second snapshot
        "SELECT min(o.id) FROM obs.observations o JOIN ref.series s ON s.id = o.series_id "
        "WHERE s.code = :code AND o.last_confirmed_run_id <> o.ingestion_run_id",
        code=STORAGE,
    )
    stored.sql("UPDATE obs.observations SET value = value + 1 WHERE id = :id", id=version)
    report = replayed(stored, series_codes=[STORAGE])
    result = check(report, "replay_eia")
    assert (result.errors, result.unsupported) == (1, eia_confirmed_later(stored) - 1)
    assert report.exit_code == 1
    assert_each_version_counted_once(stored, report)


def test_an_unverified_eia_version_is_not_also_unsupported(stored: Stored) -> None:
    payload = cited_payload(stored, stored.first_version(STORAGE))  # first run's page
    raw_file(stored, payload).unlink()
    report = replayed(stored, series_codes=[STORAGE])
    result = check(report, "replay_eia")
    assert result.unverified == versions_citing(stored, payload)
    assert result.status == "UNVERIFIED"
    assert_each_version_counted_once(stored, report)


@dataclass(frozen=True)
class Closure:
    closed: int  # the EIA version the fixture's revision closed
    successor: int  # the version that replaced it (created by the closing run)
    closing_run: int
    closing_page: int  # the closing run's raw page, cited by the successor


def eia_closure(stored: Stored) -> Closure:
    row = stored.one(
        "SELECT o.id AS closed, n.id AS successor, n.ingestion_run_id AS run, "
        "n.raw_payload_id AS page FROM obs.observations o "
        "JOIN ref.series s ON s.id = o.series_id "
        "JOIN obs.observations n ON n.series_id = o.series_id "
        "  AND n.period_start = o.period_start AND n.vintage_start = o.vintage_end "
        "WHERE s.code = :code AND o.vintage_end IS NOT NULL",
        code=STORAGE,
    )
    return Closure(int(row.closed), int(row.successor), int(row.run), int(row.page))


def eia_states(stored: Stored) -> tuple[VerifyReport, tuple[int, int, int, int]]:
    report = replayed(stored, series_codes=[STORAGE])
    assert_each_version_counted_once(stored, report)
    return report, states(check(report, "replay_eia"))


def test_closed_and_unclosed_eia_versions_are_verified_with_their_closure(
    stored: Stored,
) -> None:
    """The fixture's revision closed one version; its closing run is the successor's creator,
    and the closing page reports the new value, so the closed version is fully VERIFIED, as is
    the unclosed successor. The 9 versions re-confirmed by the second run are UNSUPPORTED."""
    closure = eia_closure(stored)
    assert stored.scalar(
        "SELECT ingestion_run_id <> :run FROM obs.observations WHERE id = :id",
        run=closure.closing_run,
        id=closure.closed,
    )
    _, (verified, errors, unverified, unsupported) = eia_states(stored)
    assert (verified, errors, unverified, unsupported) == (2, 0, 0, eia_confirmed_later(stored))


def test_a_missing_closing_page_makes_the_closed_version_unverified(stored: Stored) -> None:
    closure = eia_closure(stored)
    raw_file(stored, closure.closing_page).unlink()
    report, (verified, errors, unverified, _) = eia_states(stored)
    # The successor (its creation page) and the closed version (its closing page) lack evidence.
    assert (verified, errors, unverified) == (0, 0, 2)
    assert versions_citing(stored, closure.closing_page) == 1
    samples = check(report, "replay_eia").samples
    assert any("is missing (closing page of" in sample for sample in samples)
    assert report.exit_code == 3


def test_a_tampered_closing_page_is_an_error_for_the_closed_version(stored: Stored) -> None:
    closure = eia_closure(stored)
    raw_file(stored, closure.closing_page).write_bytes(gzip.compress(b'{"response": {}}'))
    report, (verified, errors, unverified, _) = eia_states(stored)
    assert (verified, errors, unverified) == (0, 2, 0)
    assert any(
        "does not match its recorded SHA-256 (closing page of" in sample
        for sample in check(report, "replay_eia").samples
    )


def test_a_closure_with_no_successor_cannot_identify_its_closing_run(stored: Stored) -> None:
    closure = eia_closure(stored)
    stored.sql(  # the end no longer meets the successor's start
        "UPDATE obs.observations SET vintage_end = vintage_end - interval '1 microsecond' "
        "WHERE id = :id",
        id=closure.closed,
    )
    report, (verified, errors, unverified, _) = eia_states(stored)
    assert (verified, errors, unverified) == (1, 1, 0)  # the successor is still verified
    assert "the closing run cannot be identified" in check(report, "replay_eia").samples[0]
    assert findings(report)["version_gap"] == (1, 0)  # the SQL check reports it as well


def test_a_closure_its_closing_page_does_not_justify_is_an_error(stored: Stored) -> None:
    closure = eia_closure(stored)
    closed_page = cited_payload(stored, closure.closed)
    stored.sql(  # the successor now cites a page that still reports the closed value
        "UPDATE obs.observations SET raw_payload_id = :page WHERE id = :id",
        page=closed_page,
        id=closure.successor,
    )
    report, (verified, errors, _, _) = eia_states(stored)
    assert (verified, errors) == (0, 2)  # the closure and the successor's own creation
    samples = check(report, "replay_eia").samples
    assert any("the closure is not justified" in sample for sample in samples)


def test_a_closed_version_confirmed_later_is_unsupported_not_verified(stored: Stored) -> None:
    closure = eia_closure(stored)
    stored.sql(  # a confirmation by another run before the closure (still inside its lifetime)
        "UPDATE obs.observations SET last_confirmed_run_id = :run, "
        "last_confirmed_at = first_retrieved_at + interval '1 microsecond' WHERE id = :id",
        run=closure.closing_run,
        id=closure.closed,
    )
    report, (verified, errors, unverified, unsupported) = eia_states(stored)
    assert (verified, errors, unverified) == (1, 0, 0)
    assert unsupported == eia_confirmed_later(stored)
    assert findings(report) == {}


def test_a_closure_error_outranks_a_later_confirmation(stored: Stored) -> None:
    closure = eia_closure(stored)
    stored.sql(
        "UPDATE obs.observations SET last_confirmed_run_id = :run, "
        "last_confirmed_at = first_retrieved_at + interval '1 microsecond', "
        "vintage_end = vintage_end - interval '1 microsecond' WHERE id = :id",
        run=closure.closing_run,
        id=closure.closed,
    )
    _, (verified, errors, _, unsupported) = eia_states(stored)
    assert (verified, errors) == (1, 1)
    assert unsupported == eia_confirmed_later(stored) - 1  # not counted twice


def test_fred_value_that_differs_from_its_run_output_is_an_error(stored: Stored) -> None:
    stored.sql(
        "UPDATE obs.observations SET value = value + 1 WHERE id = :id",
        id=stored.first_version("ust_10y"),
    )
    report = replayed(stored)
    only(report, "replay_fred", errors=1)
    assert "of run" in check(report, "replay_fred").samples[0]


def test_fred_version_its_run_never_produced_is_an_error(stored: Stored) -> None:
    stored.sql(START_PLUS_LOCAL_DAY, id=stored.first_version("ust_10y"))  # still midnight
    report = replayed(stored)
    only(report, "replay_fred", errors=1)
    assert "not in the run's reconstructed output" in check(report, "replay_fred").samples[0]


def test_fred_end_reported_by_the_creating_run_is_compared(stored: Stored) -> None:
    older, _ = stored.multi_version_period("real_gdp")  # closed by its own run's output
    stored.sql(END_PLUS_LOCAL_DAY, id=older)
    report = replayed(stored)
    assert findings(report) == {"version_overlap": (1, 0), "replay_fred": (1, 0)}
    assert " end " in check(report, "replay_fred").samples[0]


def test_fred_closure_by_a_later_run_is_unsupported(stored: Stored) -> None:
    """A version current when its run created it may be closed by a later run; FRED does not
    record which run, so replay neither calls it a contradiction nor claims it verified."""
    newer = stored.scalar(  # the current version of a revised period
        "SELECT o.id FROM obs.observations o JOIN ref.series s ON s.id = o.series_id "
        "WHERE s.code = 'real_gdp' AND o.vintage_end IS NULL AND o.period_start IN ("
        "  SELECT period_start FROM obs.observations WHERE series_id = o.series_id "
        "  GROUP BY period_start HAVING count(*) >= 2) "
        "ORDER BY o.period_start LIMIT 1"
    )
    assert newer is not None
    stored.sql(
        "UPDATE obs.observations SET vintage_end = "
        "((vintage_start AT TIME ZONE 'America/Chicago') + interval '30 days') AT TIME ZONE "
        "'America/Chicago' WHERE id = :id",
        id=newer,
    )
    report = replayed(stored)
    assert findings(report) == {"no_current_version": (0, 1)}  # no replay_fred error
    result = check(report, "replay_fred")
    assert (result.status, result.errors, result.unverified, result.unsupported) == (
        "UNSUPPORTED",
        0,
        0,
        1,
    )
    assert result.verified == source_versions(stored, "fred") - 1
    assert "later runs closed 1 and re-confirmed 0" in result.samples[0]
    assert result.samples[0].startswith("unsupported: ")
    assert_each_version_counted_once(stored, report)


def confirm_later(stored: Stored, version_id: int) -> None:
    """What a later FRED run does when it reports a version unchanged: only the time moves."""
    stored.sql(
        "UPDATE obs.observations SET last_confirmed_at = last_confirmed_at + interval '1 day' "
        "WHERE id = :id",
        id=version_id,
    )


def test_fred_confirmation_by_a_later_run_is_unsupported_not_verified(stored: Stored) -> None:
    confirm_later(stored, stored.first_version("ust_10y"))
    report = replayed(stored, series_codes=FRED_ONLY)
    assert findings(report) == {}  # neither an error nor a warning ...
    result = check(report, "replay_fred")
    assert (result.status, result.unsupported, result.unverified) == ("UNSUPPORTED", 1, 0)
    assert "(creation verified)" in result.samples[0] and "re-confirmed 1" in result.samples[0]
    assert (report.unsupported, report.exit_code) == (1, 3)  # ... and not a full verification
    assert_each_version_counted_once(stored, report)


def test_fred_first_retrieval_not_matching_its_run_pages_is_an_error(stored: Stored) -> None:
    version = stored.first_version("ust_10y")
    stored.sql(
        "UPDATE obs.observations SET first_retrieved_at = first_retrieved_at - interval '1 hour', "
        "last_confirmed_at = last_confirmed_at - interval '1 hour' WHERE id = :id",
        id=version,
    )
    report = replayed(stored)
    only(report, "replay_fred", errors=1)
    assert "but the run's pages were last retrieved" in check(report, "replay_fred").samples[0]
    assert_each_version_counted_once(stored, report)


def test_an_error_outranks_unsupported_for_one_version(stored: Stored) -> None:
    version = stored.first_version("ust_10y")
    confirm_later(stored, version)
    stored.sql("UPDATE obs.observations SET value = value + 1 WHERE id = :id", id=version)
    result = check(replayed(stored), "replay_fred")
    assert (result.errors, result.unsupported) == (1, 0)


def test_fred_run_without_stored_pages_is_unverified(stored: Stored) -> None:
    pages = fred_observation_pages(stored, "ust_10y")
    stored.sql("DELETE FROM ops.raw_payloads WHERE id = ANY(:ids)", ids=pages)
    report = replayed(stored)
    result = check(report, "replay_fred")
    assert (result.status, result.errors, result.unverified) == ("UNVERIFIED", 0, 3)
    assert "has no stored observation pages" in result.samples[0]
    assert report.exit_code == 3


def test_fred_run_with_a_missing_page_file_is_unverified(stored: Stored) -> None:
    (page,) = fred_observation_pages(stored, "ust_10y")
    raw_file(stored, page).unlink()
    report = replayed(stored)
    result = check(report, "replay_fred")
    assert (result.status, result.errors, result.unverified) == ("UNVERIFIED", 0, 3)
    assert result.unsupported == 0
    assert_each_version_counted_once(stored, report)


def test_fred_run_with_a_tampered_page_is_an_error(stored: Stored) -> None:
    (page,) = fred_observation_pages(stored, "ust_10y")
    raw_file(stored, page).write_bytes(gzip.compress(b"{}"))
    only(replayed(stored), "replay_fred", errors=3)


def test_replay_respects_the_series_scope(stored: Stored) -> None:
    stored.sql(
        "UPDATE obs.observations SET value = value + 1 WHERE id = :id",
        id=stored.first_version("ust_10y"),
    )
    assert findings(replayed(stored, series_codes=["real_gdp", STORAGE])) == {}
    only(replayed(stored, series_codes=["ust_10y"]), "replay_fred", errors=1)


def test_unverified_items_never_hide_errors_or_warnings(stored: Stored) -> None:
    (page,) = fred_observation_pages(stored, "ust_10y")
    raw_file(stored, page).unlink()  # unverified ...
    _, newer = stored.multi_version_period("real_gdp")
    stored.sql(START_PLUS_LOCAL_DAY, id=newer)  # ... plus a FRED gap warning and replay error
    report = replayed(stored)
    assert report.unverified == 3 and report.warnings == 1 and report.errors == 1
    assert report.exit_code == 1


def test_unverified_and_unsupported_are_counted_apart(stored: Stored) -> None:
    (page,) = fred_observation_pages(stored, "ust_10y")
    raw_file(stored, page).unlink()  # 3 ust_10y versions: evidence missing
    confirm_later(stored, stored.first_version("real_gdp"))  # 1 real_gdp version: unsupported
    report = replayed(stored)
    result = check(report, "replay_fred")
    assert (result.unverified, result.unsupported, result.errors) == (3, 1, 0)
    assert result.status == "UNVERIFIED"  # missing evidence is listed first
    assert result.findings == 2
    assert [s.split(":", 1)[0] for s in result.samples] == ["unverified", "unsupported"]
    assert report.exit_code == 3
    assert_each_version_counted_once(stored, report)


# --- CLI replay (D5.2 phase C) ------------------------------------------------------------------


def row(out: str, name: str) -> list[str]:
    """The summary-table row of check `name`, split into columns."""
    return next(line.split() for line in out.splitlines() if line.split()[:1] == [name])


def replay_line(out: str) -> str:
    """The whole replay summary line (exact comparisons; "UNSUPPORTED 1" is in "... 10")."""
    return next(line for line in out.splitlines() if line.startswith("replay (versions):"))


def replay_summary(verified: int, error: int, unverified: int, unsupported: int) -> str:
    return (
        f"replay (versions): VERIFIED {verified}, ERROR {error}, "
        f"UNVERIFIED {unverified}, UNSUPPORTED {unsupported}"
    )


def test_cli_replay_of_fully_attributable_data_exits_0(
    cli: Stored, capsys: pytest.CaptureFixture[str]
) -> None:
    before_db, before_files = cli.checksum(), files(cli.data_dir)
    assert main(["verify", "--replay", "--series", *FRED_ONLY]) == 0
    out = capsys.readouterr().out
    fred = source_versions(cli, "fred")
    # check, status, errors, warnings, unverified, unsupported, verified
    assert row(out, "replay_fred") == ["replay_fred", "ok", "0", "0", "0", "0", str(fred)]
    assert row(out, "replay_eia") == ["replay_eia", "ok", "0", "0", "0", "0", "0"]
    assert row(out, "version_overlap")[4:] == ["-", "-", "-"]  # SQL checks count no items
    assert replay_line(out) == replay_summary(fred, 0, 0, 0)
    assert "UNSUPPORTED:" not in out
    assert "read-only; nothing was changed" in out
    assert cli.checksum() == before_db and files(cli.data_dir) == before_files


def test_cli_replay_of_pipeline_data_reports_eia_confirmations_as_unsupported(
    cli: Stored, capsys: pytest.CaptureFixture[str]
) -> None:
    before_db, before_files = cli.checksum(), files(cli.data_dir)
    assert main(["verify", "--replay"]) == 3
    out = capsys.readouterr().out
    fred, eia, later = (
        source_versions(cli, "fred"),
        source_versions(cli, "eia"),
        eia_confirmed_later(cli),
    )
    assert row(out, "replay_fred") == ["replay_fred", "ok", "0", "0", "0", "0", str(fred)]
    assert row(out, "replay_eia") == [
        "replay_eia", "UNSUPPORTED", "0", "0", "0", str(later), str(eia - later)
    ]  # fmt: skip
    assert replay_line(out) == replay_summary(fred + eia - later, 0, 0, later)
    assert "  UNSUPPORTED: creation and source page matched" in out
    assert "confirmation history is not verified" in out
    assert cli.checksum() == before_db and files(cli.data_dir) == before_files


def test_cli_replay_unverified_only_shows_correct_totals(
    cli: Stored, capsys: pytest.CaptureFixture[str]
) -> None:
    (page,) = fred_observation_pages(cli, "ust_10y")
    raw_file(cli, page).unlink()
    assert main(["verify", "--replay", "--series", *FRED_ONLY]) == 3
    out = capsys.readouterr().out
    assert row(out, "replay_fred")[:6] == ["replay_fred", "UNVERIFIED", "0", "0", "3", "0"]
    assert "(1 of 1 finding(s) shown)" in out  # not "of 0": unverified findings count
    assert "unverified: run" in out and "is missing" in out
    fred = source_versions(cli, "fred")
    assert replay_line(out) == replay_summary(fred - 3, 0, 3, 0)
    assert "UNSUPPORTED:" not in out


def test_cli_replay_unsupported_only_exits_3(
    cli: Stored, capsys: pytest.CaptureFixture[str]
) -> None:
    confirm_later(cli, cli.first_version("ust_10y"))
    assert main(["verify", "--replay", "--series", *FRED_ONLY]) == 3
    out = capsys.readouterr().out
    assert row(out, "replay_fred")[:6] == ["replay_fred", "UNSUPPORTED", "0", "0", "0", "1"]
    assert "unsupported: run" in out and "cannot be replayed" in out
    assert replay_line(out) == replay_summary(source_versions(cli, "fred") - 1, 0, 0, 1)
    assert "  UNSUPPORTED: creation and source page matched" in out


def test_cli_replay_errors_and_series_scope(
    cli: Stored, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.sql(
        "UPDATE obs.observations SET value = value + 1 WHERE id = :id",
        id=cli.first_version(STORAGE),
    )
    assert main(["verify", "--replay"]) == 1
    out = capsys.readouterr().out
    assert row(out, "replay_eia")[:3] == ["replay_eia", "FAIL", "1"]
    assert "ERROR 1," in out
    assert main(["verify"]) == 0  # without --replay the SQL checks cannot see a changed value
    capsys.readouterr()
    assert main(["verify", "--replay", "--series", "ust_10y"]) == 0  # the error is elsewhere


def test_cli_replay_with_a_missing_data_directory_is_unverified(
    cli: Stored, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-dir"
    monkeypatch.setenv("MARKETPULSE_DATA_DIR", str(missing))
    get_settings.cache_clear()
    assert main(["verify", "--replay"]) == 3
    out = capsys.readouterr().out
    assert "does not exist" in out
    assert not missing.exists()  # replay never creates anything
    assert "VERIFIED 0, ERROR 0" in out


def test_cli_help_documents_replay_and_exit_code_3(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["verify", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--replay" in out
    assert (
        "3 no errors or warnings, but some replayed versions are UNVERIFIED or UNSUPPORTED" in out
    )


@pytest.mark.parametrize(
    ("add_warning", "add_error", "exit_code"),
    [(False, False, 3), (True, False, 2), (True, True, 1), (False, True, 1)],
    ids=["unverified+unsupported", "+warning", "+warning+error", "+error"],
)
def test_cli_mixed_results_follow_exit_code_precedence(
    cli: Stored,
    capsys: pytest.CaptureFixture[str],
    add_warning: bool,
    add_error: bool,
    exit_code: int,
) -> None:
    """Every state at once: each replayed version is still counted once, and the exit code is
    the highest-precedence one: 1 error > 2 warning > 3 unverified/unsupported > 0."""
    (page,) = fred_observation_pages(cli, "ust_10y")
    raw_file(cli, page).unlink()  # 3 FRED versions UNVERIFIED
    confirm_later(cli, cli.first_version("real_gdp"))  # 1 FRED version UNSUPPORTED
    errors, unsupported = 0, 1
    if add_warning:  # a later-run closure of a current version: a warning, and UNSUPPORTED
        current = cli.scalar(
            "SELECT max(o.id) FROM obs.observations o JOIN ref.series s ON s.id = o.series_id "
            "WHERE s.code = 'real_gdp' AND o.vintage_end IS NULL"
        )
        cli.sql(
            "UPDATE obs.observations SET vintage_end = "
            "((vintage_start AT TIME ZONE 'America/Chicago') + interval '30 days') AT TIME ZONE "
            "'America/Chicago' WHERE id = :id",
            id=current,
        )
        unsupported += 1
    if add_error:  # an EIA value that its cited page contradicts
        version = cli.scalar(
            "SELECT min(o.id) FROM obs.observations o JOIN ref.series s ON s.id = o.series_id "
            "WHERE s.code = :code AND o.last_confirmed_at = o.first_retrieved_at",
            code=STORAGE,
        )
        cli.sql("UPDATE obs.observations SET value = value + 1 WHERE id = :id", id=version)
        errors += 1
    before_db = cli.checksum()

    assert main(["verify", "--replay"]) == exit_code
    out = capsys.readouterr().out
    report = cli.verify(raw_store=RawPayloadStore(cli.data_dir))
    assert report.exit_code == exit_code
    assert_each_version_counted_once(cli, report)
    later = eia_confirmed_later(cli)
    total = source_versions(cli, "fred") + source_versions(cli, "eia")
    assert (report.warnings > 0) == add_warning
    unsupported += later
    assert replay_line(out) == replay_summary(
        total - errors - 3 - unsupported, errors, 3, unsupported
    )
    assert cli.checksum() == before_db
