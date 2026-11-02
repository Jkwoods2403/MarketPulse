"""Shared helpers for FRED client tests: fixture loading and a routing mock transport.

No network access: every response comes from tests/fixtures/fred (real recorded bodies) or is
synthetic and built in the test that uses it.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from marketpulse.core.ratelimit import RateLimiter
from marketpulse.sources.fred.client import FredClient
from marketpulse.sources.http import FetchResult, RetryPolicy

FIXTURES = Path(__file__).parent / "fixtures" / "fred"
TEST_KEY = "TEST-FRED-KEY-0f9e8d7c6b5a4f3e"  # synthetic; FRED keys are 32 lowercase hex chars


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


def json_response(
    body: bytes | dict[str, Any], status: int = 200, **headers: str
) -> httpx.Response:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Response(
        status, content=content, headers={"Content-Type": "application/json", **headers}
    )


class UnlimitedLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__(1_000_000, 1.0)
        self.calls = 0

    def acquire(self) -> float:
        self.calls += 1
        return 0.0


class FredHarness:
    """A FredClient wired to a request handler; records requests, sleeps, and raw responses."""

    def __init__(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.raw: list[tuple[str, FetchResult]] = []
        self.limiter = UnlimitedLimiter()

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        self.client = FredClient(
            SecretStr(TEST_KEY),
            user_agent="MarketPulse-tests/0.1",
            rate_limiter=self.limiter,
            retry=retry or RetryPolicy(max_attempts=3),
            on_response=lambda dataset, result: self.raw.append((dataset, result)),
            transport=httpx.MockTransport(record),
            sleep=self.sleeps.append,
        )

    def params(self, i: int = -1) -> dict[str, str]:
        return dict(self.requests[i].url.params)
