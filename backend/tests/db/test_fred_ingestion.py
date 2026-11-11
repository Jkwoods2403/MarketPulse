"""End-to-end FRED ingestion: simulated FRED API -> FredClient -> raw payloads -> PostgreSQL.

The FRED API is simulated (tests/fred_sim.py); everything else is the real pipeline: catalog,
client, raw payload store on disk, ops.raw_payloads, the versioned writer, and run tracking.
GDPC1 uses the real recorded revision history; other data is synthetic.

FRED is retired and in the source block list. The job is kept, unreachable; these tests lift the
block for their own process only (tests/source_block.py) to keep the dormant code tested.
"""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import marketpulse.ingestion.fred as fred_job
import marketpulse.ingestion.runs as runs_module
from fred_mock import TEST_KEY, UnlimitedLimiter, fixture
from fred_sim import FakeFred, SimRow, SimSeries, metadata_for
from marketpulse.cli import main
from marketpulse.core.config import Settings, get_settings
from marketpulse.db.models import (
    DataQualityIssue,
    IngestionRun,
    Observation,
    RawPayload,
    Series,
)
from marketpulse.ingestion.catalog_sync import sync_catalog
from marketpulse.ingestion.fred import (
    FredIngestionConfigError,
    FredIngestionSummary,
    ingest_fred,
)
from marketpulse.ingestion.raw_store import RawPayloadStore, StoredPayload
from marketpulse.ingestion.runs import IngestionRunTracker
from marketpulse.sources.catalog import load_catalog
from marketpulse.sources.fred.client import FredClient, ResponseHook
from marketpulse.sources.fred.parse import parse_observations
from marketpulse.sources.fred.versions import vintage_instant
from marketpulse.sources.http import RetryPolicy
from source_block import lift_fred_block

CATALOG = load_catalog()
SPECS = {s.code: s for s in CATALOG.series_for("fred")}


def gdpc1_rows() -> list[SimRow]:
    """The real GDPC1 2024Q1-Q2 revision history recorded from FRED."""
    pages = [parse_observations(fixture(f"observations_gdpc1_2024q1q2_page{n}")) for n in (1, 2)]
    return [
        SimRow(
            o.observation_date.isoformat(),
            o.realtime_start.isoformat(),
            o.realtime_end.isoformat(),
            "." if o.value is None else str(o.value),
        )
        for p in pages
        for o in p.observations
    ]


def dgs10_rows() -> list[SimRow]:
    """Synthetic daily yields around a holiday, including FRED's "." missing value."""
    return [
        SimRow("2025-12-24", "2025-12-26", "9999-12-31", "4.15"),
        SimRow("2025-12-25", "2025-12-26", "9999-12-31", "."),
        SimRow("2025-12-26", "2025-12-29", "9999-12-31", "4.14"),
    ]


@dataclass
class Env:
    factory: sessionmaker[Session]
    fake: FakeFred
    settings: Settings
    data_dir: Path
    client_factory: Callable[[ResponseHook], FredClient]

    def run(self, *codes: str, full: bool = False) -> FredIngestionSummary:
        return ingest_fred(
            settings=self.settings,
            session_factory=self.factory,
            series_codes=list(codes) or None,
            full=full,
            client_factory=self.client_factory,
        )

    def series_id(self, code: str) -> int:
        with self.factory() as s:
            return s.scalars(select(Series.id).where(Series.code == code)).one()

    def observations(self, code: str) -> list[Observation]:
        with self.factory() as s:
            return list(
                s.scalars(
                    select(Observation)
                    .where(Observation.series_id == self.series_id(code))
                    .order_by(Observation.period_start, Observation.vintage_start)
                )
            )

    def runs(self) -> list[IngestionRun]:
        with self.factory() as s:
            return list(s.scalars(select(IngestionRun).order_by(IngestionRun.id)))

    def raw_payloads(self, run_id: int) -> list[RawPayload]:
        with self.factory() as s:
            return list(
                s.scalars(
                    select(RawPayload)
                    .where(RawPayload.ingestion_run_id == run_id)
                    .order_by(RawPayload.id)
                )
            )

    def issues(self, run_id: int) -> list[DataQualityIssue]:
        with self.factory() as s:
            return list(
                s.scalars(
                    select(DataQualityIssue).where(DataQualityIssue.ingestion_run_id == run_id)
                )
            )


def make_client_factory(fake: FakeFred) -> Callable[[ResponseHook], FredClient]:
    def build(hook: ResponseHook) -> FredClient:
        return FredClient(
            SecretStr(TEST_KEY),
            on_response=hook,
            transport=fake.transport(),
            rate_limiter=UnlimitedLimiter(),
            retry=RetryPolicy(max_attempts=2),
            sleep=lambda _: None,
        )

    return build


@pytest.fixture
def env(factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    # TEST ONLY: FRED is blocked; these tests exercise the dormant job behind the block. The
    # block itself, against this database, is tested in tests/db/test_source_guard_writes.py.
    lift_fred_block(monkeypatch)
    with factory() as s, s.begin():
        sync_catalog(s, CATALOG)
    fake = FakeFred()
    fake.series["DGS10"] = SimSeries(metadata_for(SPECS["ust_10y"]), dgs10_rows())
    fake.series["GDPC1"] = SimSeries(metadata_for(SPECS["real_gdp"]), gdpc1_rows())
    settings = Settings(
        _env_file=None,
        fred_api_key=SecretStr(TEST_KEY),  # type: ignore[call-arg]
        marketpulse_data_dir=tmp_path,
    )
    return Env(factory, fake, settings, tmp_path, make_client_factory(fake))


# --- successful ingestion ----------------------------------------------------------------------


def test_full_ingestion_from_raw_responses_to_postgres(env: Env) -> None:
    summary = env.run("ust_10y", "real_gdp")

    assert summary.exit_code == 0
    assert [(o.code, o.mode, o.status) for o in summary.outcomes] == [
        ("ust_10y", "full", "succeeded"),
        ("real_gdp", "full", "succeeded"),
    ]

    gdp = env.observations("real_gdp")
    assert len(gdp) == 11
    q1 = [o for o in gdp if o.period_start == date(2024, 1, 1)]
    assert [str(o.value) for o in q1][:2] == ["22768.866", "22749.846"]  # exact FRED values
    assert q1[0].vintage_start == vintage_instant(date(2024, 4, 25))
    assert sum(o.vintage_end is None for o in q1) == 1

    runs = env.runs()
    assert [(r.job, r.status) for r in runs] == [("fred.observations", "succeeded")] * 2
    gdp_run = runs[1]
    assert (
        gdp_run.rows_fetched,
        gdp_run.rows_inserted,
        gdp_run.rows_revised,
        gdp_run.rows_unchanged,
    ) == (11, 11, 0, 0)
    assert gdp_run.params["series_code"] == "real_gdp"
    assert gdp_run.params["fred_series_id"] == "GDPC1"
    assert gdp_run.params["mode"] == "full" and gdp_run.params["windows"] == 1
    assert gdp_run.params["source_last_updated"] == "2026-10-30T15:16:46-05:00"
    assert all(o.ingestion_run_id == gdp_run.id for o in gdp)


def test_missing_source_values_are_stored_as_missing(env: Env) -> None:
    env.run("ust_10y")
    by_day = {o.period_start: o for o in env.observations("ust_10y")}
    assert len(by_day) == 3
    assert by_day[date(2025, 12, 25)].value is None
    assert by_day[date(2025, 12, 25)].status == "missing_at_source"
    assert by_day[date(2025, 12, 24)].value == Decimal("4.15")


def test_every_raw_response_is_preserved_with_provenance(env: Env) -> None:
    env.run("real_gdp")
    (run,) = env.runs()
    payloads = env.raw_payloads(run.id)

    paths = [p.file_path.split("/")[2] for p in payloads]  # raw/fred/<dataset>/...
    assert paths == ["series", "vintagedates", "observations"]
    store = RawPayloadStore(env.data_dir)
    for p in payloads:
        assert p.http_status == 200
        assert "api_key=REDACTED" in p.request_url and TEST_KEY not in p.request_url
        content = store.load(StoredPayload(p.file_path, p.sha256, p.size_bytes))  # hash-verified
        assert len(content) == p.size_bytes
    obs_payload = payloads[-1]
    assert b'"22768.866"' in store.load(
        StoredPayload(obs_payload.file_path, obs_payload.sha256, obs_payload.size_bytes)
    )
    # Observations record when they were retrieved: the observation response's time.
    assert {o.first_retrieved_at for o in env.observations("real_gdp")} == {
        obs_payload.retrieved_at
    }
    assert run.params["raw_payloads"] == 3


# --- repeat and revision -----------------------------------------------------------------------


def test_repeat_ingestion_is_incremental_and_adds_nothing(env: Env) -> None:
    env.run("real_gdp")
    before = len(env.observations("real_gdp"))
    second = env.run("real_gdp").outcomes[0]

    assert (second.mode, second.status, second.inserted, second.revised) == (
        "incremental",
        "succeeded",
        0,
        0,
    )
    assert second.unchanged > 0
    assert len(env.observations("real_gdp")) == before
    latest = max(r.realtime_start for r in gdpc1_rows())
    obs_requests = env.fake.requests_for("GDPC1", "/observations")
    assert obs_requests[-1].url.params["realtime_start"] == latest  # starts at newest vintage


def test_revised_data_flows_through_the_pipeline(env: Env) -> None:
    env.run("real_gdp")
    env.fake.series["GDPC1"].revise(
        "2024-01-01", "2026-12-23", "23200.125", day_before="2026-12-22"
    )
    outcome = env.run("real_gdp").outcomes[0]

    assert (outcome.status, outcome.inserted, outcome.revised) == ("succeeded", 1, 1)
    q1 = [o for o in env.observations("real_gdp") if o.period_start == date(2024, 1, 1)]
    assert len(q1) == 7  # full history kept
    old, new = q1[-2], q1[-1]
    assert (old.value, old.vintage_end) == (
        Decimal("23115.975"),
        vintage_instant(date(2026, 12, 23)),
    )
    assert (new.value, new.vintage_end) == (Decimal("23200.125"), None)


def test_full_refetch_after_data_exists_changes_nothing(env: Env) -> None:
    env.run("real_gdp")
    outcome = env.run("real_gdp", full=True).outcomes[0]
    assert (outcome.mode, outcome.inserted, outcome.unchanged) == ("full", 0, 11)


def test_changed_historical_value_is_flagged_and_run_is_partial(env: Env) -> None:
    env.run("real_gdp")
    first = next(r for r in env.fake.series["GDPC1"].rows if r.realtime_start == "2024-04-25")
    first.value = "99999.999"  # the provider rewrites an already-published vintage
    summary = env.run("real_gdp", full=True)

    assert summary.outcomes[0].status == "partial" and summary.exit_code == 2
    assert Decimal("99999.999") not in {o.value for o in env.observations("real_gdp")}
    assert [i.check_name for i in env.issues(summary.outcomes[0].run_id or 0)] == ["value_conflict"]


# --- failures ----------------------------------------------------------------------------------


def test_catalog_drift_stops_the_series_before_observations(env: Env) -> None:
    env.fake.series["DGS10"].metadata["units"] = "Basis Points"
    summary = env.run("ust_10y", "real_gdp")

    drifted, ok = summary.outcomes
    assert (drifted.status, ok.status, summary.exit_code) == ("failed", "succeeded", 1)
    assert (
        drifted.error and "CatalogDriftError" in drifted.error and "Basis Points" in drifted.error
    )
    assert env.observations("ust_10y") == []
    assert env.fake.requests_for("DGS10", "/observations") == []  # never fetched
    issues = env.issues(drifted.run_id or 0)
    assert [(i.check_name, i.severity) for i in issues] == [("catalog_drift", "error")]
    assert len(env.raw_payloads(drifted.run_id or 0)) == 1  # the metadata response is kept
    assert len(env.observations("real_gdp")) == 11


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (lambda r: httpx.Response(503, content=b"unavailable"), "HTTP 503"),
        (
            lambda r: httpx.Response(
                400,
                content=b'{"error_code":400,"error_message":'
                b'"Bad Request.  The series does not exist."}',
            ),
            "The series does not exist.",
        ),
    ],
)
def test_provider_failure_is_recorded_and_other_series_continue(
    env: Env, failure: Callable[[httpx.Request], httpx.Response], expected: str
) -> None:
    env.fake.failures["DGS10"] = failure
    summary = env.run("ust_10y", "real_gdp")

    failed, ok = summary.outcomes
    assert failed.status == "failed" and failed.error and expected in failed.error
    assert ok.status == "succeeded" and len(env.observations("real_gdp")) == 11
    run = next(r for r in env.runs() if r.id == failed.run_id)
    assert run.status == "failed" and run.finished_at is not None
    assert run.error_message and expected in run.error_message
    assert (run.rows_inserted, run.rows_revised) == (0, 0)


def test_failure_after_some_responses_writes_no_observations(env: Env) -> None:
    def fail_observations(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/observations"):
            return httpx.Response(500, content=b"boom")
        del env.fake.failures["DGS10"]  # let metadata/vintagedates through this time
        try:
            return env.fake.handle(request)
        finally:
            env.fake.failures["DGS10"] = fail_observations

    env.fake.failures["DGS10"] = fail_observations
    outcome = env.run("ust_10y").outcomes[0]

    assert outcome.status == "failed"
    assert env.observations("ust_10y") == []  # all-or-nothing per series
    datasets = [p.file_path.split("/")[2] for p in env.raw_payloads(outcome.run_id or 0)]
    assert datasets == ["series", "vintagedates"]  # responses received before the failure


def test_existing_history_survives_a_later_failed_run(env: Env) -> None:
    env.run("real_gdp")
    before = [(o.id, o.value, o.vintage_end) for o in env.observations("real_gdp")]
    env.fake.failures["GDPC1"] = lambda r: httpx.Response(503)
    assert env.run("real_gdp").outcomes[0].status == "failed"
    assert [(o.id, o.value, o.vintage_end) for o in env.observations("real_gdp")] == before


def test_run_finalization_failure_is_never_reported_as_success(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared tracker change (D4 review): the FRED job keeps its statuses and exit codes, but a
    run whose outcome could not be recorded is no longer reported as succeeded."""
    original = runs_module.update
    calls = {"n": 0}

    def update_failing_once(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:  # the first series' final run update
            raise OperationalError("UPDATE ops.ingestion_runs", {}, Exception("connection lost"))
        return original(*args, **kwargs)

    monkeypatch.setattr(runs_module, "update", update_failing_once)
    summary = env.run("ust_10y", "real_gdp")

    unrecorded, ok = summary.outcomes
    assert summary.exit_code == 1
    assert unrecorded.status == "failed"
    assert unrecorded.error and "RunFinalizationError" in unrecorded.error
    assert "already committed" in unrecorded.error  # never presented as rolled back
    assert len(env.observations("ust_10y")) == 3  # the data transaction had committed
    by_id = {r.id: r for r in env.runs()}
    assert by_id[unrecorded.run_id or 0].status == "running"
    assert ok.status == "succeeded" and by_id[ok.run_id or 0].status == "succeeded"


# --- per-series isolation of database errors (D5.1) ----------------------------------------------


ORIGINAL_LATEST_VINTAGE_DATE = fred_job._latest_vintage_date


def break_latest_vintage_read(env: Env, monkeypatch: pytest.MonkeyPatch, *codes: str) -> None:
    """Make the stored-data read that chooses the mode fail for `codes` (connection reset)."""
    broken = {env.series_id(code) for code in codes}
    original = fred_job._latest_vintage_date

    def flaky(factory: sessionmaker[Session], series_id: int) -> date | None:
        if series_id in broken:
            raise OperationalError("SELECT max(vintage_start)", {}, Exception("connection reset"))
        return original(factory, series_id)

    monkeypatch.setattr(fred_job, "_latest_vintage_date", flaky)


def test_database_error_choosing_one_mode_does_not_stop_the_other_series(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    break_latest_vintage_read(env, monkeypatch, "ust_10y")
    summary = env.run("ust_10y", "real_gdp")

    failed, ok = summary.outcomes
    assert (failed.code, failed.status, failed.mode) == ("ust_10y", "failed", "unknown")
    assert failed.error and failed.error.startswith("OperationalError")
    assert (ok.code, ok.status, ok.mode, ok.inserted) == ("real_gdp", "succeeded", "full", 11)
    assert summary.exit_code == 1

    by_id = {r.id: r for r in env.runs()}
    run = by_id[failed.run_id or 0]  # the failure is recorded on the series' own run
    assert run.status == "failed" and run.finished_at is not None
    assert run.error_message and run.error_message.startswith("OperationalError")
    assert run.params["series_code"] == "ust_10y" and "mode" not in run.params
    assert env.raw_payloads(run.id) == [] and env.issues(run.id) == []
    assert env.fake.requests_for("DGS10", "") == []  # nothing was requested for it
    assert env.observations("ust_10y") == []

    ok_run = by_id[ok.run_id or 0]  # the next series' run records its mode as before
    assert (ok_run.params["mode"], ok_run.params["realtime_start"]) == ("full", None)
    assert len(env.observations("real_gdp")) == 11


def test_series_after_a_failed_mode_read_still_runs_incrementally(env: Env) -> None:
    env.run("ust_10y", "real_gdp")  # both stored
    latest = max(o.vintage_start for o in env.observations("real_gdp"))
    # Its own MonkeyPatch, so leaving the block undoes only the broken read: the `env`
    # fixture's test-only FRED block lift (on the test's `monkeypatch`) stays in force.
    with pytest.MonkeyPatch.context() as broken_read:
        break_latest_vintage_read(env, broken_read, "ust_10y")
        failed, ok = env.run("ust_10y", "real_gdp").outcomes
        assert (failed.status, failed.mode) == ("failed", "unknown")
        assert (ok.status, ok.mode, ok.inserted) == ("succeeded", "incremental", 0)
        ok_run = next(r for r in env.runs() if r.id == ok.run_id)
        assert (
            ok_run.params["realtime_start"]
            == latest.astimezone(fred_job.FRED_TIMEZONE).date().isoformat()
        )

    # The read works again; a retry is an ordinary new run.
    assert fred_job._latest_vintage_date is ORIGINAL_LATEST_VINTAGE_DATE
    (retry,) = env.run("ust_10y").outcomes
    assert (retry.status, retry.mode, retry.inserted) == ("succeeded", "incremental", 0)


def test_full_mode_does_not_read_stored_data(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """--full never needed the stored-data read, and still does not (behavior preserved)."""
    break_latest_vintage_read(env, monkeypatch, "ust_10y", "real_gdp")
    summary = env.run("ust_10y", "real_gdp", full=True)
    assert [(o.status, o.mode) for o in summary.outcomes] == [
        ("succeeded", "full"),
        ("succeeded", "full"),
    ]


def test_run_creation_failure_for_one_series_does_not_stop_the_other(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_enter = IngestionRunTracker.__enter__

    def enter(self: IngestionRunTracker) -> IngestionRunTracker:
        if self.params.get("series_code") == "ust_10y":
            raise OperationalError("INSERT INTO ops.ingestion_runs", {}, Exception("db down"))
        return original_enter(self)

    monkeypatch.setattr(IngestionRunTracker, "__enter__", enter)
    summary = env.run("ust_10y", "real_gdp")
    failed, ok = summary.outcomes
    assert (failed.status, failed.run_id, failed.mode) == ("failed", None, "unknown")
    assert failed.error and "db down" in failed.error
    assert ok.status == "succeeded" and len(env.observations("real_gdp")) == 11
    assert [r.params["series_code"] for r in env.runs()] == ["real_gdp"]
    assert env.fake.requests_for("DGS10", "") == []
    assert summary.exit_code == 1


# --- configuration errors stop before any request ----------------------------------------------


def test_unknown_series_code_fails_before_network(env: Env) -> None:
    with pytest.raises(FredIngestionConfigError, match="unknown FRED series code"):
        env.run("not_a_series")
    assert env.fake.requests == [] and env.runs() == []


def test_unsynced_catalog_fails_before_network(env: Env) -> None:
    with env.factory() as s, s.begin():
        s.execute(text("UPDATE ref.series SET is_active = false WHERE code = 'ust_10y'"))
    with pytest.raises(FredIngestionConfigError, match="catalog sync"):
        env.run("ust_10y")
    assert env.fake.requests == []


def test_missing_api_key_fails_before_network(env: Env) -> None:
    settings = Settings(
        _env_file=None,
        fred_api_key=None,  # type: ignore[call-arg]
        marketpulse_data_dir=env.data_dir,
    )
    with pytest.raises(FredIngestionConfigError, match="FRED_API_KEY"):
        ingest_fred(settings=settings, session_factory=env.factory, series_codes=["ust_10y"])
    assert env.fake.requests == []


# --- secrets ----------------------------------------------------------------------------------


def test_api_key_appears_nowhere(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    env.fake.failures["DGS10"] = lambda r: httpx.Response(
        403, content=f'{{"error_code":403,"error_message":"bad api_key={TEST_KEY}"}}'.encode()
    )
    with caplog.at_level(logging.DEBUG):
        env.run("ust_10y", "real_gdp")

    with env.factory() as s:
        stored_text = (
            s.scalar(
                text(
                    "SELECT string_agg(coalesce(error_message, '') || params::text, ' ') "
                    "FROM ops.ingestion_runs"
                )
            )
            or ""
        )
        stored_text += (
            s.scalar(
                text("SELECT coalesce(string_agg(request_url, ' '), '') FROM ops.raw_payloads")
            )
            or ""
        )
        stored_text += (
            s.scalar(
                text("SELECT coalesce(string_agg(detail, ' '), '') FROM ops.data_quality_issues")
            )
            or ""
        )
    assert "REDACTED" in stored_text
    assert TEST_KEY not in stored_text
    assert TEST_KEY not in caplog.text
    for path in env.data_dir.rglob("*.gz"):
        assert TEST_KEY.encode() not in path.read_bytes()


# --- CLI ---------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(env: Env, test_db_name: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    """Point the CLI at the test database, a temp data dir, a synthetic key, and the simulator."""
    monkeypatch.setenv("POSTGRES_DB", test_db_name)
    monkeypatch.setenv("MARKETPULSE_DATA_DIR", str(env.data_dir))
    monkeypatch.setenv("FRED_API_KEY", TEST_KEY)
    monkeypatch.setattr(fred_job, "default_client_factory", lambda settings: env.client_factory)
    get_settings.cache_clear()
    yield env
    get_settings.cache_clear()


def test_cli_ingests_selected_series(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ingest", "fred", "--series", "real_gdp"]) == 0
    out = capsys.readouterr().out
    assert "real_gdp" in out and "succeeded" in out and "full" in out
    assert "1 series: 1 succeeded, 0 partial, 0 failed" in out
    assert TEST_KEY not in out
    assert len(cli_env.observations("real_gdp")) == 11
    assert cli_env.observations("ust_10y") == []  # not selected


def test_cli_reports_failures_with_exit_code_1(
    cli_env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env.fake.failures["DGS10"] = lambda r: httpx.Response(503)
    assert main(["ingest", "fred", "--series", "ust_10y", "real_gdp"]) == 1
    out = capsys.readouterr().out
    assert "2 series: 1 succeeded, 0 partial, 1 failed" in out
    assert "ust_10y: ProviderHTTPError" in out
    assert TEST_KEY not in out


def test_cli_rejects_unknown_series(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ingest", "fred", "--series", "nope"]) == 1
    assert "unknown FRED series code" in capsys.readouterr().out
    assert cli_env.fake.requests == []
