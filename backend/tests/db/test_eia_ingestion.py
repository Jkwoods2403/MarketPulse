"""End-to-end EIA ingestion: simulated EIA API -> EiaClient -> raw payloads -> first-seen snapshots.

The EIA API is simulated (tests/eia_sim.py); everything else is the real pipeline: catalog,
client, raw payload store on disk, ops.raw_payloads, the snapshot planner and writer, and run
tracking. All series values are SYNTHETIC (no real EIA or Refinitiv data).
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import marketpulse.ingestion.eia as eia_job
import marketpulse.ingestion.runs as runs_module
from eia_sim import FakeEia, SimRoute, SimSeries, make_rows
from marketpulse.analytics.point_in_time import series_as_of
from marketpulse.cli import main
from marketpulse.core.config import Settings, get_settings
from marketpulse.db.models import (
    DataQualityIssue,
    IngestionRun,
    Observation,
    RawPayload,
    Series,
    Source,
)
from marketpulse.ingestion.catalog_sync import sync_catalog
from marketpulse.ingestion.eia import (
    UNFINALIZED,
    EiaIngestionConfigError,
    EiaIngestionSummary,
    EiaLicenseError,
    EiaRawRecorder,
    ingest_eia,
)
from marketpulse.ingestion.raw_store import RawPayloadStore, StoredPayload
from marketpulse.ingestion.runs import IngestionRunTracker
from marketpulse.sources.catalog import Catalog, load_catalog
from marketpulse.sources.catalog.schema import License
from marketpulse.sources.eia.client import EiaClient, EiaConfigError, ResponseHook
from marketpulse.sources.eia.validate import EiaSeriesExpectation, expectation_for
from marketpulse.sources.http import RetryPolicy
from test_eia_client import KEY, SPECS, UnlimitedLimiter, business_days, fridays
from test_eia_license_guard import RESTRICTED, with_series_license

CATALOG = load_catalog()
EXP = {code: expectation_for(spec) for code, spec in SPECS.items()}
STORAGE = "l48_gas_storage"
EX_SPR, SPR = "us_crude_stocks_ex_spr", "us_crude_stocks_spr"  # share petroleum/sum/sndw

Fault = Callable[[httpx.Request, dict[str, Any]], dict[str, Any] | None]


def add_series(fake: FakeEia, code: str, points: list[tuple[str, str | None]]) -> None:
    exp = EXP[code]
    rows = make_rows(exp.facet_series, exp.description, exp.units, points)
    fake.add(
        SPECS[code].source_series_id,
        SimSeries(exp.route, exp.facet_series, exp.frequency, rows),
        SimRoute([(exp.frequency, exp.date_format)]),
    )


def weekly(n: int, base: int = 3000) -> list[tuple[str, str | None]]:
    return [(p, str(base + i)) for i, p in enumerate(fridays(date(2022, 1, 7), n))]


@dataclass
class Env:
    factory: sessionmaker[Session]
    fake: FakeEia
    settings: Settings
    data_dir: Path
    client_factory: Callable[[ResponseHook], EiaClient]

    def run(
        self, *codes: str, full: bool = False, page_size: int = 4, catalog: Catalog | None = None
    ) -> EiaIngestionSummary:
        return ingest_eia(
            settings=self.settings,
            session_factory=self.factory,
            series_codes=list(codes) or None,
            full=full,
            client_factory=self.client_factory,
            catalog=catalog,
            page_size=page_size,
        )

    def rows(self, code: str) -> list[dict[str, Any]]:
        return self.fake.series[EXP[code].facet_series].rows

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

    def checksum(self) -> str:
        with self.factory() as s:
            return str(
                s.scalar(
                    text(
                        "SELECT coalesce(md5(string_agg(o::text, '|' ORDER BY o.id)), 'empty') "
                        "FROM obs.observations o"
                    )
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
                    select(DataQualityIssue)
                    .where(DataQualityIssue.ingestion_run_id == run_id)
                    .order_by(DataQualityIssue.id)
                )
            )

    def page_periods(self, payload: RawPayload) -> list[str]:
        """Periods in the stored raw file (hash-verified on read)."""
        content = RawPayloadStore(self.data_dir).load(
            StoredPayload(payload.file_path, payload.sha256, payload.size_bytes)
        )
        return [row["period"] for row in json.loads(content)["response"]["data"]]


def make_client_factory(fake: FakeEia) -> Callable[[ResponseHook], EiaClient]:
    def build(hook: ResponseHook) -> EiaClient:
        return EiaClient(
            SecretStr(KEY),
            on_response=hook,
            transport=fake.transport(),
            rate_limiter=UnlimitedLimiter(),
            retry=RetryPolicy(max_attempts=2),
            sleep=lambda _: None,
        )

    return build


@pytest.fixture
def env(factory: sessionmaker[Session], tmp_path: Path) -> Env:
    with factory() as s, s.begin():
        sync_catalog(s, CATALOG)
    fake = FakeEia(KEY)
    settings = Settings(
        _env_file=None,
        eia_api_key=SecretStr(KEY),  # type: ignore[call-arg]
        marketpulse_data_dir=tmp_path,
    )
    return Env(factory, fake, settings, tmp_path, make_client_factory(fake))


def snapshot_at(run: IngestionRun) -> datetime:
    return datetime.fromisoformat(run.params["snapshot_at"])


# --- successful ingestion -----------------------------------------------------------------------


def test_first_ingestion_maps_every_row_to_the_raw_page_that_contained_it(env: Env) -> None:
    points = weekly(10)
    points[3] = (points[3][0], "1234.5678901234567890123")  # exact decimal text
    add_series(env.fake, STORAGE, points)

    summary = env.run(STORAGE)
    assert summary.exit_code == 0
    (outcome,) = summary.outcomes
    assert (outcome.mode, outcome.status, outcome.fetched, outcome.inserted) == (
        "full",
        "succeeded",
        10,
        10,
    )
    (run,) = env.runs()
    assert (run.job, run.status) == ("eia.observations", "succeeded")
    assert run.params["pages"] == 3 and run.params["page_offsets"] == [0, 4, 8]
    assert run.params["series_code"] == STORAGE and run.params["fetch_start"] is None

    payloads = {p.id: p for p in env.raw_payloads(run.id)}
    assert len(payloads) == 3
    at = snapshot_at(run)
    assert at.tzinfo is not None
    assert all(p.retrieved_at <= at for p in payloads.values())  # taken after every page
    observations = env.observations(STORAGE)
    assert len(observations) == 10
    for o in observations:
        assert o.raw_payload_id in payloads
        assert o.source_period in env.page_periods(payloads[o.raw_payload_id])  # its own page
        assert (o.vintage_start, o.first_retrieved_at, o.last_confirmed_at) == (at, at, at)
        assert (o.ingestion_run_id, o.last_confirmed_run_id, o.vintage_end) == (
            run.id,
            run.id,
            None,
        )
    assert str(observations[3].value) == "1234.5678901234567890123"
    assert {o.raw_payload_id for o in observations} == set(payloads)  # every page used


def test_second_run_is_incremental_and_only_confirms(env: Env) -> None:
    points = weekly(40)
    add_series(env.fake, STORAGE, points)
    env.run(STORAGE, page_size=50)
    summary = env.run(STORAGE, page_size=50)
    (outcome,) = summary.outcomes
    periods = [p for p, _ in points]
    w, before = periods[-27], periods[-28]  # 26 weeks back from the latest stored Friday
    assert (outcome.mode, outcome.status) == ("incremental", "succeeded")
    assert (outcome.fetched, outcome.inserted, outcome.unchanged) == (27, 0, 27)
    run = env.runs()[-1]
    assert (run.params["fetch_start"], run.params["scope_start"]) == (before, w)
    assert run.params["dropped_before_window"] == 1  # the boundary period, fetched for safety
    assert env.fake.data_requests()[-1].url.params["start"] == before
    assert len(env.observations(STORAGE)) == 40  # no new versions
    confirmed = [o for o in env.observations(STORAGE) if o.last_confirmed_run_id == run.id]
    assert sorted(o.source_period for o in confirmed) == periods[-27:]


def test_revision_creates_one_version_and_point_in_time_queries_see_it(env: Env) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    env.run(STORAGE)
    env.rows(STORAGE)[9]["value"] = "4000.5"
    summary = env.run(STORAGE)
    assert [(o.status, o.inserted, o.revised, o.unchanged) for o in summary.outcomes] == [
        ("succeeded", 1, 1, 9)
    ]
    first, second = env.runs()
    t1, t2 = snapshot_at(first), snapshot_at(second)
    sid = env.series_id(STORAGE)
    last_week = env.observations(STORAGE)[-1].period_start
    with env.factory() as s:
        before = series_as_of(s, sid, t1 - timedelta(microseconds=1))
        old = series_as_of(s, sid, t2 - timedelta(microseconds=1))
        new = series_as_of(s, sid, t2)
    assert before.observations == []  # nothing before Market Pulse first saw it
    assert new.vintage_basis == "first_seen"
    by_period = {o.period_start: o.value for o in old.observations}
    assert by_period[last_week] == Decimal("3009")
    assert {o.period_start: o.value for o in new.observations}[last_week] == Decimal("4000.5")


def test_one_vanished_period_within_the_limit_is_applied_and_tracked(env: Env) -> None:
    add_series(env.fake, STORAGE, weekly(200))
    env.run(STORAGE, page_size=100)
    del env.rows(STORAGE)[50]  # 1 of 200 = 0.5%
    summary = env.run(STORAGE, full=True, page_size=100)
    (outcome,) = summary.outcomes
    assert (outcome.status, outcome.vanished, outcome.rejected) == ("succeeded", 1, False)
    assert env.runs()[-1].params["vanished"] == 1
    run = env.runs()[-1]
    vanished = [i for i in env.issues(run.id) if i.check_name == "vanished_period"]
    assert [(i.severity, i.occurrences) for i in vanished] == [("warning", 1)]


# --- rejected snapshots -------------------------------------------------------------------------


def test_rejected_full_snapshot_changes_nothing_and_keeps_raw_payloads(env: Env) -> None:
    add_series(env.fake, STORAGE, weekly(40))
    env.run(STORAGE, page_size=50)
    before = env.checksum()
    del env.rows(STORAGE)[10:16]  # six periods vanish: more than five
    summary = env.run(STORAGE, full=True, page_size=50)
    (outcome,) = summary.outcomes
    assert (summary.exit_code, outcome.status, outcome.rejected) == (2, "partial", True)
    assert outcome.vanished == 6  # counted although the snapshot was rejected
    assert env.checksum() == before
    run = env.runs()[-1]
    assert run.status == "partial" and run.params["rejected"] is True
    assert run.params["vanished"] == 6
    assert len(env.raw_payloads(run.id)) == 1
    assert [i.check_name for i in env.issues(run.id)] == ["vanished_limit"]


def test_incremental_window_does_not_weaken_the_threshold(env: Env) -> None:
    add_series(env.fake, STORAGE, weekly(40))
    env.run(STORAGE, page_size=50)
    before = env.checksum()
    del env.rows(STORAGE)[-3]  # one period inside the 27-period incremental scope (> 1%)
    (outcome,) = env.run(STORAGE, page_size=50).outcomes
    assert (outcome.mode, outcome.status, outcome.rejected) == ("incremental", "partial", True)
    assert outcome.vanished == 1
    assert env.checksum() == before


# --- failures -----------------------------------------------------------------------------------


def overstated_total(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any] | None:
    body["response"]["total"] = str(int(body["response"]["total"]) + 1)
    return body


def duplicate_period(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any] | None:
    if request.url.params.get("offset") == "4":
        data = body["response"]["data"]
        body["response"]["data"] = [{**data[0], "period": "2022-01-28"}, *data[1:]]  # week 4 again
    return body


def units_drift(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any] | None:
    for row in body["response"]["data"]:
        row["units"] = "MMCF"
    return body


def unexpected_warning(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any] | None:
    body["warnings"] = [
        *body.get("warnings", []),
        {"warning": "something else", "description": "x"},
    ]
    return body


def empty_response(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any] | None:
    body["response"]["data"], body["response"]["total"] = [], "0"
    body.pop("warnings", None)  # EIA's empty answer (HTTP 200, total "0") carries no warning
    return body


@pytest.mark.parametrize(
    ("fault", "check"),
    [
        (overstated_total, "eia_incomplete"),
        (duplicate_period, "eia_incomplete"),
        (units_drift, "eia_metadata"),
        (unexpected_warning, "eia_warning"),
        (empty_response, "eia_empty_response"),
    ],
)
def test_fetch_failures_change_nothing_and_keep_raw_payloads(
    env: Env, fault: Fault, check: str
) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    env.run(STORAGE)
    before = env.checksum()
    env.fake.faults.append(fault)
    summary = env.run(STORAGE, full=True)
    (outcome,) = summary.outcomes
    assert (summary.exit_code, outcome.status) == (1, "failed")
    assert outcome.error is not None
    assert env.checksum() == before
    run = env.runs()[-1]
    assert run.status == "failed" and run.error_message
    payloads = env.raw_payloads(run.id)
    assert payloads, "raw responses are kept for diagnosis"
    assert all((env.data_dir / p.file_path).exists() for p in payloads)
    issues = env.issues(run.id)
    assert issues and {(i.check_name, i.severity) for i in issues} == {(check, "error")}


def test_retry_after_failure_is_a_new_run_with_a_fresh_snapshot_time(env: Env) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    env.fake.faults.append(overstated_total)
    assert env.run(STORAGE).outcomes[0].status == "failed"
    assert env.observations(STORAGE) == []
    env.fake.faults.clear()
    (retry,) = env.run(STORAGE).outcomes
    assert (retry.mode, retry.status, retry.inserted) == ("full", "succeeded", 10)
    failed, succeeded = env.runs()
    assert failed.id != succeeded.id
    assert "snapshot_at" not in failed.params  # the failed attempt never took one
    assert snapshot_at(succeeded) >= succeeded.started_at > failed.started_at


def test_provenance_mapping_failure_fails_the_run_without_changes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    original = eia_job.map_page_provenance

    def lose_one_page(
        pages: Any, recorded: Any, expected: EiaSeriesExpectation, **request: Any
    ) -> Any:
        recorded = dict(recorded)
        recorded.pop(next(iter(recorded)))
        return original(pages, recorded, expected, **request)

    monkeypatch.setattr(eia_job, "map_page_provenance", lose_one_page)
    (outcome,) = env.run(STORAGE).outcomes
    assert outcome.status == "failed"
    assert outcome.error is not None and "no recorded raw payload" in outcome.error
    assert env.observations(STORAGE) == []
    assert len(env.raw_payloads(env.runs()[-1].id)) == 3


# --- isolation, frequencies, licensing ----------------------------------------------------------


def test_series_on_the_shared_route_are_isolated(env: Env) -> None:
    add_series(env.fake, EX_SPR, weekly(10, base=400_000))
    add_series(env.fake, SPR, weekly(10, base=350_000))
    summary = env.run(EX_SPR, SPR)
    assert [(o.code, o.status) for o in summary.outcomes] == [
        (EX_SPR, "succeeded"),
        (SPR, "succeeded"),
    ]
    runs = {r.params["series_code"]: r for r in env.runs()}
    assert runs[EX_SPR].id != runs[SPR].id  # one run per series
    for code, other in ((EX_SPR, SPR), (SPR, EX_SPR)):
        payloads = env.raw_payloads(runs[code].id)
        own, foreign = EXP[code].facet_series, EXP[other].facet_series
        assert all(f"facets[series][0]={own}&" in p.request_url for p in payloads)
        assert not any(f"facets[series][0]={foreign}&" in p.request_url for p in payloads)
        ids = {p.id for p in payloads}
        assert all(o.raw_payload_id in ids for o in env.observations(code))
    assert env.observations(EX_SPR)[0].value == Decimal("400000")
    assert env.observations(SPR)[0].value == Decimal("350000")

    # A failure of one series on the shared route leaves the other untouched.
    spr_facet = EXP[SPR].facet_series

    def break_spr(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any] | None:
        if request.url.params.get("facets[series][0]") == spr_facet:
            return units_drift(request, body)
        return None

    env.fake.faults.append(break_spr)
    env.rows(EX_SPR)[9]["value"] = "400100"
    summary = env.run(EX_SPR, SPR)
    assert [(o.code, o.status) for o in summary.outcomes] == [
        (EX_SPR, "succeeded"),
        (SPR, "failed"),
    ]
    assert [o.value for o in env.observations(EX_SPR)][-2:] == [
        Decimal("400009"),
        Decimal("400100"),
    ]


def test_monthly_and_daily_series(env: Env) -> None:
    # The catalog's only daily EIA series are the personal_use_only spot prices, which the real
    # catalog refuses (see the licensing tests below). To keep the daily path covered, this test
    # ingests SYNTHETIC values through a test-only catalog copy in which wti_spot has no
    # override. The stored classification (ref.series, synced from the real catalog) is unchanged.
    daily_ok = with_series_license(CATALOG, "wti_spot", None)
    months = [f"{2023 + (m // 12)}-{m % 12 + 1:02d}" for m in range(30)]
    add_series(env.fake, "us_lng_exports", [(m, str(300 + i)) for i, m in enumerate(months)])
    days = business_days(date(2026, 3, 2), 120)
    add_series(
        env.fake, "wti_spot", [(d, f"{60 + i % 7}.{i % 100:02d}") for i, d in enumerate(days)]
    )
    summary = env.run("us_lng_exports", "wti_spot", page_size=50, catalog=daily_ok)
    assert [(o.code, o.mode, o.status) for o in summary.outcomes] == [
        ("us_lng_exports", "full", "succeeded"),
        ("wti_spot", "full", "succeeded"),
    ]
    feb = next(o for o in env.observations("us_lng_exports") if o.source_period == "2024-02")
    assert (feb.period_start, feb.period_end) == (date(2024, 2, 1), date(2024, 2, 29))

    summary = env.run("us_lng_exports", "wti_spot", page_size=50, catalog=daily_ok)
    lng, wti = summary.outcomes
    assert (lng.mode, lng.status, lng.fetched) == ("incremental", "succeeded", 27)  # 26 months + W
    assert (wti.mode, wti.status) == ("incremental", "succeeded")
    with env.factory() as s:
        override = s.scalar(select(Series.license_override).where(Series.code == "wti_spot"))
    assert override == "personal_use_only"  # classification preserved; not redistributable


# --- licensing guard (ADR-0008) -----------------------------------------------------------------


def add_daily(fake: FakeEia, code: str) -> None:
    """SYNTHETIC daily rows, so a request that got past the guard would succeed."""
    days = business_days(date(2026, 3, 2), 20)
    add_series(fake, code, [(d, f"{50 + i}.00") for i, d in enumerate(days)])


def eia_subset(*codes: str) -> Catalog:
    """The real catalog limited to some EIA series (classifications unchanged)."""
    return Catalog(
        sources=CATALOG.sources,
        series=tuple(
            (src, spec) for src, spec in CATALOG.series if src == "eia" and spec.code in codes
        ),
    )


def assert_nothing_happened(env: Env) -> None:
    """No HTTP request, no run, no raw payload row, no raw file, no observation."""
    assert env.fake.requests == []
    assert env.runs() == []
    with env.factory() as s:
        assert s.scalar(select(func.count()).select_from(RawPayload)) == 0
        assert s.scalar(select(func.count()).select_from(Observation)) == 0
    assert [p for p in env.data_dir.rglob("*") if p.is_file()] == []


@pytest.mark.parametrize("code", RESTRICTED)
def test_each_restricted_series_is_refused_before_any_request_or_run(env: Env, code: str) -> None:
    add_daily(env.fake, code)
    with pytest.raises(EiaLicenseError, match=rf"{code} \(personal_use_only") as excinfo:
        env.run(code)
    assert "requires appropriate licensing" in str(excinfo.value)
    assert_nothing_happened(env)


def test_a_mixed_request_does_nothing_for_any_series(env: Env) -> None:
    add_series(env.fake, "us_lng_exports", [("2025-01", "300"), ("2025-02", "301")])
    add_daily(env.fake, "wti_spot")
    with pytest.raises(EiaLicenseError, match="wti_spot"):
        env.run("us_lng_exports", "wti_spot")
    assert_nothing_happened(env)  # not even the unrestricted series was fetched


def test_default_job_skips_restricted_series_and_ingests_the_others(env: Env) -> None:
    # One unrestricted and the three restricted series, so only us_lng_exports needs data.
    add_series(env.fake, "us_lng_exports", [("2025-01", "300"), ("2025-02", "301")])
    for code in RESTRICTED:
        add_daily(env.fake, code)
    summary = env.run(catalog=eia_subset("us_lng_exports", *RESTRICTED))
    assert [(o.code, o.status) for o in summary.outcomes] == [("us_lng_exports", "succeeded")]
    assert summary.skipped_restricted == dict.fromkeys(RESTRICTED, License.PERSONAL_USE_ONLY)
    assert summary.exit_code == 0
    facets = {r.url.params.get("facets[series][0]") for r in env.fake.requests}
    assert facets == {EXP["us_lng_exports"].facet_series}  # no request for a restricted series
    assert [r.params["series_code"] for r in env.runs()] == ["us_lng_exports"]


def test_unrestricted_series_ingest_normally(env: Env) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    summary = env.run(STORAGE)
    assert [(o.code, o.status) for o in summary.outcomes] == [(STORAGE, "succeeded")]
    assert summary.skipped_restricted == {}
    assert len(env.observations(STORAGE)) == 10


@pytest.mark.parametrize("code", RESTRICTED)
def test_per_series_ingestion_refuses_restricted_series_before_opening_a_run(
    env: Env, code: str
) -> None:
    # Defense in depth: the per-series step checks the license again, so a caller that skipped
    # select_series still cannot open a run or send a request for a restricted series.
    add_daily(env.fake, code)
    with env.factory() as s:
        source_id = s.scalars(select(Source.id).where(Source.code == "eia")).one()
    recorder = EiaRawRecorder(env.factory, RawPayloadStore(env.data_dir), source_id)
    with env.client_factory(recorder) as client, pytest.raises(EiaLicenseError, match=code):
        eia_job._ingest_series(
            client,
            recorder,
            env.factory,
            spec=SPECS[code],
            license_=License.PERSONAL_USE_ONLY,
            expected=EXP[code],
            source_id=source_id,
            series_id=env.series_id(code),
            full=False,
            page_size=50,
        )
    assert_nothing_happened(env)


# --- configuration ------------------------------------------------------------------------------


def test_configuration_problems_stop_before_any_request(env: Env) -> None:
    no_key = Settings(_env_file=None, marketpulse_data_dir=env.data_dir)  # type: ignore[call-arg]
    with pytest.raises(EiaIngestionConfigError, match="EIA_API_KEY"):
        ingest_eia(settings=no_key, session_factory=env.factory, series_codes=[STORAGE])
    with pytest.raises(EiaIngestionConfigError, match="unknown EIA series"):
        env.run("nope")
    assert env.fake.requests == [] and env.runs() == []


def test_unsynced_catalog_is_a_configuration_error(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    settings = Settings(
        _env_file=None,
        eia_api_key=SecretStr(KEY),  # type: ignore[call-arg]
        marketpulse_data_dir=tmp_path,
    )
    with pytest.raises(EiaIngestionConfigError, match="catalog sync"):
        ingest_eia(settings=settings, session_factory=factory, series_codes=[STORAGE])


# --- CLI ----------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(env: Env, test_db_name: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    monkeypatch.setenv("POSTGRES_DB", test_db_name)
    monkeypatch.setenv("MARKETPULSE_DATA_DIR", str(env.data_dir))
    monkeypatch.setenv("EIA_API_KEY", KEY)
    monkeypatch.setattr(eia_job, "default_client_factory", lambda settings: env.client_factory)
    get_settings.cache_clear()
    yield env
    get_settings.cache_clear()


def test_cli_ingests_selected_series(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    add_series(cli_env.fake, STORAGE, weekly(10))
    assert main(["ingest", "eia", "--series", STORAGE]) == 0
    out = capsys.readouterr().out
    assert "vanished" in out and STORAGE in out and "1 succeeded" in out


def test_cli_reports_rejection_with_exit_code_2(
    cli_env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    add_series(cli_env.fake, STORAGE, weekly(40))
    assert main(["ingest", "eia", "--series", STORAGE]) == 0
    del cli_env.rows(STORAGE)[5:11]
    capsys.readouterr()
    assert main(["ingest", "eia", "--series", STORAGE, "--full"]) == 2
    out = capsys.readouterr().out
    assert "snapshot rejected; no observations changed" in out
    assert cli_row(out, STORAGE)["vanished"] == "6"  # counted, although rejected


def test_cli_rejects_unknown_series(cli_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ingest", "eia", "--series", "nope"]) == 1
    assert "unknown EIA series" in capsys.readouterr().out


@pytest.mark.parametrize("code", RESTRICTED)
def test_cli_refuses_each_restricted_series_cleanly(
    cli_env: Env, capsys: pytest.CaptureFixture[str], code: str
) -> None:
    add_daily(cli_env.fake, code)
    assert main(["ingest", "eia", "--series", code]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith(
        f"error: restricted EIA series cannot be ingested: {code} (personal_use_only; "
        "original source: Refinitiv, an LSEG business)."
    )
    assert "requires appropriate licensing" in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert_nothing_happened(cli_env)


def test_cli_default_run_reports_skipped_restricted_series(
    cli_env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eia_job, "load_catalog", lambda: eia_subset(STORAGE, *RESTRICTED))
    add_series(cli_env.fake, STORAGE, weekly(10))
    assert main(["ingest", "eia"]) == 0
    out = capsys.readouterr().out
    for code in RESTRICTED:
        assert (
            f"skipped {code}: licensed personal_use_only; not ingested without appropriate "
            "licensing" in out
        )
    assert "1 series: 1 succeeded" in out
    assert [r.params["series_code"] for r in cli_env.runs()] == [STORAGE]


def test_ingestion_never_stores_the_api_key(env: Env) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    env.run(STORAGE)
    with env.factory() as s:
        urls = " ".join(s.scalars(select(RawPayload.request_url)))
    assert "REDACTED" in urls and KEY not in urls
    for path in env.data_dir.rglob("*.gz"):
        assert KEY.encode() not in path.read_bytes()


def test_snapshot_time_is_after_the_run_started(env: Env) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    env.run(STORAGE)
    (run,) = env.runs()
    assert run.started_at <= snapshot_at(run) <= datetime.now(UTC)


def cli_row(out: str, code: str) -> dict[str, str]:
    """The CLI table row for `code`, by column name."""
    lines = out.splitlines()
    header = next(line for line in lines if line.startswith("series "))
    row = next(line for line in lines if line.startswith(f"{code} "))
    return dict(zip(header.split(), row.split(), strict=True))


# --- run finalization failures (D4 review) ------------------------------------------------------


def fail_run_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every final update of a run row fail, as if the connection dropped at that moment."""

    def broken_update(*args: object, **kwargs: object) -> object:
        raise OperationalError("UPDATE ops.ingestion_runs", {}, Exception("connection lost"))

    monkeypatch.setattr(runs_module, "update", broken_update)


def test_finalization_failure_after_commit_is_unfinalized_never_success(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    fail_run_finalization(monkeypatch)
    summary = env.run(STORAGE)
    (outcome,) = summary.outcomes
    assert (outcome.status, outcome.observations_committed, outcome.error) == (
        UNFINALIZED,
        True,
        None,
    )
    assert outcome.finalization_error and "connection lost" in outcome.finalization_error
    assert (summary.exit_code, outcome.inserted) == (1, 10)

    (run,) = env.runs()
    assert (run.status, run.finished_at) == ("running", None)  # the outcome was never recorded
    stored = env.observations(STORAGE)
    assert len(stored) == 10 and all(o.ingestion_run_id == run.id for o in stored)  # kept
    assert env.raw_payloads(run.id)

    # The next run is a fresh run that builds on the committed snapshot.
    monkeypatch.undo()
    (retry,) = env.run(STORAGE).outcomes
    assert (retry.mode, retry.status, retry.inserted, retry.unchanged) == (
        "incremental",
        "succeeded",
        0,
        10,
    )
    assert retry.run_id != run.id


def test_finalization_failure_after_a_failed_body_keeps_the_original_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    env.run(STORAGE)
    before = env.checksum()
    env.fake.faults.append(overstated_total)
    fail_run_finalization(monkeypatch)
    (outcome,) = env.run(STORAGE, full=True).outcomes
    assert (outcome.status, outcome.observations_committed) == (UNFINALIZED, False)
    assert outcome.error and outcome.error.startswith("EiaIncompleteError")  # not the DB error
    assert outcome.finalization_error and "connection lost" in outcome.finalization_error
    assert env.checksum() == before
    run = env.runs()[-1]
    assert run.status == "running"
    assert {i.check_name for i in env.issues(run.id)} == {"eia_incomplete"}


def test_finalization_failure_after_a_rejected_snapshot_reports_nothing_committed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_series(env.fake, STORAGE, weekly(40))
    env.run(STORAGE, page_size=50)
    before = env.checksum()
    del env.rows(STORAGE)[10:16]
    fail_run_finalization(monkeypatch)
    (outcome,) = env.run(STORAGE, full=True, page_size=50).outcomes
    assert (outcome.status, outcome.rejected, outcome.observations_committed) == (
        UNFINALIZED,
        True,
        False,
    )
    assert outcome.vanished == 6
    assert env.checksum() == before


def test_cli_reports_unfinalized_runs_with_exit_code_1(
    cli_env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    add_series(cli_env.fake, STORAGE, weekly(10))
    fail_run_finalization(monkeypatch)
    assert main(["ingest", "eia", "--series", STORAGE]) == 1
    out = capsys.readouterr().out
    assert cli_row(out, STORAGE)["status"] == "UNFINALIZED"
    assert "still says 'running'" in out and "WERE COMMITTED and are kept" in out
    assert "1 unfinalized" in out and "rolled back" not in out


# --- per-series isolation of database errors (D4 review) ----------------------------------------


def test_database_error_choosing_one_window_does_not_stop_the_other_series(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_series(env.fake, EX_SPR, weekly(10, base=400_000))
    add_series(env.fake, SPR, weekly(10, base=350_000))
    broken = env.series_id(EX_SPR)
    original = eia_job._latest_stored_period

    def flaky(factory: sessionmaker[Session], series_id: int) -> str | None:
        if series_id == broken:
            raise OperationalError("SELECT max(source_period)", {}, Exception("connection reset"))
        return original(factory, series_id)

    monkeypatch.setattr(eia_job, "_latest_stored_period", flaky)
    summary = env.run(EX_SPR, SPR)

    failed, ok = summary.outcomes
    assert (failed.code, failed.status, failed.mode, failed.vanished) == (
        EX_SPR,
        "failed",
        "unknown",
        None,
    )
    assert failed.error and failed.error.startswith("OperationalError")
    assert (ok.code, ok.status, ok.mode, ok.inserted) == (SPR, "succeeded", "full", 10)
    assert summary.exit_code == 1

    runs = {r.id: r for r in env.runs()}
    run = runs[failed.run_id or 0]  # the failure is recorded on the series' own run
    assert run.status == "failed" and run.error_message
    assert run.params["series_code"] == EX_SPR and "mode" not in run.params
    assert env.raw_payloads(run.id) == []
    facet = EXP[EX_SPR].facet_series
    assert not any(r.url.params.get("facets[series][0]") == facet for r in env.fake.requests)
    assert env.observations(EX_SPR) == [] and len(env.observations(SPR)) == 10


def test_run_creation_failure_for_one_series_does_not_stop_the_other(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_series(env.fake, EX_SPR, weekly(10, base=400_000))
    add_series(env.fake, SPR, weekly(10, base=350_000))
    original_enter = IngestionRunTracker.__enter__

    def enter(self: IngestionRunTracker) -> IngestionRunTracker:
        if self.params.get("series_code") == EX_SPR:
            raise OperationalError("INSERT INTO ops.ingestion_runs", {}, Exception("db down"))
        return original_enter(self)

    monkeypatch.setattr(IngestionRunTracker, "__enter__", enter)
    failed, ok = env.run(EX_SPR, SPR).outcomes
    assert (failed.status, failed.run_id, failed.mode) == ("failed", None, "unknown")
    assert failed.error and "db down" in failed.error
    assert ok.status == "succeeded" and len(env.observations(SPR)) == 10
    assert [r.params["series_code"] for r in env.runs()] == [SPR]


# --- failure diagnostics (D4 review) ------------------------------------------------------------

Responder = Callable[[httpx.Request], httpx.Response]


def on_data(respond: Responder) -> Callable[[httpx.Request], httpx.Response | None]:
    """A simulator override that answers only data requests."""

    def override(request: httpx.Request) -> httpx.Response | None:
        return respond(request) if request.url.path.endswith("/data") else None

    return override


def eia_error(status: int, message: str) -> Responder:
    return lambda request: httpx.Response(status, json={"error": message, "code": status})


def plain(status: int, body: bytes, content_type: str | None = None) -> Responder:
    headers = {"Content-Type": content_type} if content_type else {}
    return lambda request: httpx.Response(status, content=body, headers=headers)


def connection_refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def invalid_key(request: httpx.Request) -> httpx.Response:
    body = {"error": {"code": "API_KEY_INVALID", "message": "An invalid api_key was supplied."}}
    return httpx.Response(403, json=body)


@pytest.mark.parametrize(
    ("respond", "check", "raw_kept"),
    [
        pytest.param(
            plain(200, b"<html>maintenance</html>", "application/json"),
            "eia_malformed_response",
            1,
            id="malformed-json",
        ),
        pytest.param(eia_error(404, "No data for this route"), "eia_request_rejected", 0, id="404"),
        pytest.param(eia_error(400, "Invalid facet"), "eia_request_rejected", 0, id="400"),
        pytest.param(
            plain(404, b"<html>not found</html>"), "eia_request_rejected", 0, id="404-no-eia-body"
        ),
        pytest.param(invalid_key, None, 0, id="403-auth"),
        pytest.param(plain(429, b"slow down"), None, 0, id="429"),
        pytest.param(plain(500, b"oops"), None, 0, id="500"),
        pytest.param(plain(503, b"down"), None, 0, id="503"),
        pytest.param(connection_refused, None, 0, id="transport"),
    ],
)
def test_data_failures_record_issues_and_operational_failures_do_not(
    env: Env, respond: Responder, check: str | None, raw_kept: int
) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    env.run(STORAGE)
    before = env.checksum()
    env.fake.overrides.append(on_data(respond))
    summary = env.run(STORAGE, full=True)
    (outcome,) = summary.outcomes
    assert (summary.exit_code, outcome.status) == (1, "failed")
    assert env.checksum() == before
    run = env.runs()[-1]
    assert run.status == "failed" and run.error_message and KEY not in run.error_message
    assert len(env.raw_payloads(run.id)) == raw_kept
    issues = env.issues(run.id)
    if check is None:
        assert issues == [] and outcome.issues == 0  # operational: the run's error message only
        return
    assert {(i.check_name, i.severity) for i in issues} == {(check, "error")}
    assert outcome.issues == len(issues)
    assert all(KEY not in i.detail for i in issues)
    if check == "eia_request_rejected":
        (issue,) = issues
        assert "HTTP 4" in issue.detail and "drift" not in issue.detail  # cause not guessed


def test_failure_to_record_issues_does_not_hide_the_original_failure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    env.fake.faults.append(overstated_total)

    def broken_issue(**kwargs: object) -> object:
        raise OperationalError("INSERT INTO ops.data_quality_issues", {}, Exception("disk full"))

    monkeypatch.setattr(eia_job, "DataQualityIssue", broken_issue)
    (outcome,) = env.run(STORAGE).outcomes
    assert (outcome.status, outcome.issues) == ("failed", 0)
    assert outcome.error and outcome.error.startswith("EiaIncompleteError")
    run = env.runs()[-1]
    assert run.error_message and run.error_message.startswith("EiaIncompleteError")


def test_cli_shows_vanished_as_unknown_for_a_failed_run(
    cli_env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    add_series(cli_env.fake, STORAGE, weekly(10))
    cli_env.fake.faults.append(overstated_total)
    assert main(["ingest", "eia", "--series", STORAGE]) == 1
    assert cli_row(capsys.readouterr().out, STORAGE)["vanished"] == "-"  # not determined, not 0


# --- EIA API key and client configuration (D4 review) -------------------------------------------


@pytest.mark.parametrize("key", ["", "   ", "\t\n"])
def test_empty_or_blank_api_key_stops_before_any_request_or_run(
    env: Env, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    add_series(env.fake, STORAGE, weekly(10))
    # Guard: even if the check failed, no real client (and no live request) could be built.
    monkeypatch.setattr(eia_job, "default_client_factory", lambda settings: env.client_factory)
    settings = Settings(
        _env_file=None,
        eia_api_key=SecretStr(key),  # type: ignore[call-arg]
        marketpulse_data_dir=env.data_dir,
    )
    with pytest.raises(EiaIngestionConfigError, match="EIA_API_KEY is not set or is blank"):
        ingest_eia(settings=settings, session_factory=env.factory, series_codes=[STORAGE])
    assert env.fake.requests == [] and env.runs() == []


def test_client_configuration_error_becomes_a_configuration_error(env: Env) -> None:
    def blank_key_client(hook: ResponseHook) -> EiaClient:
        return EiaClient(SecretStr("   "), on_response=hook, transport=env.fake.transport())

    with pytest.raises(EiaIngestionConfigError, match="EIA client configuration"):
        ingest_eia(
            settings=env.settings,
            session_factory=env.factory,
            series_codes=[STORAGE],
            client_factory=blank_key_client,
        )
    assert env.fake.requests == [] and env.runs() == []


def test_cli_blank_api_key_is_a_clean_configuration_error(
    cli_env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whitespace only: an *empty* EIA_API_KEY means "not set" and falls back to the repository
    # .env file (env_ignore_empty), so it is covered by test_config and the ingest_eia test above.
    monkeypatch.setenv("EIA_API_KEY", "   ")
    get_settings.cache_clear()
    assert main(["ingest", "eia", "--series", STORAGE]) == 1
    captured = capsys.readouterr()
    assert "error: EIA_API_KEY is not set or is blank" in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert cli_env.fake.requests == [] and cli_env.runs() == []


def test_cli_client_configuration_error_has_no_traceback(
    cli_env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(settings: Settings) -> Callable[[ResponseHook], EiaClient]:
        def build(hook: ResponseHook) -> EiaClient:
            raise EiaConfigError("EIA API key is empty or blank")

        return build

    monkeypatch.setattr(eia_job, "default_client_factory", factory)
    assert main(["ingest", "eia", "--series", STORAGE]) == 1
    captured = capsys.readouterr()
    assert "error: EIA client configuration: EIA API key is empty or blank" in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert cli_env.runs() == []
