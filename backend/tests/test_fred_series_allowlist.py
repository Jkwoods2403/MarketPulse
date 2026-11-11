"""The FRED series allowlist: a code constant that a catalog or the environment cannot widen.

FRED is now in the source block list (tests/test_fred_source_block.py), which refuses first. The
allowlist stays as a second, independent layer; tests that go through the job or the CLI lift
the source block *in the test only* (`source_block_lifted`) to show that the allowlist would
still refuse on its own.
"""

from typing import Any, cast

import pytest

import marketpulse.ingestion.fred as fred_job
from marketpulse.cli import main
from marketpulse.ingestion.fred import (
    ALLOWED_FRED_SERIES,
    FredSeriesNotAllowedError,
    ingest_fred,
    require_series_allowed,
    select_series,
)
from marketpulse.sources.catalog import Catalog, SeriesSpec, load_catalog
from marketpulse.sources.catalog.schema import License
from source_block import lift_fred_block

CATALOG = load_catalog()
SPECS = {spec.code: spec for spec in CATALOG.series_for("fred")}


def replace_series(catalog: Catalog, code: str, **update: object) -> Catalog:
    series = tuple(
        (source, spec.model_copy(update=update) if spec.code == code else spec)
        for source, spec in catalog.series
    )
    return Catalog(sources=catalog.sources, series=series)


def unlisted_spec() -> SeriesSpec:
    return SPECS["ust_10y"].model_copy(update={"code": "sp500", "source_series_id": "SP500"})


def with_unlisted_series() -> Catalog:
    return Catalog(sources=CATALOG.sources, series=(*CATALOG.series, ("fred", unlisted_spec())))


@pytest.fixture
def source_block_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST ONLY: lift the FRED source block in this process to reach the allowlist layer."""
    lift_fred_block(monkeypatch)


def test_allowlist_matches_the_reviewed_catalog_exactly() -> None:
    assert dict(ALLOWED_FRED_SERIES) == {c: s.source_series_id for c, s in SPECS.items()}
    assert len(ALLOWED_FRED_SERIES) == 12


def test_allowlist_cannot_be_changed_at_runtime() -> None:
    with pytest.raises(TypeError):
        cast(Any, ALLOWED_FRED_SERIES)["sp500"] = "SP500"


def test_every_catalog_series_is_selected_by_default() -> None:
    assert [s.code for s in select_series(CATALOG, None)] == list(SPECS)


def test_a_series_added_to_the_catalog_stops_a_default_job() -> None:
    with pytest.raises(FredSeriesNotAllowedError, match="sp500"):
        select_series(with_unlisted_series(), None)


def test_a_series_added_to_the_catalog_cannot_be_requested_by_code() -> None:
    with pytest.raises(FredSeriesNotAllowedError, match="not on the allowlist"):
        select_series(with_unlisted_series(), ["sp500"])


def test_an_allowed_code_cannot_be_remapped_to_another_fred_series() -> None:
    remapped = replace_series(CATALOG, "ust_10y", source_series_id="SP500")
    with pytest.raises(FredSeriesNotAllowedError, match="permits only 'DGS10'"):
        select_series(remapped, ["ust_10y"])


def test_a_license_override_stops_the_series() -> None:
    restricted = replace_series(CATALOG, "ust_10y", license_override=License.PERSONAL_USE_ONLY)
    with pytest.raises(FredSeriesNotAllowedError, match="license override"):
        select_series(restricted, ["ust_10y"])


def test_the_per_series_guard_refuses_on_its_own() -> None:
    require_series_allowed(SPECS["real_gdp"])  # no error
    with pytest.raises(FredSeriesNotAllowedError):
        require_series_allowed(unlisted_spec())


@pytest.mark.usefixtures("source_block_lifted")
def test_the_guard_runs_before_any_database_or_provider_access() -> None:
    def no_client(_hook: object) -> Any:
        raise AssertionError("a client must not be created")

    with pytest.raises(FredSeriesNotAllowedError):
        ingest_fred(
            settings=cast(Any, None),
            session_factory=cast(Any, None),
            catalog=with_unlisted_series(),
            client_factory=no_client,
        )


def test_the_environment_cannot_widen_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ALLOWED_FRED_SERIES", "FRED_ALLOWED_SERIES", "MARKETPULSE_ALLOW_ALL_FRED"):
        monkeypatch.setenv(name, "sp500")
    with pytest.raises(FredSeriesNotAllowedError):
        select_series(with_unlisted_series(), None)


@pytest.mark.parametrize(
    "args",
    [
        ["ingest", "fred", "--series", "sp500"],
        ["ingest", "fred", "--series", "ust_10y", "sp500"],
        ["ingest", "fred", "--series", "sp500", "--full"],
    ],
)
@pytest.mark.usefixtures("source_block_lifted")
def test_cli_arguments_cannot_request_an_unlisted_series(
    args: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fred_job, "load_catalog", with_unlisted_series)
    monkeypatch.setenv("FRED_API_KEY", "synthetic-test-key")

    assert main(args) == 1
    assert "not on the allowlist" in capsys.readouterr().out


@pytest.mark.usefixtures("source_block_lifted")
def test_the_cli_exits_1_when_a_catalog_series_is_not_allowed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fred_job, "load_catalog", with_unlisted_series)
    monkeypatch.setenv("FRED_API_KEY", "synthetic-test-key")

    assert main(["ingest", "fred"]) == 1
    assert "not on the allowlist" in capsys.readouterr().out
