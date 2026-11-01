"""Structural parsing and validation of FRED API JSON responses.

Parsing is strict: every problem in a response is collected and raised together as a
FredResponseError. Values are never coerced, guessed, or filled:
- "." becomes `value=None` (FRED's explicit "no value" marker);
- anything that is not a plain decimal number is an error.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from marketpulse.sources.fred import REALTIME_OPEN_END

MISSING_VALUE = "."
_DECIMAL = re.compile(r"^-?\d+(?:\.\d+)?$")
_LAST_UPDATED_HOUR_OFFSET = re.compile(r"([+-]\d{2})$")


class FredResponseError(ValueError):
    """A FRED response failed structural validation. `problems` lists every issue."""

    def __init__(self, what: str, problems: list[str]) -> None:
        self.problems = problems
        shown = "\n".join(f"  - {p}" for p in problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        super().__init__(
            f"Invalid FRED {what} response ({len(problems)} problem(s)):\n{shown}{more}"
        )


@dataclass(frozen=True)
class FredObservation:
    """One observation value valid during the closed real-time period [start, end]."""

    observation_date: date
    realtime_start: date
    realtime_end: date  # REALTIME_OPEN_END when still current
    value: Decimal | None  # None only when FRED reported "."

    @property
    def is_current(self) -> bool:
        return self.realtime_end == REALTIME_OPEN_END


@dataclass(frozen=True)
class ObservationsPage:
    realtime_start: date
    realtime_end: date
    count: int  # total rows matching the request (across all pages)
    offset: int
    limit: int
    observations: tuple[FredObservation, ...]

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.observations) < self.count


@dataclass(frozen=True)
class VintageDatesPage:
    count: int
    offset: int
    limit: int
    vintage_dates: tuple[date, ...]

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.vintage_dates) < self.count


@dataclass(frozen=True)
class FredSeriesMetadata:
    series_id: str
    title: str
    frequency_short: str  # e.g. "D", "M", "Q"
    units: str
    seasonal_adjustment_short: str
    observation_start: date
    observation_end: date
    last_updated: datetime  # timezone-aware, as reported by FRED


# --- helpers ---------------------------------------------------------------------------------


def _load(payload: bytes | str | dict[str, Any], what: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FredResponseError(what, [f"not valid JSON: {exc}"]) from None
    if not isinstance(data, dict):
        raise FredResponseError(what, ["top-level JSON value is not an object"])
    return data


def _date(raw: object, where: str, problems: list[str]) -> date | None:
    if not isinstance(raw, str):
        problems.append(f"{where}: expected a YYYY-MM-DD string, got {type(raw).__name__}")
        return None
    try:
        if len(raw) != 10:
            raise ValueError
        return date.fromisoformat(raw)
    except ValueError:
        problems.append(f"{where}: invalid date {raw!r}")
        return None


def _non_negative_int(raw: object, where: str, problems: list[str]) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        problems.append(f"{where}: expected a non-negative integer, got {raw!r}")
        return None
    return raw


def parse_value(raw: object) -> Decimal | None:
    """Parse a FRED value string. Returns None for ".". Raises ValueError otherwise."""
    if raw == MISSING_VALUE:
        return None
    if not isinstance(raw, str) or not _DECIMAL.fullmatch(raw):
        raise ValueError(f"invalid value {raw!r}")
    return Decimal(raw)


def parse_last_updated(raw: str) -> datetime:
    """Parse FRED's `last_updated`, e.g. '2026-10-30 15:16:43-05' (offset in whole hours)."""
    normalized = _LAST_UPDATED_HOUR_OFFSET.sub(r"\g<1>00", raw.strip())
    parsed = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S%z")
    if parsed.tzinfo is None:  # pragma: no cover  (%z guarantees an offset)
        raise ValueError(f"last_updated has no timezone: {raw!r}")
    return parsed


# --- responses -------------------------------------------------------------------------------


def parse_observations(payload: bytes | str | dict[str, Any]) -> ObservationsPage:
    """Parse a `fred/series/observations` JSON response (output_type 1)."""
    what = "observations"
    data = _load(payload, what)
    problems: list[str] = []

    rt_start = _date(data.get("realtime_start"), "realtime_start", problems)
    rt_end = _date(data.get("realtime_end"), "realtime_end", problems)
    count = _non_negative_int(data.get("count"), "count", problems)
    offset = _non_negative_int(data.get("offset"), "offset", problems)
    limit = _non_negative_int(data.get("limit"), "limit", problems)
    output_type = data.get("output_type", 1)
    if output_type != 1:
        problems.append(f"output_type: expected 1 (by real-time period), got {output_type!r}")
    if data.get("units", "lin") != "lin":
        problems.append(f"units: expected 'lin' (untransformed levels), got {data.get('units')!r}")

    raw_obs = data.get("observations")
    if not isinstance(raw_obs, list):
        problems.append("observations: expected a list")
        raw_obs = []

    observations: list[FredObservation] = []
    for i, item in enumerate(raw_obs):
        where = f"observations[{i}]"
        if not isinstance(item, dict):
            problems.append(f"{where}: expected an object")
            continue
        before = len(problems)
        obs_date = _date(item.get("date"), f"{where}.date", problems)
        start = _date(item.get("realtime_start"), f"{where}.realtime_start", problems)
        end = _date(item.get("realtime_end"), f"{where}.realtime_end", problems)
        value: Decimal | None = None
        try:
            value = parse_value(item.get("value"))
        except ValueError as exc:
            problems.append(f"{where}.value: {exc}")
        if start and end and start > end:
            problems.append(f"{where}: realtime_start {start} is after realtime_end {end}")
        if start and rt_start and start < rt_start:
            problems.append(f"{where}: realtime_start {start} precedes the response's {rt_start}")
        if end and rt_end and end > rt_end:
            problems.append(f"{where}: realtime_end {end} exceeds the response's {rt_end}")
        if len(problems) == before and obs_date and start and end:
            observations.append(FredObservation(obs_date, start, end, value))

    if count is not None and offset is not None and offset + len(raw_obs) > count:
        problems.append(f"offset {offset} + {len(raw_obs)} rows exceeds count {count}")
    if limit is not None and len(raw_obs) > limit:
        problems.append(f"{len(raw_obs)} rows exceeds limit {limit}")

    if problems or rt_start is None or rt_end is None:
        raise FredResponseError(what, problems or ["missing real-time period"])
    if count is None or offset is None or limit is None:  # pragma: no cover  (already a problem)
        raise FredResponseError(what, ["missing paging fields"])
    return ObservationsPage(rt_start, rt_end, count, offset, limit, tuple(observations))


def parse_vintage_dates(payload: bytes | str | dict[str, Any]) -> VintageDatesPage:
    """Parse a `fred/series/vintagedates` JSON response. Dates must be strictly increasing."""
    what = "vintage dates"
    data = _load(payload, what)
    problems: list[str] = []
    count = _non_negative_int(data.get("count"), "count", problems)
    offset = _non_negative_int(data.get("offset"), "offset", problems)
    limit = _non_negative_int(data.get("limit"), "limit", problems)

    raw = data.get("vintage_dates")
    if not isinstance(raw, list):
        problems.append("vintage_dates: expected a list")
        raw = []
    dates: list[date] = []
    for i, item in enumerate(raw):
        parsed = _date(item, f"vintage_dates[{i}]", problems)
        if parsed is None:
            continue
        if dates and parsed <= dates[-1]:
            problems.append(f"vintage_dates[{i}]: {parsed} is not after {dates[-1]}")
        dates.append(parsed)

    if count is not None and offset is not None and offset + len(raw) > count:
        problems.append(f"offset {offset} + {len(raw)} dates exceeds count {count}")
    if problems or count is None or offset is None or limit is None:
        raise FredResponseError(what, problems or ["missing paging fields"])
    return VintageDatesPage(count, offset, limit, tuple(dates))


def parse_series_metadata(payload: bytes | str | dict[str, Any]) -> FredSeriesMetadata:
    """Parse a `fred/series` JSON response containing exactly one series."""
    what = "series"
    data = _load(payload, what)
    problems: list[str] = []
    seriess = data.get("seriess")
    if not isinstance(seriess, list) or len(seriess) != 1 or not isinstance(seriess[0], dict):
        raise FredResponseError(what, ["seriess: expected a list with exactly one object"])
    s = seriess[0]

    text: dict[str, str] = {}
    for key in ("id", "title", "frequency_short", "units", "seasonal_adjustment_short"):
        value = s.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{key}: expected a non-empty string, got {value!r}")
        else:
            text[key] = value
    obs_start = _date(s.get("observation_start"), "observation_start", problems)
    obs_end = _date(s.get("observation_end"), "observation_end", problems)
    last_updated: datetime | None = None
    raw_updated = s.get("last_updated")
    try:
        if not isinstance(raw_updated, str):
            raise ValueError
        last_updated = parse_last_updated(raw_updated)
    except ValueError:
        problems.append(f"last_updated: invalid timestamp {raw_updated!r}")

    if problems or obs_start is None or obs_end is None or last_updated is None:
        raise FredResponseError(what, problems or ["missing series dates"])
    return FredSeriesMetadata(
        series_id=text["id"],
        title=text["title"],
        frequency_short=text["frequency_short"],
        units=text["units"],
        seasonal_adjustment_short=text["seasonal_adjustment_short"],
        observation_start=obs_start,
        observation_end=obs_end,
        last_updated=last_updated,
    )
