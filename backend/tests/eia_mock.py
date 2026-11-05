"""Shared helpers for EIA tests: real fixture loading and clearly labelled synthetic pages.

Real fixtures (tests/fixtures/eia) are unmodified public-domain EIA responses. SYNTHETIC pages
built here imitate the observed native-route structure for cases that cannot be recorded
without committing Refinitiv-sourced data (e.g. the JSON null EIA reported for Henry Hub on
2018-01-05) or that the API never produced (e.g. a warning).
"""

import gzip
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "eia"


def eia_fixture(name: str) -> bytes:
    path = FIXTURES / f"{name}.json"
    if path.exists():
        return path.read_bytes()
    return gzip.decompress((FIXTURES / f"{name}.json.gz").read_bytes())


def synthetic_row(
    period: str,
    value: Any,
    *,
    series: str = "SYNTH1",
    description: str = "SYNTHETIC test series (Thousand Barrels)",
    units: str = "MBBL",
) -> dict[str, Any]:
    """SYNTHETIC row with the same fields as observed native-route rows."""
    return {
        "period": period,
        "duoarea": "NUS",
        "area-name": "U.S.",
        "product": "EPC0",
        "product-name": "Crude Oil",
        "process": "SAX",
        "process-name": "Synthetic",
        "series": series,
        "series-description": description,
        "value": value,
        "units": units,
    }


AUTO = object()  # sentinel: compute total from the rows


def synthetic_page(
    rows: list[dict[str, Any]],
    *,
    total: Any = AUTO,
    frequency: str = "weekly",
    date_format: str = "YYYY-MM-DD",
    offset: int = 0,
    length: int = 5000,
    warnings: list[dict[str, str]] | None = None,
    **response_extra: Any,
) -> bytes:
    """SYNTHETIC native-route page (total as a string, as the native route returns it).

    `warnings` are placed in the TOP-LEVEL `warnings` list, where the live API puts them.
    """
    body: dict[str, Any] = {
        "response": {
            "total": str(len(rows)) if total is AUTO else total,
            "dateFormat": date_format,
            "frequency": frequency,
            "data": rows,
            **response_extra,
        },
        "request": {
            "command": "/v2/synthetic/route/data/",
            "params": {
                "frequency": frequency,
                "data": ["value"],
                "facets": {"series": ["SYNTH1"]},
                "sort": [{"column": "period", "direction": "asc"}],
                "offset": offset,
                "length": length,
            },
        },
        "apiVersion": "2.1.14",
    }
    if warnings is not None:
        body = {"warnings": warnings, **body}
    return json.dumps(body).encode()


INCOMPLETE_RETURN = {  # verbatim from the live API (native_storage_page1 fixture)
    "warning": "incomplete return",
    "description": "The API can only return 5000 rows in JSON format.  Please consider "
    "constraining your request with facet, start, or end, or using offset to paginate results.",
}
