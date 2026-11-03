"""EiaClient against the EIA simulator (tests/eia_sim.py): pagination, warnings, truncation,
controlled failures, API errors, retries, timeouts, and API-key redaction.

All series values are SYNTHETIC. Tests that use the WTI catalog entry (to exercise the real
catalog wiring for a multi-page daily series) use made-up values, never Refinitiv data.
"""

import json
import logging
import traceback
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from eia_sim import INCOMPLETE_WARNING, FakeEia, SimRoute, SimSeries, make_rows
from marketpulse.core.config import Settings
from marketpulse.core.ratelimit import RateLimiter
from marketpulse.core.redaction import REDACTED
from marketpulse.sources.catalog import load_catalog
from marketpulse.sources.eia.client import (
    DATASET_DATA,
    DEFAULT_RATE_LIMIT_CALLS,
    DEFAULT_RATE_LIMIT_PERIOD_SECONDS,
    EiaAPIError,
    EiaClient,
    EiaConfigError,
)
from marketpulse.sources.eia.parse import EiaResponseError
from marketpulse.sources.eia.validate import (
    EiaEmptyResponseError,
    EiaIncompleteError,
    EiaMetadataError,
    EiaSeriesExpectation,
    EiaWarningError,
    expectation_for,
)
from marketpulse.sources.http import (
    FetchResult,
    ProviderHTTPError,
    ProviderTransportError,
    RetryPolicy,
)

KEY = "TEST-EIA-KEY-9a8b7c6d5e4f3a2b1c0d9e8f7a6b"  # synthetic
SPECS = {s.code: s for s in load_catalog().series_for("eia")}
WTI = expectation_for(SPECS["wti_spot"])  # route petroleum/pri/spt, facet RWTC, "$/BBL"
STORAGE = expectation_for(SPECS["l48_gas_storage"])


def business_days(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def fridays(start: date, n: int) -> list[str]:
    while start.weekday() != 4:
        start += timedelta(days=1)
    return [(start + timedelta(weeks=i)).isoformat() for i in range(n)]


class UnlimitedLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__(1_000_000, 1.0)
        self.calls = 0

    def acquire(self) -> float:
        self.calls += 1
        return 0.0


class Harness:
    """A FakeEia plus an EiaClient. `scripted` responses/exceptions are served before the
    simulator answers (for HTTP-level failures such as 429, 503, and timeouts)."""

    def __init__(self, *, retry: RetryPolicy | None = None) -> None:
        self.fake = FakeEia(KEY)
        self.scripted: list[httpx.Response | Exception] = []
        self.sleeps: list[float] = []
        self.raw: list[tuple[str, FetchResult]] = []
        self.limiter = UnlimitedLimiter()

        def handler(request: httpx.Request) -> httpx.Response:
            if self.scripted:
                self.fake.requests.append(request)
                item = self.scripted.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            return self.fake.handle(request)

        self.client = EiaClient(
            SecretStr(KEY),
            user_agent="MarketPulse-tests/0.1",
            rate_limiter=self.limiter,
            retry=retry or RetryPolicy(max_attempts=3),
            on_response=lambda dataset, result: self.raw.append((dataset, result)),
            transport=httpx.MockTransport(handler),
            sleep=self.sleeps.append,
        )

    def add_wti(self, n: int = 10_281) -> list[str]:
        """SYNTHETIC daily series under WTI's catalog route/facet (values made up)."""
        periods = business_days(date(1986, 1, 2), n)
        rows = make_rows(
            "RWTC",
            WTI.description,
            WTI.units,
            [(p, f"{20 + i % 7}.{i % 100:02d}") for i, p in enumerate(periods)],
        )
        self.fake.add(
            "PET.RWTC.D",
            SimSeries(WTI.route, "RWTC", "daily", rows),
            SimRoute([("daily", "YYYY-MM-DD"), ("weekly", "YYYY-MM-DD")]),
        )
        return periods

    def add_storage(self, n: int = 20) -> list[str]:
        periods = fridays(date(2010, 1, 1), n)
        rows = make_rows(
            STORAGE.facet_series,
            STORAGE.description,
            STORAGE.units,
            [(p, str(3000 + i)) for i, p in enumerate(periods)],
        )
        self.fake.add(
            "NG.NW2_EPG0_SWO_R48_BCF.W",
            SimSeries(STORAGE.route, STORAGE.facet_series, "weekly", rows),
            SimRoute([("weekly", "YYYY-MM-DD")]),
        )
        return periods

    def data_params(self) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.fake.data_requests()]


def fault_on_offset(offset: int, change: Callable[[dict[str, Any]], None]) -> Callable[..., Any]:
    """A simulator fault applied only to the data page requested at `offset`."""

    def fault(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any]:
        if int(request.url.params.get("offset", "0")) == offset:
            change(body)
        return body

    return fault


def assert_no_key(*texts: object) -> None:
    for text in texts:
        assert KEY not in str(text)


# --- pagination driven by the reported total ---------------------------------------------------


def test_full_fetch_pages_by_total_with_exact_offsets_and_values() -> None:
    h = Harness()
    periods = h.add_wti()
    rows = h.client.fetch_series(WTI)

    assert [r.period for r in rows] == periods  # every row once, ascending, nothing skipped
    assert rows[0].value == Decimal("20.00") and str(rows[1].value) == "21.01"  # exact text
    params = h.data_params()
    assert [p["offset"] for p in params] == ["0", "5000", "10000"]
    for p in params:
        assert p == {
            "frequency": "daily",
            "data[0]": "value",
            "facets[series][0]": "RWTC",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": "5000",
            "offset": p["offset"],
            "api_key": KEY,
        }
    assert h.fake.data_requests()[0].url.path == "/v2/petroleum/pri/spt/data"


def test_pagination_notice_on_every_non_final_page_is_tolerated() -> None:
    h = Harness()
    periods = h.add_storage(10)
    rows = h.client.fetch_series(STORAGE, page_size=4)  # pages of 4, 4, 2
    assert [r.period for r in rows] == periods
    warned = [[w["warning"] for w in json.loads(r.content).get("warnings", [])] for _, r in h.raw]
    assert warned == [["incomplete return"], ["incomplete return"], []]


def test_window_parameters_are_sent_and_respected() -> None:
    h = Harness()
    periods = h.add_storage(20)
    rows = h.client.fetch_series(STORAGE, start=periods[5], end=periods[9])
    assert [r.period for r in rows] == periods[5:10]
    assert (h.data_params()[0]["start"], h.data_params()[0]["end"]) == (periods[5], periods[9])


def test_page_size_is_bounded_by_the_5000_row_limit() -> None:
    h = Harness()
    for bad in (0, 5001):
        with pytest.raises(ValueError, match="page_size"):
            h.client.fetch_series(STORAGE, page_size=bad)
    assert h.fake.requests == []


def test_every_raw_page_reaches_the_hook_first_with_warnings_and_a_redacted_url() -> None:
    h = Harness()
    h.add_storage(6)
    h.client.fetch_series(STORAGE, page_size=4)
    assert [d for d, _ in h.raw] == [DATASET_DATA, DATASET_DATA]
    assert b'"warnings"' in h.raw[0][1].content  # warnings preserved in the raw record
    for _, result in h.raw:
        assert f"api_key={REDACTED}" in result.url
        assert_no_key(result.url, repr(result))


def test_parse_failure_still_hands_the_raw_page_to_the_hook() -> None:
    h = Harness()
    h.add_storage(3)
    h.fake.faults.append(fault_on_offset(0, lambda b: b["response"]["data"][1].update(value="W")))
    with pytest.raises(EiaResponseError, match="unrecognized value 'W'"):
        h.client.fetch_series(STORAGE)
    assert len(h.raw) == 1 and b'"W"' in h.raw[0][1].content


# --- controlled failures -----------------------------------------------------------------------


def _set_total(value: str) -> Callable[[dict[str, Any]], None]:
    return lambda b: b["response"].__setitem__("total", value)


def _rows_from_offset_zero(h: Harness) -> Callable[[dict[str, Any]], None]:
    first_page = h.fake.series["NW2_EPG0_SWO_R48_BCF"].rows[:4]
    return lambda b: b["response"].__setitem__("data", first_page)


@pytest.mark.parametrize(
    ("make_fault", "error", "match"),
    [
        (lambda h: fault_on_offset(4, _set_total("11")), EiaIncompleteError, "total changed"),
        (
            lambda h: fault_on_offset(4, _rows_from_offset_zero(h)),
            EiaIncompleteError,
            "duplicate periods",
        ),
        (
            lambda h: fault_on_offset(
                4, lambda b: b["request"]["params"].__setitem__("offset", "3")
            ),
            EiaIncompleteError,
            "executed offset 3",
        ),
        # EIA skips a row mid-series: the client requests the next page at its own count, so
        # pages overlap and the final count EQUALS the total (10 of 10). Only the duplicate
        # check catches it, which is why completeness never rests on the count alone.
        (
            lambda h: fault_on_offset(4, lambda b: b["response"]["data"].pop(0)),
            EiaIncompleteError,
            "duplicate periods",
        ),
        (
            lambda h: fault_on_offset(4, lambda b: b["response"].__setitem__("data", [])),
            EiaIncompleteError,
            "received 4 of 10",
        ),
        (
            lambda h: fault_on_offset(8, lambda b: b.__setitem__("warnings", [INCOMPLETE_WARNING])),
            EiaWarningError,
            "'incomplete return' on the final page",
        ),
        (
            lambda h: fault_on_offset(
                0,
                lambda b: b.setdefault("warnings", []).append(
                    {"warning": "data revision in progress", "description": "synthetic"}
                ),
            ),
            EiaWarningError,
            "data revision in progress",
        ),
        (
            lambda h: fault_on_offset(
                0,
                lambda b: b["request"]["params"].__setitem__(
                    "sort", [{"column": "period", "direction": "desc"}]
                ),
            ),
            EiaMetadataError,
            "did not execute the request as asked",
        ),
        (
            lambda h: fault_on_offset(
                0, lambda b: b["request"]["params"].__setitem__("facets", {"series": ["OTHER"]})
            ),
            EiaMetadataError,
            "series facet",
        ),
        (
            lambda h: fault_on_offset(
                0, lambda b: [r.update(units="MMCF") for r in b["response"]["data"]]
            ),
            EiaMetadataError,
            "units",
        ),
    ],
)
def test_controlled_failures(
    make_fault: Callable[[Harness], Any], error: type[Exception], match: str
) -> None:
    h = Harness()
    h.add_storage(10)
    h.fake.faults.append(make_fault(h))
    with pytest.raises(error, match=match) as info:
        h.client.fetch_series(STORAGE, page_size=4)
    assert_no_key(info.value, "".join(traceback.format_exception(info.value)))


def test_extra_rows_beyond_the_requested_length_fail() -> None:
    h = Harness()
    periods = h.add_storage(10)
    extra = make_rows(STORAGE.facet_series, STORAGE.description, STORAGE.units, [(periods[4], "1")])
    h.fake.faults.append(fault_on_offset(0, lambda b: b["response"]["data"].extend(extra)))
    with pytest.raises(EiaIncompleteError, match="more than requested"):
        h.client.fetch_series(STORAGE, page_size=4)


def test_pagination_that_never_advances_is_stopped() -> None:
    h = Harness()
    periods = h.add_storage(50)
    rows = h.fake.series[STORAGE.facet_series].rows

    def one_row(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any]:
        offset = int(request.url.params["offset"])
        body["response"]["data"] = rows[offset : offset + 1]  # 1 row per page, total stays 50
        return body

    h.fake.faults.append(one_row)
    with pytest.raises(EiaIncompleteError, match="not advancing"):
        h.client.fetch_series(STORAGE, page_size=25)
    assert len(h.fake.data_requests()) == 3  # ceil(50 / 25) + 1, then stopped
    assert periods  # data existed; the API just would not page through it


def test_empty_response_for_a_catalogued_series_fails() -> None:
    h = Harness()
    h.fake.routes[STORAGE.route] = SimRoute([("weekly", "YYYY-MM-DD")])  # route, but no series
    with pytest.raises(EiaEmptyResponseError, match="no data"):
        h.client.fetch_series(STORAGE)
    assert h.client.fetch_series(STORAGE, allow_empty=True) == []


def test_truncation_without_warnings_is_still_caught() -> None:
    """Like the series-ID route: a short page, no warnings, and no more data after it."""
    h = Harness()
    h.add_storage(10)

    def silent(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any]:
        body.pop("warnings", None)
        offset = int(request.url.params["offset"])
        if offset == 4:
            body["response"]["data"] = body["response"]["data"][:2]
        elif offset > 4:
            body["response"]["data"] = []  # the rest never arrives
        return body

    h.fake.faults.append(silent)
    with pytest.raises(EiaIncompleteError, match="received 6 of 10"):
        h.client.fetch_series(STORAGE, page_size=4)


def test_a_short_page_followed_by_the_remaining_rows_is_complete() -> None:
    """A smaller-than-requested page is not truncation if the remaining rows follow: the client
    requests the next page at its own count and collects every row exactly once."""
    h = Harness()
    periods = h.add_storage(10)

    def short(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any]:
        if int(request.url.params["offset"]) == 4:
            body["response"]["data"] = body["response"]["data"][:2]
        return body

    h.fake.faults.append(short)
    assert [r.period for r in h.client.fetch_series(STORAGE, page_size=4)] == periods
    assert [p["offset"] for p in h.data_params()] == ["0", "4", "6"]


# --- API error bodies (both shapes) ------------------------------------------------------------


def test_unknown_route_is_an_api_error_without_retry() -> None:
    h = Harness()
    bad = EiaSeriesExpectation(
        "bad", "petroleum/nope/route", "X1", "weekly", "YYYY-MM-DD", "d", "u", "week_ending_friday"
    )
    with pytest.raises(EiaAPIError, match="Requested path /petroleum/nope/route is not valid"):
        h.client.fetch_series(bad)
    assert len(h.fake.requests) == 1 and h.sleeps == []


def test_invalid_key_uses_the_nested_error_shape_and_fails_fast() -> None:
    fake = FakeEia("the-real-key-is-different")
    client = EiaClient(
        SecretStr(KEY),
        transport=fake.transport(),
        rate_limiter=UnlimitedLimiter(),
        sleep=lambda _: None,
    )
    with pytest.raises(EiaAPIError) as info:
        client.fetch_series(STORAGE)
    assert info.value.status_code == 403
    assert info.value.error_message.startswith("API_KEY_INVALID: An invalid api_key")
    assert len(fake.requests) == 1
    assert_no_key(info.value, "".join(traceback.format_exception(info.value)))


def test_unknown_series_id_uses_the_flat_error_shape() -> None:
    h = Harness()
    with pytest.raises(EiaAPIError) as info:
        h.client.resolve_series_id("PET.MARKETPULSENOPE.W")
    assert (info.value.status_code, info.value.error_message) == (
        404,
        "Series ID 'PET.MARKETPULSENOPE.W' is not valid.",
    )


def test_non_json_error_body_stays_a_provider_http_error() -> None:
    h = Harness(retry=RetryPolicy(max_attempts=1))
    h.add_storage(3)
    h.scripted.append(httpx.Response(502, content=b"<html>Bad Gateway</html>"))
    with pytest.raises(ProviderHTTPError) as info:
        h.client.fetch_series(STORAGE)
    assert not isinstance(info.value, EiaAPIError)


# --- retries, rate limiting, and timeouts -------------------------------------------------------


def test_429_is_retried_honoring_retry_after() -> None:
    h = Harness()
    periods = h.add_storage(3)
    h.scripted.append(httpx.Response(429, headers={"Retry-After": "7"}))
    assert [r.period for r in h.client.fetch_series(STORAGE)] == periods
    assert h.sleeps == [7.0]
    assert h.limiter.calls == 2  # the rate limiter is consulted for every attempt


def test_server_errors_and_timeouts_are_retried() -> None:
    h = Harness()
    periods = h.add_storage(3)
    h.scripted += [httpx.Response(503), httpx.ReadTimeout("timed out")]
    assert [r.period for r in h.client.fetch_series(STORAGE)] == periods
    assert len(h.fake.requests) == 3


def test_persistent_timeouts_fail_without_exposing_the_key() -> None:
    h = Harness(retry=RetryPolicy(max_attempts=2))
    h.add_storage(3)
    h.scripted += [httpx.ConnectTimeout(f"timeout api_key={KEY}")] * 2
    with pytest.raises(ProviderTransportError) as info:
        h.client.fetch_series(STORAGE)
    assert_no_key(info.value, "".join(traceback.format_exception(info.value)))


def test_default_rate_limit_is_one_per_second_with_bursts_of_five() -> None:
    assert (DEFAULT_RATE_LIMIT_CALLS, DEFAULT_RATE_LIMIT_PERIOD_SECONDS) == (5, 5.0)
    clock = {"now": 0.0}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    limiter = RateLimiter(
        DEFAULT_RATE_LIMIT_CALLS,
        DEFAULT_RATE_LIMIT_PERIOD_SECONDS,
        clock=lambda: clock["now"],
        sleep=sleep,
    )
    waits = [limiter.acquire() for _ in range(15)]
    assert waits[:5] == [0.0] * 5  # burst of five
    assert clock["now"] == pytest.approx(10.0)  # then one per second


# --- series-ID route: resolution and verification only -----------------------------------------


def test_resolve_and_verify_route_against_the_catalog() -> None:
    h = Harness()
    h.add_wti(10)
    assert h.client.resolve_series_id("PET.RWTC.D") == {
        "route": "petroleum/pri/spt",
        "facet_series": "RWTC",
        "frequency": "daily",
    }
    assert h.client.verify_route(WTI, "PET.RWTC.D") == []


def test_route_drift_is_reported() -> None:
    h = Harness()
    h.add_wti(10)
    moved = h.fake.series["RWTC"]
    moved.route = "petroleum/pri/new"
    h.fake.routes["petroleum/pri/new"] = h.fake.routes[WTI.route]
    assert h.client.verify_route(WTI, "PET.RWTC.D") == [
        "wti_spot: series-ID route maps to route 'petroleum/pri/new', catalog says "
        "'petroleum/pri/spt'"
    ]


def test_route_metadata() -> None:
    h = Harness()
    h.add_storage(3)
    meta = h.client.route_metadata(STORAGE.route)
    assert meta.frequencies == {"weekly": "YYYY-MM-DD"} and meta.data_columns == ("value",)


# --- configuration and key safety --------------------------------------------------------------


def test_from_settings_requires_a_key() -> None:
    with pytest.raises(EiaConfigError, match="EIA_API_KEY is not set"):
        EiaClient.from_settings(Settings(_env_file=None, eia_api_key=None))  # type: ignore[call-arg]


@pytest.mark.parametrize("key", ["", "   ", "\t\n"])
def test_empty_or_blank_key_is_rejected(key: str) -> None:
    with pytest.raises(EiaConfigError, match="empty or blank"):
        EiaClient(SecretStr(key))


def test_from_settings_uses_the_configured_user_agent() -> None:
    fake = FakeEia(KEY)
    settings = Settings(
        _env_file=None,
        eia_api_key=SecretStr(KEY),  # type: ignore[call-arg]
        http_user_agent="MarketPulse/0.1 (tests@example.invalid)",
    )
    with (
        EiaClient.from_settings(settings, transport=fake.transport()) as client,
        pytest.raises(EiaAPIError),
    ):
        client.resolve_series_id("PET.UNKNOWN.W")
    assert fake.requests[0].headers["User-Agent"] == "MarketPulse/0.1 (tests@example.invalid)"


def test_key_never_appears_in_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    h = Harness()
    h.add_storage(6)
    h.scripted.append(httpx.Response(503))
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        try:
            h.client.fetch_series(STORAGE, page_size=4)
        finally:
            logging.getLogger("httpx").setLevel(logging.NOTSET)
    assert any(r.name == "httpx" for r in caplog.records), "expected httpx request logs"
    assert_no_key(repr(h.client), caplog.text, *(vars(r) for r in caplog.records))
