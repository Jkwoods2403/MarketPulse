# Market Pulse

**Energy-focused market intelligence platform.** Market Pulse combines financial market data,
energy market fundamentals (crude oil, natural gas, LNG, ERCOT electricity), economic indicators,
company research, and quantitative analytics in a professional terminal-style interface.

It is both a daily monitoring tool and a data engineering and quantitative research project.

> **Status: Milestone 1 (Data core) in progress.** FRED ingestion is complete: 12 rates, FX,
> and macro series with full revision history (182,232 stored versions, verified against the
> live API). The EIA connector is next. There is no API or dashboard for the data yet.

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

**Provenance.** Every successful provider response is saved byte-exact and compressed under
`data/raw/` and recorded in `ops.raw_payloads` (redacted URL, retrieval time, SHA-256) *before*
it is parsed. Each stored value links to its ingestion run, every revision is kept as a separate
version with its validity period, and missing values stay missing. Details per source, including
licensing and known limitations, are in [`docs/data-sources.md`](docs/data-sources.md).

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

## Roadmap

| # | Milestone | Status |
|---|---|---|
| 0 | Foundation: repo structure, tooling, CI | ✅ |
| 1 | Data core and the FRED and EIA connectors | In progress: FRED ✅, EIA next |
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

## Data and licensing

Market Pulse uses free government and primary sources first. Each source's terms, rate limits,
and attribution requirements are documented in [`docs/data-sources.md`](docs/data-sources.md),
and licensing is tracked per source and per series so that a future public deployment respects
redistribution rights. See [ADR-0008](docs/adr/0008-data-sources-and-licensing.md).

Data from FRED is retrieved from FRED, Federal Reserve Bank of St. Louis; each series records
its original producer (for example the U.S. Bureau of Labor Statistics or the Bureau of Economic
Analysis).

Nothing in this project is investment advice.
