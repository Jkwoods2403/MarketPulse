"""EIA licensing guard (ADR-0008), without a database: which series the job may select.

The guard reads the catalog's license classification (a series' `license_override`, else its
source's license). These tests use the real catalog, plus copies with one classification changed,
to show the guard follows the classification rather than a list of series codes.
"""

import pytest

from marketpulse.ingestion.eia import (
    BLOCKED_LICENSES,
    EiaIngestionConfigError,
    EiaLicenseError,
    _require_ingestion_permitted,
    select_series,
)
from marketpulse.sources.catalog import Catalog, load_catalog
from marketpulse.sources.catalog.schema import License

CATALOG = load_catalog()
EIA_SPECS = {spec.code: spec for spec in CATALOG.series_for("eia")}
# Read from the catalog classification itself, NOT from BLOCKED_LICENSES: if the guard were
# weakened, these parametrized tests must fail rather than silently receive no parameters.
RESTRICTED = sorted(
    code
    for code, spec in EIA_SPECS.items()
    if (spec.license_override or CATALOG.sources["eia"].license) is License.PERSONAL_USE_ONLY
)
UNRESTRICTED = sorted(set(EIA_SPECS) - set(RESTRICTED))


def with_series_license(catalog: Catalog, code: str, license_: License | None) -> Catalog:
    """A copy of the catalog with one EIA series' license_override changed."""
    series = tuple(
        (src, spec.model_copy(update={"license_override": license_}) if spec.code == code else spec)
        for src, spec in catalog.series
    )
    return Catalog(sources=catalog.sources, series=series)


def with_source_license(catalog: Catalog, license_: License) -> Catalog:
    """A copy of the catalog with the EIA source's license changed."""
    sources = {
        **catalog.sources,
        "eia": catalog.sources["eia"].model_copy(update={"license": license_}),
    }
    return Catalog(sources=sources, series=catalog.series)


def test_only_personal_use_only_is_blocked() -> None:
    assert frozenset({License.PERSONAL_USE_ONLY}) == BLOCKED_LICENSES


def test_catalog_classification_is_unchanged() -> None:
    # Pins the reviewed classification: exactly the three Refinitiv (LSEG) spot price series.
    assert RESTRICTED == ["brent_spot", "henry_hub_spot", "wti_spot"]
    for code in RESTRICTED:
        assert EIA_SPECS[code].license_override is License.PERSONAL_USE_ONLY
        assert EIA_SPECS[code].original_source == "Refinitiv, an LSEG business"
    assert CATALOG.sources["eia"].license is License.PUBLIC_DOMAIN
    assert len(UNRESTRICTED) == 9


@pytest.mark.parametrize("code", RESTRICTED)
def test_each_restricted_series_requested_by_code_is_refused(code: str) -> None:
    with pytest.raises(EiaLicenseError) as excinfo:
        select_series(CATALOG, [code])
    message = str(excinfo.value)
    assert f"{code} (personal_use_only; original source: Refinitiv, an LSEG business)" in message
    assert "requires appropriate licensing" in message
    assert "Remove them from the requested series" in message
    # A configuration error, so every caller that stops on configuration problems stops here too.
    assert isinstance(excinfo.value, EiaIngestionConfigError)


def test_a_mixed_request_is_refused_as_a_whole_and_names_only_the_restricted_series() -> None:
    with pytest.raises(EiaLicenseError) as excinfo:
        select_series(CATALOG, ["us_lng_exports", "wti_spot", "henry_hub_spot"])
    message = str(excinfo.value)
    assert (
        "wti_spot (personal_use_only" in message and "henry_hub_spot (personal_use_only" in message
    )
    assert "us_lng_exports" not in message and "brent_spot" not in message


def test_unknown_codes_are_still_reported_first() -> None:
    with pytest.raises(EiaIngestionConfigError, match="unknown EIA series") as excinfo:
        select_series(CATALOG, ["wti_spot", "nope"])
    assert not isinstance(excinfo.value, EiaLicenseError)


def test_unrestricted_series_are_selected_unchanged() -> None:
    selection = select_series(CATALOG, ["us_lng_exports", "l48_gas_storage", "us_lng_exports"])
    assert [s.code for s in selection.specs] == ["us_lng_exports", "l48_gas_storage"]
    assert selection.licenses == {
        "us_lng_exports": License.PUBLIC_DOMAIN,
        "l48_gas_storage": License.PUBLIC_DOMAIN,
    }
    assert selection.skipped_restricted == {}


def test_default_selection_skips_restricted_series_and_keeps_all_others() -> None:
    selection = select_series(CATALOG, None)
    assert sorted(s.code for s in selection.specs) == UNRESTRICTED
    assert selection.skipped_restricted == dict.fromkeys(RESTRICTED, License.PERSONAL_USE_ONLY)
    # Catalog order is kept for the selected series.
    assert [s.code for s in selection.specs] == [c for c in EIA_SPECS if c in UNRESTRICTED]


def test_guard_follows_a_series_override_not_a_list_of_codes() -> None:
    reclassified = with_series_license(CATALOG, "us_lng_exports", License.PERSONAL_USE_ONLY)
    with pytest.raises(EiaLicenseError, match=r"us_lng_exports \(personal_use_only"):
        select_series(reclassified, ["us_lng_exports"])
    assert "us_lng_exports" in select_series(reclassified, None).skipped_restricted

    cleared = with_series_license(CATALOG, "wti_spot", None)  # falls back to public_domain
    assert [s.code for s in select_series(cleared, ["wti_spot"]).specs] == ["wti_spot"]


def test_guard_follows_the_source_license_when_a_series_has_no_override() -> None:
    restricted_source = with_source_license(CATALOG, License.PERSONAL_USE_ONLY)
    selection = select_series(restricted_source, None)
    assert selection.specs == []
    assert sorted(selection.skipped_restricted) == sorted(EIA_SPECS)
    with pytest.raises(EiaLicenseError, match=r"us_lng_exports \(personal_use_only"):
        select_series(restricted_source, ["us_lng_exports"])


@pytest.mark.parametrize(
    "license_", [License.PUBLIC_DOMAIN, License.ATTRIBUTION_REQUIRED, License.NO_REDISTRIBUTION]
)
def test_other_licenses_are_not_blocked(license_: License) -> None:
    catalog = with_series_license(CATALOG, "us_lng_exports", license_)
    assert select_series(catalog, ["us_lng_exports"]).licenses == {"us_lng_exports": license_}


@pytest.mark.parametrize("code", RESTRICTED)
def test_per_series_guard_refuses_restricted_series(code: str) -> None:
    with pytest.raises(EiaLicenseError, match=code):
        _require_ingestion_permitted(EIA_SPECS[code], License.PERSONAL_USE_ONLY)
    _require_ingestion_permitted(EIA_SPECS[code], License.PUBLIC_DOMAIN)  # no error
