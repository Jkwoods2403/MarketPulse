"""FredClient against a mock transport. Real recorded bodies where format matters; synthetic
responses (labelled) for paging failures, retries, and windowing."""

import logging
import traceback
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from fred_mock import TEST_KEY, FredHarness, fixture, json_response
from marketpulse.core.config import Settings
from marketpulse.core.redaction import REDACTED
from marketpulse.sources.fred import REALTIME_OPEN_END
from marketpulse.sources.fred.client import (
    DATASET_OBSERVATIONS,
    DATASET_SERIES,
    FredAPIError,
    FredClient,
    FredConfigError,
    FredPaginationError,
)
from marketpulse.sources.fred.parse import FredResponseError
from marketpulse.sources.fred.windows import RealtimeWindow
from marketpulse.sources.http import ProviderTransportError, RetryPolicy

FULL = RealtimeWindow(date(1776, 7, 4), REALTIME_OPEN_END)


def assert_no_key(*texts: object) -> None:
    for text in texts:
        assert TEST_KEY not in str(text)


# --- real fixtures: formats ------------------------------------------------------------------


def test_series_metadata_from_real_response() -> None:
    h = FredHarness(lambda r: json_response(fixture("series_gdpc1")))
    meta = h.client.series_metadata("GDPC1")

    assert (meta.series_id, meta.frequency_short, meta.seasonal_adjustment_short) == (
        "GDPC1",
        "Q",
        "SAAR",
    )
    assert meta.units == "Billions of Chained 2017 Dollars"
    assert meta.last_updated.utcoffset() == timedelta(hours=-5)
    assert h.requests[0].url.path == "/fred/series"
    assert h.params() == {"series_id": "GDPC1", "file_type": "json", "api_key": TEST_KEY}


def test_series_metadata_rejects_a_different_series_in_the_response() -> None:
    h = FredHarness(lambda r: json_response(fixture("series_dgs10")))
    with pytest.raises(FredPaginationError, match="returned 'DGS10'"):
        h.client.series_metadata("GDPC1")


def test_observations_pages_through_real_revision_history() -> None:
    pages = {
        "0": fixture("observations_gdpc1_2024q1q2_page1"),
        "6": fixture("observations_gdpc1_2024q1q2_page2"),
    }
    h = FredHarness(lambda r: json_response(pages[r.url.params["offset"]]))

    rows = h.client.observations(
        "GDPC1",
        FULL,
        observation_start=date(2024, 1, 1),
        observation_end=date(2024, 4, 1),
        page_size=6,
    )

    assert len(rows) == 11
    assert [h.params(i)["offset"] for i in range(2)] == ["0", "6"]
    q1 = [r for r in rows if r.observation_date == date(2024, 1, 1)]
    assert q1[0].realtime_start == date(2024, 4, 25)
    assert q1[0].value == Decimal("22768.866")  # first release, verbatim
    assert q1[-1].realtime_end == REALTIME_OPEN_END


def test_observation_request_parameters() -> None:
    h = FredHarness(lambda r: json_response(fixture("observations_cpiaucsl_2026aug_sep")))
    h.client.observations(
        "CPIAUCSL", FULL, observation_start=date(2026, 8, 1), observation_end=date(2026, 9, 1)
    )
    assert h.params() == {
        "series_id": "CPIAUCSL",
        "output_type": "1",
        "units": "lin",
        "sort_order": "asc",
        "realtime_start": "1776-07-04",
        "realtime_end": "9999-12-31",
        "observation_start": "2026-08-01",
        "observation_end": "2026-09-01",
        "limit": "100000",
        "offset": "0",
        "file_type": "json",
        "api_key": TEST_KEY,
    }


def test_real_holiday_missing_values_are_preserved_as_none() -> None:
    window = RealtimeWindow(date(2026, 10, 1), REALTIME_OPEN_END)
    h = FredHarness(lambda r: json_response(fixture("observations_dgs10_holidays_window")))
    rows = h.client.observations("DGS10", window)

    by_date = {r.observation_date: r.value for r in rows}
    assert by_date[date(2025, 12, 25)] is None
    assert by_date[date(2026, 1, 1)] is None
    assert by_date[date(2025, 12, 24)] == Decimal("4.15")
    assert len(rows) == 8  # nothing filled in for weekends or holidays


def test_vintage_dates_real_first_page_then_changed_total_is_rejected() -> None:
    real_first_page = fixture("vintagedates_gdpc1_limit5")
    remaining = ["1992-04-29", "1992-05-28"]  # synthetic continuation

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.params["offset"] == "0":
            return json_response(real_first_page)
        return json_response({"count": 7, "offset": 5, "limit": 5, "vintage_dates": remaining})

    h = FredHarness(handler)
    # The real first page reports count=420; the synthetic second page claims 7 -> rejected.
    with pytest.raises(FredPaginationError, match="total changed"):
        h.client.vintage_dates("GDPC1", page_size=5)


def synthetic_vintage_pages(dates: list[str], page_size: int) -> Any:
    def handler(r: httpx.Request) -> httpx.Response:
        offset = int(r.url.params["offset"])
        chunk = dates[offset : offset + page_size]
        return json_response(
            {"count": len(dates), "offset": offset, "limit": page_size, "vintage_dates": chunk}
        )

    return handler


def test_vintage_dates_across_multiple_pages() -> None:
    dates = [(date(2005, 6, 28) + timedelta(days=i)).isoformat() for i in range(23)]
    h = FredHarness(synthetic_vintage_pages(dates, 10))
    result = h.client.vintage_dates("DGS10", page_size=10)

    assert [d.isoformat() for d in result] == dates
    assert [h.params(i)["offset"] for i in range(3)] == ["0", "10", "20"]
    assert all(h.params(i)["sort_order"] == "asc" for i in range(3))


# --- windowed history ------------------------------------------------------------------------


def test_fetch_history_uses_windows_and_rejoins_splits() -> None:
    """Synthetic: 5 vintages, max 2 per window -> 3 windows; one unchanged value spans all."""
    vintages = ["2020-01-01", "2021-01-01", "2022-01-01", "2023-01-01", "2024-01-01"]

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("vintagedates"):
            return json_response(
                {"count": 5, "offset": 0, "limit": 10000, "vintage_dates": vintages}
            )
        start, end = r.url.params["realtime_start"], r.url.params["realtime_end"]
        rows = [
            {"date": "2019-01-01", "realtime_start": start, "realtime_end": end, "value": "1.5"}
        ]
        return json_response(
            {
                "realtime_start": start,
                "realtime_end": end,
                "count": 1,
                "offset": 0,
                "limit": 100000,
                "observations": rows,
            }
        )

    h = FredHarness(handler)
    windows, rows = h.client.fetch_history("DGS10", limit=4)  # 2 listed per window

    assert [(w.start.isoformat(), w.end.isoformat()) for w in windows] == [
        ("2020-01-01", "2021-12-31"),
        ("2022-01-01", "2023-12-31"),
        ("2024-01-01", "9999-12-31"),
    ]
    assert [
        (p.get("realtime_start"), p.get("realtime_end")) for p in (h.params(i) for i in range(1, 4))
    ] == [(w.as_params()["realtime_start"], w.as_params()["realtime_end"]) for w in windows]
    assert len(rows) == 1
    assert (rows[0].realtime_start, rows[0].realtime_end) == (date(2020, 1, 1), REALTIME_OPEN_END)


def test_fetch_history_incremental_start() -> None:
    vintages = ["2020-01-01", "2026-10-01", "2026-10-15"]

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("vintagedates"):
            return json_response(
                {"count": 3, "offset": 0, "limit": 10000, "vintage_dates": vintages}
            )
        return json_response(
            {
                "realtime_start": r.url.params["realtime_start"],
                "realtime_end": r.url.params["realtime_end"],
                "count": 0,
                "offset": 0,
                "limit": 100000,
                "observations": [],
            }
        )

    h = FredHarness(handler)
    windows, _ = h.client.fetch_history("DGS10", start=date(2026, 10, 10))
    assert windows == [RealtimeWindow(date(2026, 10, 10), REALTIME_OPEN_END)]


# --- paging consistency (synthetic) ----------------------------------------------------------


def obs_page(
    offset: int, count: int, n: int, *, start: str = "1776-07-04", end: str = "9999-12-31"
) -> dict[str, Any]:
    rows = [
        {
            "date": (date(2000, 1, 1) + timedelta(days=offset + i)).isoformat(),
            "realtime_start": "2005-06-28",
            "realtime_end": "9999-12-31",
            "value": "1",
        }
        for i in range(n)
    ]
    return {
        "realtime_start": start,
        "realtime_end": end,
        "count": count,
        "offset": offset,
        "limit": 2,
        "observations": rows,
    }


@pytest.mark.parametrize(
    ("pages", "match"),
    [
        ([obs_page(0, 4, 2), obs_page(2, 5, 2)], "total changed"),
        ([obs_page(0, 4, 2), obs_page(2, 4, 0)], "empty page"),
        ([obs_page(0, 4, 2), obs_page(3, 4, 1)], "expected page offset 2"),
        ([obs_page(0, 4, 2, start="2000-01-01")], "requested real-time window"),
    ],
)
def test_inconsistent_paging_is_rejected(pages: list[dict[str, Any]], match: str) -> None:
    responses = iter(pages)
    h = FredHarness(lambda r: json_response(next(responses)))
    with pytest.raises(FredPaginationError, match=match):
        h.client.observations("DGS10", FULL, page_size=2)


def test_parse_failure_still_hands_raw_response_to_hook_first() -> None:
    h = FredHarness(lambda r: json_response(b'{"realtime_start": "bad"}'))
    with pytest.raises(FredResponseError):
        h.client.observations("DGS10", FULL)
    assert [dataset for dataset, _ in h.raw] == [DATASET_OBSERVATIONS]
    assert h.raw[0][1].content == b'{"realtime_start": "bad"}'


def test_hook_receives_every_raw_response_with_redacted_url() -> None:
    pages = {
        "0": fixture("observations_gdpc1_2024q1q2_page1"),
        "6": fixture("observations_gdpc1_2024q1q2_page2"),
    }

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/fred/series":
            return json_response(fixture("series_gdpc1"))
        return json_response(pages[r.url.params["offset"]])

    h = FredHarness(handler)
    h.client.series_metadata("GDPC1")
    h.client.observations(
        "GDPC1",
        FULL,
        observation_start=date(2024, 1, 1),
        observation_end=date(2024, 4, 1),
        page_size=6,
    )

    assert [d for d, _ in h.raw] == [DATASET_SERIES, DATASET_OBSERVATIONS, DATASET_OBSERVATIONS]
    assert h.raw[1][1].content == fixture("observations_gdpc1_2024q1q2_page1")  # byte-exact
    for _, result in h.raw:
        assert f"api_key={REDACTED}" in result.url
        assert_no_key(result.url, repr(result))


# --- FRED errors (real bodies) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("error_nonexistent_series", "The series does not exist."),
        ("error_invalid_series_id", "Series IDs should be 25 or less alphanumeric characters."),
        ("error_too_many_vintages", "exceeds the maximum number of vintage dates"),
    ],
)
def test_real_fred_error_bodies_become_fred_api_errors_without_retry(
    name: str, message: str
) -> None:
    h = FredHarness(lambda r: json_response(fixture(name), status=400))
    with pytest.raises(FredAPIError) as info:
        h.client.observations("DGS10", FULL)

    assert info.value.status_code == 400
    assert message in info.value.error_message
    assert len(h.requests) == 1  # 4xx: no retry
    assert h.sleeps == []
    assert h.raw == []  # error bodies are not raw data
    assert_no_key(info.value, "".join(traceback.format_exception(info.value)))


def test_non_json_error_body_stays_a_provider_http_error() -> None:
    from marketpulse.sources.http import ProviderHTTPError

    h = FredHarness(lambda r: httpx.Response(403, content=b"<html>Forbidden</html>"))
    with pytest.raises(ProviderHTTPError) as info:
        h.client.series_metadata("GDPC1")
    assert not isinstance(info.value, FredAPIError)


# --- rate limits, retries, timeouts (synthetic transport behaviour) --------------------------


def test_429_is_retried_honoring_retry_after() -> None:
    responses = iter(
        [
            json_response(
                b'{"error_code":429,"error_message":"Too Many Requests."}',
                status=429,
                **{"Retry-After": "20"},
            ),
            json_response(fixture("series_gdpc1")),
        ]
    )
    h = FredHarness(lambda r: next(responses))
    assert h.client.series_metadata("GDPC1").series_id == "GDPC1"
    assert h.sleeps == [20.0]
    assert h.limiter.calls == 2  # rate limiter consulted for every attempt


def test_server_errors_and_timeouts_are_retried_then_succeed() -> None:
    def make() -> Any:
        items: list[Any] = [
            httpx.ReadTimeout("timed out"),
            json_response(b"{}", status=503),
            json_response(fixture("series_gdpc1")),
        ]

        def handler(r: httpx.Request) -> httpx.Response:
            item = items.pop(0)
            if isinstance(item, Exception):
                raise item
            return item  # type: ignore[no-any-return]

        return handler

    h = FredHarness(make())
    assert h.client.series_metadata("GDPC1").series_id == "GDPC1"
    assert len(h.requests) == 3


def test_persistent_timeouts_raise_transport_error_without_key() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout(f"timed out connecting with api_key={TEST_KEY}")

    h = FredHarness(handler, retry=RetryPolicy(max_attempts=2))
    with pytest.raises(ProviderTransportError) as info:
        h.client.series_metadata("GDPC1")
    assert len(h.requests) == 2
    assert_no_key(info.value, "".join(traceback.format_exception(info.value)))


def test_default_rate_limit_stays_below_fred_documented_120_per_minute() -> None:
    from marketpulse.sources.fred.client import (
        DEFAULT_RATE_LIMIT_CALLS,
        DEFAULT_RATE_LIMIT_PERIOD_SECONDS,
    )

    sustained_per_minute = DEFAULT_RATE_LIMIT_CALLS / DEFAULT_RATE_LIMIT_PERIOD_SECONDS * 60
    worst_minute = sustained_per_minute + DEFAULT_RATE_LIMIT_CALLS  # burst + refill
    assert worst_minute < 120


# --- configuration and key safety ------------------------------------------------------------


@pytest.mark.parametrize("bad", ["dgs10", "DGS_10", "DGS-10", "", "A" * 26, "GDPC1&api_key=x"])
def test_invalid_series_ids_are_rejected_before_any_request(bad: str) -> None:
    h = FredHarness(lambda r: json_response(fixture("series_gdpc1")))
    with pytest.raises(ValueError, match="invalid FRED series ID"):
        h.client.series_metadata(bad)
    assert h.requests == []


def test_from_settings_requires_a_key() -> None:
    with pytest.raises(FredConfigError, match="FRED_API_KEY is not set"):
        FredClient.from_settings(Settings(_env_file=None, fred_api_key=None))  # type: ignore[call-arg]


def test_empty_key_is_rejected() -> None:
    with pytest.raises(FredConfigError, match="empty"):
        FredClient(SecretStr(""))


def test_from_settings_uses_configured_user_agent() -> None:
    sent: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        sent.append(r)
        return json_response(fixture("series_gdpc1"))

    settings = Settings(
        _env_file=None,
        fred_api_key=SecretStr(TEST_KEY),  # type: ignore[call-arg]
        http_user_agent="MarketPulse/0.1 (tests@example.invalid)",
    )
    with FredClient.from_settings(settings, transport=httpx.MockTransport(handler)) as client:
        client.series_metadata("GDPC1")
    assert sent[0].headers["User-Agent"] == "MarketPulse/0.1 (tests@example.invalid)"


def test_key_never_appears_in_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    responses = iter([json_response(b"{}", status=503), json_response(fixture("series_gdpc1"))])
    h = FredHarness(lambda r: next(responses))
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        try:
            h.client.series_metadata("GDPC1")
        finally:
            logging.getLogger("httpx").setLevel(logging.NOTSET)

    assert any(r.name == "httpx" for r in caplog.records)
    assert_no_key(repr(h.client), caplog.text, *(vars(r) for r in caplog.records))
