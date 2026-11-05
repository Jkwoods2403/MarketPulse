"""EIA API v2 client built on the shared ProviderHttpClient.

Ingestion uses NATIVE routes ("<route>/data" filtered by series): they honor ascending sort and
start/end windows and return values as exact decimal strings. The series-ID route is used only
to resolve and verify a catalogued series' native route (it ignores sort and start/end, returns
JSON numbers, and truncates without warnings).

Completeness never depends on warnings. For each request the client:
- asks for pages of at most 5,000 rows, each at offset == rows received so far;
- stops when the rows received reach EIA's reported total (with a hard page cap);
- checks that EIA executed what was asked (echoed frequency, series facet, ascending sort);
then validate.assemble_pages independently re-checks offsets and length (requested and
echoed), stable totals, no duplicate periods, ascending order, the final count == total,
metadata drift, and the warning rule ("incomplete return" is tolerated only where the requested
length explains it).

Every raw response is passed to the optional `on_response` hook *before* parsing (warnings
included, since they are part of the body), so the ingestion layer can preserve it.
`fetch_series_pages` returns the validated rows grouped by the page that contained them, with
each page's offset, redacted URL, SHA-256 of the exact response body, and retrieval time, so
every observation can be traced to its raw response; `fetch_series` returns the same rows flat. Rate
limiting (1 request/second, bursts of 5), retries (429/5xx/transport errors), timeouts, and key
redaction come from ProviderHttpClient.
"""

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Self

import httpx
from pydantic import SecretStr

from marketpulse.core.config import Settings
from marketpulse.core.ratelimit import RateLimiter
from marketpulse.sources.catalog.schema import eia_facet_series
from marketpulse.sources.eia import MAX_ROWS_PER_REQUEST
from marketpulse.sources.eia.parse import (
    EiaDataPage,
    EiaRouteMetadata,
    EiaRow,
    parse_data_page,
    parse_error_message,
    parse_route_metadata,
)
from marketpulse.sources.eia.validate import (
    EiaIncompleteError,
    EiaMetadataError,
    EiaSeriesExpectation,
    assemble_pages,
)
from marketpulse.sources.http import (
    FetchResult,
    ProviderClientConfig,
    ProviderError,
    ProviderHttpClient,
    ProviderHTTPError,
    RetryPolicy,
)

EIA_BASE_URL = "https://api.eia.gov/v2/"
DEFAULT_USER_AGENT = "MarketPulse (+https://github.com/Jkwoods2403/MarketPulse)"
# Approved: 1 request/second sustained, bursts of 5 (EIA publishes no numeric limit).
DEFAULT_RATE_LIMIT_CALLS = 5
DEFAULT_RATE_LIMIT_PERIOD_SECONDS = 5.0

# Dataset names passed to `on_response` (used as raw-payload directory names).
DATASET_DATA = "data"
DATASET_SERIES_ID = "seriesid"
DATASET_METADATA = "metadata"

ResponseHook = Callable[[str, FetchResult], None]


@dataclass(frozen=True)
class EiaFetchedPage:
    """One validated data page and where it came from."""

    offset: int  # the offset this page was requested at
    url: str  # redacted request URL (as passed to `on_response` and stored)
    sha256: str  # of the exact response body
    retrieved_at: datetime  # UTC, when the response was received
    rows: tuple[EiaRow, ...]  # this page's rows, in response order


class EiaConfigError(ValueError):
    """The EIA client cannot be configured (e.g. EIA_API_KEY is not set)."""


class EiaAPIError(ProviderError):
    """EIA returned an error body (HTTP 400/404 {"error", "code"}, or 403 invalid key)."""

    def __init__(self, status_code: int, error_message: str, *, url: str) -> None:
        super().__init__(f"EIA API error {status_code}: {error_message} ({url})", url=url)
        self.status_code = status_code
        self.error_message = error_message


class EiaClient:
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
        if not api_key.get_secret_value().strip():
            raise EiaConfigError("EIA API key is empty or blank")
        config = ProviderClientConfig(
            base_url=EIA_BASE_URL,
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
        if settings.eia_api_key is None:
            raise EiaConfigError("EIA_API_KEY is not set (add it to .env)")
        return cls(
            settings.eia_api_key,
            user_agent=settings.http_user_agent or DEFAULT_USER_AGENT,
            **kwargs,  # type: ignore[arg-type]
        )

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
        return f"EiaClient(base_url={EIA_BASE_URL!r})"

    # --- data (native route) --------------------------------------------------------------------

    def fetch_series(
        self,
        expected: EiaSeriesExpectation,
        *,
        start: str | None = None,
        end: str | None = None,
        page_size: int = MAX_ROWS_PER_REQUEST,
        allow_empty: bool = False,
    ) -> list[EiaRow]:
        """All rows of one catalogued series (optionally within [start, end], EIA period strings),
        fully validated. Raises a controlled-failure error instead of returning partial data."""
        pages = self.fetch_series_pages(
            expected, start=start, end=end, page_size=page_size, allow_empty=allow_empty
        )
        return [row for page in pages for row in page.rows]

    def fetch_series_pages(
        self,
        expected: EiaSeriesExpectation,
        *,
        start: str | None = None,
        end: str | None = None,
        page_size: int = MAX_ROWS_PER_REQUEST,
        allow_empty: bool = False,
    ) -> list[EiaFetchedPage]:
        """Like `fetch_series`, but grouped by response page with each page's provenance.

        Pages are returned only after the complete set passed `assemble_pages`; their rows,
        concatenated, are exactly what `fetch_series` returns.
        """
        if not 1 <= page_size <= MAX_ROWS_PER_REQUEST:
            raise ValueError(f"page_size must be between 1 and {MAX_ROWS_PER_REQUEST}")
        base: dict[str, str] = {
            "frequency": expected.frequency,
            "data[0]": "value",
            "facets[series][0]": expected.facet_series,
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": str(page_size),
        }
        if start is not None:
            base["start"] = start
        if end is not None:
            base["end"] = end

        pages: list[tuple[int, EiaDataPage]] = []
        results: list[FetchResult] = []
        received = 0
        max_pages: int | None = None
        while True:
            result = self._get(
                f"{expected.route}/data", {**base, "offset": str(received)}, DATASET_DATA
            )
            page = parse_data_page(result.content)
            self._check_echo(page, expected, result.url)
            pages.append((received, page))
            results.append(result)
            if max_pages is None:
                max_pages = math.ceil(page.total / page_size) + 1
            if len(page.rows) > page_size:
                raise EiaIncompleteError(
                    [
                        f"{expected.code}: page at offset {received} has "
                        f"{len(page.rows)} rows, more than requested "
                        f"({page_size})"
                    ]
                )
            received += len(page.rows)
            if not page.rows or received >= page.total or page.total != pages[0][1].total:
                break  # assemble_pages decides whether this is complete or a failure
            if len(pages) >= max_pages:
                raise EiaIncompleteError(
                    [
                        f"{expected.code}: more than {max_pages} pages for "
                        f"total {page.total}; pagination is not advancing"
                    ]
                )
        rows = assemble_pages(pages, expected, page_size=page_size, allow_empty=allow_empty)
        fetched = [
            EiaFetchedPage(
                offset=offset,
                url=result.url,
                sha256=hashlib.sha256(result.content).hexdigest(),
                retrieved_at=result.retrieved_at,
                rows=tuple(page.rows),
            )
            for (offset, page), result in zip(pages, results, strict=True)
        ]
        if [row for page in fetched for row in page.rows] != rows:  # pragma: no cover
            raise EiaIncompleteError([f"{expected.code}: page grouping does not match the rows"])
        return fetched

    def fetch_page(
        self, expected: EiaSeriesExpectation, *, offset: int, length: int
    ) -> EiaDataPage:
        """One page of the native-route request `fetch_series_pages` makes (ascending by period).

        Used by the read-only probe to sample a series' first and last rows. EIA must have
        executed the request as asked: frequency, series facet, sort, offset, and length.
        Completeness is NOT checked here; callers that need all rows use `fetch_series_pages`.
        """
        if not 1 <= length <= MAX_ROWS_PER_REQUEST:
            raise ValueError(f"length must be between 1 and {MAX_ROWS_PER_REQUEST}")
        if offset < 0:
            raise ValueError("offset must not be negative")
        params = {
            "frequency": expected.frequency,
            "data[0]": "value",
            "facets[series][0]": expected.facet_series,
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": str(length),
            "offset": str(offset),
        }
        result = self._get(f"{expected.route}/data", params, DATASET_DATA)
        page = parse_data_page(result.content)
        self._check_echo(page, expected, result.url)
        executed = {key: page.echo.get(key) for key in ("offset", "length")}
        asked = {"offset": offset, "length": length}
        wrong = [
            f"{key} {executed[key]!r}, requested {asked[key]}"
            for key in asked
            if executed[key] is not None and str(executed[key]) != str(asked[key])
        ]
        if wrong:
            raise EiaMetadataError(
                [
                    f"{expected.code}: EIA did not execute the request as asked "
                    f"({'; '.join(wrong)}) at {result.url}"
                ]
            )
        return page

    # --- series-ID route (resolution and verification only) -----------------------------------

    def resolve_series_id(self, source_series_id: str) -> dict[str, str]:
        """The native route, facet, and frequency EIA's series-ID route maps an ID to."""
        result = self._get(f"seriesid/{source_series_id}", {"length": "1"}, DATASET_SERIES_ID)
        page = parse_data_page(result.content)
        echo = page.echo
        facets = echo.get("facets") if isinstance(echo.get("facets"), dict) else {}
        series = facets.get("series") if isinstance(facets, dict) else None
        route = self._route_from(result.content)
        if not (
            isinstance(series, list) and len(series) == 1 and isinstance(series[0], str) and route
        ):
            raise EiaMetadataError(
                [
                    f"{source_series_id}: series-ID response does not reveal a "
                    "native route and series facet"
                ]
            )
        return {"route": route, "facet_series": series[0], "frequency": page.frequency}

    def verify_route(self, expected: EiaSeriesExpectation, source_series_id: str) -> list[str]:
        """Drift between the catalog's native route and what the series-ID route maps to."""
        if eia_facet_series(source_series_id) != expected.facet_series:
            return [f"{expected.code}: catalog facet does not match its series ID"]
        resolved = self.resolve_series_id(source_series_id)
        problems = []
        for key, want in (
            ("route", expected.route),
            ("facet_series", expected.facet_series),
            ("frequency", expected.frequency),
        ):
            if resolved[key] != want:
                problems.append(
                    f"{expected.code}: series-ID route maps to {key} "
                    f"{resolved[key]!r}, catalog says {want!r}"
                )
        return problems

    def route_metadata(self, route: str) -> EiaRouteMetadata:
        result = self._get(route, {}, DATASET_METADATA)
        return parse_route_metadata(result.content)

    # --- internals ----------------------------------------------------------------------------

    def _get(self, path: str, params: Mapping[str, str], dataset: str) -> FetchResult:
        try:
            result = self._http.get(path, params)
        except ProviderHTTPError as exc:
            message = parse_error_message(exc.body_excerpt)
            if message is None:
                raise
            raise EiaAPIError(exc.status_code, message, url=exc.url) from None
        if self._on_response is not None:
            self._on_response(dataset, result)  # before parsing: raw kept even if invalid
        return result

    @staticmethod
    def _route_from(content: bytes) -> str | None:
        try:
            request = json.loads(content).get("request", {})
        except (json.JSONDecodeError, AttributeError):
            return None
        route = request.get("route") if isinstance(request, dict) else None
        return route if isinstance(route, str) and route else None

    @staticmethod
    def _check_echo(page: EiaDataPage, expected: EiaSeriesExpectation, url: str) -> None:
        """EIA must have executed the request as asked (it silently ignores some parameters)."""
        echo = page.echo
        problems = []
        if echo.get("frequency") != expected.frequency:
            problems.append(f"executed frequency {echo.get('frequency')!r}")
        facets = echo.get("facets")
        if not isinstance(facets, dict) or facets.get("series") != [expected.facet_series]:
            problems.append(f"executed series facet {facets!r}")
        if echo.get("sort") != [{"column": "period", "direction": "asc"}]:
            problems.append(f"executed sort {echo.get('sort')!r}")
        if problems:
            raise EiaMetadataError(
                [
                    f"{expected.code}: EIA did not execute the request as asked "
                    f"({'; '.join(problems)}) at {url}"
                ]
            )
