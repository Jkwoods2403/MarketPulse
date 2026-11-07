"""Catalog drift checks, using real recorded FRED metadata and the real catalog."""

from dataclasses import replace

import pytest

from fred_mock import fixture
from marketpulse.sources.catalog import SeriesSpec, load_catalog
from marketpulse.sources.catalog.schema import Frequency, SeasonalAdjustment
from marketpulse.sources.fred.drift import check_catalog_drift
from marketpulse.sources.fred.parse import FredSeriesMetadata, parse_series_metadata


def spec_for(series_id: str) -> SeriesSpec:
    return next(s for s in load_catalog().series_for("fred") if s.source_series_id == series_id)


@pytest.mark.parametrize(
    ("series_id", "name"), [("GDPC1", "series_gdpc1"), ("DGS10", "series_dgs10")]
)
def test_real_catalog_matches_real_fred_metadata(series_id: str, name: str) -> None:
    report = check_catalog_drift(spec_for(series_id), parse_series_metadata(fixture(name)))
    assert report.ok
    assert report.errors == [] and report.warnings == []


def gdpc1() -> FredSeriesMetadata:
    return parse_series_metadata(fixture("series_gdpc1"))


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"units": "Millions of Chained 2017 Dollars"}, "units are"),
        ({"units": "Billions of Chained 2012 Dollars"}, "units are"),
        ({"frequency_short": "A"}, "frequency is annual at FRED but quarterly"),
        ({"frequency_short": "BW"}, "unrecognized FRED frequency code"),
        ({"seasonal_adjustment_short": "NSA"}, "seasonal adjustment is not_seasonally_adjusted"),
        ({"seasonal_adjustment_short": "SSA"}, "unrecognized FRED seasonal adjustment"),
        ({"series_id": "GDPC2"}, "FRED returned series 'GDPC2'"),
    ],
)
def test_drift_in_identity_frequency_units_or_adjustment_is_an_error(
    change: dict[str, str], match: str
) -> None:
    report = check_catalog_drift(spec_for("GDPC1"), replace(gdpc1(), **change))
    assert not report.ok
    assert any(match in e for e in report.errors)


def test_title_change_is_only_a_warning() -> None:
    report = check_catalog_drift(spec_for("GDPC1"), replace(gdpc1(), title="Real GDP (renamed)"))
    assert report.ok
    assert any("title" in w for w in report.warnings)


def test_catalog_without_seasonal_adjustment_warns() -> None:
    spec = spec_for("GDPC1").model_copy(update={"seasonal_adjustment": None})
    report = check_catalog_drift(spec, gdpc1())
    assert report.ok
    assert any("records no seasonal adjustment" in w for w in report.warnings)


def test_every_catalog_frequency_and_adjustment_has_a_fred_code() -> None:
    from marketpulse.sources.fred.drift import FREQUENCY_CODES, SEASONAL_ADJUSTMENT_CODES

    # FRED publishes no sub-daily series; interval frequencies (ERCOT) have no FRED code, and a
    # FRED series wrongly catalogued with one fails the drift check as a frequency mismatch.
    interval = {Frequency.HOURLY, Frequency.FIFTEEN_MINUTE}
    assert set(FREQUENCY_CODES.values()) == set(Frequency) - interval
    assert set(SEASONAL_ADJUSTMENT_CODES.values()) == set(SeasonalAdjustment)


@pytest.mark.parametrize("frequency", [Frequency.HOURLY, Frequency.FIFTEEN_MINUTE])
def test_a_fred_series_catalogued_with_an_interval_frequency_is_drift(
    frequency: Frequency,
) -> None:
    spec = spec_for("GDPC1").model_copy(update={"frequency": frequency})
    report = check_catalog_drift(spec, gdpc1())
    assert not report.ok
    assert any(f"frequency is quarterly at FRED but {frequency.value}" in e for e in report.errors)
