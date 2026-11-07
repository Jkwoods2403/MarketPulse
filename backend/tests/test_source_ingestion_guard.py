"""Source-level ingestion block (ADR-0014), without a database.

ERCOT ingestion is blocked while licensing ticket SS0007218 is pending. These tests show the block
is a code constant that no environment variable, command-line flag, or catalog can lift; that the
raw payload store and the `ingest ercot` command refuse before any file, setting, database
connection, client, or network request; and that the separate series-level EIA license guard
(ADR-0008) is unchanged. Database write paths: tests/db/test_source_guard_writes.py.
"""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

import pytest

import marketpulse.core.source_access as source_access
from marketpulse.cli import main
from marketpulse.core.source_access import (
    INGESTION_BLOCKED_SOURCES,
    SourceIngestionBlockedError,
    ingestion_block_reason,
    require_source_ingestion_permitted,
)
from marketpulse.ingestion.eia import (
    BLOCKED_LICENSES,
    EiaLicenseError,
    select_series,
)
from marketpulse.ingestion.raw_store import RawPayloadStore
from marketpulse.ingestion.runs import subprocess_environment
from marketpulse.sources.catalog import load_catalog
from marketpulse.sources.catalog.schema import License
from marketpulse.sources.http import FetchResult

CATALOG = load_catalog()

# Environment variables someone might try in order to lift the block. None of them may matter.
OVERRIDE_ATTEMPTS = {
    "MARKETPULSE_ALLOW_ERCOT": "1",
    "MARKETPULSE_ERCOT_INGESTION": "enabled",
    "ERCOT_INGESTION_ENABLED": "true",
    "ERCOT_LICENSE_CLEARED": "true",
    "MARKETPULSE_INGESTION_BLOCKED_SOURCES": "",
    "MARKETPULSE_UNBLOCK_SOURCES": "ercot",
}


def fetch_result() -> FetchResult:
    return FetchResult(
        content=b'{"data": []}',  # synthetic test payload, not ERCOT data
        status_code=200,
        content_type="application/json",
        url="https://api.example.com/synthetic",
        retrieved_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
        elapsed_seconds=0.1,
        attempts=1,
    )


# --- the block itself -----------------------------------------------------------------------


def test_ercot_is_blocked_with_the_licensing_reason() -> None:
    with pytest.raises(SourceIngestionBlockedError) as excinfo:
        require_source_ingestion_permitted("ercot")
    assert excinfo.value.source_code == "ercot"
    message = str(excinfo.value)
    assert "SS0007218" in message
    assert "ADR-0014" in message
    assert "staging included" in message


@pytest.mark.parametrize("code", ["ERCOT", "Ercot", " ercot ", "ercot\n"])
def test_case_and_whitespace_variants_are_blocked(code: str) -> None:
    assert ingestion_block_reason(code) is not None
    with pytest.raises(SourceIngestionBlockedError) as excinfo:
        require_source_ingestion_permitted(code)
    assert excinfo.value.source_code == "ercot"


@pytest.mark.parametrize("code", sorted(CATALOG.sources))
def test_catalog_sources_other_than_blocked_ones_are_permitted(code: str) -> None:
    if code in INGESTION_BLOCKED_SOURCES:
        with pytest.raises(SourceIngestionBlockedError):
            require_source_ingestion_permitted(code)
    else:
        assert ingestion_block_reason(code) is None
        require_source_ingestion_permitted(code)  # does not raise


def test_block_list_is_read_only() -> None:
    assert isinstance(INGESTION_BLOCKED_SOURCES, MappingProxyType)
    with pytest.raises(TypeError):
        INGESTION_BLOCKED_SOURCES["ercot"] = "lifted"  # type: ignore[index]
    with pytest.raises(TypeError):
        del INGESTION_BLOCKED_SOURCES["ercot"]  # type: ignore[attr-defined]
    assert "ercot" in INGESTION_BLOCKED_SOURCES


def test_environment_variables_cannot_lift_the_block() -> None:
    """A fresh process started with every override attempt still blocks ERCOT."""
    script = (
        "from marketpulse.core.source_access import SourceIngestionBlockedError, "
        "require_source_ingestion_permitted\n"
        "try:\n"
        "    require_source_ingestion_permitted('ercot')\n"
        "except SourceIngestionBlockedError:\n"
        "    print('blocked')\n"
        "else:\n"
        "    print('permitted')\n"
    )
    env = {**subprocess_environment(), **OVERRIDE_ATTEMPTS}  # no credentials in the child
    done = subprocess.run(  # noqa: S603 (fixed interpreter and script)
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "blocked"


def test_block_does_not_depend_on_settings_or_the_environment() -> None:
    """The module reads no configuration at all: nothing to override."""
    text = Path(source_access.__file__).read_text(encoding="utf-8")
    for needle in ("os.environ", "getenv", "Settings", "get_settings", "argv"):
        assert needle not in text


# --- raw payload files ----------------------------------------------------------------------


@pytest.mark.parametrize("code", ["ercot", "ERCOT"])
def test_raw_payload_store_refuses_before_creating_anything(tmp_path: Path, code: str) -> None:
    with pytest.raises(SourceIngestionBlockedError):
        RawPayloadStore(tmp_path).save(code, "spp_node_zone_hub", fetch_result())
    assert list(tmp_path.rglob("*")) == []  # no directory, temp file, or payload


def test_raw_payload_store_still_saves_permitted_sources(tmp_path: Path) -> None:
    stored = RawPayloadStore(tmp_path).save("eia", "data", fetch_result())
    assert (tmp_path / stored.relative_path).is_file()


# --- `marketpulse ingest ercot` -------------------------------------------------------------


def _forbidden(what: str) -> Any:
    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError(f"`ingest ercot` must not reach {what}")

    return fail


@pytest.fixture
def nothing_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the command builds settings, a database, a run, a client, or a file."""
    monkeypatch.setattr("marketpulse.core.config.get_settings", _forbidden("settings"))
    monkeypatch.setattr("marketpulse.db.session.make_engine", _forbidden("a database engine"))
    monkeypatch.setattr(
        "marketpulse.ingestion.runs.IngestionRunTracker.__enter__", _forbidden("an ingestion run")
    )
    monkeypatch.setattr(
        "marketpulse.ingestion.raw_store.RawPayloadStore.save", _forbidden("a raw payload file")
    )
    monkeypatch.setattr(
        "marketpulse.sources.http.ProviderHttpClient.__init__", _forbidden("an HTTP client")
    )


@pytest.mark.usefixtures("nothing_reachable")
def test_cli_ingest_ercot_refuses_before_any_side_effect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["ingest", "ercot"]) == 1
    out = capsys.readouterr().out
    assert "blocked" in out
    assert "SS0007218" in out


@pytest.mark.usefixtures("nothing_reachable")
def test_cli_environment_variables_do_not_lift_the_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in OVERRIDE_ATTEMPTS.items():
        monkeypatch.setenv(name, value)
    assert main(["ingest", "ercot"]) == 1
    assert "blocked" in capsys.readouterr().out


@pytest.mark.parametrize(
    "flag", ["--force", "--allow", "--override", "--enable", "--unblock", "--full", "--series"]
)
def test_cli_ingest_ercot_has_no_options(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["ingest", "ercot", flag])
    assert excinfo.value.code == 2  # argparse: unrecognized arguments
    capsys.readouterr()


# --- the EIA series-level guard is separate and unchanged ------------------------------------


def test_eia_series_guard_is_unchanged() -> None:
    assert frozenset({License.PERSONAL_USE_ONLY}) == BLOCKED_LICENSES
    assert ingestion_block_reason("eia") is None  # EIA as a source is not blocked


def test_eia_restricted_series_are_still_refused_by_the_series_guard() -> None:
    with pytest.raises(EiaLicenseError) as excinfo:
        select_series(CATALOG, ["wti_spot"])
    assert not isinstance(excinfo.value, SourceIngestionBlockedError)


def test_eia_default_selection_still_skips_exactly_the_personal_use_only_series() -> None:
    restricted = {
        spec.code
        for spec in CATALOG.series_for("eia")
        if (spec.license_override or CATALOG.sources["eia"].license) is License.PERSONAL_USE_ONLY
    }
    selection = select_series(CATALOG, None)
    assert set(selection.skipped_restricted) == restricted
    assert restricted  # the catalog still classifies the Refinitiv spot prices
