"""FRED source block (project owner's decision, 2026-11-11), without a database.

FRED is retired as a data provider: Market Pulse obtains data only directly from the original
publishing organizations. These tests show that:

- FRED is in the source block list, which no environment variable, flag, or catalog can lift;
- the FRED job and `marketpulse ingest fred` refuse before any setting, catalog, database
  connection, ingestion run, raw payload file, client, or network request;
- the data API never serves a FRED series (list, observations, or metrics), while EIA series are
  served as before;
- read-only verification and replay cannot write or contact a provider, so they are not a way
  around the block;
- the FRED client itself refuses in its constructor (so any script or helper building one,
  such as the external staging spot check, cannot reach FRED), and the retired
  fixture-recording script refuses before settings, a client, a request, or a file;
- the API's SQL filter and row check normalize source codes alike, and a row only the row check
  refuses is withheld without failing the list; `observations()` refuses FRED metadata before a
  session is opened;
- the test-only lift (tests/source_block.py) is undone after each use, and the autouse guard in
  tests/conftest.py detects one that is not;
- FRED hosts appear only under the blocked `fred` catalog source and in guarded code.

The database write paths refuse FRED as well (tests/db/test_source_guard_writes.py), and the
database API filter is exercised against stored FRED rows in tests/db/test_api_series.py.
"""

import ast
import importlib.util
import logging
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import marketpulse.core.source_access as source_access
import marketpulse.ingestion.fred as fred_job
import marketpulse.ingestion.replay as replay_module
import marketpulse.ingestion.verify as verify_module
from marketpulse.api.main import create_app
from marketpulse.api.repository import (
    SQL_TRIMMED_WHITESPACE,
    DatabaseSeriesRepository,
    is_permitted,
    normalized_source_code_sql,
    only_permitted,
)
from marketpulse.cli import main
from marketpulse.core.config import Environment, Settings
from marketpulse.core.source_access import (
    INGESTION_BLOCKED_SOURCES,
    SourceIngestionBlockedError,
    ingestion_block_reason,
    require_source_ingestion_permitted,
)
from marketpulse.db.models import Series
from marketpulse.db.models import Source as DbSource
from marketpulse.dev_app import catalog_metas, create_synthetic_app
from marketpulse.ingestion.fred import ingest_fred
from marketpulse.ingestion.raw_store import RawPayloadStore
from marketpulse.ingestion.runs import subprocess_environment
from marketpulse.sources.catalog import load_catalog
from marketpulse.sources.eia.client import EiaClient
from marketpulse.sources.fred.client import FredClient
from marketpulse.sources.http import FetchResult
from source_block import (
    REAL_BLOCK_LIST,
    assert_real_block_in_force,
    lift_fred_block,
    with_fred_block_lifted,
)

CATALOG = load_catalog()
FRED_CODES = sorted(spec.code for spec in CATALOG.series_for("fred"))
EIA_PERMITTED = sorted(
    spec.code
    for spec in CATALOG.series_for("eia")
    if (spec.license_override or CATALOG.sources["eia"].license).value != "personal_use_only"
)
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)

# Environment variables someone might try in order to lift the block. None of them may matter.
OVERRIDE_ATTEMPTS = {
    "FRED_API_KEY": "synthetic-test-key",
    "MARKETPULSE_ALLOW_FRED": "1",
    "MARKETPULSE_FRED_INGESTION": "enabled",
    "FRED_INGESTION_ENABLED": "true",
    "FRED_CONSENT_RECEIVED": "true",
    "MARKETPULSE_INGESTION_BLOCKED_SOURCES": "",
    "MARKETPULSE_UNBLOCK_SOURCES": "fred",
}


def fetch_result() -> FetchResult:
    return FetchResult(
        content=b'{"observations": []}',  # synthetic test payload, not FRED data
        status_code=200,
        content_type="application/json",
        url="https://api.example.com/synthetic",
        retrieved_at=datetime(2026, 11, 11, 12, 0, tzinfo=UTC),
        elapsed_seconds=0.1,
        attempts=1,
    )


# --- the block itself -----------------------------------------------------------------------


def test_fred_is_blocked_with_the_retirement_reason() -> None:
    assert "fred" in INGESTION_BLOCKED_SOURCES
    with pytest.raises(SourceIngestionBlockedError) as excinfo:
        require_source_ingestion_permitted("fred")
    assert excinfo.value.source_code == "fred"
    message = str(excinfo.value)
    assert "retired as a data provider" in message
    assert "original publishing organizations" in message
    assert "never served" in message


@pytest.mark.parametrize("code", ["FRED", "Fred", " fred ", "fred\n", "\tFRED"])
def test_case_and_whitespace_variants_are_blocked(code: str) -> None:
    assert ingestion_block_reason(code) is not None
    with pytest.raises(SourceIngestionBlockedError) as excinfo:
        require_source_ingestion_permitted(code)
    assert excinfo.value.source_code == "fred"


def test_the_ercot_block_is_unchanged() -> None:
    with pytest.raises(SourceIngestionBlockedError, match="SS0007218"):
        require_source_ingestion_permitted("ercot")


def test_eia_stays_permitted() -> None:
    assert ingestion_block_reason("eia") is None
    require_source_ingestion_permitted("eia")  # does not raise


def test_environment_variables_cannot_lift_the_block() -> None:
    """A fresh process started with every override attempt still blocks FRED."""
    script = (
        "from marketpulse.core.source_access import SourceIngestionBlockedError, "
        "require_source_ingestion_permitted\n"
        "try:\n"
        "    require_source_ingestion_permitted('fred')\n"
        "except SourceIngestionBlockedError:\n"
        "    print('blocked')\n"
        "else:\n"
        "    print('permitted')\n"
    )
    env = {**subprocess_environment(), **OVERRIDE_ATTEMPTS}  # no real credentials in the child
    done = subprocess.run(  # noqa: S603 (fixed interpreter and script)
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "blocked"


# --- raw payload files ----------------------------------------------------------------------


@pytest.mark.parametrize("code", ["fred", "FRED"])
def test_raw_payload_store_refuses_before_creating_anything(tmp_path: Path, code: str) -> None:
    with pytest.raises(SourceIngestionBlockedError):
        RawPayloadStore(tmp_path).save(code, "observations", fetch_result())
    assert list(tmp_path.rglob("*")) == []  # no directory, temp file, or payload


def test_raw_payload_store_still_saves_eia(tmp_path: Path) -> None:
    stored = RawPayloadStore(tmp_path).save("eia", "data", fetch_result())
    assert (tmp_path / stored.relative_path).is_file()


# --- the FRED job (any caller: CLI, scheduler, script) ----------------------------------------


def _forbidden(what: str) -> Any:
    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError(f"FRED ingestion must not reach {what}")

    return fail


def test_the_job_refuses_before_catalog_database_settings_or_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fred_job, "load_catalog", _forbidden("the catalog"))
    monkeypatch.setattr(fred_job, "_resolve_database_rows", _forbidden("the database"))
    monkeypatch.setattr(fred_job, "default_client_factory", _forbidden("a client"))
    with pytest.raises(SourceIngestionBlockedError):
        ingest_fred(
            settings=cast(Any, None),
            session_factory=cast(Any, None),
            series_codes=["ust_10y"],
            full=True,
            client_factory=_forbidden("an injected client"),
        )


def test_the_job_refuses_even_with_a_catalog_and_client_supplied() -> None:
    with pytest.raises(SourceIngestionBlockedError):
        ingest_fred(
            settings=cast(Any, None),
            session_factory=cast(Any, None),
            catalog=CATALOG,
            client_factory=_forbidden("an injected client"),
        )


# --- `marketpulse ingest fred` --------------------------------------------------------------


@pytest.fixture
def nothing_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the command builds settings, a database, a run, a client, or a file."""
    monkeypatch.setattr("marketpulse.core.config.get_settings", _forbidden("settings"))
    monkeypatch.setattr("marketpulse.db.session.make_engine", _forbidden("a database engine"))
    monkeypatch.setattr(fred_job, "ingest_fred", _forbidden("the FRED job"))
    monkeypatch.setattr(
        "marketpulse.ingestion.runs.IngestionRunTracker.__enter__", _forbidden("an ingestion run")
    )
    monkeypatch.setattr(
        "marketpulse.ingestion.raw_store.RawPayloadStore.save", _forbidden("a raw payload file")
    )
    monkeypatch.setattr(
        "marketpulse.sources.http.ProviderHttpClient.__init__", _forbidden("an HTTP client")
    )


@pytest.mark.parametrize(
    "args",
    [
        ["ingest", "fred"],
        ["ingest", "fred", "--full"],
        ["ingest", "fred", "--series", "ust_10y"],
        ["ingest", "fred", "--series", *FRED_CODES, "--full"],
        ["ingest", "fred", "--series", "sp500"],  # not even the allowlist is reached
    ],
)
@pytest.mark.usefixtures("nothing_reachable")
def test_cli_ingest_fred_refuses_before_any_side_effect(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(args) == 1
    out = capsys.readouterr().out
    assert "ingestion of source 'fred' is blocked" in out
    assert "retired as a data provider" in out
    assert "allowlist" not in out


@pytest.mark.usefixtures("nothing_reachable")
def test_cli_environment_variables_do_not_lift_the_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in OVERRIDE_ATTEMPTS.items():
        monkeypatch.setenv(name, value)
    assert main(["ingest", "fred"]) == 1
    assert "blocked" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--force", "--allow", "--override", "--unblock", "--enable"])
def test_cli_ingest_fred_has_no_override_option(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["ingest", "fred", flag])
    assert excinfo.value.code == 2  # argparse: unrecognized arguments
    capsys.readouterr()


# --- the data API never serves FRED ---------------------------------------------------------


def test_the_licensing_rule_refuses_every_fred_series() -> None:
    metas = {meta.code: meta for meta in catalog_metas(CATALOG)}
    assert FRED_CODES
    assert [code for code in FRED_CODES if is_permitted(metas[code])] == []
    assert all(is_permitted(metas[code]) for code in EIA_PERMITTED)


@pytest.fixture
def client() -> TestClient:
    """The synthetic development API over the full catalog, FRED series included as metadata."""
    settings = Settings(_env_file=None, marketpulse_env=Environment.DEVELOPMENT)  # type: ignore[call-arg]
    return TestClient(create_synthetic_app(settings, now=NOW))


def test_series_list_has_no_fred_series_and_keeps_eia(client: TestClient) -> None:
    series = client.get("/series").json()["series"]
    assert {s["source"]["code"] for s in series} == {"eia"}
    codes = {s["code"] for s in series}
    assert codes.isdisjoint(FRED_CODES)
    assert codes == set(EIA_PERMITTED)


@pytest.mark.parametrize("code", FRED_CODES)
def test_fred_observations_are_not_found(client: TestClient, code: str) -> None:
    for query in ("", "?limit=5", "?start=1900-01-01&end=2100-01-01"):
        response = client.get(f"/series/{code}/observations{query}")
        assert response.status_code == 404
        assert response.json() == {"detail": "series not found"}  # same as an unknown code


def test_eia_observations_are_still_served(client: TestClient) -> None:
    for code in EIA_PERMITTED:
        body = client.get(f"/series/{code}/observations").json()
        assert body["series"]["code"] == code
        assert body["series"]["source"]["code"] == "eia"


def test_metrics_have_no_fred_series(client: TestClient) -> None:
    series = client.get("/metrics").json()["series"]
    assert {s["code"] for s in series}.isdisjoint(FRED_CODES)
    assert {s["source"]["code"] for s in series} == {"eia"}


# --- read-only verification and replay are not a way around the block ------------------------

# Anything that would write a row or a raw file, open an ingestion run, or reach a provider.
WRITE_OR_FETCH_MODULES = (
    "marketpulse.ingestion.observation_writer",
    "marketpulse.ingestion.snapshot_writer",
    "marketpulse.ingestion.version_apply",
    "marketpulse.ingestion.runs",
    "marketpulse.ingestion.fred",
    "marketpulse.ingestion.eia",
    "marketpulse.sources.http",
    "marketpulse.sources.fred.client",
    "marketpulse.sources.eia.client",
)
WRITE_OR_FETCH_CALLS = frozenset({"save", "record_raw_payload", "add_all", "flush", "commit"})
# Ambiguous method names (`set.add`, ...) count only when called on a session.
SESSION_WRITE_CALLS = frozenset({"add", "merge", "delete", "execute_write"})


def _is_session(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and "session" in node.id


@pytest.mark.parametrize("module", [verify_module, replay_module], ids=["verify", "replay"])
def test_verification_and_replay_neither_write_nor_fetch(module: Any) -> None:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            called.add(name)
            if isinstance(func, ast.Attribute) and _is_session(func.value):
                assert name not in SESSION_WRITE_CALLS, f"session.{name}() in {module.__name__}"
    assert imported.isdisjoint(WRITE_OR_FETCH_MODULES)
    assert called.isdisjoint(WRITE_OR_FETCH_CALLS)


def test_verification_runs_in_a_read_only_transaction() -> None:
    source = Path(verify_module.__file__).read_text(encoding="utf-8")
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY" in source


# --- M1: the FRED client itself refuses -----------------------------------------------------


@pytest.fixture
def no_http_client(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Fail the test if an HTTP client is built; record any request that a transport sees."""
    monkeypatch.setattr(
        "marketpulse.sources.http.ProviderHttpClient.__init__", _forbidden("an HTTP client")
    )
    return []


def _recording_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise AssertionError("FRED must not be contacted")

    return httpx.MockTransport(handler)


def test_direct_construction_refuses_before_any_http_client_or_request(
    no_http_client: list[httpx.Request],
) -> None:
    with pytest.raises(SourceIngestionBlockedError, match="fred"):
        FredClient(SecretStr("synthetic-test-key"), transport=_recording_transport(no_http_client))
    assert no_http_client == []


def test_construction_like_the_staging_spot_check_refuses(
    no_http_client: list[httpx.Request],
) -> None:
    """MarketPulseStaging/scripts/staging_fred_spotcheck.py builds `FredClient(SecretStr(key))`
    with no other argument: the same call refuses here, before any client or request."""
    with pytest.raises(SourceIngestionBlockedError):
        FredClient(SecretStr("synthetic-test-key"))


def test_from_settings_with_a_key_refuses(no_http_client: list[httpx.Request]) -> None:
    settings = Settings(_env_file=None, fred_api_key=SecretStr("synthetic-test-key"))  # type: ignore[call-arg]
    with pytest.raises(SourceIngestionBlockedError):
        FredClient.from_settings(settings, transport=_recording_transport(no_http_client))
    assert no_http_client == []


def test_the_eia_client_is_not_affected() -> None:
    client = EiaClient(SecretStr("synthetic-test-key"), transport=_recording_transport([]))
    client.close()


# --- M2: the retired fixture-recording script -------------------------------------------------

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "record_fred_fixtures.py"
REAL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fred"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("record_fred_fixtures_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # defines functions only; main() is not called
    return module


def _fingerprint(directory: Path) -> dict[str, tuple[int, int]]:
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in directory.iterdir()}


def test_the_fixture_script_refuses_before_settings_client_request_or_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    target = tmp_path / "fixtures"
    monkeypatch.setattr(script, "FIXTURE_DIR", target)
    monkeypatch.setattr(script, "get_settings", _forbidden("settings"))
    monkeypatch.setattr(script, "ProviderHttpClient", _forbidden("an HTTP client"))
    monkeypatch.setenv("FRED_API_KEY", "synthetic-test-key")
    real_before = _fingerprint(REAL_FIXTURES)

    assert script.main() == 1

    err = capsys.readouterr().err
    assert "ingestion of source 'fred' is blocked" in err
    assert not target.exists()  # no directory, fixture, or manifest
    assert list(tmp_path.iterdir()) == []
    assert _fingerprint(REAL_FIXTURES) == real_before  # recorded fixtures kept, untouched


# --- L1: one normalization for the SQL filter and the row check -------------------------------


def test_the_sql_filter_trims_and_lowercases_source_codes() -> None:
    sql = str(
        select(DbSource.id)
        .where(normalized_source_code_sql(DbSource.code).not_in(["ercot", "fred"]))
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "lower(btrim(ref.sources.code, ' \t\n\r\x0b\x0c'))" in sql


@pytest.mark.parametrize("code", ["fred", "FRED", " fred ", "Fred\t", "\nFRED\r", "fred\x0b"])
def test_codes_the_sql_filter_normalizes_are_blocked_by_the_row_check_too(code: str) -> None:
    sql_normalized = code.strip(SQL_TRIMMED_WHITESPACE).lower()
    assert sql_normalized == "fred"  # what btrim/lower yield in PostgreSQL
    assert ingestion_block_reason(code) is not None


def _row(source_code: str, series_code: str, series_id: int) -> tuple[Series, DbSource, str]:
    source = DbSource(
        id=series_id,
        code=source_code,
        name=f"{source_code!r} (synthetic)",
        homepage_url="https://example.invalid",
        terms_url=None,
        license="public_domain",
        access_requirement="open",
        attribution_text=None,
    )
    series = Series(
        id=series_id,
        source_id=series_id,
        source_series_id=series_code.upper(),
        code=series_code,
        name="Synthetic series",
        description=None,
        category="crude_oil",
        frequency="weekly",
        period_convention="week_ending_friday",
        units="Synthetic units",
        seasonal_adjustment=None,
        original_source="Synthetic",
        reference_url="https://example.invalid/series",
        vintage_basis="first_seen",
        max_staleness=timedelta(days=14),
        is_active=True,
    )
    return series, source, "public_domain"


class _FakeSession:
    """Returns fixed metadata rows, as if the SQL filter had let them through. No database."""

    def __init__(self, rows: list[tuple[Series, DbSource, str]]) -> None:
        self.rows = rows

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, _query: object) -> list[tuple[Series, DbSource, str]]:
        return self.rows

    def scalars(self, _query: object) -> list[object]:
        return []  # no observations


def test_a_row_the_sql_filter_misses_is_withheld_without_failing_the_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A FRED code with non-ASCII whitespace passes `btrim` but not the row check: it is dropped
    and logged, and every other series is still served by /series and /metrics."""
    rows = [_row("eia", "eia_weekly_test", 1), _row("\u00a0fred\u00a0", "fred_hidden_test", 2)]
    repository = DatabaseSeriesRepository(cast(Any, lambda: _FakeSession(rows)))

    with caplog.at_level(logging.ERROR, logger="marketpulse.api.repository"):
        assert [meta.code for meta, _ in repository.list_series()] == ["eia_weekly_test"]
    assert "fred_hidden_test" in caplog.text

    settings = Settings(_env_file=None, marketpulse_env=Environment.TEST)  # type: ignore[call-arg]
    client = TestClient(create_app(settings, repository=repository, clock=lambda: NOW))
    series = client.get("/series")
    assert series.status_code == 200
    assert [s["code"] for s in series.json()["series"]] == ["eia_weekly_test"]
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert [s["code"] for s in metrics.json()["series"]] == ["eia_weekly_test"]


def test_only_permitted_drops_every_blocked_spelling() -> None:
    metas = {meta.code: meta for meta in catalog_metas(CATALOG)}
    eia = metas[EIA_PERMITTED[0]]
    fred = metas[FRED_CODES[0]]
    variants = [
        replace(fred, source=fred.source.model_copy(update={"code": code}))
        for code in ("fred", "FRED", " fred", "fred\u00a0", "\u2003Fred")
    ]
    assert only_permitted([eia, *variants]) == [eia]


# --- L2: the repository refuses FRED metadata before opening a session -------------------------


def test_observations_refuse_fred_metadata_before_a_session_is_opened() -> None:
    opened: list[bool] = []

    def factory() -> NoReturn:
        opened.append(True)
        raise AssertionError("a database session must not be opened")

    repository = DatabaseSeriesRepository(cast(Any, factory))
    fred = next(meta for meta in catalog_metas(CATALOG) if meta.code == "ust_10y")
    for as_of in (None, NOW):
        with pytest.raises(RuntimeError, match="licensing filter failed"):
            repository.observations(fred, start=None, end=None, as_of=as_of, limit=10, offset=0)
    assert opened == []


# --- L4: the test-only lift is undone; the autouse guard notices if it is not ------------------


def test_the_real_block_is_in_force_during_ordinary_tests() -> None:
    assert_real_block_in_force()
    assert source_access.INGESTION_BLOCKED_SOURCES is REAL_BLOCK_LIST


def test_the_guard_detects_a_lift_and_its_reversal() -> None:
    with pytest.MonkeyPatch.context() as patch:
        lift_fred_block(patch)
        assert ingestion_block_reason("fred") is None
        assert ingestion_block_reason("ercot") is not None  # ERCOT is never lifted
        with pytest.raises(AssertionError, match="INGESTION_BLOCKED_SOURCES was replaced"):
            assert_real_block_in_force()
    assert_real_block_in_force()
    assert ingestion_block_reason("fred") is not None


def test_the_guard_detects_a_replaced_check_function() -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(source_access, "require_source_ingestion_permitted", lambda _c: None)
        with pytest.raises(AssertionError, match="require_source_ingestion_permitted"):
            assert_real_block_in_force()
    assert_real_block_in_force()


def test_a_construction_only_lift_ends_with_the_call() -> None:
    def build() -> bool:
        return ingestion_block_reason("fred") is None

    assert with_fred_block_lifted(build) is True
    assert ingestion_block_reason("fred") is not None
    assert_real_block_in_force()


def test_a_construction_only_lift_ends_even_when_the_call_fails() -> None:
    def build() -> NoReturn:
        raise ValueError("synthetic failure")

    with pytest.raises(ValueError, match="synthetic failure"):
        with_fred_block_lifted(build)
    assert_real_block_in_force()


# --- L5: FRED hosts only under the blocked `fred` source ---------------------------------------

FRED_HOST_SUFFIX = "stlouisfed.org"


def _is_fred_host(url_or_host: str) -> bool:
    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").strip().lower().rstrip(".")
    return host == FRED_HOST_SUFFIX or host.endswith("." + FRED_HOST_SUFFIX)


def test_fred_hosts_appear_only_under_the_blocked_fred_source() -> None:
    """A catalog source (or series) pointing at the St. Louis Fed under another code would not
    be matched by the code-keyed block list; the catalog must not contain one."""
    offenders = []
    for code, source in CATALOG.sources.items():
        urls = [source.homepage_url, source.terms_url, *source.reference_hosts]
        if code != "fred" and any(_is_fred_host(u) for u in urls):
            offenders.append(f"source {code}")
    for code, spec in CATALOG.series:
        if code != "fred" and _is_fred_host(spec.reference_url):
            offenders.append(f"series {spec.code} ({code})")
    assert offenders == []
    fred = CATALOG.sources["fred"]  # the check is meaningful: FRED itself uses these hosts
    assert any(_is_fred_host(h) for h in fred.reference_hosts)
    assert ingestion_block_reason("fred") is not None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://api.stlouisfed.org/fred/", True),
        ("fred.stlouisfed.org", True),
        ("ALFRED.STLOUISFED.ORG.", True),
        ("https://www.eia.gov/", False),
        ("https://notstlouisfed.org/", False),
    ],
)
def test_the_fred_host_rule(value: str, expected: bool) -> None:
    assert _is_fred_host(value) is expected


# Code that names a FRED host, each guarded by the block before any request can be made.
FRED_HOST_CODE_ALLOWED = {
    "src/marketpulse/sources/fred/client.py": "FredClient: refuses in its constructor",
    "scripts/record_fred_fixtures.py": "retired: main() refuses before any client",
}


def test_only_guarded_code_names_a_fred_host() -> None:
    backend = Path(__file__).resolve().parents[1]
    found = {
        path.relative_to(backend).as_posix()
        for folder in ("src", "scripts")
        for path in (backend / folder).rglob("*.py")
        if FRED_HOST_SUFFIX in path.read_text(encoding="utf-8").lower()
    }
    assert found == set(FRED_HOST_CODE_ALLOWED)
    for relative in FRED_HOST_CODE_ALLOWED:
        text = (backend / relative).read_text(encoding="utf-8")
        assert 'require_source_ingestion_permitted("fred")' in text or (
            "require_source_ingestion_permitted(SOURCE_CODE)" in text
        )
