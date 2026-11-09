"""SYNTHETIC development API and API error handling (no database).

The synthetic app serves generated fixtures only in development, labels every response, and
applies the same licensing filter as the database API.
"""

import calendar
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from marketpulse.api.main import create_app
from marketpulse.core.config import Environment, Settings
from marketpulse.dev_app import SyntheticModeError, create_synthetic_app
from marketpulse.sources.catalog import load_catalog

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
CATALOG = load_catalog()
RESTRICTED = {
    spec.code
    for source, spec in CATALOG.series
    if (spec.license_override or CATALOG.sources[source].license).value == "personal_use_only"
}


def settings(env: Environment) -> Settings:
    return Settings(_env_file=None, marketpulse_env=env)  # type: ignore[call-arg]


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_synthetic_app(settings(Environment.DEVELOPMENT), now=NOW))


@pytest.mark.parametrize("env", [Environment.TEST, Environment.STAGING, Environment.PRODUCTION])
def test_synthetic_app_refuses_outside_development(env: Environment) -> None:
    with pytest.raises(SyntheticModeError, match="only served in the development environment"):
        create_synthetic_app(settings(env), now=NOW)


def test_every_response_is_labeled_synthetic(client: TestClient) -> None:
    assert client.get("/health").json()["data_mode"] == "synthetic"
    body = client.get("/series").json()
    assert body["data_mode"] == "synthetic"
    assert body["series"]
    assert all(s["synthetic"] is True for s in body["series"])
    detail = client.get("/series/ust_10y/observations").json()
    assert detail["data_mode"] == "synthetic"
    assert detail["series"]["synthetic"] is True


def test_restricted_series_and_blocked_sources_are_not_served(client: TestClient) -> None:
    assert {"wti_spot", "brent_spot", "henry_hub_spot"} == RESTRICTED
    codes = {s["code"] for s in client.get("/series").json()["series"]}
    assert codes.isdisjoint(RESTRICTED)
    assert all(s["source"]["code"] != "ercot" for s in client.get("/series").json()["series"])
    permitted = {spec.code for _, spec in CATALOG.series} - RESTRICTED
    assert codes == permitted
    for code in sorted(RESTRICTED):
        response = client.get(f"/series/{code}/observations")
        assert response.status_code == 404
        assert response.json() == {"detail": "series not found"}


def test_synthetic_values_are_deterministic_and_obviously_not_real(client: TestClient) -> None:
    again = TestClient(create_synthetic_app(settings(Environment.DEVELOPMENT), now=NOW))
    first = {s["code"]: s["latest"]["value"] for s in client.get("/series").json()["series"]}
    second = {s["code"]: s["latest"]["value"] for s in again.get("/series").json()["series"]}
    assert first == second
    # Every series walks around 100 regardless of units (e.g. a ~100 % unemployment rate).
    assert all(80 < float(v) < 125 for v in first.values())


def test_synthetic_periods_follow_catalog_conventions(client: TestClient) -> None:
    series = {s["code"]: s for s in client.get("/series").json()["series"]}
    weekly = series["l48_gas_storage"]["latest"]
    assert weekly["period_end"] == weekly["source_period"]
    assert date.fromisoformat(weekly["period_end"]).weekday() == calendar.FRIDAY
    monthly = series["us_lng_exports"]["latest"]
    assert monthly["period_start"].endswith("-01")
    quarterly = series["real_gdp"]["latest"]
    assert "-Q" in quarterly["source_period"]


class FailingRepository:
    data_mode = "database"

    def list_series(self) -> list[object]:
        raise OperationalError("SELECT 1", {}, Exception("connection refused: secret host"))


def test_database_errors_become_503_without_details() -> None:
    app = create_app(settings(Environment.TEST), repository=FailingRepository())  # type: ignore[arg-type]
    response = TestClient(app, raise_server_exceptions=False).get("/series")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "secret host" not in response.text


# --- calculated metrics (Phase 4) ---------------------------------------------------------------


def test_synthetic_metrics_are_labeled_synthetic_and_calculated(client: TestClient) -> None:
    body = client.get("/metrics").json()
    assert body["data_mode"] == "synthetic"
    assert body["series"]
    for s in body["series"]:
        assert (s["synthetic"], s["calculated"], s["basis"]) == (True, True, "current_versions")
        for metric in s["metrics"]:
            assert metric["methodology"]
            assert metric["current"] == s["current"]


def test_synthetic_metrics_exclude_restricted_and_blocked_series(client: TestClient) -> None:
    series = client.get("/metrics").json()["series"]
    codes = {s["code"] for s in series}
    assert codes.isdisjoint(RESTRICTED)
    assert codes == {s["code"] for s in client.get("/series").json()["series"]}
    assert all(s["source"]["code"] != "ercot" for s in series)


def test_metric_plans_follow_series_semantics(client: TestClient) -> None:
    series = {s["code"]: s for s in client.get("/metrics").json()["series"]}
    kinds = {code: [m["kind"] for m in s["metrics"]] for code, s in series.items()}
    assert kinds["l48_gas_storage"] == [
        "period_change",
        "year_over_year",
        "seasonal_range",
        "trailing_extremes",
    ]
    assert kinds["us_crude_stocks_spr"] == ["period_change", "year_over_year", "trailing_extremes"]
    assert kinds["us_lng_exports"] == ["period_change", "year_over_year", "seasonal_range"]
    # Interest rates: percentage points and basis points, never a percent change of a percent.
    ten = series["ust_10y"]["metrics"][0]
    assert ten["change_units"] == "percentage points"
    assert ten["percent_change"] is None
    if ten["status"] == "ok":
        assert ten["basis_points"] is not None
    assert "seasonal_range" in {n["kind"] for n in series["ust_10y"]["not_calculated"]}
    # Monthly and quarterly series are never compared week over week.
    assert series["us_lng_exports"]["metrics"][0]["label"] == "Month over month"
    assert "week_over_week" in {n["kind"] for n in series["us_lng_exports"]["not_calculated"]}


def test_metrics_endpoint_is_not_served_by_the_synthetic_app_outside_development() -> None:
    for env in (Environment.TEST, Environment.STAGING, Environment.PRODUCTION):
        with pytest.raises(SyntheticModeError):
            create_synthetic_app(settings(env), now=NOW)
