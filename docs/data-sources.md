# Data sources

Every source Market Pulse uses, with its terms, limits, attribution, and how Market Pulse
ingests it (required by [ADR-0008](adr/0008-data-sources-and-licensing.md)). Facts are verified
against official documentation or the live API when a connector is built; the verification date
is given for each.

The authoritative list of tracked series is the version-controlled catalog in
`backend/src/marketpulse/sources/catalog/data/` (`sources.yaml` plus one file per provider).

| Source | Status | License (catalog) |
|---|---|---|
| [FRED](#fred-federal-reserve-bank-of-st-louis) | **Ingested** (12 series, full version history) | `attribution_required` |
| [EIA](#eia-us-energy-information-administration) | Catalogued (12 series), connector pending (Milestone 1.5) | `public_domain`; 3 series `personal_use_only` |

---

## FRED (Federal Reserve Bank of St. Louis)

*Verified 2026-11-01 against the FRED API documentation, the FRED legal page, every tracked
series' page, and the live API.*

### Terms and attribution

- **Terms:** <https://fred.stlouisfed.org/legal/>. FRED asks users to cite the source of the
  data, note that it was accessed via FRED, and keep any copyright notices shown with the data.
  Series with a copyright notice are owned by third parties and need the owner's permission for
  anything beyond personal use.
- **None of the 12 tracked series carries a third-party copyright notice** (checked in each
  series' notes; the S&P 500 series was used as a control and does carry one).
- **Catalog license:** `attribution_required`.
- **Attribution text:** "Source: original data producer, retrieved from FRED, Federal Reserve
  Bank of St. Louis". Each series also records its original producer (`original_source`), e.g.
  U.S. Bureau of Labor Statistics for CPI.

### API facts Market Pulse relies on

| Fact | Value | Source |
|---|---|---|
| Base URL | `https://api.stlouisfed.org/fred/` (HTTPS only) | API docs |
| Rate limit | 120 requests/minute, then HTTP 429; persistent violations can cause a temporary block | API errors page |
| Market Pulse rate | bursts of 10, refilled at 100/minute (worst minute < 120) | `FredClient` defaults |
| Observations per request | ≤ 100,000 rows (`limit`/`offset` paging) | API docs |
| Vintage dates per request | ≤ 10,000 (`fred/series/vintagedates`) | API docs |
| Real-time window limit | 2,000 vintage dates per observations request, **counted as below** | API docs + live behavior |
| Missing values | the string `"."` | live API |
| Real-time periods | `[realtime_start, realtime_end]`, closed at both ends; `9999-12-31` = still current | API docs |
| Observation dates | first day of the period (monthly `YYYY-MM-01`, quarterly first month of the quarter) | live API |
| Errors | JSON `{"error_code", "error_message"}`; a nonexistent series is **HTTP 400**, not 404; HTTP 423 can also occur | live API, API errors page |
| Series IDs | "25 or less alphanumeric characters" (FRED's own error message) | live API |

**Effective vintage-date count (observed, not documented).** FRED's observations endpoint counts
the vintage dates listed inside the window **plus one if the window's start is not itself a
vintage date, plus one if its end is not**. A window holding exactly 2,000 listed vintage dates is
therefore rejected with "There are 2001 vintage dates…" when it ends (or starts) in a weekend or
holiday gap. Market Pulse plans windows of at most 1,998 listed vintage dates, contiguous and
self-checked against this rule. This was discovered when the first live backfill
(2026-11-01) failed for 5 of 12 series; see the commits "Reproduce live FRED 2,001-vintage-date
incident…" and "Fix FRED window planning…", and `backend/tests/test_fred_window_limit.py`.

### How Market Pulse ingests FRED

```bash
cd backend
uv run marketpulse ingest fred                     # all 12 catalog series
uv run marketpulse ingest fred --series ust_10y    # one or more catalog codes
uv run marketpulse ingest fred --series real_gdp --full   # force a full re-fetch
```

Exit codes: 0 all series succeeded, 1 any series failed, 2 none failed but some were partial.

Per series, in its own ingestion run (`ops.ingestion_runs`, job `fred.observations`):

1. The catalog entry and `ref.series` row must match (run `marketpulse catalog sync` first).
2. **Drift check:** FRED's live metadata must match the catalog's frequency, units, and seasonal
   adjustment (errors stop the series before any observation request; title changes are
   warnings). Recorded in `ops.data_quality_issues`.
3. **Mode:** full backfill if nothing is stored (or `--full`); otherwise incremental from the
   newest stored vintage date.
4. **Fetch** all real-time windows and pages, requesting untransformed levels (`units=lin`) by
   real-time period (`output_type=1`).
5. **Raw responses are saved first:** each successful response is written, gzip-compressed and
   byte-exact, to `data/raw/fred/<series|vintagedates|observations>/YYYY/MM/` and recorded in
   `ops.raw_payloads` (redacted URL, HTTP status, retrieval time, file path, size, SHA-256) in its
   own committed transaction, before parsing. `data/` is gitignored.
6. **Write:** values are converted and written in one all-or-nothing transaction per series,
   under a per-series PostgreSQL advisory lock.

Series are independent: a failure is recorded on that series' run and the job continues.

### How FRED values are stored

- **Every version is kept.** Each row of `obs.observations` is one value of one period, valid
  during `[vintage_start, vintage_end)`; a revision closes the old row and inserts a new one.
  Nothing is overwritten or deleted.
- **Timestamps are date-level.** FRED's real-time dates are stored as midnight US Central
  (`America/Chicago`, daylight saving aware) on that date. A value published at 3 pm on a date is
  treated as known from midnight that day: this is date precision, not an exact publication time.
- **Values are exact decimals** as FRED published them (e.g. `22768.866`, `4.190`); `"."` is
  stored as `NULL` with status `missing_at_source`. Nothing is filled, rounded, or interpolated.
- **Point-in-time query** (the value of a period as known at instant *t*):

  ```sql
  SELECT value FROM obs.observations
  WHERE series_id = :s AND period_start = :p
    AND vintage_start <= :t AND (vintage_end IS NULL OR vintage_end > :t);
  ```

- **Limitation: version history starts at each series' first vintage date.** Observations
  published before FRED began tracking vintages are marked as known only from that date (for
  example, 1962 Treasury yields are "known since 2005-06-28"). Strict point-in-time research
  therefore has no data for those series before their first vintage date (table below).

### Tracked series and backfill results

Backfilled 2026-11-01 (runs 2 and 7–17). "Versions" counts every stored real-time version;
"current" is one per period.

| Code | FRED ID | Freq. | Periods | Versions | Current | First vintage |
|---|---|---|---|---|---|---|
| `ust_3m` | DGS3MO | daily | 1981-09-01 – 2026-10-29 | 11,789 | 11,783 | 2005-06-28 |
| `ust_2y` | DGS2 | daily | 1976-06-01 – 2026-10-29 | 13,161 | 13,153 | 2005-06-28 |
| `ust_10y` | DGS10 | daily | 1962-01-02 – 2026-10-29 | 16,920 | 16,913 | 2005-06-28 |
| `ust_30y` | DGS30 | daily | 1977-02-15 – 2026-10-29 | 13,966 | 12,968 | 2005-06-28 |
| `ust_10y_2y_spread` | T10Y2Y | daily | 1976-06-01 – 2026-10-30 | 13,158 | 13,154 | 2014-01-27 |
| `fed_funds_effective` | DFF | daily (7-day) | 1954-07-01 – 2026-10-29 | 28,084 | 26,419 | 2005-06-28 |
| `usd_broad_index` | DTWEXBGS | daily | 2006-01-02 – 2026-10-23 | 22,107 | 5,430 | 2019-02-04 |
| `cpi_all_items` | CPIAUCSL | monthly | 1947-01 – 2026-09 | 3,363 | 957 | 1972-07-21 |
| `nonfarm_payrolls` | PAYEMS | monthly | 1939-01 – 2026-09 | 13,691 | 1,053 | 1955-05-06 |
| `unemployment_rate` | UNRATE | monthly | 1948-01 – 2026-09 | 2,199 | 945 | 1960-03-15 |
| `industrial_production` | INDPRO | monthly | 1919-01 – 2026-09 | 39,368 | 1,293 | 1927-01-26 |
| `real_gdp` | GDPC1 | quarterly | 1947 Q1 – 2026 Q3 | 4,426 | 319 | 1991-12-04 |
| **Total** | | | | **182,232** | **104,387** | |

Verification after the backfill (2026-11-01):

- Database integrity: no period with zero or several current versions, no overlapping or gapped
  versions, no duplicates, every vintage at midnight Central, nothing written by failed runs,
  every stored URL redacted.
- The database equals a replay of the stored raw responses for all 12 series.
- 98 independent spot checks against the live API passed: current values on random dates for
  every series, and ALFRED (as-of) values such as GDP 2024 Q1 as of 2024-05-01 = 22768.866, as of
  2024-06-01 = 22749.846, and as of 2025-01-15 = 23053.545.
- Each daily series' number of current versions equals the observation count FRED reports.

---

## EIA (U.S. Energy Information Administration)

*Catalog verified 2026-11-01; the connector is Milestone 1.5.*

- **Terms:** <https://www.eia.gov/about/copyrights_reuse.php>. U.S. government publications are
  in the public domain; third-party items in EIA products may be protected.
- **Catalog license:** `public_domain`, except WTI, Brent, and Henry Hub spot prices, which are
  Refinitiv (LSEG) data republished by EIA and are classified `personal_use_only` until
  redistribution rights are confirmed.
- **Series IDs:** EIA API v1-style IDs (e.g. `PET.RWTC.D`), which EIA API v2 accepts through its
  series-ID route.
- API limits, error formats, and ingestion details will be documented here when the connector is
  built.
