"""An in-memory EIA API v2 simulator (served through httpx.MockTransport), built from the
recorded fixtures and verified live behavior (2026-11-01). test_eia_sim_fidelity.py checks that,
given the same rows, it reproduces the real fixtures exactly.

Behaviors reproduced:
- native routes "<route>/data": string values and total; ascending/descending sort; start/end
  filters on period strings; offset/length paging capped at 5,000 rows; echo of only the
  parameters sent (offset/length as strings); top-level `warnings` FIRST in the body:
  "incomplete return" on any page that does not reach the total, plus
  "parameter out of range: length" when length > 5,000 (texts copied from the fixture);
- unknown series facet on a native route: HTTP 200, total "0", no rows;
- series-ID route "seriesid/<ID>": ignores sort/start/end (always newest first), JSON-number
  values and total, offset/length honored, NO warnings even when truncated; echo includes the
  native route;
- route metadata "<route>"; error bodies for unknown series ID (404), invalid data column /
  frequency / route (400) in compact JSON, and an invalid key (403, nested, indented).

Fault injection (`FakeEia.faults`) lets tests break individual responses (drift, inconsistent
totals, ignored offsets, extra or missing rows, warnings) to exercise every controlled failure.
`FakeEia.overrides` answer a request before the simulator does (any status or body, or raise a
transport error), for failures a JSON body fault cannot express.
All series data is synthetic unless a test loads real fixture rows.
"""

import gzip
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "eia"
_OVERLIMIT = json.loads(
    gzip.decompress((FIXTURES / "native_overlimit_length_6000.json.gz").read_bytes())
)
# EIA's exact warning objects, copied from the recorded over-limit response.
LENGTH_WARNING, INCOMPLETE_WARNING = _OVERLIMIT["warnings"]
MAX_ROWS = 5000
API_VERSION, EXCEL_VERSION = "2.1.14", "2.1.0"

Fault = Callable[[httpx.Request, dict[str, Any]], dict[str, Any] | None]


@dataclass
class SimRoute:
    frequencies: list[tuple[str, str]]  # (id, date format), e.g. [("weekly", "YYYY-MM-DD")]
    description: str = "Synthetic route"
    facets: tuple[str, ...] = ("duoarea", "product", "process", "series")


@dataclass
class SimSeries:
    route: str
    facet: str
    frequency: str
    rows: list[dict[str, Any]] = field(default_factory=list)  # full row objects, ascending
    seriesid_description: str = "EIA synthetic survey data"


def make_rows(
    facet: str, description: str, units: str, points: list[tuple[str, str | None]]
) -> list[dict[str, Any]]:
    """Rows with the observed native-route field order; values as strings (or None)."""
    return [
        {
            "period": p,
            "duoarea": "NUS",
            "area-name": "U.S.",
            "product": "EPC0",
            "product-name": "Crude Oil",
            "process": "SAX",
            "process-name": "Synthetic",
            "series": facet,
            "series-description": description,
            "value": v,
            "units": units,
        }
        for p, v in points
    ]


def _json(
    body: Any, status: int = 200, *, compact: bool = False, indent: int | None = None
) -> httpx.Response:
    text = json.dumps(body, separators=(",", ":")) if compact else json.dumps(body, indent=indent)
    return httpx.Response(
        status, content=text.encode(), headers={"Content-Type": "application/json"}
    )


def _error(message: str, code: int) -> httpx.Response:
    return _json({"error": message, "code": code}, code, compact=True)


class FakeEia:
    def __init__(self, valid_key: str) -> None:
        self.valid_key = valid_key
        self.routes: dict[str, SimRoute] = {}
        self.series: dict[str, SimSeries] = {}  # facet -> series
        self.series_ids: dict[str, str] = {}  # "PET.WCESTUS1.W" -> facet
        self.faults: list[Fault] = []
        # Answer (or raise) before the simulator; None lets the request through.
        self.overrides: list[Callable[[httpx.Request], httpx.Response | None]] = []
        self.requests: list[httpx.Request] = []

    def add(self, source_series_id: str, series: SimSeries, route: SimRoute) -> None:
        self.routes.setdefault(series.route, route)
        self.series[series.facet] = series
        self.series_ids[source_series_id] = series.facet

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def data_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/data")]

    # --- dispatch -------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for override in self.overrides:
            response = override(request)
            if response is not None:
                return response
        if request.url.params.get("api_key") != self.valid_key:
            return _json(
                {
                    "error": {
                        "code": "API_KEY_INVALID",
                        "message": "An invalid api_key was supplied. Get one at "
                        "https://api.eia.gov:443",
                    }
                },
                403,
                indent=2,
            )
        path = request.url.path.removeprefix("/v2/")
        if path.startswith("seriesid/"):
            return self._series_id(request, path.removeprefix("seriesid/"))
        if path.endswith("/data") or path.endswith("/data/"):
            return self._data(request, path.rstrip("/").removesuffix("/data"))
        route = path.rstrip("/")
        if route in self.routes:
            return self._metadata(route)
        return _error(f"Requested path /{route} is not valid.", 400)

    # --- native data route ----------------------------------------------------------------------

    def _data(self, request: httpx.Request, route: str) -> httpx.Response:
        if route not in self.routes:
            return _error(f"Requested path /{route} is not valid.", 400)
        params = request.url.params
        frequencies = dict(self.routes[route].frequencies)
        frequency = params.get("frequency", "")
        if frequency not in frequencies:
            names = [f"'{f}'" for f in frequencies]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + ", and " + names[-1]
            return _error(
                f"Invalid frequency '{frequency}' provided. The only valid frequencies "
                f"are {listed}.",
                400,
            )
        if params.get("data[0]", "value") != "value":
            return _error(
                f"Invalid data '{params['data[0]']}' provided. The only valid data are 'value'.",
                400,
            )
        facet = params.get("facets[series][0]", "")
        sim = self.series.get(facet)
        rows = list(sim.rows) if sim and sim.route == route and sim.frequency == frequency else []
        start, end = params.get("start"), params.get("end")
        rows = [
            r
            for r in rows
            if (start is None or r["period"] >= start) and (end is None or r["period"] <= end)
        ]
        direction = params.get("sort[0][direction]", "desc")  # default not verified live
        rows.sort(key=lambda r: r["period"], reverse=direction == "desc")
        offset = int(params.get("offset", "0"))
        requested_length = int(params.get("length", str(MAX_ROWS)))
        length = min(requested_length, MAX_ROWS)
        page = rows[offset : offset + length]

        echo: dict[str, Any] = {
            "frequency": frequency,
            "data": [params.get("data[0]", "value")],
            "facets": {"series": [facet]},
        }
        if "sort[0][column]" in params:
            echo["sort"] = [{"column": params["sort[0][column]"], "direction": direction}]
        for key in ("start", "end", "offset", "length"):
            if key in params:
                echo[key] = params[key]
        warnings = []
        if requested_length > MAX_ROWS:
            warnings.append(LENGTH_WARNING)
        if offset + len(page) < len(rows):
            warnings.append(INCOMPLETE_WARNING)
        body: dict[str, Any] = {}
        if warnings:
            body["warnings"] = warnings
        body.update(
            {
                "response": {
                    "total": str(len(rows)),
                    "dateFormat": frequencies[frequency],
                    "frequency": frequency,
                    "data": page,
                },
                "request": {"command": f"/v2/{route}/data/", "params": echo},
                "apiVersion": API_VERSION,
                "ExcelAddInVersion": EXCEL_VERSION,
            }
        )
        for fault in self.faults:
            result = fault(request, body)
            if result is not None:
                body = result
        return _json(body)

    # --- series-ID route ------------------------------------------------------------------------

    def _series_id(self, request: httpx.Request, series_id: str) -> httpx.Response:
        facet = self.series_ids.get(series_id)
        if facet is None:
            return _error(f"Series ID '{series_id}' is not valid.", 404)
        sim = self.series[facet]
        params = request.url.params
        offset = int(params.get("offset", "0"))
        length = min(int(params.get("length", str(MAX_ROWS))), MAX_ROWS)
        rows = sorted(sim.rows, key=lambda r: r["period"], reverse=True)  # sort always ignored
        page = [
            {**r, "value": None if r["value"] is None else f"__NUM__{r['value']}"}
            for r in rows[offset : offset + length]
        ]
        body = {
            "response": {
                "total": f"__NUM__{len(rows)}",
                "dateFormat": dict(self.routes[sim.route].frequencies)[sim.frequency],
                "frequency": sim.frequency,
                "data": page,
                # observed: "petroleum" for route petroleum/sum/sndw (first segment)
                "description": sim.seriesid_description,
                "id": sim.route.split("/")[0],
            },
            "request": {
                "command": f"/v2/seriesid/{series_id}",
                "params": {
                    "frequency": sim.frequency,
                    "data": ["value"],
                    "facets": {"series": [facet]},
                    "start": None,
                    "end": None,
                    "sort": [{"column": "period", "direction": "desc"}],
                    "offset": offset,
                    "length": length,
                },
                "route": sim.route,
            },
            "apiVersion": API_VERSION,
            "ExcelAddInVersion": EXCEL_VERSION,
        }
        # JSON numbers, written from their decimal text (never through float).
        text = re.sub(r'"__NUM__([-0-9.]+)"', r"\1", json.dumps(body))
        return httpx.Response(
            200, content=text.encode(), headers={"Content-Type": "application/json"}
        )

    # --- route metadata -------------------------------------------------------------------------

    def _metadata(self, route: str) -> httpx.Response:
        r = self.routes[route]
        periods = sorted(
            row["period"] for s in self.series.values() if s.route == route for row in s.rows
        ) or ["", ""]
        body = {
            "response": {
                "id": route.split("/")[-1],
                "name": r.description,
                "description": r.description,
                "frequency": [
                    {"id": f, "description": f, "query": f[0].upper(), "format": fmt}
                    for f, fmt in r.frequencies
                ],
                "facets": [{"id": f, "description": f.title()} for f in r.facets],
                "data": {"value": []},
                "startPeriod": periods[0],
                "endPeriod": periods[-1],
                "defaultDateFormat": r.frequencies[0][1],
                "defaultFrequency": r.frequencies[0][0],
            },
            "request": {"command": f"/v2/{route}/", "params": []},
            "apiVersion": API_VERSION,
            "ExcelAddInVersion": EXCEL_VERSION,
        }
        return _json(body)
