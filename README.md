# Market Pulse

**Energy-focused market intelligence platform.** Market Pulse combines financial market data,
energy market fundamentals (crude oil, natural gas, LNG, ERCOT electricity), economic indicators,
company research, and quantitative analytics in a professional terminal-style interface.

It is both a daily monitoring tool and a data engineering and quantitative research project.

> **Status: Milestone 1 (Data core) in progress.** FRED ingestion is complete: 12 rates, FX,
> and macro series with full revision history (182,232 stored versions, verified against the
> live API). The EIA connector (12 energy series, first-seen snapshot versioning) is built and
> tested but has not yet been run against the live API. There is no API or dashboard for the
> data yet.

## Principles

- **Real data only.** Values come from primary sources (EIA, FRED, ERCOT, U.S. Treasury, SEC
  EDGAR, and others). Nothing is fabricated, and missing data is shown as missing.
- **Every data point is traceable:** source, timestamp, and freshness or quality status.
- **Correlation is not causation.** Analytics separate statistical evidence from proposed
  explanations, and models are always compared against simple baselines.
- **No trading recommendations.**

## Architecture

```
Providers → Ingestion (Python) → PostgreSQL → API (FastAPI) → Web UI (Next.js)
                                     ↕
                              Analytics (Python)
```

| Layer | Technology |
|---|---|
| Frontend | Next.js 16, TypeScript, Tailwind CSS; TradingView Lightweight Charts and Apache ECharts (from Milestone 3) |
| API | FastAPI, Pydantic |
| Ingestion and analytics | Python 3.13, uv |
| Storage | PostgreSQL 17 |
| CI | GitHub Actions |

The API reads only from the database. It never calls an external provider while serving a
request, so provider outages show up as clearly labeled stale data rather than broken pages.
Module boundaries are enforced in CI. Decisions are recorded in
[`docs/adr/`](docs/adr/README.md).

## Repository layout

```
├── backend/     Python package `marketpulse` (core, db, sources, ingestion, analytics, content, api)
├── frontend/    Next.js application
├── research/    Exploratory notebooks (never imported by the app)
├── data/        Local raw provider responses (gitignored)
├── docs/adr/    Architecture decision records
├── docs/data-sources.md   Per-source terms, limits, attribution, and ingestion details
└── docker-compose.yml   Local PostgreSQL
```

## Getting started

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for PostgreSQL)
- [uv](https://docs.astral.sh/uv/) (Python 3.13 is installed automatically if missing)
- [Node.js](https://nodejs.org/) 20.9+ and [pnpm](https://pnpm.io/)

### Setup

```bash
cp .env.example .env              # then set POSTGRES_PASSWORD (and see "Ingesting data")
docker compose up -d --wait       # PostgreSQL on 127.0.0.1:5432

cd backend
uv sync
uv run marketpulse db upgrade     # create the database schema
uv run marketpulse catalog sync   # load the series catalog into ref.sources / ref.series
uv run marketpulse serve --reload # API: http://127.0.0.1:8000  (docs: /docs)

cd ../frontend                    # in a second terminal
pnpm install
pnpm dev                          # UI: http://localhost:3000
```

On Windows, use `POSTGRES_HOST=127.0.0.1` rather than `localhost`: `localhost` may resolve to
IPv6 first, which Docker does not publish, causing slow connection timeouts.

### Ingesting data

FRED needs a free API key (<https://fredaccount.stlouisfed.org/apikeys>). Add it to `.env`
together with a descriptive User-Agent:

```bash
FRED_API_KEY=...
HTTP_USER_AGENT=MarketPulse/0.1 (you@example.com)
```

Then, from `backend/`:

```bash
uv run marketpulse ingest fred                      # all 12 FRED series
uv run marketpulse ingest fred --series ust_10y     # selected series (catalog codes)
uv run marketpulse ingest fred --series real_gdp --full   # force a full re-fetch
```

The first run for a series is a full backfill of its version history; later runs are
incremental. The command prints a per-series summary and exits 0 (all succeeded), 1 (a series
failed), or 2 (a series was partial because of data conflicts).

EIA needs a free API key (<https://www.eia.gov/opendata/register.php>), added to `.env` as
`EIA_API_KEY=...`. Then, from `backend/`:

```bash
uv run marketpulse probe eia                               # read-only live check; stores nothing
uv run marketpulse ingest eia                              # all 12 EIA series
uv run marketpulse ingest eia --series l48_gas_storage     # selected series (catalog codes)
uv run marketpulse ingest eia --series wti_spot --full     # full-history snapshot
```

EIA publishes no vintages, so each run records a **first-seen snapshot**: version times are
when Market Pulse retrieved the values, not EIA's publication times, and EIA history starts at
the first ingestion ([ADR-0011](docs/adr/0011-first-seen-snapshot-versioning.md)). The first run
for a series takes a full snapshot; later runs are incremental (90 days, 26 weeks, or 26 months
back). A snapshot in which too many stored periods disappear (more than 5, or more than 1% of
the periods checked) is rejected without changing anything, and the run exits 2 (partial). Exit
code 1 means a series failed or is `UNFINALIZED` (its outcome could not be recorded; the CLI
says whether its observations had already been committed). A missing or blank `EIA_API_KEY`
stops the job before any request.
WTI, Brent, and Henry Hub spot prices are Refinitiv data classified `personal_use_only`: they
are ingested for personal use only and must not be redistributed.

**Provenance.** Every successful provider response is saved byte-exact and compressed under
`data/raw/` and recorded in `ops.raw_payloads` (redacted URL, retrieval time, SHA-256) *before*
it is parsed. Each stored value links to its ingestion run, every revision is kept as a separate
version with its validity period, and missing values stay missing. Each EIA value also links to
the exact raw response page that contained it. Details per source, including
licensing and known limitations, are in [`docs/data-sources.md`](docs/data-sources.md).

### Verifying stored data

From `backend/`:

```bash
uv run marketpulse verify                     # SQL integrity checks over all series
uv run marketpulse verify --series ust_10y    # limit series-level checks (catalog codes)
uv run marketpulse verify --replay            # also replay the stored raw pages
```

`verify` is **read-only**. It runs every check in one `REPEATABLE READ, READ ONLY` transaction
that is rolled back at the end. With `--replay` it only reads the raw files (hash-verified). It
never calls a provider and never prints credential values.

The SQL checks cover version overlaps and gaps, current versions, failed or unfinalized runs,
provenance, timestamps, tracked issues, and credentials in stored text.

`--replay` compares every stored version with the raw pages it came from. It counts each version
in exactly one state, by precedence:

1. **ERROR**: a mismatch;
2. **UNVERIFIED**: evidence is missing;
3. **UNSUPPORTED**: the creation matched, but a later EIA confirmation, or a FRED closure or
   confirmation, cannot be replayed;
4. **VERIFIED**: creation, provenance, and any closure all matched.

Exit codes, the first that applies:

- **1**: any error;
- **2**: warnings;
- **3**: some versions UNVERIFIED or UNSUPPORTED;
- **0**: nothing found.

On real data, `verify --replay` normally exits 3 once values have been re-confirmed. Details,
the full list of checks, and limitations are in [`docs/verification.md`](docs/verification.md).

### Checks

```bash
# backend/  (start PostgreSQL first; database tests are skipped if it is unreachable)
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run pytest                     # MARKETPULSE_REQUIRE_DB=1 makes database tests mandatory

# frontend/
pnpm lint && pnpm typecheck && pnpm build
```

Backend tests run isolated ([`backend/tests/isolation.py`](backend/tests/isolation.py)):
- they never read provider API keys or the repository `data/` directory;
- they can reach only loopback network addresses;
- they connect only to a local PostgreSQL server (`localhost`, loopback, or Unix socket) on the
  configured `POSTGRES_PORT`, and only to databases whose name ends in `_test`. Database tests use
  `<POSTGRES_DB>_test`, which is created and dropped for each session.

## Roadmap

| # | Milestone | Status |
|---|---|---|
| 0 | Foundation: repo structure, tooling, CI | ✅ |
| 1 | Data core and the FRED and EIA connectors | In progress: FRED ✅, EIA connector built (not yet run live) |
| 2 | API v1: series, observations, freshness | |
| 3 | Dashboard v1 | |
| 4 | ERCOT and weather | |
| 5 | Equities and company intelligence | |
| 6 | Events calendar and watchlists | |
| 7 | News and daily briefing | |
| 8 | Market Journal | |
| 9 | Market Connections and Analytics Lab v1 | |
| 10 | Forecasting and machine learning | |
| 11 | Deployment | |

**Deferred work (backlog):**

- An audited operator decision to retire a period EIA removed permanently (until then, such a
  period keeps counting as vanished and can cause snapshots to be rejected; ADR-0011).
- Consolidate the FRED and EIA raw-response recorders into one shared component.
- Use one clock for a new vanished-period issue's `created_at` and `last_detected_at`.
- A series column on raw payloads, if a run ever needs to cover more than one series.
- Scheduled ingestion (no scheduler exists yet).

## Data and licensing

Market Pulse uses free government and primary sources first. Each source's terms, rate limits,
and attribution requirements are documented in [`docs/data-sources.md`](docs/data-sources.md),
and licensing is tracked per source and per series so that a future public deployment respects
redistribution rights. See [ADR-0008](docs/adr/0008-data-sources-and-licensing.md).

Data from FRED is retrieved from FRED, Federal Reserve Bank of St. Louis; each series records
its original producer (for example the U.S. Bureau of Labor Statistics or the Bureau of Economic
Analysis).

Nothing in this project is investment advice.
