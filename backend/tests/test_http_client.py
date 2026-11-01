"""ProviderHttpClient: behavior, failure handling, and credential protection.

All responses come from httpx.MockTransport; no network access. Secrets are synthetic.
"""

import logging
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from pydantic import SecretStr

from marketpulse.core.ratelimit import RateLimiter
from marketpulse.core.redaction import REDACTED
from marketpulse.sources.http import (
    FetchResult,
    ProviderClientConfig,
    ProviderError,
    ProviderHttpClient,
    ProviderHTTPError,
    ProviderTransportError,
    ResponseTooLargeError,
    RetryPolicy,
)

SECRET = "TEST-SECRET-9f8e7d6c5b4a"
HEADER_SECRET = "TEST-HEADER-SECRET-1a2b3c4d"
BASE = "https://api.example.com/v1/"

Handler = Callable[[httpx.Request], httpx.Response]


class CountingLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__(1000, 1.0)
        self.calls = 0

    def acquire(self) -> float:
        self.calls += 1
        return 0.0


class Harness:
    """A client wired to a scripted sequence of responses, recording requests and sleeps."""

    def __init__(
        self,
        responses: list[httpx.Response | Exception],
        *,
        secret_headers: dict[str, SecretStr] | None = None,
        retry: RetryPolicy | None = None,
        max_response_bytes: int = 1024 * 1024,
    ) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.limiter = CountingLimiter()
        config = ProviderClientConfig(
            base_url=BASE,
            user_agent="MarketPulse-tests/0.1",
            rate_limiter=self.limiter,
            secret_params={"api_key": SecretStr(SECRET)},
            secret_headers=secret_headers or {},
            retry=retry or RetryPolicy(max_attempts=3),
            max_response_bytes=max_response_bytes,
        )
        self.client = ProviderHttpClient(
            config,
            transport=httpx.MockTransport(self._handle),
            sleep=self.sleeps.append,
            jitter=lambda: 1.0,  # deterministic: full backoff
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, path: str = "series", **params: str) -> FetchResult:
        return self.client.get(path, params or {"series_id": "DGS10"})


def json_response(
    status: int = 200, body: bytes = b'{"ok": true}', **headers: str
) -> httpx.Response:
    return httpx.Response(
        status, content=body, headers={"Content-Type": "application/json", **headers}
    )


def assert_no_secret(*texts: object) -> None:
    for text in texts:
        assert SECRET not in str(text)
        assert HEADER_SECRET not in str(text)


# --- success path ----------------------------------------------------------------------------


def test_success_returns_content_and_lineage_metadata() -> None:
    h = Harness([json_response()])
    before = datetime.now(UTC)
    result = h.get()

    assert result.content == b'{"ok": true}'
    assert result.status_code == 200
    assert result.content_type == "application/json"
    assert result.attempts == 1
    assert result.retrieved_at.tzinfo is not None
    assert before <= result.retrieved_at <= datetime.now(UTC)
    assert result.url.startswith("https://api.example.com/v1/series?")


def test_credentials_are_sent_to_provider_but_redacted_in_result() -> None:
    h = Harness([json_response()], secret_headers={"X-Api-Key": SecretStr(HEADER_SECRET)})
    result = h.get()

    sent = h.requests[0]
    assert sent.url.params["api_key"] == SECRET
    assert sent.headers["X-Api-Key"] == HEADER_SECRET
    assert sent.headers["User-Agent"] == "MarketPulse-tests/0.1"
    assert f"api_key={REDACTED}" in result.url
    assert_no_secret(result.url, repr(result))


def test_rate_limiter_is_acquired_for_every_attempt() -> None:
    h = Harness([json_response(503), json_response(503), json_response()])
    h.get()
    assert h.limiter.calls == 3


# --- retries ---------------------------------------------------------------------------------


def test_retries_transient_5xx_then_succeeds_with_exponential_backoff() -> None:
    h = Harness([json_response(502), json_response(503), json_response()])
    result = h.get()

    assert result.attempts == 3
    assert h.sleeps == [1.0, 2.0]


def test_backoff_jitter_stays_within_half_to_full_delay() -> None:
    config = ProviderClientConfig(
        base_url=BASE,
        user_agent="t",
        rate_limiter=CountingLimiter(),
        retry=RetryPolicy(max_attempts=2),
    )
    sleeps: list[float] = []
    responses = iter([json_response(503), json_response()])
    client = ProviderHttpClient(
        config,
        transport=httpx.MockTransport(lambda r: next(responses)),
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )
    client.get("x")
    assert sleeps == [0.5]


def test_backoff_is_capped() -> None:
    h = Harness(
        [json_response(500)] * 5 + [json_response()],
        retry=RetryPolicy(max_attempts=6, backoff_base_seconds=10, backoff_max_seconds=25),
    )
    h.get()
    assert h.sleeps == [10.0, 20.0, 25.0, 25.0, 25.0]


def test_honors_retry_after_seconds_on_429() -> None:
    h = Harness([json_response(429, **{"Retry-After": "7"}), json_response()])
    h.get()
    assert h.sleeps == [7.0]


def test_honors_retry_after_http_date() -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    h = Harness([json_response(503, **{"Retry-After": when}), json_response()])
    h.get()
    assert 25.0 <= h.sleeps[0] <= 30.0


def test_retry_after_is_capped() -> None:
    h = Harness(
        [json_response(429, **{"Retry-After": "86400"}), json_response()],
        retry=RetryPolicy(max_attempts=2, max_retry_after_seconds=60),
    )
    h.get()
    assert h.sleeps == [60.0]


def test_gives_up_after_max_attempts_with_http_error() -> None:
    h = Harness([json_response(500)] * 3)
    with pytest.raises(ProviderHTTPError) as info:
        h.get()
    assert info.value.status_code == 500
    assert len(h.requests) == 3
    assert len(h.sleeps) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_fail_immediately_without_retry(status: int) -> None:
    h = Harness([json_response(status, b'{"error": "bad request"}')])
    with pytest.raises(ProviderHTTPError) as info:
        h.get()
    assert info.value.status_code == status
    assert "bad request" in info.value.body_excerpt
    assert len(h.requests) == 1
    assert h.sleeps == []


def test_transport_errors_are_retried() -> None:
    h = Harness(
        [httpx.ConnectError("connection refused"), httpx.ReadTimeout("timed out"), json_response()]
    )
    assert h.get().attempts == 3


def test_transport_error_after_retries_raises_provider_transport_error() -> None:
    h = Harness([httpx.ConnectError("connection refused")] * 3)
    with pytest.raises(ProviderTransportError, match="ConnectError"):
        h.get()
    assert len(h.requests) == 3


def test_corrupt_compressed_body_fails_fast_as_provider_error() -> None:
    corrupt = httpx.Response(
        200, stream=httpx.ByteStream(b"not gzip data"), headers={"Content-Encoding": "gzip"}
    )
    h = Harness([corrupt])
    with pytest.raises(ProviderTransportError, match="DecodingError"):
        h.get()
    assert len(h.requests) == 1  # not transient: no retry
    assert h.sleeps == []


def test_redirects_are_not_followed() -> None:
    h = Harness([httpx.Response(302, headers={"Location": "https://evil.example.net/steal"})])
    with pytest.raises(ProviderHTTPError) as info:
        h.get()
    assert info.value.status_code == 302
    assert len(h.requests) == 1


# --- response size limits ----------------------------------------------------------------------


def test_rejects_response_with_oversized_content_length() -> None:
    h = Harness([json_response(body=b"x" * 2048)], max_response_bytes=1024)
    with pytest.raises(ResponseTooLargeError):
        h.get()


def test_rejects_oversized_streamed_response_without_content_length() -> None:
    def chunks() -> httpx.SyncByteStream:
        class Stream(httpx.SyncByteStream):
            def __iter__(self):  # type: ignore[no-untyped-def]
                for _ in range(10):
                    yield b"x" * 512

        return Stream()

    h = Harness([httpx.Response(200, stream=chunks())], max_response_bytes=1024)
    with pytest.raises(ResponseTooLargeError):
        h.get()
    assert len(h.requests) == 1  # not retried


# --- configuration validation --------------------------------------------------------------


def test_rejects_plain_http_base_url() -> None:
    with pytest.raises(ValueError, match="https"):
        ProviderClientConfig(
            base_url="http://api.example.com/", user_agent="t", rate_limiter=CountingLimiter()
        )


def test_rejects_empty_user_agent() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        ProviderClientConfig(base_url=BASE, user_agent="  ", rate_limiter=CountingLimiter())


def test_rejects_empty_secret() -> None:
    with pytest.raises(ValueError, match="api_key"):
        ProviderClientConfig(
            base_url=BASE,
            user_agent="t",
            rate_limiter=CountingLimiter(),
            secret_params={"api_key": SecretStr("")},
        )


def test_rejects_invalid_retry_policy() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)


# --- security --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example.net/collect",
        "https://api.example.com.evil.example.net/v1/series",
        "http://api.example.com/v1/series",
    ],
)
def test_never_sends_credentials_to_another_host_or_over_http(path: str) -> None:
    h = Harness([json_response()])
    with pytest.raises(ValueError, match="Refusing") as info:
        h.client.get(path)
    assert h.requests == []
    assert_no_secret(info.value)


@pytest.mark.parametrize("path", ["//evil.example.net/collect", "../other", "/abs"])
def test_relative_and_protocol_relative_paths_stay_on_configured_host(path: str) -> None:
    h = Harness([json_response()])
    h.client.get(path)
    assert len(h.requests) == 1
    assert h.requests[0].url.scheme == "https"
    assert h.requests[0].url.host == "api.example.com"


def test_caller_cannot_override_credential_parameter() -> None:
    h = Harness([json_response()])
    with pytest.raises(ValueError, match="collide"):
        h.client.get("series", {"API_KEY": "attacker"})
    assert h.requests == []


def test_http_error_never_exposes_secret_even_if_provider_echoes_it() -> None:
    echo = f'{{"error": "invalid api_key {SECRET}", "url": "/v1/series?api_key={SECRET}"}}'
    h = Harness([json_response(403, echo.encode())])
    with pytest.raises(ProviderHTTPError) as info:
        h.get()

    error = info.value
    formatted = "".join(traceback.format_exception(error))
    assert_no_secret(str(error), repr(error), error.url, error.body_excerpt, formatted)
    assert REDACTED in error.body_excerpt


def test_transport_error_never_exposes_secret_or_chains_original_exception() -> None:
    message = f"failed for https://api.example.com/v1/series?api_key={SECRET}"
    h = Harness([httpx.ConnectError(message)] * 3)
    with pytest.raises(ProviderTransportError) as info:
        h.get()

    error = info.value
    formatted = "".join(traceback.format_exception(error))
    assert_no_secret(str(error), error.url, formatted)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_all_errors_share_base_class_and_carry_redacted_url() -> None:
    h = Harness([json_response(404)])
    with pytest.raises(ProviderError) as info:
        h.get()
    assert f"api_key={REDACTED}" in info.value.url


def test_secrets_absent_from_reprs() -> None:
    h = Harness([json_response()], secret_headers={"X-Api-Key": SecretStr(HEADER_SECRET)})
    assert_no_secret(repr(h.client), repr(h.client._config))


def test_no_secret_in_any_log_record_including_httpx_debug_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = Harness(
        [json_response(503), httpx.ConnectError(f"boom api_key={SECRET}"), json_response()],
        secret_headers={"X-Api-Key": SecretStr(HEADER_SECRET)},
    )
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        try:
            h.get()
        finally:
            logging.getLogger("httpx").setLevel(logging.NOTSET)

    assert any(r.name == "httpx" for r in caplog.records), "expected httpx to log requests"
    assert any("retrying" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        assert_no_secret(record.getMessage(), vars(record))
    assert_no_secret(caplog.text)
