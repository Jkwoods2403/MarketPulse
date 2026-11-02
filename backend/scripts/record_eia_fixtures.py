"""Record small, real EIA API v2 responses as test fixtures (public-domain series ONLY).

Usage (from backend/):  uv run python scripts/record_eia_fixtures.py

- Makes ~13 small read-only requests using EIA_API_KEY from the environment/.env.
- Saves each response body exactly as received to tests/fixtures/eia/<name>.json (or .json.gz
  for the one large response, compressed losslessly), plus a manifest with the redacted request
  URL, HTTP status, retrieval time, and SHA-256 of the *uncompressed* body.
- Never records Refinitiv-sourced series (WTI, Brent, Henry Hub spot prices): they are
  personal_use_only and must not be redistributed. A fixture is refused if it mentions them or
  contains the API key. The invalid-key fixture uses a made-up key, never the real one.

Data: U.S. Energy Information Administration (EIA), public domain.
"""

import gzip
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr

from marketpulse.core.config import REPO_ROOT, get_settings
from marketpulse.core.ratelimit import RateLimiter
from marketpulse.sources.http import (
    ProviderClientConfig,
    ProviderHttpClient,
    ProviderHTTPError,
    RetryPolicy,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "eia"
FORBIDDEN = ("RWTC", "RBRTE", "RNGWHHD")  # Refinitiv-sourced: personal_use_only
ASC = {"sort[0][column]": "period", "sort[0][direction]": "asc"}


def weekly(series: str, **extra: str) -> dict[str, str]:
    return {"frequency": "weekly", "data[0]": "value", "facets[series][0]": series, **ASC, **extra}


# (name, path, params, purpose, compress)
REQUESTS: list[tuple[str, str, dict[str, str], str, bool]] = [
    ("metadata_petroleum_sum_sndw", "petroleum/sum/sndw", {},
     "Route metadata: frequencies (weekly and four-week-average), facets, data columns, "
     "periods", False),
    ("seriesid_wcestus1_sort_asc_ignored", "seriesid/PET.WCESTUS1.W", {**ASC, "length": "3"},
     "Series-ID route: JSON-number values and total; requested ascending sort is ignored "
     "(echo shows desc); echo reveals the native route; 3 of 2,300 rows with NO warning "
     "(the series-ID route truncates silently)", False),
    ("seriesid_wcestus1_start_end_ignored", "seriesid/PET.WCESTUS1.W",
     {"start": "2026-01-01", "end": "2026-02-28", "length": "3"},
     "Series-ID route ignores start/end (echo shows null; total is the full history)", False),
    ("native_wcestus1_2026_jan_feb", "petroleum/sum/sndw/data",
     weekly("WCESTUS1", start="2026-01-01", end="2026-02-28"),
     "Native route, weekly window: string values and total, ascending, weeks ending Friday", False),
    ("native_wpuleus3_2026_jan_feb", "petroleum/pnp/wiup/data",
     weekly("WPULEUS3", start="2026-01-01", end="2026-02-28"),
     "Native route, decimal string values (refinery utilization percent)", False),
    ("native_n9133us2_2025", "natural-gas/move/expc/data",
     {"frequency": "monthly", "data[0]": "value", "facets[series][0]": "N9133US2", **ASC,
      "start": "2025-01", "end": "2025-12"},
     "Native route, monthly window: YYYY-MM periods", False),
    ("native_storage_page1", "natural-gas/stor/wkly/data",
     weekly("NW2_EPG0_SWO_R48_BCF", offset="0", length="3"),
     "Native route paging, page 1 (offset 0, length 3) with the pagination notice "
     "'incomplete return' in top-level warnings; earliest observation 2010-01-01", False),
    ("native_storage_page2", "natural-gas/stor/wkly/data",
     weekly("NW2_EPG0_SWO_R48_BCF", offset="3", length="3"),
     "Native route paging, page 2 (offset 3, length 3) with the pagination notice "
     "'incomplete return' in top-level warnings", False),
    ("native_overlimit_length_6000", "petroleum/sum/sndw/data",
     {"frequency": "weekly", "data[0]": "value", "facets[product][0]": "EPC0", **ASC,
      "length": "6000"},
     "Over-limit request (length=6000) with 65,642 rows available: HTTP 200, 5,000 rows, and "
     "top-level warnings 'parameter out of range: length' and 'incomplete return'", True),
    ("native_unknown_facet_empty", "petroleum/sum/sndw/data", weekly("MARKETPULSENOPE"),
     "Unknown series facet on the native route: HTTP 200, total \"0\", no rows (not an error)",
     False),
    ("error_unknown_series_id", "seriesid/PET.MARKETPULSENOPE.W", {},
     "HTTP 404 body for an unknown series ID", False),
    ("error_invalid_data_column", "petroleum/sum/sndw/data",
     {"frequency": "weekly", "data[0]": "monsters", "facets[series][0]": "WCESTUS1"},
     "HTTP 400 body for an invalid data column", False),
    ("error_invalid_frequency", "petroleum/sum/sndw/data",
     {"frequency": "hourly", "data[0]": "value", "facets[series][0]": "WCESTUS1"},
     "HTTP 400 body for an invalid frequency", False),
]  # fmt: skip


def make_client(key: SecretStr) -> ProviderHttpClient:
    return ProviderHttpClient(
        ProviderClientConfig(
            base_url="https://api.eia.gov/v2/",
            user_agent=get_settings().http_user_agent or "MarketPulse/0.1",
            rate_limiter=RateLimiter(5, 5.0),  # 1 request/second, bursts of 5
            secret_params={"api_key": key},
            retry=RetryPolicy(max_attempts=2),
        )
    )


def record(
    client: ProviderHttpClient,
    name: str,
    path: str,
    params: dict[str, str],
    purpose: str,
    compress: bool,
    secret: str,
) -> dict[str, object]:
    try:
        result = client.get(path, params)
        body, status, url, retrieved = (
            result.content,
            result.status_code,
            result.url,
            result.retrieved_at,
        )
    except ProviderHTTPError as exc:  # error fixtures: keep EIA's (short) error body
        body = exc.body_excerpt.encode()
        status, url, retrieved = exc.status_code, exc.url, datetime.now(UTC)
    text = body.decode("utf-8", "replace")
    if secret and (secret in text or secret in url):
        raise RuntimeError(f"refusing to write {name}: response contains the API key")
    if any(f in text for f in FORBIDDEN):
        raise RuntimeError(f"refusing to write {name}: contains Refinitiv-sourced data")
    file = f"{name}.json.gz" if compress else f"{name}.json"
    (FIXTURE_DIR / file).write_bytes(gzip.compress(body, mtime=0) if compress else body)
    print(f"{status} {file:<46} {len(body):>8} bytes  {url}")
    return {
        "file": file,
        "purpose": purpose,
        "request_url": url,
        "http_status": status,
        "retrieved_at": retrieved.isoformat(),
        "compressed": compress,
        "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
    }


def main() -> int:
    key = SecretStr((dotenv_values(REPO_ROOT / ".env").get("EIA_API_KEY") or "").strip())
    if not key.get_secret_value():
        print("EIA_API_KEY is not set", file=sys.stderr)
        return 1
    secret = key.get_secret_value()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []
    with make_client(key) as client:
        for name, path, params, purpose, compress in REQUESTS:
            manifest.append(record(client, name, path, params, purpose, compress, secret))
    fake = SecretStr("INVALIDKEYFORTESTING0000000000000000000")  # made up, never the real key
    with make_client(fake) as client:
        manifest.append(
            record(
                client,
                "error_invalid_api_key",
                "seriesid/PET.WCESTUS1.W",
                {},
                "HTTP 403 body for an invalid API key (made-up key)",
                False,
                secret,
            )
        )
    (FIXTURE_DIR / "manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
