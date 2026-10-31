"""FastAPI application factory."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from marketpulse import __version__
from marketpulse.core.config import Environment, Settings, get_settings
from marketpulse.core.logging import configure_logging


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: Environment
    server_time_utc: datetime


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Market Pulse API",
        version=__version__,
        description="Energy-focused market intelligence. Every data point carries its source, "
        "timestamp, and freshness status.",
    )

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        """Liveness check. (A database readiness check is added in Milestone 1.)"""
        return HealthResponse(
            status="ok",
            version=__version__,
            environment=settings.marketpulse_env,
            server_time_utc=datetime.now(UTC),
        )

    return app
