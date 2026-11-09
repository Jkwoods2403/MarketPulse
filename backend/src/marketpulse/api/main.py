"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError

from marketpulse import __version__
from marketpulse.api import metrics, series
from marketpulse.api.repository import DatabaseSeriesRepository, SeriesRepository
from marketpulse.api.schemas import DataMode
from marketpulse.core.config import Environment, Settings, get_settings
from marketpulse.core.logging import configure_logging
from marketpulse.db.session import make_engine, make_session_factory

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: Environment
    data_mode: DataMode
    server_time_utc: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(
    settings: Settings | None = None,
    *,
    repository: SeriesRepository | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> FastAPI:
    """Build the API. By default it reads the configured database; tests and the synthetic
    development app (marketpulse.dev_app) pass their own `repository`."""
    settings = settings or get_settings()
    configure_logging(settings)

    engine = None
    if repository is None:
        engine = make_engine(settings)  # connects lazily, on the first request that needs it
        repository = DatabaseSeriesRepository(make_session_factory(engine))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            engine.dispose()

    app = FastAPI(
        title="Market Pulse API",
        version=__version__,
        description="Energy-focused market intelligence. Every data point carries its source, "
        "timestamp, and freshness status.",
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.state.clock = clock
    app.include_router(series.router)
    app.include_router(metrics.router)

    @app.exception_handler(DBAPIError)
    async def database_unavailable(_: Request, exc: DBAPIError) -> JSONResponse:
        # Connection details never reach the client; the cause is logged server-side.
        logger.error("database error while serving a request: %s", type(exc.orig).__name__)
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        """Liveness check, with the data mode the API serves."""
        return HealthResponse(
            status="ok",
            version=__version__,
            environment=settings.marketpulse_env,
            data_mode=repository.data_mode,
            server_time_utc=clock(),
        )

    return app
