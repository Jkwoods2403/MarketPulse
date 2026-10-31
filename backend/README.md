# Market Pulse backend

The Python package `marketpulse`: data ingestion, the FastAPI service, and quantitative analytics.

```bash
uv sync                              # create .venv and install locked dependencies
uv run marketpulse serve --reload    # API at http://127.0.0.1:8000  (docs at /docs)

uv run pytest                        # tests
uv run ruff check . && uv run ruff format --check .   # lint + format check
uv run mypy                          # strict type checking
uv run lint-imports                  # module boundary contracts (ADR-0002)
```

See the [root README](../README.md) and [docs/adr](../docs/adr/) for architecture.
