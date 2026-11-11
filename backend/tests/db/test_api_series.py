"""Read-only series API against PostgreSQL, with synthetic rows in the test database.

Restricted (personal_use_only) series, series of blocked sources (ERCOT, FRED), and inactive
series are given observations on purpose, so their absence from every response proves the
licensing filter rather than an empty table. The FRED rows stand in for the historical FRED data
in production: stored, current and revised, and never served.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from marketpulse.api.main import create_app
from marketpulse.api.repository import DatabaseSeriesRepository
from marketpulse.core.config import Environment, Settings
from marketpulse.db.models import IngestionRun, Observation, Series, Source

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

PERMITTED = {"eia_weekly_test", "vintaged_daily_test", "vintaged_empty_test"}
NOT_PERMITTED = {
    "eia_override_test",  # series license_override personal_use_only
    "vendor_series_test",  # source license personal_use_only
    "ercot_series_test",  # blocked source
    "fred_ust_10y_test",  # blocked source (FRED, retired as a provider), current version
    "fred_revised_test",  # blocked source (FRED), revised: closed and current versions
    "fred_upper_test",  # blocked source spelled "FRED" (hand-written row, not via the catalog)
    "fred_spaced_test",  # blocked source spelled " fred\t" (hand-written row)
    "eia_inactive_test",  # inactive
}


def _source(s: Session, code: str, license_: str) -> Source:
    row = Source(
        code=code,
        name=f"{code} (synthetic)",
        homepage_url="https://example.invalid",
        terms_url="https://example.invalid/terms",
        license=license_,
        access_requirement="registration_required",
        attribution_text=f"Source: {code} (synthetic)",
    )
    s.add(row)
    s.flush()
    return row


def _series(
    s: Session,
    source: Source,
    code: str,
    frequency: str,
    convention: str,
    *,
    staleness_days: int,
    license_override: str | None = None,
    is_active: bool = True,
    basis: str = "first_seen",
) -> Series:
    row = Series(
        source_id=source.id,
        source_series_id=code.upper(),
        code=code,
        name=f"Synthetic {code}",
        category="crude_oil",
        frequency=frequency,
        period_convention=convention,
        units="Synthetic units",
        original_source="Synthetic producer",
        reference_url="https://example.invalid/series",
        license_override=license_override,
        is_active=is_active,
        max_staleness=timedelta(days=staleness_days),
        vintage_basis=basis,
    )
    s.add(row)
    s.flush()
    return row


def _obs(
    s: Session,
    series: Series,
    run_id: int,
    start: date,
    end: date,
    value: str | None,
    *,
    vintage_start: datetime = T0,
    vintage_end: datetime | None = None,
) -> None:
    s.add(
        Observation(
            series_id=series.id,
            period_start=start,
            period_end=end,
            value=None if value is None else Decimal(value),
            status="missing_at_source" if value is None else "ok",
            vintage_start=vintage_start,
            vintage_end=vintage_end,
            first_retrieved_at=vintage_start,
            last_confirmed_at=T1,
            ingestion_run_id=run_id,
            source_period=end.isoformat(),
        )
    )


@pytest.fixture
def world(factory: sessionmaker[Session]) -> None:
    with factory() as s, s.begin():
        run = IngestionRun(job="test.synthetic")
        s.add(run)
        s.flush()
        eia = _source(s, "eia", "public_domain")
        # A permitted, synthetic provider with real-time vintages (not FRED).
        vintaged = _source(s, "vintage_provider_test", "attribution_required")
        fred = _source(s, "fred", "attribution_required")  # blocked: never served
        vendor = _source(s, "vendor", "personal_use_only")
        ercot = _source(s, "ercot", "no_redistribution")

        weekly = _series(
            s, eia, "eia_weekly_test", "weekly", "week_ending_friday", staleness_days=14
        )
        # Two weeks; the latest was revised (old version closed at T1).
        _obs(s, weekly, run.id, date(2026, 10, 17), date(2026, 10, 23), "421.500")
        _obs(s, weekly, run.id, date(2026, 10, 24), date(2026, 10, 30), "430.000", vintage_end=T1)
        _obs(s, weekly, run.id, date(2026, 10, 24), date(2026, 10, 30), "430.250", vintage_start=T1)

        daily = _series(
            s, vintaged, "vintaged_daily_test", "daily", "business_day", staleness_days=5,
            basis="source_vintage",
        )  # fmt: skip
        _obs(s, daily, run.id, date(2026, 10, 28), date(2026, 10, 28), "4.190")
        _obs(s, daily, run.id, date(2026, 10, 29), date(2026, 10, 29), None)  # no value

        _series(s, vintaged, "vintaged_empty_test", "monthly", "month", staleness_days=45)

        hidden = [
            _series(
                s, eia, "eia_override_test", "daily", "business_day", staleness_days=14,
                license_override="personal_use_only",
            ),
            _series(s, vendor, "vendor_series_test", "daily", "business_day", staleness_days=14),
            _series(s, ercot, "ercot_series_test", "hourly", "hour_ending", staleness_days=2),
            _series(
                s, fred, "fred_ust_10y_test", "daily", "business_day", staleness_days=5,
                basis="source_vintage",
            ),
            _series(
                s, eia, "eia_inactive_test", "weekly", "week_ending_friday", staleness_days=14,
                is_active=False,
            ),
        ]  # fmt: skip
        for series in hidden:
            _obs(s, series, run.id, date(2026, 11, 2), date(2026, 11, 2), "99.000")

        # FRED history with a revision: a closed version and its current successor.
        revised = _series(
            s, fred, "fred_revised_test", "quarterly", "quarter", staleness_days=125,
            basis="source_vintage",
        )  # fmt: skip
        _obs(s, revised, run.id, date(2026, 4, 1), date(2026, 6, 30), "100.0", vintage_end=T1)
        _obs(s, revised, run.id, date(2026, 4, 1), date(2026, 6, 30), "100.5", vintage_start=T1)

        # Hand-written source codes the catalog would reject: the SQL filter must withhold them
        # itself (lower + btrim), so the rest of the list is still served.
        for code, series_code in (("FRED", "fred_upper_test"), (" fred\t", "fred_spaced_test")):
            variant = _source(s, code, "attribution_required")
            row = _series(s, variant, series_code, "daily", "business_day", staleness_days=5)
            _obs(s, row, run.id, date(2026, 11, 2), date(2026, 11, 2), "99.000")


@pytest.fixture
def client(factory: sessionmaker[Session], world: None) -> TestClient:
    settings = Settings(_env_file=None, marketpulse_env=Environment.TEST)  # type: ignore[call-arg]
    app = create_app(settings, repository=DatabaseSeriesRepository(factory), clock=lambda: NOW)
    return TestClient(app)


def by_code(client: TestClient) -> dict[str, Any]:
    return {s["code"]: s for s in client.get("/series").json()["series"]}


# --- licensing ----------------------------------------------------------------------------------


def test_list_serves_only_permitted_series(client: TestClient) -> None:
    body = client.get("/series").json()
    assert body["data_mode"] == "database"
    assert {s["code"] for s in body["series"]} == PERMITTED
    assert all(s["synthetic"] is False for s in body["series"])
    assert all(s["license"] != "personal_use_only" for s in body["series"])
    assert {s["source"]["code"] for s in body["series"]}.isdisjoint({"ercot", "fred"})


@pytest.mark.parametrize("code", sorted(NOT_PERMITTED | {"no_such_series"}))
def test_restricted_blocked_inactive_and_unknown_codes_look_identical(
    client: TestClient, code: str
) -> None:
    for path in (
        f"/series/{code}/observations",
        f"/series/{code}/observations?as_of=2026-11-07T00:00:00Z",
        f"/series/{code}/observations?start=2000-01-01&limit=5000",
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "series not found"}


@pytest.mark.parametrize(
    "query",
    [
        "include_restricted=true",
        "license=personal_use_only",
        "source=ercot",
        "source=fred",
        "all=1",
        "override=1",
    ],
)
def test_no_query_parameter_widens_the_list(client: TestClient, query: str) -> None:
    body = client.get(f"/series?{query}").json()
    assert {s["code"] for s in body["series"]} == PERMITTED


@pytest.mark.parametrize(
    "code",
    [
        "ERCOT_SERIES_TEST",
        "Eia_Override_Test",
        "ercot_series_test ",
        "FRED_UST_10Y_TEST",
        "Fred_Revised_Test",
    ],
)
def test_code_variants_do_not_reach_hidden_series(client: TestClient, code: str) -> None:
    assert client.get(f"/series/{code}/observations").status_code == 404


def test_the_sql_filter_itself_withholds_fred_code_variants(
    factory: sessionmaker[Session], world: None, caplog: pytest.LogCaptureFixture
) -> None:
    """'FRED' and ' fred\\t' are excluded in SQL, not only by the row check (no error logged)."""
    with caplog.at_level(logging.ERROR, logger="marketpulse.api.repository"):
        listed = {meta.code for meta, _ in DatabaseSeriesRepository(factory).list_series()}
    assert listed == PERMITTED
    assert "licensing filter" not in caplog.text


def test_stored_fred_rows_are_never_served(client: TestClient) -> None:
    """FRED rows exist in the database (current and revised) and never reach any response."""
    codes = {"fred_ust_10y_test", "fred_revised_test", "fred_upper_test", "fred_spaced_test"}
    assert codes.isdisjoint(by_code(client))
    for code in sorted(codes):
        for query in ("", "?as_of=2026-11-02T00:00:00Z", "?as_of=2026-11-07T00:00:00Z"):
            assert client.get(f"/series/{code}/observations{query}").status_code == 404
    metrics = client.get("/metrics").json()["series"]
    assert {m["code"] for m in metrics}.isdisjoint(codes)
    assert {m["source"]["code"] for m in metrics}.isdisjoint({"ercot", "fred"})


def test_the_repository_never_returns_fred_metadata(
    factory: sessionmaker[Session], world: None
) -> None:
    repository = DatabaseSeriesRepository(factory)
    listed = {meta.code for meta, _ in repository.list_series()}
    assert listed == PERMITTED
    assert repository.get_series("fred_ust_10y_test") is None
    assert repository.get_series("fred_revised_test") is None


# --- metadata, latest observation, freshness ------------------------------------------------------


def test_latest_is_the_current_version_with_a_value(client: TestClient) -> None:
    series = by_code(client)
    weekly = series["eia_weekly_test"]
    assert weekly["latest"] == {
        "period_start": "2026-10-24",
        "period_end": "2026-10-30",
        "source_period": "2026-10-30",
        "value": "430.250",  # the revised value, exact decimal string
        "status": "ok",
        "vintage_start": "2026-11-06T12:00:00Z",
        "first_retrieved_at": "2026-11-06T12:00:00Z",
        "last_confirmed_at": "2026-11-06T12:00:00Z",
    }
    # The provider reported no value for Oct 29: the card shows Oct 28, the latest with a value.
    assert series["vintaged_daily_test"]["latest"]["period_end"] == "2026-10-28"
    assert series["vintaged_daily_test"]["latest"]["value"] == "4.190"


def test_freshness_uses_each_series_own_limit(client: TestClient) -> None:
    series = by_code(client)
    weekly = series["eia_weekly_test"]["freshness"]
    assert weekly == {
        "status": "fresh",
        "reference_date": "2026-11-08",
        "latest_period_end": "2026-10-30",
        "age_days": 9,
        "max_staleness_days": 14,
    }
    daily = series["vintaged_daily_test"]["freshness"]
    assert (daily["status"], daily["age_days"], daily["max_staleness_days"]) == ("stale", 11, 5)
    empty = series["vintaged_empty_test"]
    assert empty["latest"] is None
    assert empty["freshness"]["status"] == "unavailable"


def test_metadata_carries_source_attribution_and_semantics(client: TestClient) -> None:
    weekly = by_code(client)["eia_weekly_test"]
    assert weekly["source"] == {
        "code": "eia",
        "name": "eia (synthetic)",
        "attribution": "Source: eia (synthetic)",
        "terms_url": "https://example.invalid/terms",
    }
    assert (weekly["frequency"], weekly["period_convention"], weekly["units"]) == (
        "weekly",
        "week_ending_friday",
        "Synthetic units",
    )
    assert weekly["vintage_basis"] == "first_seen"
    assert weekly["license"] == "public_domain"
    assert weekly["original_source"] == "Synthetic producer"


# --- observations ----------------------------------------------------------------------------


def test_observations_are_current_versions_oldest_first(client: TestClient) -> None:
    body = client.get("/series/eia_weekly_test/observations").json()
    assert body["total"] == 2
    assert [o["period_end"] for o in body["observations"]] == ["2026-10-23", "2026-10-30"]
    assert [o["value"] for o in body["observations"]] == ["421.500", "430.250"]
    assert all(o["vintage_end"] is None for o in body["observations"])
    assert body["series"]["code"] == "eia_weekly_test"


def test_missing_at_source_is_returned_as_missing_not_filled(client: TestClient) -> None:
    body = client.get("/series/vintaged_daily_test/observations").json()
    last = body["observations"][-1]
    assert (last["period_end"], last["value"], last["status"]) == (
        "2026-10-29",
        None,
        "missing_at_source",
    )


def test_date_range_and_pagination(client: TestClient) -> None:
    ranged = client.get("/series/eia_weekly_test/observations?start=2026-10-24").json()
    assert (ranged["total"], len(ranged["observations"])) == (1, 1)
    page = client.get("/series/eia_weekly_test/observations?limit=1&offset=1").json()
    assert (page["total"], page["limit"], page["offset"]) == (2, 1, 1)
    assert [o["period_end"] for o in page["observations"]] == ["2026-10-30"]


def test_as_of_returns_the_version_known_then(client: TestClient) -> None:
    before = client.get("/series/eia_weekly_test/observations?as_of=2026-11-05T00:00:00Z").json()
    assert [o["value"] for o in before["observations"]] == ["421.500", "430.000"]
    assert before["as_of"] == "2026-11-05T00:00:00Z"


@pytest.mark.parametrize(
    "query",
    [
        "start=2026-10-30&end=2026-10-01",
        "as_of=2026-11-05T00:00:00",  # no timezone
        "limit=0",
        "limit=5001",
        "offset=-1",
    ],
)
def test_invalid_parameters_are_rejected(client: TestClient, query: str) -> None:
    assert client.get(f"/series/eia_weekly_test/observations?{query}").status_code == 422


# --- calculated metrics (Phase 4) ---------------------------------------------------------------


def metrics_by_code(client: TestClient) -> dict[str, Any]:
    body = client.get("/metrics").json()
    assert body["data_mode"] == "database"
    return {s["code"]: s for s in body["series"]}


def test_metrics_cover_only_permitted_series(client: TestClient) -> None:
    series = metrics_by_code(client)
    assert set(series) == PERMITTED
    assert all(s["source"]["code"] != "ercot" for s in series.values())


def test_metrics_use_current_versions_only(client: TestClient) -> None:
    weekly = metrics_by_code(client)["eia_weekly_test"]
    assert (weekly["calculated"], weekly["basis"], weekly["synthetic"]) == (
        True,
        "current_versions",
        False,
    )
    assert weekly["current"]["value"] == "430.250"  # revised; the closed 430.000 is never used
    wow = weekly["metrics"][0]
    assert (wow["kind"], wow["label"], wow["status"]) == ("period_change", "Week over week", "ok")
    assert wow["comparison"]["value"] == "421.500"
    assert wow["absolute_change"] == "8.750"
    assert wow["percent_change"] == "2.08"
    assert wow["change_units"] == "Synthetic units"
    yoy = weekly["metrics"][1]
    assert (yoy["kind"], yoy["status"], yoy["absolute_change"]) == (
        "year_over_year",
        "insufficient_history",
        None,
    )


def test_metrics_do_not_fill_missing_observations(client: TestClient) -> None:
    daily = metrics_by_code(client)["vintaged_daily_test"]
    # Oct 29 is missing at source: the latest valued observation (Oct 28) is described, and
    # nothing earlier was stored, so no change is computed.
    assert daily["current"]["period_end"] == "2026-10-28"
    change = daily["metrics"][0]
    assert change["status"] == "missing_comparison"
    assert change["absolute_change"] is None
    kinds = {n["kind"] for n in daily["not_calculated"]}
    assert {"week_over_week", "seasonal_range"} <= kinds


def test_metrics_for_a_series_without_observations(client: TestClient) -> None:
    empty = metrics_by_code(client)["vintaged_empty_test"]
    assert (empty["current"], empty["metrics"]) == (None, [])
