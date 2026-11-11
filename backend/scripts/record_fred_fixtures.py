"""Record small, real FRED API responses as test fixtures.

RETIRED: FRED is no longer a Market Pulse data provider and is in the source block list
(core/source_access.py). This script now exits 1 before reading settings, creating an HTTP
client, making a request, or writing a file. The fixtures it recorded earlier are kept.

Usage (from backend/):  uv run python scripts/record_fred_fixtures.py

- Makes 10 small read-only requests (see REQUESTS) using FRED_API_KEY from the environment/.env.
- Saves each response body exactly as received to tests/fixtures/fred/<name>.json, plus a
  manifest with the redacted request URL, HTTP status, retrieval time, and SHA-256.
- The API key is never printed; a fixture is refused if its body contains the key.

Fixture data comes from FRED (Federal Reserve Bank of St. Louis); the original producers are
the U.S. Bureau of Economic Analysis, the U.S. Bureau of Labor Statistics, and the Board of
Governors of the Federal Reserve System.
"""

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from marketpulse.core.config import get_settings
from marketpulse.core.ratelimit import RateLimiter
from marketpulse.core.source_access import (
    SourceIngestionBlockedError,
    require_source_ingestion_permitted,
)
from marketpulse.sources.http import (
    ProviderClientConfig,
    ProviderHttpClient,
    ProviderHTTPError,
    RetryPolicy,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fred"
FULL_HISTORY = {"realtime_start": "1776-07-04", "realtime_end": "9999-12-31"}
OBS = {"file_type": "json", "output_type": "1", "units": "lin", "sort_order": "asc"}

# (name, path, params, purpose)
REQUESTS: list[tuple[str, str, dict[str, str], str]] = [
    ("series_gdpc1", "series", {"series_id": "GDPC1", "file_type": "json"},
     "Series metadata: quarterly, seasonally adjusted annual rate"),
    ("series_dgs10", "series", {"series_id": "DGS10", "file_type": "json"},
     "Series metadata: daily, not seasonally adjusted"),
    ("vintagedates_gdpc1_limit5", "series/vintagedates",
     {"series_id": "GDPC1", "file_type": "json", "limit": "5"},
     "Vintage dates first page (count exceeds returned rows)"),
    ("observations_gdpc1_2024q1q2_page1", "series/observations",
     {**OBS, "series_id": "GDPC1", "observation_start": "2024-01-01",
      "observation_end": "2024-04-01", "limit": "6", "offset": "0", **FULL_HISTORY},
     "Quarterly revision history, page 1 of 2"),
    ("observations_gdpc1_2024q1q2_page2", "series/observations",
     {**OBS, "series_id": "GDPC1", "observation_start": "2024-01-01",
      "observation_end": "2024-04-01", "limit": "6", "offset": "6", **FULL_HISTORY},
     "Quarterly revision history, page 2 of 2"),
    ("observations_cpiaucsl_2026aug_sep", "series/observations",
     {**OBS, "series_id": "CPIAUCSL", "observation_start": "2026-08-01",
      "observation_end": "2026-09-01", **FULL_HISTORY},
     "Monthly observations (first-of-month dates); no revisions in this range"),
    ("observations_dgs10_holidays_window", "series/observations",
     {**OBS, "series_id": "DGS10", "observation_start": "2025-12-24",
      "observation_end": "2026-01-02", "realtime_start": "2026-10-01",
      "realtime_end": "9999-12-31"},
     "Daily observations with holiday missing values ('.'), within a real-time window"),
    ("error_invalid_series_id", "series/observations",
     {**OBS, "series_id": "MARKETPULSE_NOT_A_SERIES"},
     "HTTP 400 error body for a malformed series ID (underscores are not allowed)"),
    ("error_nonexistent_series", "series/observations",
     {**OBS, "series_id": "MARKETPULSENOSUCHSERIES"},
     "HTTP error body for a well-formed series ID that does not exist"),
    ("error_too_many_vintages", "series/observations",
     {**OBS, "series_id": "DGS10", "limit": "1", **FULL_HISTORY},
     "HTTP 400 error body when a request spans more than 2,000 vintage dates"),
]  # fmt: skip


def main() -> int:
    # FRED is retired and blocked (core/source_access.py): refuse before settings, an HTTP
    # client, a request, or any fixture file. The existing fixtures are kept unchanged.
    try:
        require_source_ingestion_permitted("fred")
    except SourceIngestionBlockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    settings = get_settings()
    if settings.fred_api_key is None:
        print("FRED_API_KEY is not set", file=sys.stderr)
        return 1
    secret = settings.fred_api_key.get_secret_value()
    config = ProviderClientConfig(
        base_url="https://api.stlouisfed.org/fred/",
        user_agent=settings.http_user_agent or "MarketPulse/0.1",
        rate_limiter=RateLimiter(10, 6.0),
        secret_params={"api_key": settings.fred_api_key},
        retry=RetryPolicy(max_attempts=2),
    )
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []
    with ProviderHttpClient(config) as client:
        for name, path, params, purpose in REQUESTS:
            try:
                result = client.get(path, params)
                body, status, url = result.content, result.status_code, result.url
                retrieved = result.retrieved_at
            except ProviderHTTPError as exc:  # error fixtures: keep FRED's (short) error body
                body = exc.body_excerpt.encode()
                status, url, retrieved = exc.status_code, exc.url, datetime.now(UTC)
            if secret in body.decode("utf-8", "replace") or secret in url:
                raise RuntimeError(f"refusing to write {name}: response contains the API key")
            (FIXTURE_DIR / f"{name}.json").write_bytes(body)
            manifest.append(
                {
                    "file": f"{name}.json",
                    "purpose": purpose,
                    "request_url": url,  # redacted by ProviderHttpClient
                    "http_status": status,
                    "retrieved_at": retrieved.isoformat(),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size_bytes": len(body),
                }
            )
            print(f"{status} {name:<38} {len(body):>6} bytes  {url}")
    (FIXTURE_DIR / "manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
