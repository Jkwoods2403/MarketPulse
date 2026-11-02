"""Strict structural parsing of EIA API v2 JSON responses.

Numbers are parsed from the raw JSON text straight into Decimal (never through float), and
string values must be plain decimals, so values keep exactly the digits EIA published.
JSON null becomes `value=None` (EIA's missing value). Every other non-numeric value, such as
the "NA", "W", or "--" markers EIA uses on its web pages, is an error rather than a guess.

Warnings are *recorded*, not ignored: EIA puts them in a top-level `warnings` list of
{"warning", "description"} objects (observed live), and any other key containing "warning" is
captured too. Whether a warning is acceptable is decided by validate.assemble_pages, which
treats every warning as a failure except EIA's pagination notice on a page that is not the last.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_DECIMAL_TEXT = re.compile(r"^-?\d+(?:\.\d+)?$")
_ROW_TEXT_FIELDS = ("period", "series", "series-description", "units")


class EiaResponseError(ValueError):
    """An EIA response failed structural validation. `problems` lists every issue."""

    def __init__(self, what: str, problems: list[str]) -> None:
        self.problems = problems
        shown = "\n".join(f"  - {p}" for p in problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        super().__init__(
            f"Invalid EIA {what} response ({len(problems)} problem(s)):\n{shown}{more}"
        )


@dataclass(frozen=True)
class EiaWarning:
    warning: str  # e.g. "incomplete return", "parameter out of range: length"
    description: str | None


@dataclass(frozen=True)
class EiaRow:
    period: str  # exactly as EIA reported it, e.g. "2026-10-23" or "2026-08"
    series: str
    description: str
    units: str
    value: Decimal | None  # None only for JSON null


@dataclass(frozen=True)
class EiaDataPage:
    total: int  # rows matching the request across all pages
    frequency: str
    date_format: str
    rows: tuple[EiaRow, ...]
    echo: Mapping[str, Any]  # the request parameters EIA reports it executed
    warnings: tuple[EiaWarning, ...] = ()


@dataclass(frozen=True)
class EiaRouteMetadata:
    route_id: str
    frequencies: Mapping[str, str]  # frequency id -> date format
    facets: tuple[str, ...]
    data_columns: tuple[str, ...]
    start_period: str
    end_period: str
    default_date_format: str


def _load(payload: bytes | str, what: str) -> dict[str, Any]:
    try:
        data = json.loads(payload, parse_float=Decimal, parse_int=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError, InvalidOperation) as exc:
        raise EiaResponseError(what, [f"not valid JSON: {exc}"]) from None
    if not isinstance(data, dict):
        raise EiaResponseError(what, ["top-level JSON value is not an object"])
    return data


def _warnings(data: Mapping[str, Any], problems: list[str]) -> list[EiaWarning]:
    """Every warning in the response: the observed top-level `warnings` list of
    {"warning", "description"} objects, plus any other key containing "warning" at the top
    level or inside "response" (recorded verbatim, so unknown shapes are never dropped)."""
    found: list[EiaWarning] = []
    response = data.get("response")
    scopes: list[tuple[str, Mapping[str, Any]]] = [("", data)]
    if isinstance(response, dict):
        scopes.append(("response.", response))
    for prefix, scope in scopes:
        for key, value in scope.items():
            if "warning" not in str(key).lower():
                continue
            if prefix == "" and key == "warnings" and isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, dict) and isinstance(item.get("warning"), str):
                        description = item.get("description")
                        found.append(
                            EiaWarning(
                                item["warning"],
                                description if isinstance(description, str) else None,
                            )
                        )
                    else:
                        problems.append(f"warnings[{i}]: unrecognized warning {item!r}")
            else:
                found.append(EiaWarning(f"{prefix}{key}", str(value)))
    return found


def parse_value(raw: object) -> Decimal | None:
    """An EIA value: JSON null -> None; JSON number or decimal string -> exact Decimal."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"invalid value {raw!r}")
    if isinstance(raw, Decimal):  # JSON number, parsed from text by json.loads(parse_float=...)
        if not raw.is_finite():
            raise ValueError(f"invalid value {raw!r}")
        return raw
    if isinstance(raw, str) and _DECIMAL_TEXT.fullmatch(raw):
        return Decimal(raw)
    raise ValueError(f"unrecognized value {raw!r} (only decimals and null are accepted)")


def _total(raw: object, problems: list[str]) -> int | None:
    if isinstance(raw, Decimal) and raw == raw.to_integral_value() and raw >= 0:
        return int(raw)
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    problems.append(f"response.total: expected a non-negative integer, got {raw!r}")
    return None


def parse_data_page(payload: bytes | str) -> EiaDataPage:
    """Parse one page of a data request (native route or series-ID route)."""
    what = "data"
    data = _load(payload, what)
    problems: list[str] = []
    warnings = _warnings(data, problems)
    response = data.get("response")
    if not isinstance(response, dict):
        raise EiaResponseError(what, ["missing 'response' object"])
    total = _total(response.get("total"), problems)
    frequency = response.get("frequency")
    date_format = response.get("dateFormat")
    for key, value in (("frequency", frequency), ("dateFormat", date_format)):
        if not isinstance(value, str) or not value:
            problems.append(f"response.{key}: expected a non-empty string, got {value!r}")
    raw_rows = response.get("data")
    if not isinstance(raw_rows, list):
        problems.append("response.data: expected a list")
        raw_rows = []
    request = data.get("request")
    echo = request.get("params", {}) if isinstance(request, dict) else {}
    if not isinstance(echo, dict):
        problems.append("request.params: expected an object")
        echo = {}

    rows: list[EiaRow] = []
    for i, item in enumerate(raw_rows):
        where = f"data[{i}]"
        if not isinstance(item, dict):
            problems.append(f"{where}: expected an object")
            continue
        before = len(problems)
        text: dict[str, str] = {}
        for key in _ROW_TEXT_FIELDS:
            value = item.get(key)
            if not isinstance(value, str) or not value:
                problems.append(f"{where}.{key}: expected a non-empty string, got {value!r}")
            else:
                text[key] = value
        if "value" not in item:
            problems.append(f"{where}.value: missing")
            parsed: Decimal | None = None
        else:
            try:
                parsed = parse_value(item["value"])
            except ValueError as exc:
                problems.append(f"{where}.value: {exc}")
                parsed = None
        if len(problems) == before:
            rows.append(
                EiaRow(
                    text["period"],
                    text["series"],
                    text["series-description"],
                    text["units"],
                    parsed,
                )
            )

    if total is not None and len(raw_rows) > total:
        problems.append(f"{len(raw_rows)} rows exceeds total {total}")
    if (
        problems
        or total is None
        or not isinstance(frequency, str)
        or not isinstance(date_format, str)
    ):
        raise EiaResponseError(what, problems or ["missing response fields"])
    return EiaDataPage(total, frequency, date_format, tuple(rows), echo, tuple(warnings))


def parse_route_metadata(payload: bytes | str) -> EiaRouteMetadata:
    """Parse a route metadata response (a route without the trailing /data node)."""
    what = "route metadata"
    data = _load(payload, what)
    problems: list[str] = []
    response = data.get("response")
    if not isinstance(response, dict):
        raise EiaResponseError(what, ["missing 'response' object"])
    freqs: dict[str, str] = {}
    for i, f in enumerate(response.get("frequency") or []):
        if (
            isinstance(f, dict)
            and isinstance(f.get("id"), str)
            and isinstance(f.get("format"), str)
        ):
            freqs[f["id"]] = f["format"]
        else:
            problems.append(f"frequency[{i}]: expected an object with 'id' and 'format'")
    facets = tuple(f.get("id") for f in response.get("facets") or [] if isinstance(f, dict))
    columns = response.get("data")
    text: dict[str, str] = {}
    for key in ("id", "startPeriod", "endPeriod", "defaultDateFormat"):
        value = response.get(key)
        if not isinstance(value, str) or not value:
            problems.append(f"response.{key}: expected a non-empty string, got {value!r}")
        else:
            text[key] = value
    if not freqs:
        problems.append("response.frequency: no frequencies listed")
    if not isinstance(columns, dict) or not columns:
        problems.append("response.data: expected a non-empty object of data columns")
        columns = {}
    if problems:
        raise EiaResponseError(what, problems)
    return EiaRouteMetadata(
        text["id"],
        freqs,
        tuple(str(f) for f in facets),
        tuple(columns),
        text["startPeriod"],
        text["endPeriod"],
        text["defaultDateFormat"],
    )


def parse_error_message(body: str) -> str | None:
    """EIA's error message from either observed error-body shape, or None if unrecognized."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, str):  # {"error": "...", "code": 400}
        return error
    if isinstance(error, dict):  # {"error": {"code": "API_KEY_INVALID", "message": "..."}}
        code, message = error.get("code"), error.get("message")
        if isinstance(message, str):
            return f"{code}: {message}" if isinstance(code, str) else message
    return None
