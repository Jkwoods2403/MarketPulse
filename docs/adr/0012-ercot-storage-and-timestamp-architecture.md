# ADR-0012: ERCOT storage and timestamp architecture

- **Status:** Proposed
- **Date:** 2026-11-07

## Context

ERCOT publishes interval data: 15-minute real-time settlement point prices, hourly day-ahead
prices, hourly actual load, and hourly load forecasts that are republished every hour. Existing
storage does not fit it:

- `obs.observations` keys values by `DATE` periods (`period_start`, `period_end`), so it cannot
  hold sub-daily intervals.
- On a fall-back day one local clock hour occurs twice. ERCOT's hourly label for that hour
  (expected to be "hour ending 02"; an assumption, see ADR-0013) therefore names two different
  hours, so local labels cannot be a key.
- Load forecasts are genuine provider vintages: every posting carries ERCOT's own posting time
  (`postedDatetime`), its model (`model`) and whether it is the model in use (`inUseFlag`).
- Prices and actual load carry no publication vintage, and Market Pulse assumes ERCOT may
  correct them after posting.

ADR-0007 already reserves an `ercot` schema for ERCOT prices, load and fuel mix, requires UTC
`timestamptz`, and requires local market conventions (interval ending, the repeated hour) to be
stored explicitly alongside. ADR-0011 defines first-seen versioning for providers without
vintages.

This ADR fixes the storage design before any code is written. Licensing and the ingestion gate
are in ADR-0014; market-time conversion and daylight-saving rules are in ADR-0013.

## Decision

### Scope

- **Approved series (decided by the project owner on 2026-11-06):** real-time and day-ahead
  settlement point prices for 9 points (HB_NORTH, HB_HOUSTON, HB_SOUTH, HB_WEST, LZ_NORTH,
  LZ_HOUSTON, LZ_SOUTH, LZ_WEST, LZ_LCRA); actual system load and load forecasts, each with 9
  fields: 8 weather zones plus the system total. 36 series. Field names differ between the two
  products (actual load: `coast`, `east`, `farWest`, `north`, `northC`, `southern`, `southC`,
  `west`, `total`; forecast: `northCentral`, `southCentral` and `systemTotal` instead of
  `northC`, `southC` and `total`), per the OpenAPI specification.
- **Deferred:** HB_PAN, HB_HUBAVG, HB_BUSAVG, LZ_AEN, LZ_CPS and LZ_RAYBN. Adding one later is a
  catalog change, not a schema change.
- **Generation by fuel comes from EIA**, not ERCOT. This narrows ADR-0007's "fuel mix" entry for
  the `ercot` schema.
- **History:** API history only (the API's earliest data date is Dec. 11, 2023); older archive
  files are deferred.

### Reuse, and what stays untouched

ERCOT reuses the source and series catalog (`ref.sources`, `ref.series`), ingestion runs
(`ops.ingestion_runs`), the raw payload store (`ops.raw_payloads` plus compressed files),
data-quality issues (`ops.data_quality_issues`), and `marketpulse verify --replay`.
`obs.observations`, the FRED and EIA writers, and the EIA licensing guard are not changed.

### Interval identity

- The canonical identity of every ERCOT value is a **half-open UTC interval**
  `[interval_start, interval_end)` stored as `timestamptz`. Two current versions of one series
  never share an `interval_start`.
- ERCOT's own labels are stored **as received**, never used as the key: `operating_date`,
  `hour_ending`, `interval_number` (null for hourly data), `repeated_hour_flag` (null when the
  product has none), and the raw time label text.
- Conversion from labels to UTC follows ADR-0013: `interval_start` comes from the labels, and
  `interval_end = interval_start + interval length`, computed in UTC. A row whose labels cannot
  be converted unambiguously is rejected, never guessed.

### Tables (proposed; DDL is written only after the read-only probe, ADR-0014)

**`ercot.interval_observations`**: prices and actual load.

| Column | Meaning |
|---|---|
| `series_id` | `ref.series` |
| `interval_start`, `interval_end` | UTC, half-open |
| `operating_date`, `hour_ending`, `interval_number`, `repeated_hour_flag`, `source_label` | ERCOT labels as received |
| `value` | `numeric`, exactly as reported (no rounding, no unit conversion) |
| `status` | as in `obs.observations` |
| `vintage_start`, `vintage_end` | first-seen validity (ADR-0011 semantics) |
| `retrieved_at`, `ingestion_run_id`, `raw_payload_id` | provenance |
| `last_confirmed_at`, `last_confirmed_run_id` | confirmation, as in ADR-0011 |

Constraints: one current version per (`series_id`, `interval_start`); unique
(`series_id`, `interval_start`, `vintage_start`); `interval_end > interval_start`; index on
(`series_id`, `operating_date`). A cross-check that (`series_id`, `operating_date`,
`hour_ending`, `interval_number`, `repeated_hour_flag`) identifies at most one current version
must be declared **`NULLS NOT DISTINCT`** (PostgreSQL 15 and later; the project uses 17).
Without it PostgreSQL treats null values as distinct, and `interval_number` and
`repeated_hour_flag` are null for many products, so the check would catch nothing.

**`ercot.forecast_postings`**: one row per posting of a forecast product. This is the
authority for what was available at any time.

| Column | Meaning |
|---|---|
| `id` | key |
| `product` | ERCOT report ID, e.g. NP3-565-CD |
| `posted_at`, `posted_at_ambiguous` | ERCOT `postedDatetime` in UTC; ambiguity per ADR-0013 |
| `model`, `in_use` | the model retained for this posting, and its `inUseFlag` exactly as received (expected true) |
| `expected_first_target_start`, `expected_last_target_start` | the target range the posting is expected to cover, from the product's horizon rule (below) |
| `horizon_rule` | the version of the horizon rule used, so a corrected rule can be re-applied from raw payloads |
| `first_target_start`, `last_target_start` | the first and last target interval actually present in the posting |
| `targets_expected`, `targets_received`, `rows_stored`, `targets_missing` | counts for the posting |
| `raw_payload_ids` | every raw response page of the posting (or a link table) |
| `retrieved_at`, `ingestion_run_id` | provenance |

Unique on (`product`, `posted_at`).

**`ercot.forecast_observations`**: load forecasts. Append-only; never updated.

| Column | Meaning |
|---|---|
| `series_id` | `ref.series` |
| `posting_id` | `ercot.forecast_postings`; gives `posted_at` and the model |
| `target_start`, `target_end` | UTC, half-open; ERCOT's target labels stored alongside |
| `posted_at`, `model` | copied from the posting, so the change rule and indexes need no join |
| `value` | `numeric` as reported, or null with `status = missing` (below) |
| `status` | `reported` or `missing` |
| `raw_payload_id` | the page that contained the value (null for a `missing` row) |

Constraints: unique (`series_id`, `target_start`, `posting_id`); `value` is null exactly when
`status = missing`. Indexes on (`series_id`, `target_start`, `posted_at`) and (`posting_id`).

**`ercot.market_regimes`**: `code`, `name`, `effective_from` (UTC), `source_url`. Rows:
`pre_rtcb`, and `rtcb` from operating day 2025-12-05 (RTC+B go-live, ERCOT market notice
[M-F110525-01](https://www.ercot.com/services/comm/mkt_notices/M-F110525-01)), whose first
interval starts at 2025-12-05 00:00 CST = 06:00 UTC. Analyses join on `interval_start` so
results never mix market designs without saying so.

### Vintages

- **Prices and actual load:** `vintage_basis = first_seen`. ADR-0011's snapshot rules apply per
  interval: a new interval inserts a version, an equal value confirms, a changed value closes the
  old version and inserts a new one, and history is append-only.
- **Load forecasts:** `vintage_basis = source_vintage`. The vintage is ERCOT's `posted_at`.
- This requires a **series-level `vintage_basis`** in the catalog. Today it is copied from the
  source (ADR-0011), and ERCOT needs both values under one source. `ref.series.vintage_basis`
  already exists as a column; the catalog change is a per-series override, made later.

### Forecast retention and point-in-time reconstruction

Decided by the project owner on 2026-11-06: retain only the model flagged in use, keep every
distinct published value, and do not store unchanged values again.

**Which model.** For each posting, only rows of the model with `inUseFlag = true` are retained.
Its model code and flag are recorded on the posting. A posting with no model, or more than one
model, flagged in use is rejected and recorded as an issue; how often that happens is unknown
until the probe.

**Change rule.** For each series and target interval, a posting's value is compared with the
**latest stored row for that series and target, whatever its model**:

- a row is stored when the value differs, when the model differs, or when the latest stored row
  is a `missing` row;
- otherwise nothing is stored (the value is unchanged); the posting row still records it.

Comparing across models, not per model, is what keeps model switches correct. With a switch
A → B → A, the return to model A is stored even when A's value equals A's earlier row, because
the latest stored row belongs to B.

**Expected range (horizon rule).** Each posting is expected to cover a range of target hours
that follows from its posting time and the product's horizon H. The working rule is "from the
posting's operating day through the end of the seventh following day" (H up to 192 hours). This
is an **assumption** taken from ERCOT's product description and must be verified by the probe;
the rule is versioned (`horizon_rule`) and recorded on every posting.

**Missing targets.** A target inside the posting's **expected** range that the posting does not
contain gets a `missing` row (null value), unless the latest stored row for that target is
already `missing`. So "unchanged" (no row) and "absent from this posting" (a `missing` row) are
always distinguishable. Targets outside the expected range are not marked missing, because the
horizon moves every hour. A posting whose received rows fall outside its expected range is
rejected and recorded as an issue, because that means the horizon rule is wrong.

**Reconstruction.** "The forecast for target T that was available at time A" is answered from
the posting first, then the target rows:

1. Select P = the latest posting of the product with `posted_at <= A`. If there is none, no
   forecast was available.
2. If T is outside P's expected range, P did not forecast T. Queries report this explicitly;
   they may also return the latest earlier forecast for T, labeled with its own posting time.
3. Otherwise the answer is the latest stored row for (series, T) with `posted_at <=
   P.posted_at`. If that row is `missing`, the forecast was missing in P. Its model is P's model.

Step 3 is exact because of the change rule: after every posting, the latest stored row for each
target in the posting's expected range equals that posting's value, model and missing state.
Example, one target: P1 (model A, 100) stores a row; P2 (model B, 100) stores a row because the
model changed; P3 (model A, 100) stores a row because the latest row is B's; P4 (model A, 100)
stores nothing; P5 omits the target and stores a `missing` row; P6 (model A, 100) stores a row.
As of P4 the answer is P3's row (A, 100); as of P5 it is "missing"; as of P2 it is (B, 100).

**Storage implications.**

- Forecast rows per year are at most 9 fields × H target hours × 24 postings × 365, where H is
  the forecast horizon. With H = 192 (the assumed horizon rule above, to be verified by the
  probe), that is 15,137,280, about 15.1 million, before deduplication. It is an upper bound:
  under the assumed rule a posting late in the day covers fewer than 192 hours.
- Deduplication removes unchanged values; the share is unknown until the probe. Each model
  switch can add up to 9 × H rows (1,728 at H = 192), and each missing target adds one row.
- The posting table adds 8,760 rows per year (one per hourly posting), plus the raw-page
  references.
- The forecast table is designed so it can be partitioned by `posted_at` month; whether to
  partition is decided after the probe.

### Catalog and operational changes (proposed; implemented in later steps)

- `ref.sources.access_requirement`: `open`, `registration_required`, `subscription_required`
  (ADR-0014).
- `ref.series.frequency` CHECK widened with `hourly`, `fifteen_minute`, `five_minute`.
- `ops.data_quality_issues` gains a nullable `interval_start` (UTC) so interval-level issues such
  as `missing_interval` can be tracked like `vanished_period`.

### Missing data, provenance and raw payloads

- **Missing intervals are not filled.** An expected interval that is absent opens one
  `missing_interval` warning per series and interval; the rest of the day is stored. Expected
  counts per operating day come from ADR-0013.
- **Provenance** follows ADR-0011: each new version or forecast row cites the raw response page
  that contained it, matched by redacted URL and SHA-256 of the body, from the same run.
- **Raw payloads** would be stored before parsing, in their own transaction, as for EIA. **No
  retention period is set for ERCOT raw payloads.** ERCOT's terms do not address retention, and
  the question is with ERCOT (ADR-0014). No ERCOT raw payload exists until the ingestion gate is
  lifted. Replay (`verify --replay`) works only for payloads still held; the retention period is
  decided, and recorded in a new ADR, before Phase 2 ingestion.
- **Units** are stored as reported. That prices are $/MWh and quantities MW is an assumption
  until the probe reads the API's field metadata.

## Open decisions and assumptions

| Item | Status | Settled by |
|---|---|---|
| Retention period for raw ERCOT responses | **Open decision**; ERCOT's terms are silent; asked in ticket SS0007218 | ERCOT's answer, then a new ADR |
| Rejection thresholds for interval snapshots (ADR-0011 uses more than 5 periods or 1% vanished) | **Open decision** | Probe data and a later ADR |
| Units ($/MWh, MW) | Assumption | Probe |
| Settlement point names in the price products; `settlementPointType` values; whether load zones return both plain and energy-weighted rows | Unresolved | Probe |
| Load-forecast model codes; how many models are flagged in use per posting; share of unchanged values | Unresolved | Probe |
| Forecast horizon H (192 hours assumed) and the horizon rule that sets each posting's expected range | Assumption | Probe |
| Whether the API keeps every historical forecast posting | Assumption | Probe |
| That ERCOT corrects prices or load after posting | Assumption | Observation over time |
| Partitioning `ercot.forecast_observations` | Open decision | After the probe |
| Exact column types and DDL | Deferred | Written after the probe, in its own migration |

## Consequences

- ERCOT intervals have one unambiguous identity, and fall-back days cannot collide.
- Point-in-time research works for prices (first-seen) and forecasts (ERCOT's own posting times)
  without inventing history, including across model switches and missing targets.
- The forecast table is the largest store (at most about 2.3 GB per year before deduplication at
  H = 192); the posting table keeps deduplication auditable.
- Nothing is built until ADR-0013's open rule is settled and ADR-0014's gate allows it.
