# ADR-0002: System architecture and module boundaries

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

Market Pulse combines scheduled data ingestion from many external providers, a historical data
store, an API, a professional web interface, and a research/ML layer. The top priorities are
reliability and data accuracy, followed by a polished interface and the ability to grow without
major rewrites. External providers have rate limits, outages, and revisions; the interface must
never break or mislead because of them.

## Decision

Adopt a layered architecture with one-directional data flow:

```
Providers → Ingestion (Python) → PostgreSQL → API (FastAPI) → Web UI (Next.js)
                                     ↕
                              Analytics (Python)
```

1. **Ingestion** fetches from providers on a schedule, saves the raw payload, validates,
   normalizes, and upserts idempotently, recording every run in an operations table.
2. **Storage** (PostgreSQL) is the single source of truth for everything the UI displays.
3. **API** is read-mostly and **reads only from the database**. It never calls an external
   provider while serving a request. Provider outages therefore surface as clearly labeled stale
   data, never as a broken page.
4. **Analytics** reads from and writes results back to the database (relationships, model runs,
   forecasts, evaluations). It never fetches from providers directly.
5. **Web UI** consumes only the API. It contains presentation logic, not data logic.

### Enforced Python module boundaries

Inside the single Python package `marketpulse` (see ADR-0003):

| Module | May import | Must not import |
|---|---|---|
| `core` | standard library, third-party | any other `marketpulse` module |
| `db` | `core` | `sources`, `ingestion`, `api`, `analytics`, `content` |
| `sources` | `core` | `db`, `ingestion`, `api`, `analytics`, `content` |
| `ingestion` | `core`, `db`, `sources` | `api`, `analytics` |
| `analytics` | `core`, `db` | `sources`, `ingestion`, `api` |
| `content` | `core`, `db` | `api` |
| `api` | `core`, `db`, `analytics` (read helpers) | `sources`, `ingestion` |

These contracts are enforced in CI with `import-linter`, so a boundary violation fails the build.

### Data lineage is a cross-cutting invariant

Every stored and served data point carries its source, its source-side timestamp, its retrieval
timestamp, and a quality/freshness status (see ADR-0007). Missing values are stored and served as
missing — never silently filled, interpolated, or substituted.

## Consequences

- Provider failures are isolated from the user experience.
- Each provider integration is self-contained in `sources/`; adding one does not touch the API or UI.
- The API cannot show "live" data fresher than the last ingestion run; this is intentional and
  is communicated by freshness labels.
- Boundary enforcement adds a small amount of tooling but prevents the most common form of
  architectural decay.
