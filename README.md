# Market Pulse

**Energy-focused market intelligence platform.** Market Pulse combines financial market data,
energy market fundamentals (crude oil, natural gas, LNG, ERCOT electricity), economic indicators,
company research, and quantitative analytics in a professional terminal-style interface.

It is both a daily monitoring tool and a data engineering and quantitative research project.

> **Status (2026-11-11):** the read-only data API (Milestone 2) and dashboard v1 (Milestone 3)
> are implemented. Real-data activation has completed **Phase 6.1 in staging only**: nothing
> from it has been deployed to production. See the
> [real-data activation log](#real-data-activation-log).
>
> **Direct sources only** ([ADR-0015](docs/adr/0015-direct-source-data-only.md)): Market Pulse
> obtains data only directly from the organizations that publish it.
>
> - **EIA: active and approved.** The connector (12 energy series, 9 of them permitted;
>   first-seen snapshot versioning) is built and tested. Live ingestion has been run in staging
>   only (9 permitted series, 14,579 stored versions, last retrieved 2026-11-06). As last
>   recorded, the production database holds no EIA data; Phase 6.1 made no production
>   connection, so this was not re-checked.
> - **FRED: retired as a data provider** (2026-11-11). Ingestion is blocked in code, the API
>   never serves FRED, and the dashboard shows no FRED series. The 12 series backfilled into the
>   production database on 2026-11-01 (182,232 stored versions) and the raw FRED files are
>   preserved unchanged; any decision about them needs its own approved plan. Staging holds no
>   FRED data. See [`docs/data-sources.md`](docs/data-sources.md#fred-federal-reserve-bank-of-st-louis).
> - **Macroeconomic indicators** (rates, inflation, employment, output) are not available until
>   a direct primary source is integrated. None is approved or integrated yet.
> - **ERCOT: on hold** pending ERCOT's written permission (ADR-0014).
> - **Not available:** commodity spot prices (WTI, Brent, Henry Hub) are licensed for personal
>   use only and are neither ingested nor displayed.

## Principles

- **Real data only, directly from its publisher.** Values come from the organizations that
  publish them (today EIA), never from aggregators. Nothing is fabricated, and missing data is
  shown as missing.
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
| Frontend | Next.js 16 (App Router, server components), React 19, TypeScript, Tailwind CSS 4, TradingView Lightweight Charts 5 (Apache ECharts is planned for analytics charts and not yet used; ADR-0005) |
| API | FastAPI, Pydantic; the OpenAPI schema is committed (`backend/openapi.json`) and the frontend's TypeScript types are generated from it with openapi-typescript |
| Ingestion and analytics | Python 3.13, uv, SQLAlchemy 2, Alembic, httpx |
| Storage | PostgreSQL 17 |
| CI | GitHub Actions (backend checks and tests on PostgreSQL; frontend lint, types, tests, generated-file checks, build) |

The API reads only from the database. It never calls an external provider while serving a
request, so provider outages show up as clearly labeled stale data rather than broken pages.
Module boundaries are enforced in CI. Decisions are recorded in
[`docs/adr/`](docs/adr/README.md).

### Data API

Read-only HTTP API (FastAPI). Interactive documentation is served at `/docs`.

| Endpoint | Returns |
|---|---|
| `GET /health` | Liveness, version, environment, and `data_mode` (`database` or `synthetic`) |
| `GET /series` | Every permitted series with its metadata, source attribution, effective license, latest observation, and freshness (`fresh`, `stale`, `unavailable`, judged against each series' own release interval) |
| `GET /series/{code}/observations` | Observations of one series, oldest first. Current versions by default; `as_of` returns the versions known at an instant (no look-ahead). `start`, `end`, `limit` (≤ 5000), `offset`, and `total` for paging |
| `GET /metrics` | Calculated metrics per series (period change, year over year, five-year seasonal range, five-year low and high, as appropriate to each series), from current versions only, each with its methodology and the observations it used |

- **Licensing is enforced on the server.** Series licensed for personal use only and sources
  whose ingestion is blocked (ERCOT) are never served. Unknown and restricted codes get the
  same `404 {"detail": "series not found"}`.
- **Values are exact.** They are decimal strings as published (`"4.190"`), never floats.
  Periods missing at source are returned as missing (`value: null`), never filled in.
- **Errors are typed.** 404, 422, and 503 (database unavailable) bodies are declared in the
  OpenAPI schema.

### Dashboard

Next.js application. The server calls the API; the browser never does (ADR-0004).

| Module | Status |
|---|---|
| Market Overview, Natural Gas, Crude & Petroleum | Operational (EIA): latest values, calculated changes, and interactive historical charts |
| Macroeconomics | Planned: FRED is retired (ADR-0015) and no direct macroeconomic source is integrated yet; the page says so and shows no values |
| Electricity | On hold: no ERCOT data is requested or shown (ADR-0014) |
| Analytics, News, Watchlist, Journal | Planned: placeholder pages labeled as not operational |

- **Every value is attributed.** It shows its source, period, retrieval time, and freshness.
- **Calculated values are labeled.** They carry a "Calculated" label, separate from observed
  values, with methodology and comparison periods.
- **Spot prices are shown as unavailable,** with the licensing reason.
- **Charts** use TradingView Lightweight Charts with its attribution. Third-party notices are at
  `/notices` and in [`frontend/THIRD_PARTY_NOTICES.md`](frontend/THIRD_PARTY_NOTICES.md). While
  a chart is zoomed or panned, its caption names the observations in view and a Reset view
  button returns to the whole selected range.
- **Accessibility is checked.** The interface is keyboard accessible, meets WCAG AA contrast,
  and gives each chart a text description. See [`frontend/README.md`](frontend/README.md).
- **Non-production deployments are named.** With `MARKETPULSE_ENV=staging` the top bar of every
  page shows a Staging badge, `/health` reports `staging`, and application log lines carry the
  environment. Uvicorn's own server and access log lines do not.

### Synthetic development mode

To work on the interface without a database, serve generated fixtures:

```bash
cd backend && uv run marketpulse serve --synthetic    # development environment only (the default)
cd frontend && pnpm dev                                # open http://localhost:3000
```

The safeguards:
- The synthetic app refuses to start outside the development environment (staging included).
- Values are a deterministic walk around 100 for every series, regardless of units, so they
  cannot pass for real figures. They exist only in memory and are never written to a database.
- Every response is marked `data_mode: "synthetic"`. The dashboard shows a banner, a
  "Synthetic" tag on every value and calculation, and dashed, watermarked chart lines.
- The same licensing filter applies: restricted series are never served, even as fixtures.
- Production builds of the frontend refuse synthetic responses and show no values.

## Repository layout

```
├── backend/     Python package `marketpulse` (core, db, sources, ingestion, analytics, content, api)
│   └── openapi.json   The API contract (generated; checked by tests)
├── frontend/    Next.js application (THIRD_PARTY_NOTICES.md, third-party/ license sources)
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
- [Node.js](https://nodejs.org/) 24 (the version CI uses; 22.18 or later is required, because
  the unit tests run TypeScript files directly with Node's built-in type stripping)
- [pnpm](https://pnpm.io/) 12 (the exact version is pinned in `frontend/package.json`;
  `corepack enable` or `npm install -g pnpm@12.16.1` installs it)

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

> **FRED is retired as a data provider** ([ADR-0015](docs/adr/0015-direct-source-data-only.md)).
> `marketpulse ingest fred` is kept only to document the old connector: it refuses with an error
> before reading any setting or contacting FRED, and no option lifts that. Do not request a FRED
> API key. The connector's history is in
> [`docs/data-sources.md`](docs/data-sources.md#fred-federal-reserve-bank-of-st-louis).

Provider requests carry a descriptive User-Agent, set in `.env`:

```bash
HTTP_USER_AGENT=MarketPulse/0.1 (you@example.com)
```

EIA needs a free API key (<https://www.eia.gov/opendata/register.php>), added to `.env` as
`EIA_API_KEY=...`. Then, from `backend/`:

```bash
uv run marketpulse probe eia                               # read-only live check; stores nothing
uv run marketpulse ingest eia                              # all permitted EIA series (9 of 12)
uv run marketpulse ingest eia --series l48_gas_storage     # selected series (catalog codes)
uv run marketpulse ingest eia --series us_lng_exports --full  # full-history snapshot
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
WTI, Brent, and Henry Hub spot prices are Refinitiv data classified `personal_use_only`, and
the application does not ingest them: requesting one with `--series` is an error (exit 1,
before any request or run), and a default run skips them with a notice. The guard reads the
catalog's license classification; there is no override.

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
# backend/
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run pytest                     # database tests are skipped: see "Database tests" below

# frontend/
pnpm lint && pnpm typecheck && pnpm test && pnpm build
pnpm api:check                    # generated API types match backend/openapi.json
pnpm notices:check                # THIRD_PARTY_NOTICES.md matches installed packages
```

After changing an API response model or route, regenerate the contract and the frontend types
together (CI fails if either is stale):

```bash
cd backend  && uv run marketpulse openapi --output openapi.json   # no database or .env needed
cd frontend && pnpm api:types
```

Backend tests run isolated ([`backend/tests/isolation.py`](backend/tests/isolation.py)):
- they never read the repository `.env` (not even for database settings), provider API keys, or
  the repository `data/` directory;
- they can reach only loopback network addresses;
- they connect to PostgreSQL only in a session that explicitly opted in to a **disposable**
  server, only on that server's port, and only to databases whose name ends in `_test`
  (`<POSTGRES_DB>_test`, created and dropped for each session);
- ports **5432 (production) and 5433 (staging) are always refused**, before any connection.
  Automated tests never use the production or staging database;
- the server must also prove it is the disposable server created for this session (below), and
  connections use SCRAM authentication only.

#### Database tests

Without the opt-in, database tests are skipped without connecting (or fail, under
`MARKETPULSE_REQUIRE_DB=1`, as in CI). To run them, start a throwaway server on a free port that
is not 5432 or 5433, with synthetic credentials, and set every value explicitly in the same
command. Nothing is taken from `.env`.

A local host, an approved port, and a `_test` database name say only *where* a connection goes,
not *what* listens there: a tunnel or port forward to a real server would pass all three. So each
throwaway server gets a random **token** when it is created: its initial database is
`mp_disposable_<token>`, and the test session receives the same token as
`MARKETPULSE_TEST_DB_TOKEN` (12 to 40 lowercase letters or digits). Before dropping or creating
anything, the database fixtures check that the server has that marker database, and they check
again right before the final drop; until the check has passed, every other connection is refused.
A server without the marker fails the session. This ties the session to the server it was
created with; it does not stop anyone from deliberately creating the marker on a real server.
Connections also require SCRAM authentication (`PGREQUIREAUTH=scram-sha-256`), which the
`postgres` image uses by default, so a wrong server never receives the password in a reusable
form.

```bash
# a new token for a new throwaway server (remove it afterwards with `docker rm -fv mp-test-pg`)
TOKEN=$(python -c "import secrets; print(secrets.token_hex(12))")
docker run -d --name mp-test-pg -e POSTGRES_USER=mp_test -e POSTGRES_PASSWORD=synthetic-only \
  -e POSTGRES_DB=mp_disposable_$TOKEN -p 127.0.0.1:55432:5432 postgres:17-alpine

# backend/ (same shell, so $TOKEN is still set)
MARKETPULSE_TEST_DB=disposable MARKETPULSE_TEST_DB_TOKEN=$TOKEN \
  POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 \
  POSTGRES_USER=mp_test POSTGRES_PASSWORD=synthetic-only POSTGRES_DB=marketpulse \
  MARKETPULSE_REQUIRE_DB=1 uv run pytest -W error
```

On Windows PowerShell:

```powershell
$token = python -c "import secrets; print(secrets.token_hex(12))"
docker run -d --name mp-test-pg -e POSTGRES_USER=mp_test -e POSTGRES_PASSWORD=synthetic-only `
  -e POSTGRES_DB=mp_disposable_$token -p 127.0.0.1:55432:5432 postgres:17-alpine

# backend/
$env:MARKETPULSE_TEST_DB = "disposable"; $env:MARKETPULSE_TEST_DB_TOKEN = $token
$env:POSTGRES_HOST = "127.0.0.1"; $env:POSTGRES_PORT = "55432"; $env:POSTGRES_USER = "mp_test"
$env:POSTGRES_PASSWORD = "synthetic-only"; $env:POSTGRES_DB = "marketpulse"
$env:MARKETPULSE_REQUIRE_DB = "1"; uv run pytest -W error
```

A server created before the token was introduced has no marker and is refused: recreate it.
CI does the same with its own service container on port 55432, using a token derived from the
run (`ci<run id>x<run attempt>`).

## Real-data activation log

Phase 6 is the operational track for showing real data. Its phases are separate
from the numbered milestones in the roadmap below.

| Phase | Status | Scope |
|---|---|---|
| 6.0 Read-only staging preflight | Complete (2026-11-09) | Staging audit |
| 6.1 Staging dashboard activation | Complete and signed off (2026-11-09) | **Staging only** |
| 6.2 and later | **Not started**; requires separate approval | None |

### Phase 6.1: what changed in staging

- **Staging environment.** `MARKETPULSE_ENV=staging` is a distinct environment. It runs in
  database mode only: the synthetic app refuses to start outside development, and production
  frontend builds refuse synthetic responses.
- **Database role separation.** The application no longer logs in to staging as a superuser.

  | Role | Used for | Access |
  |---|---|---|
  | `mp_staging` | Administration only, over the container's local socket | Bootstrap superuser; no password, so no TCP login |
  | `mp_staging_read` | API and read-only verification | `SELECT` only |
  | `mp_staging_ingest` | Ingestion (no password set yet, so it cannot log in) | `SELECT`, plus `INSERT` and `UPDATE` on observations and operational tables; no deletes, catalog writes, or schema changes |

  Roles differ per environment, so they are managed by staging-only scripts kept with the
  staging deployment, not by Alembic. The grants were rehearsed on a throwaway restore of a
  staging backup before being applied.
- **Charts.** Captions follow the visible range; a Reset view button appears while zoomed; the
  last-value tag was removed from the price axis because the legend gives the exact latest
  value; the price scale keeps the line clear of the legend. A Max-range defect was also fixed:
  long histories were silently cut short on narrow panels while the caption claimed all of them.

### Phase 6.1: what was verified, in staging

Verified on 2026-11-09 against the staging database, using the 9 permitted EIA series already
stored there. No provider was contacted, and nothing was ingested.

- 1,699 backend tests, on a disposable database; 51 frontend unit tests; lint, type, import,
  and API-contract checks; a production build of the frontend.
- `marketpulse verify`: exit 0. `marketpulse verify --replay`: exit 3, with 14,336 versions
  verified, 243 unsupported, and 0 errors. Exit 3 is expected: those 243 versions match the
  raw pages they came from, but later re-confirmations are not replayed.
- Row, sequence, and schema fingerprints were identical before and after the role change.
- All 14,579 observations served by the API matched the database. An independent recomputation
  of the calculated metrics found 0 mismatches in 119 values.
- A browser run covered every route and latest-value card, with no synthetic data, and no
  console or network errors in normal use. Range buttons, hover, zoom, and Reset view were
  exercised on three representative weekly and monthly charts, not on all 11. Restricted
  spot-price series stayed unavailable, and ERCOT stayed absent.

### Staging and production are separate

| | Staging | Production |
|---|---|---|
| Phase 6.1 changes | Applied and verified | **Not deployed** |
| Database | Isolated container and volume | Not connected to or modified during Phase 6.1 |
| Role separation | Applied | Not applied; its role setup has not been reviewed |
| EIA data | 9 permitted series, last retrieved 2026-11-06 | None, as last recorded (not re-checked) |
| FRED data | None; FRED retired (ADR-0015) | Backfilled 2026-11-01, as last recorded (not re-checked); preserved unchanged and never served |

### Known limitations

- Staging verification is not a production deployment, and passing it says nothing about the
  production database.
- The dashboard has no macroeconomic data: FRED is retired and no direct macroeconomic source
  is integrated yet (ADR-0015).
- Staging runs code from before the FRED retirement (PR #18 and the dashboard update); it is
  updated separately, with its own approval.
- The Stale label has not been seen on real data: as of 2026-11-09, no series stored in staging
  was stale.
- Staging's EIA data ages until it is next refreshed; no scheduler exists.

## Roadmap

| # | Milestone | Status |
|---|---|---|
| 0 | Foundation: repo structure, tooling, CI | ✅ |
| 1 | Data core and the EIA connector | ✅ EIA connector built and tested (run live in staging only). The FRED connector was built, then retired (ADR-0015); its ingestion is blocked in code |
| 2 | API v1: series, observations, freshness | ✅ Plus calculated metrics (`/metrics`) and a committed OpenAPI contract |
| 3 | Dashboard v1 | ✅ Overview, Natural Gas, Crude & Petroleum; tested with synthetic fixtures and test databases; verified on real EIA data in staging (Phase 6.1), not yet deployed to production. Macroeconomics: planned, awaiting a direct source |
| 4 | ERCOT and weather | ERCOT design proposed (ADR-0012 to ADR-0014); ERCOT source catalogued with ingestion blocked in code until ERCOT answers the licensing inquiry |
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
- Use one clock for a new vanished-period issue's `created_at` and `last_detected_at`.
- A series column on raw payloads, if a run ever needs to cover more than one series.
- Scheduled ingestion (no scheduler exists yet).

## Data and licensing

Market Pulse uses free government and primary sources, obtained directly from the organizations
that publish them ([ADR-0015](docs/adr/0015-direct-source-data-only.md)). Each source's terms,
rate limits, and attribution requirements are documented in
[`docs/data-sources.md`](docs/data-sources.md), and licensing is tracked per source and per
series so that a future public deployment respects redistribution rights. See
[ADR-0008](docs/adr/0008-data-sources-and-licensing.md).

| Source | Status |
|---|---|
| EIA | Active and approved |
| FRED | Retired as a data provider; historical records and raw files preserved, never served |
| ERCOT | On hold pending ERCOT's written permission (ADR-0014) |

Future macroeconomic data will come from approved direct primary sources, each added only after
its terms (including storage rights) are verified and recorded.

Nothing in this project is investment advice.
