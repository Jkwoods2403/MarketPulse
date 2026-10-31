# ADR-0003: Monorepo and project structure

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

The project has a TypeScript frontend and Python backend, ingestion, and analytics code. These
change together (API schemas, types, features), and the repository doubles as a public portfolio
piece, so it should be easy to navigate.

Splitting Python into several separately versioned packages would add packaging overhead with no
current benefit. A single package without rules, however, tends to become tangled.

## Decision

Use a single monorepo:

```
MarketPulse/
├── frontend/          # Next.js + TypeScript (pnpm)
├── backend/           # one Python project (uv), package `marketpulse`
│   ├── src/marketpulse/{core,db,sources,ingestion,api,analytics,content}/
│   ├── alembic/       # database migrations (added in Milestone 1)
│   └── tests/
├── research/          # exploratory notebooks; never imported by application code
├── data/              # local raw payloads and exports (gitignored)
├── docs/              # ADRs and data-source documentation
└── docker-compose.yml # local PostgreSQL (later: API and worker)
```

- **One Python package** (`marketpulse`) with internal boundaries enforced by `import-linter`
  (ADR-0002). Any module can be extracted into its own service later if needed.
- **Tooling:** `uv` for Python environments and lockfiles; `pnpm` for the frontend; Ruff for
  Python linting and formatting; mypy for type checking; pytest for tests; ESLint and the
  TypeScript compiler for the frontend.
- **Notebooks** live in `research/`. Code that proves useful is promoted into
  `marketpulse.analytics` with tests.

## Consequences

- One clone, one CI pipeline, atomic cross-stack changes.
- No inter-package versioning overhead.
- Boundaries depend on the import-linter contracts staying in CI.
