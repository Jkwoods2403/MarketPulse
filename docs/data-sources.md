# Data sources

Every source Market Pulse uses, with its terms, limits, attribution, and how Market Pulse
ingests it (required by [ADR-0008](adr/0008-data-sources-and-licensing.md)). Facts are verified
against official documentation or the live API when a connector is built; the verification date
is given for each.

The authoritative list of tracked series is the version-controlled catalog in
`backend/src/marketpulse/sources/catalog/data/` (`sources.yaml` plus one file per provider).

| Source | Status | License (catalog) | Access requirement (catalog) |
|---|---|---|---|
| [EIA](#eia-us-energy-information-administration) | **Active and approved.** Connector built (12 series, first-seen snapshots); run against the live API in staging only (9 permitted series) | `public_domain`; 3 series `personal_use_only` | `registration_required` (free API key) |
| [FRED](#fred-federal-reserve-bank-of-st-louis) | **Retired as a data provider** (ADR-0015, 2026-11-11). Ingestion is blocked in code and FRED is never served. Historical data backfilled into production on 2026-11-01 and the raw FRED files are preserved unchanged | `attribution_required` | `registration_required` (free API key) |
| [ERCOT](#ercot-electric-reliability-council-of-texas) | Source catalogued, no series; **ingestion blocked** pending ERCOT licensing inquiry SS0007218 | `no_redistribution` (conservative) | `subscription_required` |

The access requirement (ADR-0014) records how a source's data may be fetched (`open`,
`registration_required`, or `subscription_required`), separately from the license, which records
what may be done with it.

**Direct sources only (ADR-0015).** Market Pulse obtains data only directly from the organization
that publishes it; aggregators and republishers are not used. A new source (for example a direct
macroeconomic publisher such as the U.S. Treasury, BLS, BEA, or the Federal Reserve) is added only
after its official access method and terms, including whether observations may be stored, are
verified and recorded here and the project owner approves it. None is integrated yet.

---

## FRED (Federal Reserve Bank of St. Louis)

> **Status (2026-11-11): FRED is RETIRED as a Market Pulse data provider**
> ([ADR-0015](adr/0015-direct-source-data-only.md)). This is a project decision, not a pending
> licensing question: Market Pulse obtains data only directly from the organizations that publish
> it, and written consent from the Bank would not by itself reactivate FRED.
>
> - **Enforced in code** (PR #18): `fred` is in the source block list
>   (`backend/src/marketpulse/core/source_access.py`). `marketpulse ingest fred`, the FRED job, the
>   FRED client, and `scripts/record_fred_fixtures.py` all refuse before any request. The data API
>   never serves a FRED series, and the dashboard defines and shows none.
> - **Historical data is preserved:** the 12 series backfilled into the production database on
>   2026-11-01 ([below](#tracked-series-and-backfill-results)) and the raw FRED responses under
>   `data/raw/fred/` are kept unchanged. Any retention, deletion or archiving decision requires its
>   own reviewed and approved plan. Read-only `marketpulse verify` may still check them.
> - **Staging** holds no FRED data.
> - **Replacement:** the macroeconomic series below are not replaced yet. Any replacement comes
>   from an approved direct primary source (the original producers named in the table below).
>
> The rest of this section is the historical record of the FRED integration and of the
> 2026-11-10 terms review that first blocked it.

*API facts and series were verified 2026-11-01 against the FRED API documentation, every tracked
series' page, and the live API. The terms were re-reviewed on 2026-11-10 (below). That review
found restrictions the 2026-11-01 record did not include.*

### Terms and attribution

*Reviewed 2026-11-10 against the [FRED legal page](https://fred.stlouisfed.org/legal/) (FRED
Services Terms of Use) and the
[Additional Terms of Use for the FRED® API](https://fred.stlouisfed.org/docs/api/terms_of_use.html).
Both pages were read in full. Neither shows an effective or "last updated" date, so it cannot be
determined whether the clauses below were present on 2026-11-01. The terms may change without
notice, and continued use counts as acceptance. This is a project record, not legal advice.*

**Database storage requires the Bank's prior written consent.** The FRED Services Terms list what
users "may not, without the Bank's prior written consent" do:

- **Legal clause (p):** "Store, cache, or archive any portion of the FRED® Services or FRED®
  Content;" ... "incorporate any FRED® Content in any database, compilation, archive, cache, or
  other medium."
- **API clause (l):** do not "Use the FRED® API in connection with storing, caching, or archiving
  any portion of the FRED® Services or FRED® Content" ... "or incorporating any FRED® Content in
  any database, compilation, archive, cache, or other medium."
- **General license:** FRED content may be downloaded or printed "solely for your personal,
  non-commercial use", with notices kept intact. "Any other use of the FRED® Content is strictly
  prohibited." The Property Rights section adds that FRED content may not be "incorporated in any
  other database or compilation ... without the Bank's prior written permission."
- **No personal-use exception for database storage** was found. Market Pulse stores observations,
  revisions and raw API responses in PostgreSQL and under `data/raw/fred/`, so its FRED ingestion
  falls under clauses (p) and (l).

**Machine learning (API clause (k)).** The API terms prohibit use in developing or training "any
software program or system or machine learning". FRED data must not be used to develop or train
machine-learning models, including in the Analytics Lab, unless the Bank's written clarification
establishes that the proposed use is permitted.

**Required API attribution notice.** Any product that uses the FRED API must prominently display
this exact notice:

> This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of
> St. Louis.

Market Pulse does not display it yet. It must be added before any future permitted display of
FRED data, alongside the per-value attribution below.

**Other terms:**

- **Third-party copyright:** series with a copyright notice in their notes are owned by third
  parties and need the owner's permission for anything beyond personal use. **None of the 12
  tracked series carries a third-party copyright notice** (checked in each series' notes on
  2026-11-01; the S&P 500 series was used as a control and does carry one). The "Additional Terms
  of Use for Certain Data Series" cover only Visa SMI, which is not tracked.
- **Bulk downloading:** API clause (m) prohibits "wholesale downloading".
- **Endorsement and names:** clauses (f) and (i) prohibit implying the Bank's endorsement and
  using FRED names in hostnames.
- **Termination:** if access ends, all copies of FRED content must be destroyed.
- **Catalog license:** `attribution_required`. The license value does not encode the
  storage-consent requirement; FRED's exclusion is enforced by the source block list
  (ADR-0015), not by the catalog.
- **Per-value attribution text:** "Source: original data producer, retrieved from FRED, Federal
  Reserve Bank of St. Louis". Each series also records its original producer
  (`original_source`), e.g. U.S. Bureau of Labor Statistics for CPI.

**Safeguards kept in place.** The licensing block does not loosen any existing control. The
application-level series allowlist ([below](#how-market-pulse-ingests-fred)) and the read-only
future-date checks used before any FRED run stay in force and must not be weakened or bypassed.
The future-date checks fail if any FRED `period_start` is after today in `America/Chicago` or any
`vintage_start` is after the database's `now()`.

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

> **Retired (2026-11-11, ADR-0015):** the commands below describe the connector, which is kept
> unreachable. They refuse with an error: `fred` is in the source block list
> ([status](#fred-federal-reserve-bank-of-st-louis)).

```bash
cd backend
uv run marketpulse ingest fred                     # all 12 catalog series
uv run marketpulse ingest fred --series ust_10y    # one or more catalog codes
uv run marketpulse ingest fred --series real_gdp --full   # force a full re-fetch
```

Exit codes: 0 all series succeeded, 1 any series failed, 2 none failed but some were partial.

**Allowlist:** only the 12 series in `ALLOWED_FRED_SERIES` (`backend/src/marketpulse/ingestion/fred.py`)
can be ingested. Both the catalog code and the FRED series ID must match, and the series may carry
no license override. The list is a code constant: the catalog, the environment, and the command
line cannot widen it. Adding a series is a reviewed code change, made after checking that it
carries no third-party copyright notice.

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

Backfilled into the production database on 2026-11-01 (runs 2 and 7–17), before the
2026-11-10 terms review. These are historical figures as recorded on that date and have not been
re-checked. This data is preserved unchanged and never served (ADR-0015) until a separate plan
is approved. "Versions" counts every stored real-time version; "current" is one per period.

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
simulator of that behavior. Live EIA ingestion has been run in staging only (9 permitted series,
last retrieved 2026-11-06, as last recorded).*

### Terms and licensing

- **Terms:** <https://www.eia.gov/about/copyrights_reuse.php>. U.S. government publications are
  in the public domain; third-party items in EIA products may be protected.
- **Catalog license:** `public_domain`, except **WTI, Brent, and Henry Hub spot prices**
  (`wti_spot`, `brent_spot`, `henry_hub_spot`), which are Refinitiv (LSEG) data republished by
  EIA. They are classified **`personal_use_only`**, and Market Pulse does not assume any right
  to store, redistribute, or publicly display them. **The EIA ingestion job does not ingest
  them:** a series whose effective catalog license (its `license_override`, else the source's
  license) is `personal_use_only` is refused when requested by code (exit 1, before any HTTP
  request or ingestion run) and skipped, with a notice, by a default run. There is no override;
  ingesting them would require appropriate licensing from the original source. That
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
  at EIA's reported total and then re-checks requested and echoed offsets and length, stable
  totals, that each page holds exactly min(length, total - offset) rows, duplicate periods,
  ascending order, received == total, and metadata (frequency, date format, series,
  description, units) against the catalog. An unknown series answers HTTP 200 with total 0,
  which fails as an empty response.
- **The "incomplete return" warning is not truncation.** Observed live on 2026-11-05, EIA attaches
  it whenever the requested `length` is below the reported `total`, including on a page that
  holds every remaining row:
  - the nine-series probe got it on the final row of every series (offset total - 1, length 1);
  - three requests for `us_lng_exports` (356 rows) got no warning with length 5000 at offset 0
    or 355, nor with length 356 at offset 0.

  So it is tolerated exactly when length < total, and only on a page that also holds every row
  its request should; any other warning, or the notice where length >= total, fails the request.
  Real truncation is caught by the row counts and the total, never by the warning. This rule is
  inferred from those observations on these routes; EIA does not document it, and it is not
  guaranteed for every route.
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
- **One-row samples carry "incomplete return"** whenever a series has more than one row (length
  1 < total), on the last row too. The probe accepts it there, after checking that each sample
  holds exactly one row; the first live probe (2026-11-05) failed all nine series on this
  before the rule was corrected.
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
- **Series over 5,000 rows** are fetched in pages of 5,000; every page carries "incomplete
  return" (length 5,000 < total), which is tolerated as above. Today only the three restricted
  daily spot-price series are that long; the behavior is covered by simulator tests, not yet by
  a live run.
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

---

## ERCOT (Electric Reliability Council of Texas)

*Terms reviewed 2026-11-07 against the documents listed below. **Design only: no ERCOT account,
API request, or ingestion has been made.** ERCOT ingestion is blocked until ERCOT answers the
licensing inquiry and the project owner decides
([ADR-0014](adr/0014-ercot-licensing-and-ingestion-gate.md)). Storage and time handling are
proposed in [ADR-0012](adr/0012-ercot-storage-and-timestamp-architecture.md) and
[ADR-0013](adr/0013-ercot-market-time-and-daylight-saving.md).*

### Terms reviewed

| Document | Governs | Date shown |
|---|---|---|
| [ERCOT Terms of Use](https://www.ercot.com/help/terms), Sec. 1–10 | ercot.com and the reports and data posted on it | "Last updated: 07/20/2023" |
| [Terms of Use for ERCOT Application Programmatic Interface (API)](https://www.ercot.com/help/terms/data-portal), Sec. A–H plus Data Access Portal and Developer Portal terms | "the ERCOT API, which includes the Data Access Portal site and the ERCOT Developer Portal site" | None |
| [ERCOT Public API Applications](https://www.ercot.com/services/mdt/data-portal), [API Explorer Support](https://apiexplorer.ercot.com/support), [Registration and Authentication](https://developer.ercot.com/applications/pubapi/user-guide/registration-and-authentication/) | Point to the API Terms | None stated (the registration guide shows "October 26, 2026"; the page does not say what the date means) |

No separate data-use agreement, license, or market-data restriction for public reports was found.
Not reviewed: any confidentiality or protected-information provisions of the ERCOT Nodal
Protocols (section not identified), content behind the ERCOT Public Portal login, and the Data
Access Portal application (its page could not be read).

### Findings

| Question | Status | Source | Exact language |
|---|---|---|---|
| Store API data privately | **Confirmed** that ERCOT expects downloaded reports to be stored on the user's own hardware (an instruction, not a license grant) | API Terms, Data Access Portal terms, "Limitations on Downloads"; the Public API Applications page says the Data Access Portal includes API access | "Reports should be downloaded and stored on the users' own hardware to avoid downloading from the site multiple times during a yearly time period." / "...access to expired ERCOT public data though a web portal and directly, through the use of APIs." |
| Keep raw API responses indefinitely | **Unresolved**: no retention period or deletion duty anywhere | Not addressed | None |
| Personal, non-commercial research | **Confirmed for public ercot.com content; unresolved for API data** | Website Terms Sec. 5 | "The publicly available contents of this website may be used, reproduced, and redistributed, provided that the contents are not modified and that you maintain all copyright and other notices contained in the contents, including this Agreement." |
| Commercial use; separate agreement | **Unresolved**: neither document mentions commercial use; written consent exists only for exceeding download limits | API Terms, Data Access Portal General Limitations | "...ERCOT may refuse such request or condition approval of the request on your agreement to additional terms and/or charges for that use" |
| Redistribute derived charts, statistics, analyses | **Confirmed for public ercot.com raw data; unresolved for API data** | Website Terms Sec. 5 | "...raw data provided in public portions of this website may be used, reproduced, and redistributed in compilations, charts, and analyses without maintaining such notices. ERCOT does not guarantee the accuracy of any such compilations, charts, or analyses." |
| Attribution | **Confirmed for ercot.com content**: notices kept when redistributed unmodified, not required for raw data in compilations, charts and analyses; the logo and name need permission. **Unresolved for API data** | Website Terms Sec. 5 and 9 | "The ERCOT logo, "ERCOT," and "The Texas Connection" are either trademarks or registered trademarks of ERCOT and may not be used without the prior written permission of ERCOT." |
| API data vs. website reports | **Confirmed that different documents apply.** Whether Sec. 5 covers API copies is **unresolved** | API Terms (introduction, Sec. A, B, D, H); Website Terms Sec. 1, 5 | "By accessing or using ERCOT's API, you are agreeing to the below Terms of Use." |

### Confirmed restrictions (API Terms)

- Accounts (Sec. A): "Accounts registered by "bots" or other automated methods are not permitted.
  You are responsible for protecting your log-in information and shall not share that information
  with other parties."
- Identity (Sec. B): "You will not misrepresent or mask either your identity or your Client's
  identity when using the ERCOT API."
- Limits (Sec. D; Data Access Portal General Limitations): "You agree to, and will not attempt to
  circumvent, such limitations."
- Repeat downloads ("Limitations on Downloads"): "You may not download the same report from the
  Data Access Portal more than three times within a 12-month period." Whether this applies to
  Public API requests, and what "the same report" means for them, is **unresolved**. Market
  Pulse's rule of never sending the same request twice is a conservative internal safeguard; it
  does not establish that the restriction is satisfied (ADR-0014).
- Enforcement (introduction): "Violation of these Terms of Use may result in suspension or
  termination of your access to the API, and any further legal action deemed appropriate by
  ERCOT."
- Changes (Website Terms Sec. 10): the terms may change "at any time without notice"; they are
  re-read before each ERCOT phase gate.

### Classification and licensing inquiry

- **Catalog:** source code `ercot`, license `no_redistribution`, access requirement
  `subscription_required`, default `vintage_basis` `first_seen` (future load-forecast series
  override it to `source_vintage`). Both classifications are conservative internal
  classifications, not legal conclusions (ADR-0014). No ERCOT series are catalogued yet; they
  follow the read-only probe.
- **Ingestion is blocked in code:** `ercot` is in `INGESTION_BLOCKED_SOURCES`
  (`backend/src/marketpulse/core/source_access.py`). Syncing the catalog creates the
  `ref.sources` row, but every ingestion run, raw payload and observation write for it is
  refused, and `marketpulse ingest ercot` exits 1. Lifting the block is a reviewed code change
  citing ERCOT's written response and the project owner's recorded decision.
- **Period conventions for interval data** (catalog, used by future ERCOT series):
  `hour_ending` (frequency `hourly`: a one-hour interval labeled by the hour in which it ends,
  ERCOT "hour ending 1-24") and `fifteen_minute_ending` (frequency `fifteen_minute`: a 15-minute
  settlement interval labeled by its end). Labels are local market time; stored interval
  identity is UTC (ADR-0012, ADR-0013).
- **Contact:** clientservices@ercot.com is an official ERCOT address, but the API Terms name it
  only for requests to exceed download limits. For "questions or clarifications related to this
  policy", API Terms Sec. H directs users to a ticket in the
  [ERCOT Public Portal](https://www.publicportal.ercot.com/csp).
- **Inquiry:** ticket **SS0007218**, submitted 2026-11-07 through the ERCOT Public Portal; initial
  state New; priority 4 - Low. **Licensing clarification is pending.** When ERCOT answers, record
  the reply date and exact wording here, followed by the project owner's explicit decision. No
  ERCOT ingestion, staging included, happens before both are recorded (ADR-0014).
- **Follow-up question (drafted, not submitted):** how ERCOT defines "the same report" for
  Public API requests and counts API downloads toward the three-per-12-months limit. The text is
  in ADR-0014, for the project owner to add to SS0007218.

### API facts

| Fact | Status | Source |
|---|---|---|
| Credentials: a subscription key header plus a one-hour ID token that cannot be refreshed | Verified | Registration and Authentication: "ID tokens are valid for one hour. There is no way to refresh an ID token." |
| Earliest API data date: Dec. 11, 2023 | Verified | API Explorer Support: "The earliest date for which data can be obtained via the ERCOT Public API is Dec. 11, 2023." |
| 30 requests per minute | **Not re-checked**; in no terms document | ERCOT developer documentation read during the research: "The API allows up to 30 requests per minute"; to be confirmed in the logged-in API Explorer |
| U.S.-only access | **Unverified**; in no terms document | Stated in the first research revision; its source could not be re-identified; to be confirmed in the logged-in API Explorer |
| Maximum page size, units, settlement point names in the price products, the repeated-hour flag rule | Unresolved | Read-only probe (ADR-0013, ADR-0014) |
