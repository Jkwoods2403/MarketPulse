# ADR-0007: PostgreSQL and the data lineage model

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

The platform stores long histories of economic indicators, energy fundamentals, prices, ERCOT
grid data, news metadata, journal entries, and analytics results. Accuracy and traceability are
the top priority: every value shown must be traceable to its source and time, and its freshness
must be known. Many sources revise past values (e.g. GDP, employment, EIA estimates), and
backtests must not use data that did not exist at the time.

## Decision

### Database

- **PostgreSQL 17**, run locally via Docker Compose.
- **Plain PostgreSQL**, without the TimescaleDB extension. Expected volume (tens of millions of
  rows) is well within plain Postgres with good indexing. Tables are designed so they can be
  partitioned by time later. Adopting TimescaleDB would require a new ADR.

### Schemas

| Schema | Purpose |
|---|---|
| `ref` | Sources (provider, terms, attribution), series catalog, instruments, companies, ERCOT locations |
| `obs` | Normalized observations of economic and energy series |
| `prices` | OHLCV bars for instruments |
| `ercot` | ERCOT prices (hubs and load zones), load, fuel mix |
| `content` | News article metadata, briefings, briefing items with source links |
| `journal` | Entries, immutable revisions, hypotheses and their evaluations |
| `analytics` | Relationships, features, model runs, forecasts, evaluations |
| `ops` | Ingestion runs, raw payload references, data quality checks |

### Lineage invariants (apply to every stored data point)

1. **Source:** a foreign key to `ref.sources` plus the provider's own series or record identifier.
2. **Source time:** the period or instant the value describes, and its semantics (instant,
   interval-ending, week ending, month, and so on).
3. **Retrieval time:** when Market Pulse fetched it.
4. **Vintage:** the date the value became known (e.g. ALFRED realtime start). Revisions create new
   rows. Earlier vintages are never overwritten.
5. **Quality status:** an enumerated status such as `ok`, `preliminary`, `revised`, `estimated`,
   `delayed`, or `suspect`. Freshness (fresh, stale, unavailable) is computed by comparing the
   series' expected release schedule with its latest observation.

### Rules

- **All timestamps are stored as `timestamptz` in UTC.** Local market conventions (e.g. ERCOT
  interval-ending Central Time and the daylight-saving repeated hour) are stored explicitly
  alongside.
- **Idempotent upserts** keyed on natural keys (series, period, vintage), so re-running a load
  never creates duplicates.
- **Missing data is represented as missing.** No forward-filling, interpolation, or substitution
  in storage. Any filling done for analysis happens in the analytics layer, explicitly and
  documented.
- **Raw payloads are retained** (compressed files under `data/raw/` locally, referenced from
  `ops.raw_payloads`) so data can be reprocessed without refetching.

## Consequences

- Every value in the UI can show source, time, and status; the data is auditable.
- Point-in-time (vintage-aware) queries enable honest backtesting (ADR-0009).
- Vintage storage increases row counts for revised series, accepted.
- Raw payload retention needs disk space and a retention policy, to be defined in Milestone 1.
