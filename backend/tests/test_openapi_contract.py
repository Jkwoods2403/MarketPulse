"""The committed OpenAPI schema (backend/openapi.json) matches the application.

The frontend's TypeScript types are generated from that file, so a stale file would let the two
drift silently. Regenerate with `uv run marketpulse openapi --output openapi.json`, then
`pnpm api:types` in frontend/.
"""

import json
from pathlib import Path
from typing import Any

from marketpulse.api.openapi import openapi_schema, render_openapi
from marketpulse.cli import main

SNAPSHOT = Path(__file__).resolve().parents[1] / "openapi.json"
REGENERATE = "uv run marketpulse openapi --output openapi.json (then pnpm api:types in frontend/)"


def test_committed_schema_is_current() -> None:
    assert SNAPSHOT.read_text(encoding="utf-8") == render_openapi(), (
        f"backend/openapi.json is out of date; regenerate it: {REGENERATE}"
    )


def schemas() -> dict[str, Any]:
    components: dict[str, Any] = openapi_schema()["components"]["schemas"]
    return components


def test_decimal_values_are_strings_not_numbers() -> None:
    s = schemas()
    assert s["LatestObservation"]["properties"]["value"] == {"type": "string", "title": "Value"}
    nullable = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    for model, field in [
        ("Observation", "value"),
        ("CalculatedMetric", "absolute_change"),
        ("CalculatedMetric", "percent_change"),
        ("CalculatedMetric", "basis_points"),
        ("ObservationRef", "value"),
    ]:
        prop = dict(s[model]["properties"][field])
        prop.pop("title", None)
        assert prop == nullable, (model, field)


def test_error_responses_are_declared() -> None:
    paths = openapi_schema()["paths"]
    ref = {"$ref": "#/components/schemas/ErrorResponse"}
    observations = paths["/series/{code}/observations"]["get"]["responses"]
    assert observations["404"]["content"]["application/json"]["schema"] == ref
    assert observations["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ValidationErrorResponse"
    }
    for path in ("/series", "/series/{code}/observations", "/metrics"):
        assert (
            paths[path]["get"]["responses"]["503"]["content"]["application/json"]["schema"] == ref
        )
    assert "503" not in paths["/health"]["get"]["responses"]  # /health never reads the database


def test_cli_prints_the_schema(capsys: Any) -> None:
    assert main(["openapi"]) in (0, None)
    assert json.loads(capsys.readouterr().out) == openapi_schema()
