"""The read-only EIA probe (marketpulse.sources.eia.probe and `marketpulse probe eia`).

Every response comes from the in-memory simulator (tests/eia_sim.py); nothing reaches the
network (tests/isolation.py also blocks it). All series values here are SYNTHETIC, including
those served under the restricted Refinitiv/LSEG series' routes and facets: they are made up,
distinctive numbers so the tests can prove that no value ever appears in the probe's output.
"""

import calendar
import json
from collections.abc import Callable, Iterator
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

import marketpulse.sources.eia.client as client_module
from eia_sim import FakeEia, SimRoute, SimSeries, make_rows
from marketpulse.cli import main
from marketpulse.core.config import get_settings
from marketpulse.sources.catalog import load_catalog
from marketpulse.sources.eia.client import EiaClient
from marketpulse.sources.eia.probe import (
    FAIL,
    OK,
    VALUE_WITHHELD,
    WARN,
    EiaProbeConfigError,
    EiaProbeReport,
    SeriesProbe,
    probe_eia,
    withhold_values,
)
from marketpulse.sources.eia.validate import EiaMetadataError, expectation_for
from marketpulse.sources.http import FetchResult, RetryPolicy
from test_eia_client import KEY, UnlimitedLimiter

CATALOG = load_catalog()
SPECS = {s.code: s for s in CATALOG.series_for("eia")}
EXP = {code: expectation_for(spec) for code, spec in SPECS.items()}
RESTRICTED = {"wti_spot", "brent_spot", "henry_hub_spot"}
ROUTES = {e.route for e in EXP.values()}
TODAY = date(2026, 11, 2)  # a Monday
STORAGE, WTI, LNG = "l48_gas_storage", "wti_spot", "us_lng_exports"
EX_SPR = "us_crude_stocks_ex_spr"  # shares petroleum/sum/sndw with four other series

Fault = Callable[[httpx.Request, dict[str, Any]], dict[str, Any] | None]


# --- synthetic series --------------------------------------------------------------------------


def periods_until(frequency: str, end: date, n: int) -> list[str]:
    """`n` periods of `frequency` ending on or before `end`, ascending (EIA's formats)."""
    out: list[str] = []
    if frequency == "daily":
        d = end
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d.isoformat())
            d -= timedelta(days=1)
    elif frequency == "weekly":
        d = end - timedelta(days=(end.weekday() - 4) % 7)  # the last Friday on or before end
        out = [(d - timedelta(weeks=i)).isoformat() for i in range(n)]
    else:  # monthly: the last complete month before end's month
        year, month = end.year, end.month
        for _ in range(n):
            month -= 1
            if month == 0:
                year, month = year - 1, 12
            out.append(f"{year:04d}-{month:02d}")
    return sorted(out)


def synthetic_value(code: str, i: int) -> str:
    """Distinctive made-up values that cannot collide with counts, periods, or ages."""
    return f"{770000 + sorted(SPECS).index(code) * 1000 + i}.4321"


class Sim:
    def __init__(self, *, end: date = TODAY, rows: int = 30) -> None:
        self.fake = FakeEia(KEY)
        self.values: set[str] = set()
        self.responses: list[tuple[str, FetchResult]] = []
        for code in SPECS:
            self.add(code, periods_until(EXP[code].frequency, end, rows))

    def add(self, code: str, periods: list[str], *, values: list[str | None] | None = None) -> None:
        exp = EXP[code]
        points = [
            (p, synthetic_value(code, i) if values is None else values[i])
            for i, p in enumerate(periods)
        ]
        self.values.update(v for _, v in points if v is not None)
        rows = make_rows(exp.facet_series, exp.description, exp.units, points)
        self.fake.add(
            SPECS[code].source_series_id,
            SimSeries(exp.route, exp.facet_series, exp.frequency, rows),
            SimRoute([(exp.frequency, exp.date_format)]),
        )

    def client(self, *, retry: RetryPolicy | None = None) -> EiaClient:
        return EiaClient(
            SecretStr(KEY),
            rate_limiter=UnlimitedLimiter(),
            retry=retry or RetryPolicy(max_attempts=2),
            on_response=lambda dataset, result: self.responses.append((dataset, result)),
            transport=self.fake.transport(),
            sleep=lambda _: None,
        )

    def probe(self, *codes: str, today: date = TODAY, **kwargs: Any) -> EiaProbeReport:
        with self.client(**kwargs) as client:
            return probe_eia(
                client,
                CATALOG,
                series_codes=list(codes) or None,
                today=today,
                responses=lambda: len(self.responses),
            )

    def requests_by_kind(self) -> dict[str, int]:
        kinds = {"metadata": 0, "seriesid": 0, "data": 0}
        for r in self.fake.requests:
            path = r.url.path
            kind = (
                "seriesid"
                if "/seriesid/" in path
                else "data"
                if path.endswith("/data")
                else "metadata"
            )
            kinds[kind] += 1
        return kinds


def on_data(code: str, change: Callable[[httpx.Request, dict[str, Any]], None]) -> Fault:
    """A simulator fault applied to `code`'s native-route data pages only."""

    def fault(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any]:
        if request.url.params.get("facets[series][0]") == EXP[code].facet_series:
            change(request, body)
        return body

    return fault


def series(report: EiaProbeReport, code: str) -> SeriesProbe:
    return next(s for s in report.series if s.code == code)


def everything_printed(report: EiaProbeReport) -> str:
    return repr(report) + "".join("".join(s.problems) + "".join(s.warnings) for s in report.series)


# --- a healthy catalog -------------------------------------------------------------------------


def test_every_catalogued_series_is_ok_with_its_coverage() -> None:
    sim = Sim()
    report = sim.probe()
    assert [s.code for s in report.series] == list(SPECS)
    assert {s.status for s in report.series} == {OK}
    assert report.exit_code == 0
    for s in report.series:
        periods = periods_until(EXP[s.code].frequency, TODAY, 30)
        assert (s.total, s.first_period, s.last_period) == (30, periods[0], periods[-1])
        assert s.age_days is not None and s.age_days <= s.max_staleness_days
        assert s.problems == [] and s.warnings == []


def test_request_budget_is_four_per_series_with_route_metadata_once_per_route() -> None:
    sim = Sim()
    report = sim.probe()
    assert sim.requests_by_kind() == {
        "metadata": len(ROUTES),  # 7 routes for 12 series
        "seriesid": len(SPECS),
        "data": 2 * len(SPECS),  # first and last row
    }
    assert report.responses == len(ROUTES) + 3 * len(SPECS) == len(sim.fake.requests) == 43


def test_data_requests_sample_only_the_first_and_last_rows_ascending() -> None:
    sim = Sim()
    sim.probe(STORAGE)
    data = [dict(r.url.params) for r in sim.fake.data_requests()]
    assert [(p["offset"], p["length"]) for p in data] == [("0", "1"), ("29", "1")]
    for params in data:
        assert params["sort[0][direction]"] == "asc" and params["sort[0][column]"] == "period"
        assert params["facets[series][0]"] == EXP[STORAGE].facet_series
        assert params["frequency"] == "weekly" and "start" not in params and "end" not in params


def test_a_single_row_series_needs_one_data_request() -> None:
    sim = Sim()
    sim.add(LNG, periods_until("monthly", TODAY, 1))
    report = sim.probe(LNG)
    assert series(report, LNG).total == 1
    assert series(report, LNG).first_period == series(report, LNG).last_period
    assert sim.requests_by_kind()["data"] == 1


def test_shared_route_metadata_is_fetched_once() -> None:
    sim = Sim()
    sim.probe(EX_SPR, "us_crude_stocks_spr", "us_gasoline_stocks")
    assert sim.requests_by_kind()["metadata"] == 1


def test_unknown_series_code_stops_before_any_request() -> None:
    sim = Sim()
    with pytest.raises(EiaProbeConfigError, match="nope"):
        sim.probe("nope")
    assert sim.fake.requests == []


# --- no values, no retention --------------------------------------------------------------------


def test_the_report_has_no_value_fields() -> None:
    names = {f.name for f in fields(SeriesProbe)}
    assert not any("value" in name for name in names)


def test_no_value_ever_appears_in_the_report() -> None:
    sim = Sim()
    report = sim.probe()
    printed = everything_printed(report)
    assert sim.values and not any(v in printed for v in sim.values)


def test_restricted_series_are_flagged_by_their_effective_license() -> None:
    report = Sim().probe()
    for s in report.series:
        assert s.restricted == (s.code in RESTRICTED)
        assert s.license == ("personal_use_only" if s.code in RESTRICTED else "public_domain")


@pytest.mark.parametrize(
    "malformed",
    ["7.25E1", "72.5 ", "+72.5", "1,234.5"],  # value-like text the strict parser refuses
)
def test_a_malformed_restricted_value_is_withheld_from_problem_texts(malformed: str) -> None:
    sim = Sim()

    def corrupt(request: httpx.Request, body: dict[str, Any]) -> None:
        body["response"]["data"][0]["value"] = malformed

    sim.fake.faults.append(on_data(WTI, corrupt))
    report = sim.probe(WTI)
    s = series(report, WTI)
    assert s.status == FAIL
    assert any(VALUE_WITHHELD in p for p in s.problems)
    assert malformed.strip() not in everything_printed(report)


def test_withhold_values_removes_values_and_credentials() -> None:
    text = (
        "first row: EiaResponseError: Invalid EIA data response (1 problem(s)):\n"
        "  - data[0].value: unrecognized value '7.25E1' (only decimals and null are accepted)\n"
        f"at https://api.eia.gov/v2/x/data?api_key={KEY}&length=1"
    )
    cleaned = withhold_values(text)
    assert "7.25E1" not in cleaned and KEY not in cleaned
    assert f"data[0].value: {VALUE_WITHHELD}" in cleaned


# --- failures (anything that would stop ingestion) -----------------------------------------------


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("units", "MMBBL", "units"),
        ("series-description", "Something else entirely", "description"),
        ("series", "OTHER", "series"),
    ],
)
def test_metadata_drift_in_a_sampled_row_fails_the_series(
    field: str, replacement: str, message: str
) -> None:
    sim = Sim()

    def drift(request: httpx.Request, body: dict[str, Any]) -> None:
        for row in body["response"]["data"]:
            row[field] = replacement

    sim.fake.faults.append(on_data(STORAGE, drift))
    report = sim.probe(STORAGE, EX_SPR)
    assert series(report, STORAGE).status == FAIL
    assert any(message in p for p in series(report, STORAGE).problems)
    assert series(report, EX_SPR).status == OK  # series are independent
    assert report.exit_code == 1


def test_drift_only_in_the_last_row_is_found() -> None:
    sim = Sim()

    def drift(request: httpx.Request, body: dict[str, Any]) -> None:
        if request.url.params["offset"] != "0":
            body["response"]["data"][0]["units"] = "MMCF"

    sim.fake.faults.append(on_data(STORAGE, drift))
    assert any("units" in p for p in series(sim.probe(STORAGE), STORAGE).problems)


def test_a_route_without_the_frequency_fails_its_series() -> None:
    sim = Sim()
    sim.fake.routes[EXP[STORAGE].route].frequencies = [("monthly", "YYYY-MM")]
    s = series(sim.probe(STORAGE), STORAGE)
    assert s.status == FAIL
    assert any("no longer offers frequency 'weekly'" in p for p in s.problems)


def test_a_route_with_another_date_format_fails_its_series() -> None:
    sim = Sim()
    route = EXP[STORAGE].route

    def metadata_override(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.rstrip("/") != f"/v2/{route}":
            return None
        body = json.loads(sim.fake._metadata(route).content)
        body["response"]["frequency"][0]["format"] = 'YYYY-"W"IW'
        return httpx.Response(200, json=body)

    sim.fake.overrides.append(metadata_override)
    s = series(sim.probe(STORAGE), STORAGE)
    assert any("date format" in p for p in s.problems)


@pytest.mark.parametrize(
    ("key", "message"), [("facets", "no 'series' facet"), ("data", "no 'value' data column")]
)
def test_a_route_without_the_series_facet_or_value_column_fails(key: str, message: str) -> None:
    sim = Sim()
    route = EXP[STORAGE].route

    def metadata_override(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.rstrip("/") != f"/v2/{route}":
            return None
        body = json.loads(sim.fake._metadata(route).content)
        body["response"][key] = [{"id": "duoarea"}] if key == "facets" else {"other": []}
        return httpx.Response(200, json=body)

    sim.fake.overrides.append(metadata_override)
    assert any(message in p for p in series(sim.probe(STORAGE), STORAGE).problems)


def test_route_metadata_failure_fails_every_series_on_the_route_but_not_others() -> None:
    sim = Sim()
    route = EXP[EX_SPR].route

    def broken(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.rstrip("/") == f"/v2/{route}":
            return httpx.Response(400, json={"error": f"Requested path /{route} is not valid."})
        return None

    sim.fake.overrides.append(broken)
    report = sim.probe(EX_SPR, "us_crude_stocks_spr", STORAGE)
    assert series(report, EX_SPR).status == FAIL == series(report, "us_crude_stocks_spr").status
    assert series(report, STORAGE).status == OK
    assert all("route metadata" in p for p in series(report, EX_SPR).problems[:1])


def test_a_series_id_that_maps_to_another_route_fails() -> None:
    sim = Sim()
    sim.fake.series[EXP[STORAGE].facet_series].route = "natural-gas/stor/other"
    sim.fake.routes["natural-gas/stor/other"] = SimRoute([("weekly", "YYYY-MM-DD")])
    s = series(sim.probe(STORAGE), STORAGE)
    assert s.status == FAIL
    assert any("series-ID route maps to route" in p for p in s.problems)


def test_an_unknown_series_id_fails() -> None:
    sim = Sim()
    del sim.fake.series_ids[SPECS[STORAGE].source_series_id]
    s = series(sim.probe(STORAGE), STORAGE)
    assert any(p.startswith("series-ID route: EiaAPIError") for p in s.problems)


def test_a_series_with_no_data_fails() -> None:
    sim = Sim()
    sim.add(STORAGE, [])
    s = series(sim.probe(STORAGE), STORAGE)
    assert s.status == FAIL and s.total == 0
    assert any("returned no data" in p for p in s.problems)
    assert sim.requests_by_kind()["data"] == 1


def notices(sim: Sim) -> list[list[str]]:
    return [
        [w["warning"] for w in json.loads(r.content).get("warnings", [])]
        for dataset, r in sim.responses
        if dataset == "data"
    ]


def test_the_notice_on_both_one_row_samples_is_expected() -> None:
    """The live failure of 2026-11-05: length 1 < total, so EIA sends "incomplete return" with
    the first AND the last row. Both pages are complete, so the series is ok."""
    sim = Sim()
    s = series(sim.probe(STORAGE), STORAGE)
    assert notices(sim) == [["incomplete return"], ["incomplete return"]]
    assert s.status == OK and s.problems == []


def test_a_single_row_series_gets_no_notice_and_an_unexplained_one_fails() -> None:
    sim = Sim()
    sim.add(LNG, periods_until("monthly", TODAY, 1))
    assert series(sim.probe(LNG), LNG).status == OK
    assert notices(sim) == [[]]  # length 1 == total 1: no notice

    sim = Sim()
    sim.add(LNG, periods_until("monthly", TODAY, 1))

    def notice(request: httpx.Request, body: dict[str, Any]) -> None:
        body["warnings"] = [{"warning": "incomplete return", "description": "synthetic"}]

    sim.fake.faults.append(on_data(LNG, notice))
    s = series(sim.probe(LNG), LNG)
    assert s.problems == ["us_lng_exports: EIA warning 'incomplete return' at offset 0: synthetic"]


@pytest.mark.parametrize(("offset", "rows"), [("0", 2), ("29", 0), ("29", 2)])
def test_a_sample_with_the_wrong_number_of_rows_fails(offset: str, rows: int) -> None:
    sim = Sim()
    series_rows = sim.fake.series[EXP[STORAGE].facet_series].rows

    def resize(request: httpx.Request, body: dict[str, Any]) -> None:
        if request.url.params["offset"] == offset:
            start = int(offset) - (1 if rows > 1 and offset != "0" else 0)
            body["response"]["data"] = series_rows[start : start + rows]

    sim.fake.faults.append(on_data(STORAGE, resize))
    s = series(sim.probe(STORAGE), STORAGE)
    assert s.status == FAIL
    assert any(f"{rows} row(s) at offset {offset}, expected 1" in p for p in s.problems)


def test_an_unexpected_warning_fails_but_the_pagination_notice_does_not() -> None:
    sim = Sim()

    def warn(request: httpx.Request, body: dict[str, Any]) -> None:
        if request.url.params["offset"] != "0":  # added to the final page's own notice
            body["warnings"] = [
                *body.get("warnings", []),
                {"warning": "something new", "description": "unexpected"},
            ]

    sim.fake.faults.append(on_data(STORAGE, warn))
    s = series(sim.probe(STORAGE), STORAGE)
    assert [p for p in s.problems if "warning" in p] == [
        "l48_gas_storage: EIA warning 'something new' at offset 29: unexpected"
    ]  # both pages' "incomplete return" notices were tolerated


def test_a_total_that_changes_between_requests_fails() -> None:
    sim = Sim()

    def grow(request: httpx.Request, body: dict[str, Any]) -> None:
        if request.url.params["offset"] != "0":
            body["response"]["total"] = "31"

    sim.fake.faults.append(on_data(STORAGE, grow))
    s = series(sim.probe(STORAGE), STORAGE)
    assert any("total changed between requests (30, then 31)" in p for p in s.problems)


def test_an_ignored_offset_fails() -> None:
    sim = Sim()

    def ignore(request: httpx.Request, body: dict[str, Any]) -> None:
        body["request"]["params"]["offset"] = "0"

    sim.fake.faults.append(on_data(STORAGE, ignore))
    s = series(sim.probe(STORAGE), STORAGE)
    assert any("last row: EiaMetadataError" in p and "offset '0'" in p for p in s.problems)


def test_a_period_that_breaks_the_catalogued_convention_fails() -> None:
    sim = Sim()
    sim.add(STORAGE, ["2026-10-01", "2026-10-29"])  # Thursdays, not week-ending Fridays
    s = series(sim.probe(STORAGE), STORAGE)
    assert s.status == FAIL
    assert any("is a Thursday" in p for p in s.problems)


# --- staleness ---------------------------------------------------------------------------------


def test_a_series_older_than_its_max_staleness_is_a_warning() -> None:
    sim = Sim(end=TODAY - timedelta(days=60))
    report = sim.probe(STORAGE, LNG)
    s = series(report, STORAGE)
    assert s.status == WARN and s.age_days is not None and s.age_days > 14
    assert any("max_staleness_days (14)" in w for w in s.warnings)
    assert series(report, LNG).status == OK  # monthly: max 100 days
    assert report.exit_code == 2


def test_age_is_measured_from_the_end_of_the_last_period() -> None:
    sim = Sim()
    report = sim.probe(LNG)
    year, month = (int(x) for x in series(report, LNG).last_period.split("-"))  # type: ignore[union-attr]
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    assert series(report, LNG).last_period_end == month_end
    assert series(report, LNG).age_days == (TODAY - month_end).days


# --- stopping early ----------------------------------------------------------------------------


def test_an_invalid_key_stops_the_probe_after_one_request() -> None:
    sim = Sim()
    sim.fake.valid_key = "another-key"
    report = sim.probe()
    assert len(sim.fake.requests) == 1
    first, *rest = report.series
    assert any("API_KEY_INVALID" in p for p in first.problems)
    assert all(
        p.startswith("not probed: the probe stopped after") for s in rest for p in s.problems
    )
    assert {s.status for s in report.series} == {FAIL} and report.exit_code == 1
    assert KEY not in everything_printed(report)


def test_an_exhausted_rate_limit_stops_the_probe() -> None:
    sim = Sim()
    sim.fake.overrides.append(lambda request: httpx.Response(429, text="Too Many Requests"))
    report = sim.probe(retry=RetryPolicy(max_attempts=2))
    assert len(sim.fake.requests) == 2  # one request, retried once, then the probe stops
    assert all(s.status == FAIL for s in report.series)
    assert "HTTP 429" in report.series[1].problems[0]


def test_a_server_error_on_one_route_does_not_stop_the_probe() -> None:
    sim = Sim()
    route = EXP[STORAGE].route

    def failing(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(503) if request.url.path.endswith(f"{route}/data") else None

    sim.fake.overrides.append(failing)
    report = sim.probe(STORAGE, LNG)
    assert series(report, STORAGE).status == FAIL
    assert series(report, LNG).status == OK


# --- the client's single-page fetch ------------------------------------------------------------


def test_fetch_page_rejects_bad_bounds_without_a_request() -> None:
    sim = Sim()
    with sim.client() as client:
        for kwargs in ({"offset": 0, "length": 0}, {"offset": 0, "length": 5001}):
            with pytest.raises(ValueError, match="length"):
                client.fetch_page(EXP[STORAGE], **kwargs)
        with pytest.raises(ValueError, match="offset"):
            client.fetch_page(EXP[STORAGE], offset=-1, length=1)
    assert sim.fake.requests == []


def test_fetch_page_checks_the_executed_length() -> None:
    sim = Sim()

    def change_length(request: httpx.Request, body: dict[str, Any]) -> None:
        body["request"]["params"]["length"] = "5000"

    sim.fake.faults.append(on_data(STORAGE, change_length))
    with sim.client() as client, pytest.raises(EiaMetadataError, match="length '5000'"):
        client.fetch_page(EXP[STORAGE], offset=0, length=1)


# --- CLI ---------------------------------------------------------------------------------------


@pytest.fixture
def cli_sim(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sim]:
    """`marketpulse probe eia` against the simulator, with a synthetic key and no database."""
    sim = Sim(end=datetime.now(UTC).date())
    real_init = EiaClient.__init__

    def from_settings(cls: type[EiaClient], /, settings: Any, **kwargs: Any) -> EiaClient:
        client = cls.__new__(cls)
        real_init(
            client,
            settings.eia_api_key,
            rate_limiter=UnlimitedLimiter(),
            retry=RetryPolicy(max_attempts=2),
            transport=sim.fake.transport(),
            sleep=lambda _: None,
            **kwargs,
        )
        return client

    def no_database(*args: object, **kwargs: object) -> None:
        raise AssertionError("the probe must not touch the database")

    monkeypatch.setenv("EIA_API_KEY", KEY)
    monkeypatch.setattr(client_module.EiaClient, "from_settings", classmethod(from_settings))
    monkeypatch.setattr("marketpulse.db.session.make_engine", no_database)
    monkeypatch.setattr("marketpulse.db.session.make_session_factory", no_database)
    get_settings.cache_clear()
    yield sim
    get_settings.cache_clear()


def test_cli_reports_every_series_without_values(
    cli_sim: Sim, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["probe", "eia"]) == 0
    out = capsys.readouterr().out
    for code in SPECS:
        assert code in out
    assert "12 series, 12 ok, 0 warn, 0 fail; 43 responses received and discarded" in out
    assert "restricted (licensed for personal use only" in out
    for code in RESTRICTED:
        assert f"{code:<24} personal_use_only" in out
    assert "read-only: no database access" in out
    assert not any(v in out for v in cli_sim.values)
    assert KEY not in out


def test_cli_keeps_no_raw_responses(cli_sim: Sim, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = get_settings().marketpulse_data_dir
    before = sorted(data_dir.rglob("*"))
    assert main(["probe", "eia", "--series", STORAGE]) == 0
    assert sorted(data_dir.rglob("*")) == before


def test_cli_selected_series_and_failures(cli_sim: Sim, capsys: pytest.CaptureFixture[str]) -> None:
    cli_sim.add(STORAGE, [])
    assert main(["probe", "eia", "--series", STORAGE, LNG]) == 1
    out = capsys.readouterr().out
    assert "2 series, 1 ok, 0 warn, 1 fail" in out
    assert "FAIL l48_gas_storage: EIA returned no data (total 0)" in out
    assert "restricted (" not in out  # no restricted series was selected


def test_cli_unknown_series_is_a_clean_error(
    cli_sim: Sim, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["probe", "eia", "--series", "nope"]) == 1
    assert "error: unknown EIA series code(s) ['nope']" in capsys.readouterr().out
    assert cli_sim.fake.requests == []


@pytest.mark.parametrize("key", [None, "   "])
def test_cli_without_a_key_makes_no_request(
    key: str | None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    if key is None:
        monkeypatch.delenv("EIA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("EIA_API_KEY", key)
    assert main(["probe", "eia"]) == 1
    assert "error: EIA_API_KEY is not set or is blank" in capsys.readouterr().out


def test_cli_help_documents_read_only_and_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["probe", "eia", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "Stores nothing" in out and "Never shows data values" in out
    assert "0 every series ok; 1 any series failed" in out and "2 warnings only" in out
