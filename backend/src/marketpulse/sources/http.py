"""Shared HTTP client for external data providers (FRED, EIA, ...).

Guarantees, for every provider built on it:

- **HTTPS only.** Plain-HTTP base URLs are rejected.
- **Credentials are injected only at send time.** API keys are held as `SecretStr` and added
  to the outgoing request; they never appear in returned URLs, exceptions, or logs.
- **Rate limiting** before every attempt, including retries.
- **Timeouts** on connect, read, write, and pool acquisition.
- **Retries** with exponential backoff and jitter for transient failures only (connection
  errors, timeouts, 429 and 5xx), honoring `Retry-After` up to a cap. Other 4xx responses fail
  immediately: retrying a bad request or invalid key cannot succeed.
- **No redirects.** A redirect is treated as an error rather than silently followed.
- **Bounded responses.** Bodies larger than `max_response_bytes` are rejected while streaming.

The client returns raw bytes plus lineage metadata. Parsing is the provider module's job, and
saving the raw payload is the ingestion layer's job.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Self

import httpx
from pydantic import SecretStr

from marketpulse.core.logging import install_http_log_redaction
from marketpulse.core.ratelimit import RateLimiter
from marketpulse.core.redaction import redact_text, redact_url

logger = logging.getLogger(__name__)
install_http_log_redaction()

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
DEFAULT_MAX_RESPONSE_BYTES = 50 * 1024 * 1024
_ERROR_BODY_EXCERPT_CHARS = 500


# --- errors ----------------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for provider request failures. Messages never contain credentials."""

    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url  # always the redacted URL


class ProviderHTTPError(ProviderError):
    """The provider returned a non-success HTTP status (after any retries)."""

    def __init__(self, status_code: int, *, url: str, body_excerpt: str) -> None:
        super().__init__(f"HTTP {status_code} from {url}: {body_excerpt}", url=url)
        self.status_code = status_code
        self.body_excerpt = body_excerpt


class ProviderTransportError(ProviderError):
    """The request failed at the network level (after any retries)."""


class ResponseTooLargeError(ProviderError):
    """The response exceeded the configured size limit."""


# --- configuration and results ---------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    max_retry_after_seconds: float = 120.0
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")


@dataclass(frozen=True)
class ProviderClientConfig:
    """Per-provider settings. `secret_params` / `secret_headers` hold credentials."""

    base_url: str
    user_agent: str
    rate_limiter: RateLimiter
    secret_params: Mapping[str, SecretStr] = field(default_factory=dict)
    secret_headers: Mapping[str, SecretStr] = field(default_factory=dict)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout: httpx.Timeout = field(default_factory=lambda: DEFAULT_TIMEOUT)
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if httpx.URL(self.base_url).scheme != "https":
            raise ValueError(f"Provider base_url must use https: {redact_url(self.base_url)}")
        if not self.user_agent.strip():
            raise ValueError("user_agent must not be empty")
        for name, value in self.secret_params.items():
            if not value.get_secret_value():
                raise ValueError(f"Secret parameter {name!r} is empty")


@dataclass(frozen=True)
class FetchResult:
    """A successful provider response with the metadata needed for lineage."""

    content: bytes
    status_code: int
    content_type: str | None
    url: str  # redacted: safe to log and store
    retrieved_at: datetime  # UTC, when the response was received
    elapsed_seconds: float
    attempts: int


# --- client ----------------------------------------------------------------------------------


class ProviderHttpClient:
    def __init__(
        self,
        config: ProviderClientConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._jitter = jitter
        self._secret_param_names = frozenset(config.secret_params)
        self._secret_values = [
            *(v.get_secret_value() for v in config.secret_params.values()),
            *(v.get_secret_value() for v in config.secret_headers.values()),
        ]
        self._client = httpx.Client(
            base_url=config.base_url,
            headers={"User-Agent": config.user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=config.timeout,
            follow_redirects=False,
            transport=transport,
        )

    # context management
    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def __repr__(self) -> str:
        return f"ProviderHttpClient(base_url={redact_url(self._config.base_url)!r})"

    # public API
    def get(self, path: str, params: Mapping[str, str | int | float] | None = None) -> FetchResult:
        """GET `path` (relative to base_url) with `params`; credentials are added automatically."""
        params = dict(params or {})
        overlap = {k.lower() for k in params} & {k.lower() for k in self._secret_param_names}
        if overlap:
            raise ValueError(f"Parameters collide with credential parameters: {sorted(overlap)}")

        request_params = {**params, **self._secret_param_values()}
        headers = {k: v.get_secret_value() for k, v in self._config.secret_headers.items()}
        request = self._client.build_request("GET", path, params=request_params, headers=headers)
        base = httpx.URL(self._config.base_url)
        if request.url.scheme != "https" or request.url.host != base.host:
            # e.g. an absolute URL passed as `path`: never send credentials to another host.
            raise ValueError(
                f"Refusing request outside {base.scheme}://{base.host}: "
                f"{self._redact_url(str(request.url))}"
            )
        safe_url = self._redact_url(str(request.url))

        policy = self._config.retry
        for attempt in range(1, policy.max_attempts + 1):
            self._config.rate_limiter.acquire()
            started = time.monotonic()
            try:
                status, content_type, body = self._send(request, safe_url)
            except httpx.RequestError as exc:
                # TransportError (connect/read/timeouts) is transient and retried. Other request
                # errors (e.g. DecodingError for a corrupt body) are not, but are still wrapped
                # so callers only ever see redacted ProviderErrors.
                error = ProviderTransportError(
                    f"{type(exc).__name__} requesting {safe_url}: {self._scrub(str(exc))}",
                    url=safe_url,
                )
                if isinstance(exc, httpx.TransportError) and attempt < policy.max_attempts:
                    self._wait_before_retry(attempt, None, safe_url, reason=type(exc).__name__)
                    continue
                raise error from None

            elapsed = time.monotonic() - started
            if status in policy.retry_statuses and attempt < policy.max_attempts:
                self._wait_before_retry(
                    attempt, body.retry_after, safe_url, reason=f"HTTP {status}"
                )
                continue
            if not 200 <= status < 300:
                excerpt = self._scrub(
                    body.content[:_ERROR_BODY_EXCERPT_CHARS].decode("utf-8", "replace")
                )
                raise ProviderHTTPError(status, url=safe_url, body_excerpt=excerpt)

            logger.debug(
                "provider request ok",
                extra={
                    "url": safe_url,
                    "status": status,
                    "attempts": attempt,
                    "elapsed_s": elapsed,
                },
            )
            return FetchResult(
                content=body.content,
                status_code=status,
                content_type=content_type,
                url=safe_url,
                retrieved_at=body.received_at,
                elapsed_seconds=elapsed,
                attempts=attempt,
            )
        raise AssertionError("unreachable")  # pragma: no cover

    # internals
    def _secret_param_values(self) -> dict[str, str]:
        return {k: v.get_secret_value() for k, v in self._config.secret_params.items()}

    def _redact_url(self, url: str) -> str:
        return self._scrub(redact_url(url, self._secret_param_names))

    def _scrub(self, text: str) -> str:
        return redact_text(text, self._secret_values)

    def _send(self, request: httpx.Request, safe_url: str) -> tuple[int, str | None, _Body]:
        limit = self._config.max_response_bytes
        response = self._client.send(request, stream=True)
        try:
            declared = response.headers.get("Content-Length")
            if declared is not None and declared.isdigit() and int(declared) > limit:
                raise ResponseTooLargeError(
                    f"Response declared {declared} bytes, over the {limit}-byte limit: {safe_url}",
                    url=safe_url,
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > limit:
                    raise ResponseTooLargeError(
                        f"Response exceeded the {limit}-byte limit: {safe_url}", url=safe_url
                    )
                chunks.append(chunk)
            body = _Body(
                content=b"".join(chunks),
                received_at=datetime.now(UTC),
                retry_after=_parse_retry_after(response.headers.get("Retry-After")),
            )
            return response.status_code, response.headers.get("Content-Type"), body
        finally:
            response.close()

    def _wait_before_retry(
        self, attempt: int, retry_after: float | None, safe_url: str, *, reason: str
    ) -> None:
        policy = self._config.retry
        if retry_after is not None:
            delay = min(retry_after, policy.max_retry_after_seconds)
        else:
            backoff = min(
                policy.backoff_max_seconds, policy.backoff_base_seconds * 2 ** (attempt - 1)
            )
            delay = backoff * (0.5 + self._jitter() / 2)  # jitter within [50%, 100%]
        logger.warning(
            "provider request failed (%s); retrying in %.1fs",
            reason,
            delay,
            extra={"url": safe_url, "attempt": attempt},
        )
        self._sleep(delay)


@dataclass(frozen=True)
class _Body:
    content: bytes
    received_at: datetime
    retry_after: float | None


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds, or None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
