# Market Pulse backend

The Python package `marketpulse`: data ingestion, the FastAPI service, and quantitative analytics.

```bash
uv sync                              # create .venv and install locked dependencies
uv run marketpulse serve --reload    # API at http://127.0.0.1:8000  (docs at /docs)

uv run marketpulse db upgrade        # apply database migrations
uv run marketpulse catalog validate  # check the series catalog (no database needed)
uv run marketpulse catalog sync      # apply the catalog to ref.sources / ref.series (--dry-run)
uv run marketpulse ingest eia        # ingest the permitted EIA series (--series CODE ..., --full)
uv run marketpulse probe eia         # read-only live EIA check, stores nothing (--series CODE ...)
uv run marketpulse verify            # read-only integrity checks (--series CODE ..., --replay)

uv run pytest                        # tests
uv run ruff check . && uv run ruff format --check .   # lint + format check
uv run mypy                          # strict type checking
uv run lint-imports                  # module boundary contracts (ADR-0002)
```

See the [root README](../README.md), [docs/adr](../docs/adr/) for architecture, and
[docs/data-sources.md](../docs/data-sources.md) for provider details.
