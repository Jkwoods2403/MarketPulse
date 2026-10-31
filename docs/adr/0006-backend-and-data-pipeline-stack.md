# ADR-0006: Backend and data pipeline stack — Python, FastAPI, scheduled ingestion

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

Data engineering, statistics, and machine learning are strongest in Python. The API should share
models and validation with the ingestion and analytics code. Operating costs should stay low, and
infrastructure should match actual needs.

## Decision

- **Python 3.13**, managed with **uv**.
- **FastAPI** for the HTTP API, with **Pydantic v2** for request/response schemas and validation.
- **SQLAlchemy 2.0** for database access and **Alembic** for migrations.
- **pydantic-settings** for configuration from environment variables; secrets are never committed.
- **Structured logging** (JSON in deployed environments, readable text locally).
- **Ingestion jobs** are plain Python functions exposed as CLI commands, run by a lightweight
  scheduler (APScheduler in a worker process, or cron). Every run is recorded in an
  `ops.ingestion_runs` table with status, row counts, and errors.
- **Provider clients** (`marketpulse.sources`) own rate limiting, retries with backoff, and
  timeouts for their provider.
- **Testing:** pytest. Provider clients are tested against recorded real responses (fixtures),
  so tests are deterministic and never hit live APIs in CI.

### Deliberately excluded for now

Kafka, Airflow, Celery, Redis, Kubernetes, and microservices. Prefect or Dagster may be adopted
later if pipeline complexity justifies it (would require a new ADR).

## Consequences

- One language for ingestion, API, and analytics; shared models and validation.
- Simple operations: a database, an API process, and a worker process.
- Orchestration features such as dependency graphs and a UI are not available until needed.
