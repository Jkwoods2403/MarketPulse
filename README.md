# Market Pulse

**Energy-focused market intelligence platform.** Market Pulse combines financial market data,
energy market fundamentals (crude oil, natural gas, LNG, ERCOT electricity), economic indicators,
company research, and quantitative analytics in a professional terminal-style interface.

It is both a daily monitoring tool and a data engineering and quantitative research project.

> **Status: Milestone 0 (Foundation).** The project skeleton, tooling, and CI are in place.
> No market data is ingested yet; that begins in Milestone 1.

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
├── data/        Local raw data (gitignored)
├── docs/adr/    Architecture decision records
└── docker-compose.yml   Local PostgreSQL
```

## Getting started

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for PostgreSQL)
- [uv](https://docs.astral.sh/uv/) (Python 3.13 is installed automatically if missing)
- [Node.js](https://nodejs.org/) 20.9+ and [pnpm](https://pnpm.io/)

### Setup

```bash
cp .env.example .env              # then set POSTGRES_PASSWORD
docker compose up -d --wait       # PostgreSQL on localhost:5432

cd backend
uv sync
uv run marketpulse serve --reload # API: http://127.0.0.1:8000  (docs: /docs)

cd ../frontend                    # in a second terminal
pnpm install
pnpm dev                          # UI: http://localhost:3000
```

### Checks

```bash
# backend/
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run pytest

# frontend/
pnpm lint && pnpm typecheck && pnpm build
```

## Roadmap

| # | Milestone | Status |
|---|---|---|
| 0 | Foundation: repo structure, tooling, CI | ✅ |
| 1 | Data core and the FRED and EIA connectors | Next |
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
and attribution requirements are documented, and licensing is tracked per source so that a
future public deployment respects redistribution rights. See
[ADR-0008](docs/adr/0008-data-sources-and-licensing.md).

Nothing in this project is investment advice.
