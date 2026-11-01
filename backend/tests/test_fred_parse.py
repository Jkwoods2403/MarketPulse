"""FRED response parsing and structural validation.

Payloads follow the documented FRED JSON structure. Values are synthetic unless noted.
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from marketpulse.sources.fred import REALTIME_OPEN_END
from marketpulse.sources.fred.parse import (
    FredObservation,
    FredResponseError,
    parse_last_updated,
    parse_observations,
    parse_series_metadata,
    parse_value,
    parse_vintage_dates,
)


def obs_payload(observations: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "realtime_start": "1776-07-04",
        "realtime_end": "9999-12-31",
        "observation_start": "1600-01-01",
        "observation_end": "9999-12-31",
        "units": "lin",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": len(observations),
        "offset": 0,
        "limit": 100000,
        "observations": observations,
    }
    payload.update(overrides)
    return payload


def row(
    date_: str = "2024-01-01",
    start: str = "2024-04-25",
    end: str = "9999-12-31",
    value: str = "100.5",
) -> dict[str, str]:
    return {"realtime_start": start, "realtime_end": end, "date": date_, "value": value}


def problems(payload: Any) -> list[str]:
    with pytest.raises(FredResponseError) as info:
        parse_observations(payload)
    return info.value.problems


# --- values ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4.98", Decimal("4.98")),
        ("-0.25", Decimal("-0.25")),
        ("22768.866", Decimal("22768.866")),
        ("0", Decimal("0")),
        ("158861", Decimal("158861")),
        ("0.000001", Decimal("0.000001")),
    ],
)
def test_parse_value_exact_decimal(raw: str, expected: Decimal) -> None:
    value = parse_value(raw)
    assert value == expected
    assert str(value) == raw  # exact: no float rounding or reformatting


def test_parse_value_missing_marker_is_none() -> None:
    assert parse_value(".") is None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "NaN",
        "nan",
        "Infinity",
        "-inf",
        "1e5",
        "1,000",
        "+1",
        ".5",
        "1.",
        "abc",
        "..",
        " 4.98",
        "4.98 ",
        None,
        4.98,
        5,
    ],
)
def test_parse_value_rejects_anything_else(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_value(raw)


# --- observations ------------------------------------------------------------------------------


def test_parses_valid_observations_page() -> None:
    page = parse_observations(
        obs_payload([row(), row("2024-04-01", "2024-07-25", "9999-12-31", ".")])
    )

    assert page.count == 2 and page.offset == 0 and page.limit == 100000
    assert page.realtime_start == date(1776, 7, 4)
    assert page.realtime_end == REALTIME_OPEN_END
    assert page.observations == (
        FredObservation(date(2024, 1, 1), date(2024, 4, 25), REALTIME_OPEN_END, Decimal("100.5")),
        FredObservation(date(2024, 4, 1), date(2024, 7, 25), REALTIME_OPEN_END, None),
    )
    assert page.observations[0].is_current
    assert not page.has_more


def test_accepts_bytes_and_str_payloads() -> None:
    payload = obs_payload([row()])
    assert parse_observations(json.dumps(payload)) == parse_observations(
        json.dumps(payload).encode()
    )


def test_has_more_when_count_exceeds_returned_rows() -> None:
    page = parse_observations(obs_payload([row()], count=3, limit=1))
    assert page.has_more


def test_empty_observation_list_is_valid() -> None:
    assert parse_observations(obs_payload([])).observations == ()


def test_unknown_extra_fields_are_ignored() -> None:
    page = parse_observations(obs_payload([{**row(), "new_field": "x"}], new_top_level="y"))
    assert len(page.observations) == 1


@pytest.mark.parametrize("payload", [b"not json", b"\xff\xfe", "[1, 2]", "null"])
def test_rejects_non_object_or_invalid_json(payload: Any) -> None:
    assert problems(payload)


@pytest.mark.parametrize(
    "key", ["realtime_start", "realtime_end", "count", "offset", "limit", "observations"]
)
def test_rejects_missing_top_level_fields(key: str) -> None:
    payload = obs_payload([row()])
    del payload[key]
    assert any(key in p for p in problems(payload))


@pytest.mark.parametrize("key", ["date", "realtime_start", "realtime_end", "value"])
def test_rejects_missing_observation_fields(key: str) -> None:
    item = row()
    del item[key]
    assert any(f"observations[0].{key}" in p for p in problems(obs_payload([item])))


@pytest.mark.parametrize(
    "bad",
    ["2024-13-01", "2024-02-30", "24-01-01", "2024/01/01", "2024-01-01T00:00:00", "", 20240101],
)
def test_rejects_invalid_dates(bad: object) -> None:
    assert any("observations[0].date" in p for p in problems(obs_payload([{**row(), "date": bad}])))


@pytest.mark.parametrize("bad", ["NaN", "", "1e3", None])
def test_rejects_invalid_values(bad: object) -> None:
    assert any(
        "observations[0].value" in p for p in problems(obs_payload([{**row(), "value": bad}]))
    )


def test_rejects_realtime_start_after_end() -> None:
    bad = row(start="2024-05-01", end="2024-04-30")
    assert any("is after realtime_end" in p for p in problems(obs_payload([bad])))


def test_rejects_rows_outside_the_requested_real_time_window() -> None:
    payload = obs_payload(
        [row(start="2024-12-31", end="2025-02-01")],
        realtime_start="2025-01-01",
        realtime_end="2025-01-31",
    )
    found = problems(payload)
    assert any("precedes the response's" in p for p in found)
    assert any("exceeds the response's" in p for p in found)


@pytest.mark.parametrize(
    ("key", "value"), [("count", -1), ("offset", "0"), ("limit", True), ("count", 1.5)]
)
def test_rejects_invalid_paging_numbers(key: str, value: object) -> None:
    assert any(key in p for p in problems(obs_payload([row()], **{key: value})))


def test_rejects_inconsistent_paging() -> None:
    assert any(
        "exceeds count" in p for p in problems(obs_payload([row(), row("2024-04-01")], count=1))
    )
    assert any(
        "exceeds limit" in p for p in problems(obs_payload([row(), row("2024-04-01")], limit=1))
    )


@pytest.mark.parametrize(("key", "value"), [("output_type", 2), ("units", "pch")])
def test_rejects_transformed_or_non_period_output(key: str, value: object) -> None:
    assert any(key in p for p in problems(obs_payload([row()], **{key: value})))


def test_collects_every_problem_and_names_each_row() -> None:
    payload = obs_payload([{**row(), "value": "NaN"}, {**row(), "date": "bad"}, "not an object"])
    found = problems(payload)
    assert len(found) == 3
    assert {p.split(":")[0].split(".")[0] for p in found} == {
        "observations[0]",
        "observations[1]",
        "observations[2]",
    }


def test_error_message_is_bounded_for_huge_failures() -> None:
    payload = obs_payload([{**row(), "value": "x"}] * 500)
    with pytest.raises(FredResponseError) as info:
        parse_observations(payload)
    assert len(info.value.problems) == 500
    assert "and 480 more" in str(info.value)


# --- vintage dates -----------------------------------------------------------------------------


def vintage_payload(dates: list[Any], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "realtime_start": "1776-07-04",
        "realtime_end": "9999-12-31",
        "order_by": "vintage_date",
        "sort_order": "asc",
        "count": len(dates),
        "offset": 0,
        "limit": 10000,
        "vintage_dates": dates,
    }
    payload.update(overrides)
    return payload


def test_parses_vintage_dates() -> None:
    page = parse_vintage_dates(vintage_payload(["2005-06-28", "2005-06-29", "2005-06-30"]))
    assert page.vintage_dates == (date(2005, 6, 28), date(2005, 6, 29), date(2005, 6, 30))
    assert not page.has_more


@pytest.mark.parametrize(
    "dates", [["2005-06-29", "2005-06-28"], ["2005-06-28", "2005-06-28"], ["2005-06-28", "bad"]]
)
def test_rejects_unordered_duplicate_or_invalid_vintage_dates(dates: list[str]) -> None:
    with pytest.raises(FredResponseError):
        parse_vintage_dates(vintage_payload(dates))


def test_vintage_dates_paging() -> None:
    page = parse_vintage_dates(vintage_payload(["2005-06-28"], count=5, limit=1))
    assert page.has_more


# --- series metadata ---------------------------------------------------------------------------


def series_payload(**overrides: Any) -> dict[str, Any]:
    series = {
        "id": "DGS10",
        "realtime_start": "2026-11-01",
        "realtime_end": "2026-11-01",
        "title": "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, "
        "Quoted on an Investment Basis",
        "observation_start": "1962-01-02",
        "observation_end": "2026-10-29",
        "frequency": "Daily",
        "frequency_short": "D",
        "units": "Percent",
        "units_short": "%",
        "seasonal_adjustment": "Not Seasonally Adjusted",
        "seasonal_adjustment_short": "NSA",
        "last_updated": "2026-10-30 15:16:46-05",
        "popularity": 95,
    }
    series.update(overrides)
    return {"realtime_start": "2026-11-01", "realtime_end": "2026-11-01", "seriess": [series]}


def test_parses_series_metadata_with_timezone_aware_last_updated() -> None:
    meta = parse_series_metadata(series_payload())
    assert meta.series_id == "DGS10"
    assert meta.frequency_short == "D"
    assert meta.units == "Percent"
    assert meta.observation_end == date(2026, 10, 29)
    assert meta.last_updated == datetime(2026, 10, 30, 20, 16, 46, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "utc"),
    [
        ("2026-10-30 15:16:46-05", datetime(2026, 10, 30, 20, 16, 46, tzinfo=UTC)),
        ("2026-03-02 08:30:00-06", datetime(2026, 3, 2, 14, 30, tzinfo=UTC)),
        ("2026-03-02 08:30:00+0530", datetime(2026, 3, 2, 3, 0, tzinfo=UTC)),
    ],
)
def test_parse_last_updated_offsets(raw: str, utc: datetime) -> None:
    parsed = parse_last_updated(raw)
    assert parsed == utc
    assert parsed.utcoffset() is not None


@pytest.mark.parametrize("raw", ["2026-10-30 15:16:46", "2026-10-30", "yesterday", ""])
def test_parse_last_updated_rejects_missing_timezone_or_garbage(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_last_updated(raw)


@pytest.mark.parametrize(
    "overrides",
    [
        {"frequency_short": ""},
        {"units": None},
        {"last_updated": "2026-10-30"},
        {"observation_start": "bad"},
    ],
)
def test_rejects_invalid_series_metadata(overrides: dict[str, Any]) -> None:
    with pytest.raises(FredResponseError):
        parse_series_metadata(series_payload(**overrides))


@pytest.mark.parametrize("seriess", [[], [{}, {}], "x"])
def test_rejects_series_response_without_exactly_one_series(seriess: object) -> None:
    with pytest.raises(FredResponseError, match="exactly one"):
        parse_series_metadata({"seriess": seriess})


def test_open_end_is_date_max_so_code_must_not_add_a_day() -> None:
    assert date.max == REALTIME_OPEN_END
    with pytest.raises(OverflowError):
        REALTIME_OPEN_END + timedelta(days=1)
