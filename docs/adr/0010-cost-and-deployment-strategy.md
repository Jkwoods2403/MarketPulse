# ADR-0010: Cost and deployment strategy

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

Operating costs should stay as low as reasonably possible. The application is for personal use
initially and may later be deployed publicly.

## Decision

- **Development (now through Milestone 10):** everything runs locally:
  - PostgreSQL in Docker Compose
  - the Python API and worker via uv
  - the Next.js development server
  - Cost: $0.
- **Continuous integration:** GitHub Actions (free for public repositories) runs linting, type
  checks, import-boundary checks, and tests on every push and pull request.
- **Deployment (Milestone 11, decided then in a new ADR):** the default candidate is one small
  VPS running Docker Compose (Postgres, API, worker, frontend) with automated database backups.
  Estimated cost is about $6–12 per month. The alternative is a free-tier frontend host plus
  managed Postgres.
- **Paid data** is deferred per ADR-0008.

## Consequences

- Near-zero cost during development.
- Local-first development avoids premature infrastructure decisions.
- Deployment work (TLS, backups, monitoring) is deferred to a later milestone.
