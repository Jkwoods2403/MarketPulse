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
| [EIA](#eia-us-energy-information-administration) | Connector built (12 series, first-seen snapshots); not yet run against the live API | `public_domain`; 3 series `personal_use_only` |

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
- **Limitation: closures and confirmations do not record their run.** When a later run closes or
  re-confirms a FRED version, only the time is stored. `marketpulse verify --replay` verifies
  each version's creation against its run's stored pages, but reports versions that a later run
  closed or re-confirmed as UNSUPPORTED ([verification.md](verification.md)).

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

*Catalog and API behavior verified 2026-11-01 against the live API (recorded fixtures in
`backend/tests/fixtures/eia/`). The connector (Milestone 1.5) is built and tested against a
simulator of that behavior; **no EIA data has been ingested from the live API yet.***

### Terms and licensing

- **Terms:** <https://www.eia.gov/about/copyrights_reuse.php>. U.S. government publications are
  in the public domain; third-party items in EIA products may be protected.
- **Catalog license:** `public_domain`, except **WTI, Brent, and Henry Hub spot prices**
  (`wti_spot`, `brent_spot`, `henry_hub_spot`), which are Refinitiv (LSEG) data republished by
  EIA. They are classified **`personal_use_only`**: they are ingested by default for personal use,
  but Market Pulse does not assume any right to redistribute or publicly display them. That
  classification stays until redistribution rights are confirmed.
- **Attribution:** "Source: U.S. Energy Information Administration (EIA)".

### API usage

- **Series IDs:** EIA API v1-style IDs (e.g. `PET.RWTC.D`). Ingestion uses the **native route**
  of each series (`<route>/data`, filtered by the `series` facet), which honors ascending sort and
  returns values as exact decimal strings. The series-ID route is used only to resolve and verify
  routes when the catalog is built: it ignores sort and `start`/`end`, returns JSON numbers, and
  truncates without any warning. It is not called on every ingestion run.
- **Limits:** EIA publishes no numeric rate limit; Market Pulse uses 1 request/second with bursts
  of 5, retries 429/5xx/transport errors, and never logs or stores the API key (URLs are
  redacted). Pages hold at most 5,000 rows.
- **Completeness:** every page is requested at offset = rows received so far; the client stops
  at EIA's reported total and then re-checks requested and echoed offsets, stable totals,
  duplicate periods, ascending order, received == total, and metadata (frequency, date format,
  series, description, units) against the catalog. EIA's "incomplete return" notice is expected
  on every page that does not hold all remaining rows; any other warning fails the request. An
  unknown series answers HTTP 200 with total 0, which fails as an empty response.
- **Periods:** daily and weekly periods are `YYYY-MM-DD` (weekly = the week's last day, a Friday
  for every tracked weekly series); monthly periods are `YYYY-MM`. Other frequencies are refused.

### Read-only probe (`marketpulse probe eia`)

Before ingesting, the probe checks every catalogued series against the live API and stores
nothing. Per series it checks:
- that the route still offers the series' frequency and date format, a `series` facet, and a
  `value` column;
- that the series-ID route still maps to the catalogued route, facet, and frequency;
- that the first and last rows (ascending, one row each) match the catalogued frequency, date
  format, series, description, and units.

It reports the row count, the first and last periods, and how old the latest period is,
compared with `max_staleness_days`.

- **Requests:** at most 4 per series, with route metadata fetched once per route. All 12 series
  take 43 requests at the client's 1 request/second.
- **Stores nothing:**
  - the probe lives in the `sources` layer, and an import-linter contract forbids that layer from
    importing the database or ingestion code, so it opens no database connection, creates no
    ingestion run, and records no raw payload;
  - responses are parsed in memory and discarded.
- **Never shows values, for any series.** The report holds only metadata, counts, and periods. A
  malformed `value` field is reported with the value withheld. The three Refinitiv/LSEG spot-price
  series are flagged as licensed for personal use only.
- **Stops early:** an invalid key (HTTP 401/403) or an exhausted rate limit (429 after retries)
  stops the probe; the remaining series are reported as not probed.
- **Exit codes:**
  - 0: every series ok;
  - 1: any series failed, or a configuration error such as a missing key or an unknown code;
  - 2: warnings only, meaning a series is older than its `max_staleness_days`.

### How Market Pulse versions EIA data (first-seen snapshots)

EIA publishes **no vintages**, so Market Pulse records what it saw and when
([ADR-0011](adr/0011-first-seen-snapshot-versioning.md)); `ref.series.vintage_basis` is
`first_seen` for every EIA series.

- Each complete, validated response for one series is a **snapshot** at one instant,
  `snapshot_at`, taken only after every page was retrieved, validated, and mapped to its raw
  payload. New periods start a version at `snapshot_at`; unchanged values (exact decimal
  equality) are confirmed; changed values close the old version and start a new one.
- **Version times are when Market Pulse first retrieved a value, not EIA's publication time.**
  No earlier vintages are invented.
- **Provenance:** every stored EIA version records the period exactly as EIA reported it and the
  raw response **page** that contained it (`raw_payload_id`, with the page's SHA-256 and retrieval
  time in `ops.raw_payloads`). Each series is ingested in its own run, and each page's URL must
  be exactly the request made for that series: EIA host, path `/v2/<route>/data`, series facet,
  frequency, sort, page length, offset, and window `start` (incremental runs only), with no other
  parameters (several series share `petroleum/sum/sndw`).
- **Disappearing periods:** a stored period missing from a snapshot is kept unchanged and tracked
  as an open `vanished_period` warning. If **more than 5 periods, or more than 1% of the stored
  periods in scope**, vanish at once, the whole snapshot is rejected and nothing changes (no
  minimum scope size). Stale, replayed, or out-of-order snapshots are rejected the same way.
- **Modes:** the first run for a series (or `--full`) takes a full-history snapshot. Later runs
  are incremental from W = the latest stored period minus **90 days** (daily), **26 weeks**
  (weekly), or **26 months** (monthly); the request starts one period before W and rows before W
  are dropped, so correctness does not depend on whether EIA's `start` is inclusive. In a small
  incremental window the 1% rule means a single vanished period rejects the snapshot.
- **Outcomes:** `succeeded` (applied), `partial` (snapshot rejected; no observation changed),
  `failed` (fetch, validation, provenance, or write error; no observation changed), and
  `UNFINALIZED` (the run's outcome could not be recorded, so its run row still says `running`;
  the CLI states whether the snapshot's observations had already been committed, and committed
  changes are kept, never reported as rolled back). Exit codes: 0 all succeeded, 1 any failed or
  unfinalized, 2 otherwise if any partial. The vanished count is reported for rejected snapshots
  too; `-` means it was not determined. Successful raw responses and the run record are always
  kept (HTTP error responses are not stored). A retry is a new run. Each series is independent:
  a database error for one series (even while choosing its window) fails only that series.
- **Diagnostics:** failures that describe what EIA returned also record error-level
  data-quality issues: `eia_empty_response`, `eia_incomplete`, `eia_warning`, `eia_metadata`,
  `eia_data_error` (other validation failures), `eia_malformed_response`, `eia_invalid_row`, and
  `eia_request_rejected` (HTTP 400/404; the cause is not assumed). Authentication failures (403),
  rate limits (429), server and transport failures, and internal provenance or database errors
  are operational: recorded only in the run's error message.
- **API key:** a missing, empty, or whitespace-only `EIA_API_KEY` stops the job with a
  configuration error before any request or run is made.
- **Point-in-time queries** (`marketpulse.analytics.point_in_time`): the version active at an
  instant, `vintage_start <= as_of < vintage_end` (or no end), returned with the vintage basis.

### Known limitations

- **EIA point-in-time history starts at Market Pulse's first ingestion of each series.** As-of
  queries before that return nothing for EIA series, and revisions EIA made earlier are unknown.
- Revisions are only as fine-grained as ingestion runs.
- A period EIA removes permanently stays stored and counts as vanished in every snapshot that
  covers it; past the threshold, those snapshots are rejected until an operator acts (see the
  backlog in ADR-0011).
- An `UNFINALIZED` run row stays `running` until operational reconciliation of stuck runs is
  implemented (backlog).
- HTTP error responses (e.g. 400/404 rejections) are not stored as raw payloads; the run's error
  message and any `eia_request_rejected` issue are the record (backlog).
- **Verification replay** (`marketpulse verify --replay`) checks each EIA version's creation
  against the page it cites, and each closure against the closing run's page. Confirmations by
  later snapshots are not replayed, so those versions are reported as UNSUPPORTED, not VERIFIED
  ([verification.md](verification.md)).

### Tracked series

| Code | EIA ID | Freq. | License |
|---|---|---|---|
| `wti_spot` | PET.RWTC.D | daily | `personal_use_only` |
| `brent_spot` | PET.RBRTE.D | daily | `personal_use_only` |
| `henry_hub_spot` | NG.RNGWHHD.D | daily | `personal_use_only` |
| `us_crude_stocks_ex_spr` | PET.WCESTUS1.W | weekly | `public_domain` |
| `us_crude_stocks_spr` | PET.WCSSTUS1.W | weekly | `public_domain` |
| `us_crude_production` | PET.WCRFPUS2.W | weekly | `public_domain` |
| `us_refinery_utilization` | PET.WPULEUS3.W | weekly | `public_domain` |
| `us_gasoline_stocks` | PET.WGTSTUS1.W | weekly | `public_domain` |
| `us_distillate_stocks` | PET.WDISTUS1.W | weekly | `public_domain` |
| `l48_gas_storage` | NG.NW2_EPG0_SWO_R48_BCF.W | weekly | `public_domain` |
| `us_dry_gas_production` | NG.N9070US2.M | monthly | `public_domain` |
| `us_lng_exports` | NG.N9133US2.M | monthly | `public_domain` |
