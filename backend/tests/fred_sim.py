"""An in-memory FRED API simulator for end-to-end tests (served through httpx.MockTransport).

It behaves like the real API in the ways the pipeline depends on (verified against FRED's docs
and the recorded fixtures):
- /fred/series returns metadata; /fred/series/vintagedates returns paged, sorted vintage dates;
- /fred/series/observations returns rows by real-time period, *cut to the requested window*
  (start = max(row start, window start), end = min(row end, window end)), with offset paging;
- a window spanning more than 2,000 vintage dates gets FRED's HTTP 400 error body;
- unknown series get FRED's "series does not exist" HTTP 400 body.

All data is synthetic and built by the test that uses it.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import httpx

from marketpulse.sources.catalog import SeriesSpec

OPEN_END = "9999-12-31"
FREQUENCY_SHORT = {"daily": "D", "weekly": "W", "monthly": "M", "quarterly": "Q", "annual": "A"}
SA_SHORT = {
    "not_seasonally_adjusted": "NSA",
    "seasonally_adjusted": "SA",
    "seasonally_adjusted_annual_rate": "SAAR",
}


@dataclass
class SimRow:
    date: str
    realtime_start: str
    realtime_end: str
    value: str  # "." for missing


@dataclass
class SimSeries:
    metadata: dict[str, object]
    rows: list[SimRow] = field(default_factory=list)

    def vintage_dates(self) -> list[str]:
        return sorted({r.realtime_start for r in self.rows})

    def revise(self, obs_date: str, new_vintage: str, value: str, *, day_before: str) -> None:
        """Close the current row for `obs_date` on `day_before` and add a new current row."""
        current = next(r for r in self.rows if r.date == obs_date and r.realtime_end == OPEN_END)
        current.realtime_end = day_before
        self.rows.append(SimRow(obs_date, new_vintage, OPEN_END, value))


def metadata_for(spec: SeriesSpec, **overrides: object) -> dict[str, object]:
    meta: dict[str, object] = {
        "id": spec.source_series_id,
        "realtime_start": "2026-11-01",
        "realtime_end": "2026-11-01",
        "title": spec.name,
        "observation_start": "2000-01-01",
        "observation_end": "2026-10-01",
        "frequency": spec.frequency.value.title(),
        "frequency_short": FREQUENCY_SHORT[spec.frequency.value],
        "units": spec.units,
        "seasonal_adjustment_short": SA_SHORT[spec.seasonal_adjustment.value]
        if spec.seasonal_adjustment
        else "NSA",
        "last_updated": "2026-10-30 15:16:46-05",
    }
    meta.update(overrides)
    return meta


def _json(body: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )


def _fred_error(message: str, status: int = 400) -> httpx.Response:
    return _json({"error_code": status, "error_message": message}, status)


class FakeFred:
    def __init__(self) -> None:
        self.series: dict[str, SimSeries] = {}
        # series_id -> callable(request) returning a failure response (or raising) for any request
        self.failures: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def requests_for(self, series_id: str, path_suffix: str = "") -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.url.params.get("series_id") == series_id and r.url.path.endswith(path_suffix)
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        series_id = params.get("series_id", "")
        if series_id in self.failures:
            return self.failures[series_id](request)
        sim = self.series.get(series_id)
        if sim is None:
            return _fred_error("Bad Request.  The series does not exist.")
        path = request.url.path
        if path == "/fred/series":
            return _json(
                {
                    "realtime_start": "2026-11-01",
                    "realtime_end": "2026-11-01",
                    "seriess": [sim.metadata],
                }
            )
        if path == "/fred/series/vintagedates":
            dates = sim.vintage_dates()
            offset, limit = int(params.get("offset", 0)), int(params.get("limit", 10000))
            return _json(
                {
                    "realtime_start": "1776-07-04",
                    "realtime_end": OPEN_END,
                    "count": len(dates),
                    "offset": offset,
                    "limit": limit,
                    "vintage_dates": dates[offset : offset + limit],
                }
            )
        if path == "/fred/series/observations":
            return self._observations(sim, params)
        return _fred_error("Bad Request.  Unknown path.")

    def _observations(self, sim: SimSeries, params: httpx.QueryParams) -> httpx.Response:
        w_start, w_end = params["realtime_start"], params["realtime_end"]
        in_window = [v for v in sim.vintage_dates() if w_start <= v <= w_end]
        if len(in_window) > 2000:
            return _fred_error(
                f"Bad Request.  There are {len(in_window)} vintage dates in the specified "
                f"real-time period: {w_start} to {w_end}.  This exceeds the maximum number of "
                "vintage dates allowed for this file type (2000)."
            )
        rows = [
            {
                "realtime_start": max(r.realtime_start, w_start),
                "realtime_end": min(r.realtime_end, w_end),
                "date": r.date,
                "value": r.value,
            }
            for r in sorted(sim.rows, key=lambda r: (r.date, r.realtime_start))
            if r.realtime_start <= w_end and r.realtime_end >= w_start
        ]
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 100000))
        return _json(
            {
                "realtime_start": w_start,
                "realtime_end": w_end,
                "observation_start": "1600-01-01",
                "observation_end": OPEN_END,
                "units": "lin",
                "output_type": 1,
                "file_type": "json",
                "order_by": "observation_date",
                "sort_order": "asc",
                "count": len(rows),
                "offset": offset,
                "limit": limit,
                "observations": rows[offset : offset + limit],
            }
        )


def day(d: date) -> str:
    return d.isoformat()
