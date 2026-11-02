"""EIA controlled failures (drift, warnings, truncation, incomplete paging, empty data) and
conversion."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from eia_mock import INCOMPLETE_RETURN, eia_fixture, synthetic_page, synthetic_row
from marketpulse.sources.catalog import load_catalog
from marketpulse.sources.catalog.schema import eia_facet_series
from marketpulse.sources.eia.observations import to_observations
from marketpulse.sources.eia.parse import EiaDataPage, parse_data_page
from marketpulse.sources.eia.validate import (
    EiaEmptyResponseError,
    EiaIncompleteError,
    EiaMetadataError,
    EiaSeriesExpectation,
    EiaWarningError,
    assemble_pages,
    check_page_metadata,
    expectation_for,
)

SPECS = {s.code: s for s in load_catalog().series_for("eia")}


def expect(code: str) -> EiaSeriesExpectation:
    return expectation_for(SPECS[code])


# --- the verified catalog matches real native-route responses --------------------------------


@pytest.mark.parametrize(
    ("code", "fixture"),
    [
        ("us_crude_stocks_ex_spr", "native_wcestus1_2026_jan_feb"),
        ("us_refinery_utilization", "native_wpuleus3_2026_jan_feb"),
        ("us_lng_exports", "native_n9133us2_2025"),
        ("l48_gas_storage", "native_storage_page1"),
    ],
)
def test_catalog_metadata_matches_real_responses(code: str, fixture: str) -> None:
    assert check_page_metadata(parse_data_page(eia_fixture(fixture)), expect(code)) == []


def test_expectation_from_catalog() -> None:
    e = expect("us_dry_gas_production")
    assert (e.route, e.facet_series, e.frequency, e.date_format, e.units) == (
        "natural-gas/sum/snd",
        "N9070US2",
        "monthly",
        "YYYY-MM",
        "MMCF",
    )


@pytest.mark.parametrize(
    ("sid", "facet"),
    [
        ("PET.RWTC.D", "RWTC"),
        ("NG.N9133US2.M", "N9133US2"),
        ("NG.NW2_EPG0_SWO_R48_BCF.W", "NW2_EPG0_SWO_R48_BCF"),
    ],
)
def test_facet_is_the_middle_of_the_series_id(sid: str, facet: str) -> None:
    assert eia_facet_series(sid) == facet


# --- drift is a controlled failure -------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"units": "BBL"}, "units"),
        ({"description": "U.S. Ending Stocks of Crude Oil (Thousand Barrels)"}, "description"),
        ({"facet_series": "WCSSTUS1"}, "series"),
        ({"frequency": "monthly"}, "frequency"),
        ({"date_format": "YYYY-MM"}, "date format"),
    ],
)
def test_metadata_drift_is_detected(change: dict[str, str], match: str) -> None:
    page = parse_data_page(eia_fixture("native_wcestus1_2026_jan_feb"))
    expected = replace(expect("us_crude_stocks_ex_spr"), **change)
    found = check_page_metadata(page, expected)
    assert any(match in p for p in found)
    with pytest.raises(EiaMetadataError):
        assemble_pages([(0, page)], expected)


# --- completeness: truncation, paging, empty ---------------------------------------------------


def _one_series_view(page: EiaDataPage) -> tuple[EiaDataPage, EiaSeriesExpectation]:
    """The over-limit fixture spans 26 series; keep one so the metadata check passes."""
    first = page.rows[0]
    view = replace(page, rows=tuple(r for r in page.rows if r.series == first.series))
    expected = EiaSeriesExpectation(
        "overlimit",
        "petroleum/sum/sndw",
        first.series,
        "weekly",
        "YYYY-MM-DD",
        first.description,
        first.units,
        "week_ending_friday",
    )
    return view, expected


def test_real_overlimit_response_fails_on_its_length_warning() -> None:
    """The real length=6000 response carries 'parameter out of range: length': a failure."""
    view, expected = _one_series_view(parse_data_page(eia_fixture("native_overlimit_length_6000")))
    with pytest.raises(EiaWarningError, match="parameter out of range: length"):
        assemble_pages([(0, view)], expected)


def test_truncation_is_caught_by_the_total_even_without_warnings() -> None:
    """Remove the warnings (as the series-ID route sends none): the shortfall against the
    reported total is still a controlled failure."""
    view, expected = _one_series_view(parse_data_page(eia_fixture("native_overlimit_length_6000")))
    silent = replace(view, warnings=())
    with pytest.raises(EiaIncompleteError, match=rf"received {len(silent.rows)} of 65642"):
        assemble_pages([(0, silent)], expected)


def test_real_paging_notice_is_tolerated_but_partial_paging_still_fails() -> None:
    """The two real storage pages carry 'incomplete return' (tolerated: more rows remain), and
    hold 6 of 878 rows: stopping there is an incomplete-pagination failure."""
    pages = [
        (0, parse_data_page(eia_fixture("native_storage_page1"))),
        (3, parse_data_page(eia_fixture("native_storage_page2"))),
    ]
    with pytest.raises(EiaIncompleteError, match="received 6 of 878 rows"):
        assemble_pages(pages, expect("l48_gas_storage"))


def test_real_empty_response_for_a_catalogued_series_is_a_failure() -> None:
    page = parse_data_page(eia_fixture("native_unknown_facet_empty"))
    with pytest.raises(EiaEmptyResponseError, match="no data"):
        assemble_pages([(0, page)], expect("us_crude_stocks_ex_spr"))
    assert assemble_pages([(0, page)], expect("us_crude_stocks_ex_spr"), allow_empty=True) == []


# SYNTHETIC multi-page scenarios (structure of the native route, values not real)
SYNTH = EiaSeriesExpectation(
    "synth",
    "synthetic/route",
    "SYNTH1",
    "weekly",
    "YYYY-MM-DD",
    "SYNTHETIC test series (Thousand Barrels)",
    "MBBL",
    "week_ending_friday",
)
FRIDAYS = ["2026-01-02", "2026-01-09", "2026-01-16", "2026-01-23", "2026-01-30"]


def page(
    periods: list[str], total: int, offset: int, warnings: list[dict[str, str]] | None = None
) -> EiaDataPage:
    return parse_data_page(
        synthetic_page(
            [synthetic_row(p, str(i)) for i, p in enumerate(periods)],
            total=str(total),
            offset=offset,
            warnings=warnings,
        )
    )


def test_complete_multi_page_assembly() -> None:
    rows = assemble_pages([(0, page(FRIDAYS[:3], 5, 0)), (3, page(FRIDAYS[3:], 5, 3))], SYNTH)
    assert [r.period for r in rows] == FRIDAYS


def test_pagination_notice_on_non_final_pages_is_tolerated_like_the_live_api() -> None:
    """SYNTHETIC, with EIA's verbatim notice: every page except the last carries it."""
    rows = assemble_pages(
        [
            (0, page(FRIDAYS[:2], 5, 0, [INCOMPLETE_RETURN])),
            (2, page(FRIDAYS[2:4], 5, 2, [INCOMPLETE_RETURN])),
            (4, page(FRIDAYS[4:], 5, 4)),
        ],
        SYNTH,
    )
    assert [r.period for r in rows] == FRIDAYS


def test_pagination_notice_on_the_final_page_is_a_failure() -> None:
    with pytest.raises(EiaWarningError, match="'incomplete return' on the final page"):
        assemble_pages([(0, page(FRIDAYS, 5, 0, [INCOMPLETE_RETURN]))], SYNTH)


@pytest.mark.parametrize(
    "warning",
    [
        {"warning": "parameter out of range: length", "description": "max 5000"},
        {"warning": "something new", "description": "unknown"},
    ],
)
def test_any_other_warning_is_a_failure_even_on_non_final_pages(warning: dict[str, str]) -> None:
    with pytest.raises(EiaWarningError, match=warning["warning"]):
        assemble_pages(
            [
                (0, page(FRIDAYS[:3], 5, 0, [INCOMPLETE_RETURN, warning])),
                (3, page(FRIDAYS[3:], 5, 3)),
            ],
            SYNTH,
        )


@pytest.mark.parametrize(
    ("pages", "match"),
    [
        ([(0, page(FRIDAYS[:3], 5, 0)), (3, page(FRIDAYS[3:], 6, 3))], "total changed"),
        ([(0, page(FRIDAYS[:3], 5, 0)), (2, page(FRIDAYS[3:], 5, 2))], "requested at offset 2"),
        ([(0, page(FRIDAYS[:3], 5, 0)), (3, page(FRIDAYS[3:], 5, 4))], "executed offset 4"),
        ([(0, page(FRIDAYS[:3], 5, 0)), (3, page([], 5, 3))], "received 3 of 5"),
        ([(0, page(FRIDAYS[:3] + FRIDAYS[2:4], 5, 0))], "duplicate periods"),
        ([], "no pages"),
    ],
)
def test_incomplete_or_inconsistent_paging_fails(
    pages: list[tuple[int, EiaDataPage]], match: str
) -> None:
    with pytest.raises(EiaIncompleteError, match=match):
        assemble_pages(pages, SYNTH)


def test_descending_order_is_rejected() -> None:
    with pytest.raises(EiaMetadataError, match="ascending"):
        assemble_pages([(0, page(list(reversed(FRIDAYS)), 5, 0))], SYNTH)


# --- conversion keeps the source period and exact values --------------------------------------


def test_observations_from_real_fixture_keep_source_periods_and_exact_values() -> None:
    rows = assemble_pages(
        [(0, parse_data_page(eia_fixture("native_wpuleus3_2026_jan_feb")))],
        expect("us_refinery_utilization"),
    )
    obs = to_observations(rows, expect("us_refinery_utilization"))
    first = obs[0]
    assert first.source_period == "2026-01-02"
    assert (first.period_start, first.period_end) == (date(2025, 12, 27), date(2026, 1, 2))
    assert str(first.value) == "94.7" and first.status == "ok"


def test_monthly_observations_from_real_fixture() -> None:
    rows = assemble_pages(
        [(0, parse_data_page(eia_fixture("native_n9133us2_2025")))], expect("us_lng_exports")
    )
    obs = to_observations(rows, expect("us_lng_exports"))
    assert (obs[1].source_period, obs[1].period_start, obs[1].period_end) == (
        "2025-02",
        date(2025, 2, 1),
        date(2025, 2, 28),
    )


def test_synthetic_null_becomes_missing_at_source() -> None:
    """SYNTHETIC null row (mirrors Henry Hub's 2018-01-05 null; real data not committed)."""
    rows = assemble_pages(
        [
            (
                0,
                parse_data_page(
                    synthetic_page(
                        [synthetic_row("2026-01-02", None), synthetic_row("2026-01-09", "2.5")]
                    )
                ),
            )
        ],
        SYNTH,
    )
    obs = to_observations(rows, SYNTH)
    assert (obs[0].value, obs[0].status) == (None, "missing_at_source")
    assert (obs[1].value, obs[1].status) == (Decimal("2.5"), "ok")


def test_misaligned_weekly_period_fails_conversion() -> None:
    from marketpulse.sources.eia.periods import PeriodError

    rows = assemble_pages([(0, page(["2026-01-03"], 1, 0))], SYNTH)  # a Saturday
    with pytest.raises(PeriodError):
        to_observations(rows, SYNTH)
