"""Catalog loading and validation. Uses temporary catalogs for failure cases."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from marketpulse.cli import main
from marketpulse.db import enums as db_enums
from marketpulse.sources.catalog import CatalogError, load_catalog, schema

# The series IDs verified against official provider pages on 2026-11-01. Changing the catalog's
# identifiers must be a deliberate, reviewed act, so this list is pinned here.
VERIFIED_IDS = {
    "eia": {
        "PET.RWTC.D", "PET.RBRTE.D", "NG.RNGWHHD.D", "PET.WCESTUS1.W", "PET.WCSSTUS1.W",
        "PET.WCRFPUS2.W", "PET.WPULEUS3.W", "PET.WGTSTUS1.W", "PET.WDISTUS1.W",
        "NG.NW2_EPG0_SWO_R48_BCF.W", "NG.N9070US2.M", "NG.N9133US2.M",
    },
    "fred": {
        "DGS3MO", "DGS2", "DGS10", "DGS30", "T10Y2Y", "DFF",
        "DTWEXBGS", "CPIAUCSL", "PAYEMS", "UNRATE", "GDPC1", "INDPRO",
    },
}  # fmt: skip


# --- the real catalog ------------------------------------------------------------------------


def test_real_catalog_is_valid_and_complete() -> None:
    catalog = load_catalog()
    assert set(catalog.sources) == {"eia", "fred"}
    for source, ids in VERIFIED_IDS.items():
        assert {s.source_series_id for s in catalog.series_for(source)} == ids


def test_real_catalog_every_series_has_provenance() -> None:
    for _, spec in load_catalog().series:
        assert spec.original_source
        assert spec.reference_url.startswith("https://")
        assert spec.units


REFINITIV_SPOT_SERIES = {"wti_spot", "brent_spot", "henry_hub_spot"}


def test_third_party_spot_prices_are_personal_use_only() -> None:
    """Refinitiv data republished by EIA: attribution alone does not grant redistribution
    rights, so these stay personal_use_only until licensing is confirmed (ADR-0008)."""
    catalog = load_catalog()
    eia = {s.code: s for s in catalog.series_for("eia")}

    refinitiv = {code for code, s in eia.items() if "Refinitiv" in s.original_source}
    assert refinitiv == REFINITIV_SPOT_SERIES

    for code in REFINITIV_SPOT_SERIES:
        spec = eia[code]
        assert spec.original_source == "Refinitiv, an LSEG business"
        assert spec.license_override == schema.License.PERSONAL_USE_ONLY
        assert spec.notes and "redistribution" in spec.notes


def test_only_third_party_series_override_their_source_license() -> None:
    overridden = {s.code for _, s in load_catalog().series if s.license_override is not None}
    assert overridden == REFINITIV_SPOT_SERIES


def test_every_eia_series_has_verified_api_metadata() -> None:
    for spec in load_catalog().series_for("eia"):
        assert spec.api_route and not spec.api_route.startswith("/")
        assert spec.api_description and spec.api_units


def test_eia_api_units_are_the_raw_api_codes_not_display_units() -> None:
    eia = {s.code: s for s in load_catalog().series_for("eia")}
    assert (eia["wti_spot"].units, eia["wti_spot"].api_units) == ("Dollars per Barrel", "$/BBL")
    assert eia["us_refinery_utilization"].api_units == "%"


def test_catalog_vocabularies_match_database_check_constraints() -> None:
    assert {m.value for m in schema.License} == {m.value for m in db_enums.License}
    assert {m.value for m in schema.Frequency} == {m.value for m in db_enums.Frequency}


def test_cli_validate_reports_counts(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["catalog", "validate"]) == 0
    assert "2 sources, 24 series" in capsys.readouterr().out


# --- temporary catalogs for failure cases ---------------------------------------------------

SOURCE: dict[str, Any] = {
    "code": "eia",
    "name": "U.S. Energy Information Administration",
    "homepage_url": "https://www.eia.gov/",
    "terms_url": "https://www.eia.gov/about/copyrights_reuse.php",
    "license": "public_domain",
    "attribution_text": "Source: EIA",
    "reference_hosts": ["eia.gov"],
}
FRED_SOURCE: dict[str, Any] = {
    **SOURCE,
    "code": "fred",
    "name": "FRED",
    "homepage_url": "https://fred.stlouisfed.org/",
    "terms_url": "https://fred.stlouisfed.org/legal/",
    "license": "attribution_required",
    "reference_hosts": ["fred.stlouisfed.org"],
}
SERIES: dict[str, Any] = {
    "code": "wti_spot",
    "source_series_id": "PET.RWTC.D",
    "name": "Cushing, OK WTI Spot Price FOB",
    "category": "crude_oil",
    "frequency": "daily",
    "period_convention": "business_day",
    "units": "Dollars per Barrel",
    "original_source": "Refinitiv, an LSEG business",
    "reference_url": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RWTC&f=D",
    "max_staleness_days": 14,
    "api_route": "petroleum/pri/spt",
    "api_description": "Cushing, OK WTI Spot Price FOB (Dollars per Barrel)",
    "api_units": "$/BBL",
}
EIA_API_FIELDS = ("api_route", "api_description", "api_units")
FRED_SERIES: dict[str, Any] = {
    **{k: v for k, v in SERIES.items() if k not in EIA_API_FIELDS},
    "code": "ust_10y",
    "source_series_id": "DGS10",
    "name": "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity",
    "category": "rates",
    "units": "Percent",
    "original_source": "Board of Governors of the Federal Reserve System (US)",
    "reference_url": "https://fred.stlouisfed.org/series/DGS10",
    "max_staleness_days": 5,
}


def write_catalog(
    root: Path,
    *,
    sources: list[dict[str, Any]] | None = None,
    series: dict[str, list[dict[str, Any]]] | None = None,
) -> Path:
    (root / "series").mkdir(parents=True)
    (root / "sources.yaml").write_text(yaml.safe_dump({"sources": sources or [SOURCE]}))
    for source, entries in (series if series is not None else {"eia": [SERIES]}).items():
        (root / "series" / f"{source}.yaml").write_text(
            yaml.safe_dump({"source": source, "verified_on": "2026-11-01", "series": entries})
        )
    return root


def problems_for(root: Path) -> list[str]:
    with pytest.raises(CatalogError) as info:
        load_catalog(root)
    return info.value.problems


def with_changes(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    entry = copy.deepcopy(base)
    for key, value in changes.items():
        if value is ...:
            del entry[key]
        else:
            entry[key] = value
    return entry


def test_minimal_valid_catalog_loads(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))
    assert [spec.code for _, spec in catalog.series] == ["wti_spot"]


# duplicates


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    root = write_catalog(tmp_path)
    text = (root / "series" / "eia.yaml").read_text()
    (root / "series" / "eia.yaml").write_text(text.replace("units:", "units: X\n  units:", 1))
    assert any("duplicate key 'units'" in p for p in problems_for(root))


def test_duplicate_series_code_across_files(tmp_path: Path) -> None:
    root = write_catalog(
        tmp_path,
        sources=[SOURCE, FRED_SOURCE],
        series={"eia": [SERIES], "fred": [with_changes(FRED_SERIES, code="wti_spot")]},
    )
    assert "duplicate series code 'wti_spot'" in problems_for(root)


def test_duplicate_series_id_within_source(tmp_path: Path) -> None:
    root = write_catalog(tmp_path, series={"eia": [SERIES, with_changes(SERIES, code="wti_2")]})
    assert "duplicate series ID 'PET.RWTC.D' for source 'eia'" in problems_for(root)


def test_same_series_id_in_different_sources_is_allowed(tmp_path: Path) -> None:
    root = write_catalog(
        tmp_path,
        sources=[SOURCE, FRED_SOURCE],
        series={"eia": [SERIES], "fred": [with_changes(FRED_SERIES, source_series_id="RWTC")]},
    )
    assert len(load_catalog(root).series) == 2


def test_duplicate_source_code(tmp_path: Path) -> None:
    root = write_catalog(tmp_path, sources=[SOURCE, SOURCE])
    assert "sources.yaml: duplicate source code 'eia'" in problems_for(root)


# structure


def test_unknown_source_in_series_file(tmp_path: Path) -> None:
    root = write_catalog(tmp_path, series={"fred": [FRED_SERIES]})
    assert "fred.yaml: unknown source 'fred'" in problems_for(root)


def test_series_file_name_must_match_source(tmp_path: Path) -> None:
    root = write_catalog(tmp_path)
    (root / "series" / "eia.yaml").rename(root / "series" / "energy.yaml")
    assert any("file name must match its source" in p for p in problems_for(root))


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    root = write_catalog(tmp_path, series={"eia": [with_changes(SERIES, unit="typo")]})
    assert any("series.0.unit" in p and "Extra inputs" in p for p in problems_for(root))


@pytest.mark.parametrize("field", ["name", "units", "original_source", "reference_url"])
def test_required_field_missing(tmp_path: Path, field: str) -> None:
    root = write_catalog(tmp_path, series={"eia": [with_changes(SERIES, **{field: ...})]})
    assert any(f"series.0.{field}" in p and "required" in p.lower() for p in problems_for(root))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", "WTI-Spot"),
        ("category", "crypto"),
        ("frequency", "hourly"),
        ("license_override", "free"),
        ("seasonal_adjustment", "SA"),
        ("max_staleness_days", 0),
        ("max_staleness_days", 401),
        ("name", "   "),
    ],
)
def test_invalid_field_values(tmp_path: Path, field: str, value: Any) -> None:
    root = write_catalog(tmp_path, series={"eia": [with_changes(SERIES, **{field: value})]})
    assert any(f"series.0.{field}" in p for p in problems_for(root))


def test_invalid_source_license(tmp_path: Path) -> None:
    root = write_catalog(tmp_path, sources=[with_changes(SOURCE, license="free")])
    assert any("sources.0.license" in p for p in problems_for(root))


# semantic rules


@pytest.mark.parametrize(
    ("frequency", "convention", "source_series_id"),
    [
        ("weekly", "month", "PET.WCESTUS1.W"),
        ("daily", "week_ending_friday", "PET.RWTC.D"),
        ("monthly", "quarter", "NG.N9133US2.M"),
    ],
)
def test_period_convention_must_match_frequency(
    tmp_path: Path, frequency: str, convention: str, source_series_id: str
) -> None:
    entry = with_changes(
        SERIES, frequency=frequency, period_convention=convention, source_series_id=source_series_id
    )
    root = write_catalog(tmp_path, series={"eia": [entry]})
    assert any("period_convention" in p for p in problems_for(root))


def test_eia_id_frequency_letter_must_match_frequency(tmp_path: Path) -> None:
    entry = with_changes(
        SERIES, frequency="weekly", period_convention="week_ending_friday"
    )  # ID still ends in .D
    root = write_catalog(tmp_path, series={"eia": [entry]})
    assert any("implies daily, but frequency is weekly" in p for p in problems_for(root))


@pytest.mark.parametrize("bad_id", ["RWTC", "pet.rwtc.d", "PET.RWTC", "PET.RWTC.X"])
def test_eia_id_format(tmp_path: Path, bad_id: str) -> None:
    root = write_catalog(tmp_path, series={"eia": [with_changes(SERIES, source_series_id=bad_id)]})
    assert any("must look like 'PET.RWTC.D'" in p for p in problems_for(root))


@pytest.mark.parametrize("field", EIA_API_FIELDS)
def test_eia_series_requires_verified_api_metadata(tmp_path: Path, field: str) -> None:
    root = write_catalog(tmp_path, series={"eia": [with_changes(SERIES, **{field: ...})]})
    assert any(f"EIA series must set {field}" in p for p in problems_for(root))


@pytest.mark.parametrize(
    "route",
    [
        "/petroleum/pri/spt",
        "petroleum/pri/spt/data",
        "Petroleum/Pri/Spt",
        "petroleum",
        "petroleum//spt",
        "petroleum/pri spt",
    ],
)
def test_eia_api_route_format(tmp_path: Path, route: str) -> None:
    root = write_catalog(tmp_path, series={"eia": [with_changes(SERIES, api_route=route)]})
    assert any("api_route" in p for p in problems_for(root))


def test_fred_series_do_not_need_eia_api_metadata(tmp_path: Path) -> None:
    root = write_catalog(tmp_path, sources=[FRED_SOURCE], series={"fred": [FRED_SERIES]})
    assert load_catalog(root).series


@pytest.mark.parametrize(
    "bad_id",
    ["dgs10", "DGS-10", "DGS 10", "DGS_10", "A" * 26],  # FRED: <=25 alphanumeric
)
def test_fred_id_format(tmp_path: Path, bad_id: str) -> None:
    root = write_catalog(
        tmp_path,
        sources=[FRED_SOURCE],
        series={"fred": [with_changes(FRED_SERIES, source_series_id=bad_id)]},
    )
    assert any("FRED series ID" in p for p in problems_for(root))


@pytest.mark.parametrize(
    "url",
    [
        "http://www.eia.gov/dnav/pet/hist/x.htm",  # not https
        "https://fred.stlouisfed.org/series/DGS10",  # another source's host
        "https://eia.gov.evil.example/x",  # look-alike host
    ],
)
def test_reference_url_must_be_official_https(tmp_path: Path, url: str) -> None:
    root = write_catalog(tmp_path, series={"eia": [with_changes(SERIES, reference_url=url)]})
    assert problems_for(root)


def test_reference_url_subdomain_of_official_host_is_allowed(tmp_path: Path) -> None:
    root = write_catalog(
        tmp_path, series={"eia": [with_changes(SERIES, reference_url="https://ir.eia.gov/x")]}
    )
    assert load_catalog(root).series


# files


def test_missing_sources_file(tmp_path: Path) -> None:
    root = write_catalog(tmp_path)
    (root / "sources.yaml").unlink()
    assert "missing sources.yaml" in problems_for(root)


def test_no_series_files(tmp_path: Path) -> None:
    root = write_catalog(tmp_path, series={})
    assert "no series files found in series/" in problems_for(root)


def test_empty_series_list(tmp_path: Path) -> None:
    root = write_catalog(tmp_path, series={"eia": []})
    assert any("series" in p and "at least 1" in p for p in problems_for(root))


def test_invalid_yaml_syntax(tmp_path: Path) -> None:
    root = write_catalog(tmp_path)
    (root / "series" / "eia.yaml").write_text("source: eia\nseries: [unclosed\n")
    assert any("invalid YAML" in p for p in problems_for(root))


def test_yaml_tags_cannot_construct_python_objects(tmp_path: Path) -> None:
    root = write_catalog(tmp_path)
    (root / "series" / "eia.yaml").write_text("source: !!python/object/apply:os.system ['echo']\n")
    assert any("invalid YAML" in p for p in problems_for(root))


def test_all_problems_are_reported_together(tmp_path: Path) -> None:
    root = write_catalog(
        tmp_path,
        series={
            "eia": [
                with_changes(SERIES, category="crypto"),
                with_changes(SERIES, max_staleness_days=0),
            ]
        },
    )
    assert len(problems_for(root)) >= 2
