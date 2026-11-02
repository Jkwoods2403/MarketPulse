"""EIA response parsing: real fixtures for observed formats, labelled synthetic data otherwise."""

import json
from decimal import Decimal

import pytest

from eia_mock import eia_fixture, synthetic_page, synthetic_row
from marketpulse.sources.eia.parse import (
    EiaResponseError,
    parse_data_page,
    parse_error_message,
    parse_route_metadata,
    parse_value,
)

# --- values ------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["94.7", "410625", "-0.25", "0", "3.0", "0.001"])
def test_decimal_strings_are_exact(raw: str) -> None:
    value = parse_value(raw)
    assert value == Decimal(raw) and str(value) == raw  # digits preserved exactly


def test_json_numbers_parsed_from_text_are_exact_decimals() -> None:
    # The series-ID route returns JSON numbers; parse_float=Decimal keeps the text's digits.
    page = parse_data_page(
        b'{"response": {"total": 1, "frequency": "weekly", '
        b'"dateFormat": "YYYY-MM-DD", "data": ['
        + json.dumps(synthetic_row("2026-01-02", 0))
        .replace('"value": 0', '"value": 92.10')
        .encode()
        + b"]}}"
    )
    assert str(page.rows[0].value) == "92.10"  # a float would have produced 92.1


def test_null_is_missing() -> None:
    assert parse_value(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        "NA",
        "W",
        "--",
        "(s)",
        "",
        " ",
        "NaN",
        "Infinity",
        "1e5",
        "1,000",
        "+1",
        ".5",
        "1.",
        " 94.7",
        "94.7 ",
        True,
        False,
        [],
        {},
    ],
)
def test_unrecognized_values_are_rejected_not_guessed(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_value(raw)


def test_non_finite_json_number_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_value(Decimal("NaN"))


# --- real native-route fixtures ------------------------------------------------------------


def test_native_weekly_window_real_fixture() -> None:
    page = parse_data_page(eia_fixture("native_wcestus1_2026_jan_feb"))
    assert page.total == 9  # the native route sends total as the string "9"
    assert (page.frequency, page.date_format) == ("weekly", "YYYY-MM-DD")
    assert [r.period for r in page.rows][:2] == ["2026-01-02", "2026-01-09"]
    assert page.rows[0].value == Decimal("419056")
    assert {r.series for r in page.rows} == {"WCESTUS1"}
    assert page.echo["sort"] == [{"column": "period", "direction": "asc"}]


def test_native_decimal_strings_real_fixture() -> None:
    page = parse_data_page(eia_fixture("native_wpuleus3_2026_jan_feb"))
    assert [str(r.value) for r in page.rows][:2] == ["94.7", "95.3"]
    assert {r.units for r in page.rows} == {"%"}


def test_native_monthly_real_fixture() -> None:
    page = parse_data_page(eia_fixture("native_n9133us2_2025"))
    assert page.date_format == "YYYY-MM"
    assert [r.period for r in page.rows] == [f"2025-{m:02d}" for m in range(1, 13)]


def test_series_id_route_real_fixture_numbers_and_ignored_sort() -> None:
    page = parse_data_page(eia_fixture("seriesid_wcestus1_sort_asc_ignored"))
    assert page.total == 2300  # a JSON number on this route
    assert page.rows[0].value == Decimal("410625")
    assert page.echo["sort"] == [{"column": "period", "direction": "desc"}]  # asc was ignored


def test_overlimit_real_fixture_records_both_top_level_warnings() -> None:
    page = parse_data_page(eia_fixture("native_overlimit_length_6000"))
    assert (page.total, len(page.rows)) == (65642, 5000)
    assert [w.warning for w in page.warnings] == [
        "parameter out of range: length",
        "incomplete return",
    ]
    assert page.warnings[0].description and "maximum value for length is 5000" in (
        page.warnings[0].description
    )


def test_real_paging_fixtures_carry_the_pagination_notice() -> None:
    for name in ("native_storage_page1", "native_storage_page2"):
        page = parse_data_page(eia_fixture(name))
        assert [w.warning for w in page.warnings] == ["incomplete return"]


def test_complete_real_responses_carry_no_warnings() -> None:
    for name in (
        "native_wcestus1_2026_jan_feb",
        "native_wpuleus3_2026_jan_feb",
        "native_n9133us2_2025",
    ):
        assert parse_data_page(eia_fixture(name)).warnings == ()


def test_series_id_route_truncates_without_any_warning() -> None:
    page = parse_data_page(eia_fixture("seriesid_wcestus1_sort_asc_ignored"))
    assert (page.total, len(page.rows), page.warnings) == (2300, 3, ())


def test_unknown_series_real_fixture_is_an_empty_page() -> None:
    page = parse_data_page(eia_fixture("native_unknown_facet_empty"))
    assert (page.total, page.rows) == (0, ())


# --- labelled synthetic cases ------------------------------------------------------------------


def test_synthetic_null_row_like_henry_hub_2018_01_05() -> None:
    """SYNTHETIC: mirrors the JSON null observed live for NG.RNGWHHD.D on 2018-01-05 (the real
    response is Refinitiv-sourced and is not committed)."""
    page = parse_data_page(
        synthetic_page([synthetic_row("2018-01-05", None), synthetic_row("2018-01-12", "1.5")])
    )
    assert [r.value for r in page.rows] == [None, Decimal("1.5")]


def test_warnings_in_other_places_are_recorded_too() -> None:
    """SYNTHETIC: a warning key anywhere else (top level or inside `response`) is never dropped."""
    body = json.loads(synthetic_page([synthetic_row("2026-01-02", "1")], warning="odd"))
    body["warning"] = "also odd"
    page = parse_data_page(json.dumps(body))
    assert sorted(w.warning for w in page.warnings) == ["response.warning", "warning"]


@pytest.mark.parametrize("item", ["just a string", {"description": "no warning field"}, 3])
def test_malformed_warning_entries_are_rejected(item: object) -> None:
    """SYNTHETIC: unrecognized entries in the warnings list fail parsing."""
    payload = synthetic_page([synthetic_row("2026-01-02", "1")], warnings=[item])  # type: ignore[list-item]
    with pytest.raises(EiaResponseError, match="unrecognized warning"):
        parse_data_page(payload)


def problems(payload: bytes | str) -> list[str]:
    with pytest.raises(EiaResponseError) as info:
        parse_data_page(payload)
    return info.value.problems


@pytest.mark.parametrize("payload", [b"not json", b"[1, 2]", b"null", b'{"response": []}'])
def test_rejects_invalid_documents(payload: bytes) -> None:
    assert problems(payload)


@pytest.mark.parametrize("total", ["abc", "-1", "1.5", None, True, -3])
def test_rejects_invalid_totals(total: object) -> None:
    assert any("total" in p for p in problems(synthetic_page([], total=total)))


@pytest.mark.parametrize("missing", ["period", "series", "series-description", "units", "value"])
def test_rejects_rows_missing_required_fields(missing: str) -> None:
    row = synthetic_row("2026-01-02", "1")
    del row[missing]
    assert any(missing in p for p in problems(synthetic_page([row])))


def test_collects_every_problem() -> None:
    rows = [synthetic_row("2026-01-02", "NA"), synthetic_row("2026-01-09", "W"), "junk"]
    assert len(problems(synthetic_page(rows, total="3"))) == 3  # type: ignore[list-item]


def test_more_rows_than_total_is_inconsistent() -> None:
    assert any(
        "exceeds total" in p
        for p in problems(synthetic_page([synthetic_row("2026-01-02", "1")] * 2, total="1"))
    )


# --- metadata and error bodies (real fixtures) ----------------------------------------------


def test_route_metadata_real_fixture() -> None:
    meta = parse_route_metadata(eia_fixture("metadata_petroleum_sum_sndw"))
    assert meta.frequencies == {"weekly": "YYYY-MM-DD", "four-week-average": "YYYY-MM-DD"}
    assert "series" in meta.facets and meta.data_columns == ("value",)
    assert meta.start_period == "1982-08-20"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("error_unknown_series_id", "Series ID 'PET.MARKETPULSENOPE.W' is not valid."),
        (
            "error_invalid_data_column",
            "Invalid data 'monsters' provided. The only valid data are 'value'.",
        ),
        ("error_invalid_frequency", "Invalid frequency 'hourly' provided."),
        ("error_invalid_api_key", "API_KEY_INVALID: An invalid api_key was supplied."),
    ],
)
def test_both_real_error_body_shapes(name: str, message: str) -> None:
    parsed = parse_error_message(eia_fixture(name).decode())
    assert parsed is not None and parsed.startswith(message)


@pytest.mark.parametrize("body", ["<html>oops</html>", "[]", '{"other": 1}', '{"error": 5}'])
def test_unrecognized_error_bodies_return_none(body: str) -> None:
    assert parse_error_message(body) is None
