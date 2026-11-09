"""The API's OpenAPI schema as a file: the contract the frontend's TypeScript types are generated
from (frontend/src/lib/api-schema.ts, via openapi-typescript).

`uv run marketpulse openapi --output openapi.json` rewrites backend/openapi.json. The schema is
built from the application's routes and models alone: no settings file is read and no database
is contacted. tests/test_openapi_contract.py fails if the committed file is out of date.
"""

import json
from datetime import date, datetime
from typing import Any, NoReturn

from marketpulse.api.main import create_app
from marketpulse.api.repository import ObservationPage, SeriesMeta
from marketpulse.api.schemas import DataMode, LatestObservation
from marketpulse.core.config import Environment, Settings


class _SchemaOnlyRepository:
    """Satisfies the repository protocol so the app can be built; never serves a request."""

    data_mode: DataMode = "database"

    def _unused(self) -> NoReturn:
        raise RuntimeError("the schema-only application does not serve requests")

    def list_series(self) -> list[tuple[SeriesMeta, LatestObservation | None]]:
        self._unused()

    def get_series(self, code: str) -> tuple[SeriesMeta, LatestObservation | None] | None:
        self._unused()

    def observations(
        self,
        meta: SeriesMeta,
        *,
        start: date | None,
        end: date | None,
        as_of: datetime | None,
        limit: int,
        offset: int,
    ) -> ObservationPage:
        self._unused()


def openapi_schema() -> dict[str, Any]:
    settings = Settings(_env_file=None, marketpulse_env=Environment.TEST)
    return create_app(settings, repository=_SchemaOnlyRepository()).openapi()


def render_openapi() -> str:
    """Deterministic JSON text (stable key order, two-space indent, trailing newline)."""
    return json.dumps(openapi_schema(), indent=2, ensure_ascii=False) + "\n"
