"""FRED API client built on the shared ProviderHttpClient.

Responsibilities:
- build correct FRED requests (JSON, untransformed levels, observations by real-time period);
- page through results with strict consistency checks;
- split long histories into real-time windows of at most 2,000 vintage dates and rejoin the
  window-edge splits;
- turn FRED error bodies into FredAPIError with FRED's own message.

It does not store anything. Every raw response is passed to the optional `on_response`
callback *before* parsing, so the ingestion layer can save it even if parsing fails.
Rate limiting, retries (429/5xx/transport errors), timeouts, and credential redaction are
handled by ProviderHttpClient.
"""

import json
import math
import time
from collections.abc import Callable, Mapping
from datetime import date
from types import TracebackType
from typing import Self

import httpx
from pydantic import SecretStr

from marketpulse.core.config import Settings
from marketpulse.core.ratelimit import RateLimiter
from marketpulse.sources.fred import (
    MAX_ROWS_PER_REQUEST,
    MAX_VINTAGES_PER_REQUEST,
    SERIES_ID_PATTERN,
)
from marketpulse.sources.fred.merge import merge_window_splits
from marketpulse.sources.fred.parse import (
    FredObservation,
    FredSeriesMetadata,
    parse_observations,
    parse_series_metadata,
    parse_vintage_dates,
)
from marketpulse.sources.fred.windows import RealtimeWindow, plan_windows
from marketpulse.sources.http import (
    FetchResult,
    ProviderClientConfig,
    ProviderError,
    ProviderHttpClient,
    ProviderHTTPError,
    RetryPolicy,
)

FRED_BASE_URL = "https://api.stlouisfed.org/fred/"
DEFAULT_USER_AGENT = "MarketPulse (+https://github.com/Jkwoods2403/MarketPulse)"
MAX_VINTAGE_DATES_PER_PAGE = 10_000  # documented limit for fred/series/vintagedates

# FRED allows 120 requests/minute. Bursts of 10, refilled at 100/minute, stay well below it.
DEFAULT_RATE_LIMIT_CALLS = 10
DEFAULT_RATE_LIMIT_PERIOD_SECONDS = 6.0

# Dataset names passed to `on_response` (used as raw-payload directory names).
DATASET_SERIES = "series"
DATASET_VINTAGE_DATES = "vintagedates"
DATASET_OBSERVATIONS = "observations"

ResponseHook = Callable[[str, FetchResult], None]


class FredConfigError(ValueError):
    """The FRED client cannot be configured (e.g. FRED_API_KEY is not set)."""


class FredAPIError(ProviderError):
    """FRED returned an error response (e.g. HTTP 400 for an unknown series)."""

    def __init__(self, status_code: int, error_message: str, *, url: str) -> None:
        super().__init__(f"FRED API error {status_code}: {error_message} ({url})", url=url)
        self.status_code = status_code
        self.error_message = error_message


class FredPaginationError(ProviderError):
    """Paged responses were inconsistent (changing totals, gaps, or missing rows)."""


class FredClient:
    def __init__(
        self,
        api_key: SecretStr,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        on_response: ResponseHook | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.get_secret_value():
            raise FredConfigError("FRED API key is empty")
        config = ProviderClientConfig(
            base_url=FRED_BASE_URL,
            user_agent=user_agent,
            rate_limiter=rate_limiter
            or RateLimiter(DEFAULT_RATE_LIMIT_CALLS, DEFAULT_RATE_LIMIT_PERIOD_SECONDS),
            secret_params={"api_key": api_key},
            retry=retry or RetryPolicy(),
        )
        self._http = ProviderHttpClient(config, transport=transport, sleep=sleep)
        self._on_response = on_response

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: object) -> Self:
        if settings.fred_api_key is None:
            raise FredConfigError("FRED_API_KEY is not set (add it to .env)")
        return cls(
            settings.fred_api_key,
            user_agent=settings.http_user_agent or DEFAULT_USER_AGENT,
            **kwargs,  # type: ignore[arg-type]
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
        self._http.close()

    def __repr__(self) -> str:
        return f"FredClient(base_url={FRED_BASE_URL!r})"

    # --- public API ---------------------------------------------------------------------------

    def series_metadata(self, series_id: str) -> FredSeriesMetadata:
        result = self._get("series", {"series_id": _checked(series_id)}, DATASET_SERIES)
        metadata = parse_series_metadata(result.content)
        if metadata.series_id != series_id:
            raise FredPaginationError(
                f"requested series {series_id!r} but FRED returned {metadata.series_id!r}",
                url=result.url,
            )
        return metadata

    def vintage_dates(
        self, series_id: str, *, page_size: int = MAX_VINTAGE_DATES_PER_PAGE
    ) -> list[date]:
        """All vintage dates for a series, oldest first, across as many pages as needed."""
        params = {"series_id": _checked(series_id), "sort_order": "asc"}
        dates: list[date] = []
        expected_total: int | None = None
        url = ""
        for _ in range(_max_pages(None, page_size)):
            result = self._get(
                "series/vintagedates",
                {**params, "limit": str(page_size), "offset": str(len(dates))},
                DATASET_VINTAGE_DATES,
            )
            url = result.url
            page = parse_vintage_dates(result.content)
            expected_total = _check_page(
                expected_total, page.count, page.offset, len(dates), len(page.vintage_dates), url
            )
            if dates and page.vintage_dates and page.vintage_dates[0] <= dates[-1]:
                raise FredPaginationError("vintage dates are not increasing across pages", url=url)
            dates.extend(page.vintage_dates)
            if not page.has_more:
                break
        else:  # pragma: no cover  (guarded by _check_page progress checks)
            raise FredPaginationError("too many vintage-date pages", url=url)
        _check_complete(expected_total, len(dates), url)
        return dates

    def observations(
        self,
        series_id: str,
        window: RealtimeWindow,
        *,
        observation_start: date | None = None,
        observation_end: date | None = None,
        page_size: int = MAX_ROWS_PER_REQUEST,
    ) -> list[FredObservation]:
        """All observation rows for one real-time window (as returned, not merged)."""
        params: dict[str, str] = {
            "series_id": _checked(series_id),
            "output_type": "1",  # observations by real-time period
            "units": "lin",  # untransformed levels
            "sort_order": "asc",
            **window.as_params(),
        }
        if observation_start is not None:
            params["observation_start"] = observation_start.isoformat()
        if observation_end is not None:
            params["observation_end"] = observation_end.isoformat()

        rows: list[FredObservation] = []
        expected_total: int | None = None
        url = ""
        while True:
            result = self._get(
                "series/observations",
                {**params, "limit": str(page_size), "offset": str(len(rows))},
                DATASET_OBSERVATIONS,
            )
            url = result.url
            page = parse_observations(result.content)
            if (page.realtime_start, page.realtime_end) != (window.start, window.end):
                raise FredPaginationError(
                    f"requested real-time window {window.start}..{window.end} but FRED returned "
                    f"{page.realtime_start}..{page.realtime_end}",
                    url=url,
                )
            expected_total = _check_page(
                expected_total, page.count, page.offset, len(rows), len(page.observations), url
            )
            rows.extend(page.observations)
            if not page.has_more:
                break
        _check_complete(expected_total, len(rows), url)
        return rows

    def fetch_history(
        self,
        series_id: str,
        *,
        start: date | None = None,
        limit: int = MAX_VINTAGES_PER_REQUEST,
        page_size: int = MAX_ROWS_PER_REQUEST,
    ) -> tuple[list[RealtimeWindow], list[FredObservation]]:
        """Fetch every real-time period from `start` (default: first vintage) to today.

        `limit` is FRED's effective vintage-dates-per-request limit (see windows.py).
        Returns the windows used and the observations with window-edge splits rejoined.
        """
        vintages = self.vintage_dates(series_id)
        windows = plan_windows(vintages, start=start, limit=limit)
        rows: list[FredObservation] = []
        for window in windows:
            rows.extend(self.observations(series_id, window, page_size=page_size))
        return windows, merge_window_splits(rows)

    # --- internals ----------------------------------------------------------------------------

    def _get(self, path: str, params: Mapping[str, str], dataset: str) -> FetchResult:
        try:
            result = self._http.get(path, {**params, "file_type": "json"})
        except ProviderHTTPError as exc:
            message = _fred_error_message(exc.body_excerpt)
            if message is None:
                raise
            raise FredAPIError(exc.status_code, message, url=exc.url) from None
        if self._on_response is not None:
            self._on_response(dataset, result)  # before parsing: raw is kept even if invalid
        return result


def _checked(series_id: str) -> str:
    if not SERIES_ID_PATTERN.fullmatch(series_id):
        raise ValueError(f"invalid FRED series ID {series_id!r} (1-25 uppercase letters/digits)")
    return series_id


def _fred_error_message(body: str) -> str | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    message = data.get("error_message") if isinstance(data, dict) else None
    return message if isinstance(message, str) else None


def _max_pages(total: int | None, page_size: int) -> int:
    if total is None:
        return 10_000
    return math.ceil(total / page_size) + 1


def _check_page(
    expected_total: int | None, count: int, offset: int, have: int, got: int, url: str
) -> int:
    if offset != have:
        raise FredPaginationError(f"expected page offset {have}, got {offset}", url=url)
    if expected_total is not None and count != expected_total:
        raise FredPaginationError(
            f"total changed between pages ({expected_total} -> {count})", url=url
        )
    if got == 0 and have < count:
        raise FredPaginationError(
            f"empty page at offset {offset} with {count - have} rows outstanding", url=url
        )
    return count


def _check_complete(expected_total: int | None, have: int, url: str) -> None:
    if expected_total is not None and have != expected_total:
        raise FredPaginationError(f"received {have} rows, FRED reported {expected_total}", url=url)
