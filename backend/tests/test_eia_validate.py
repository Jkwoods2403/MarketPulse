"""EIA controlled failures (drift, warnings, truncation, incomplete paging, empty data) and
conversion."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from eia_mock import INCOMPLETE_RETURN, eia_fixture, synthetic_page, synthetic_row
from marketpulse.sources.catalog import load_catalog
from marketpulse.sources.catalog.schema import eia_facet_series
from marketpulse.sources.eia import MAX_ROWS_PER_REQUEST
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
    expected_page_rows,
    pagination_notice_expected,
)

SPECS = {s.code: s for s in load_catalog().series_for("eia")}
FULL = MAX_ROWS_PER_REQUEST  # what ingestion requests (and what unpaged fixtures answered)


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
        assemble_pages([(0, page)], expected, page_size=FULL)


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
        assemble_pages([(0, view)], expected, page_size=FULL)


def test_truncation_is_caught_by_the_total_even_without_warnings() -> None:
    """Remove the warnings (as the series-ID route sends none): the shortfall against the
    reported total is still a controlled failure."""
    view, expected = _one_series_view(parse_data_page(eia_fixture("native_overlimit_length_6000")))
    silent = replace(view, warnings=())
    with pytest.raises(EiaIncompleteError, match=rf"received {len(silent.rows)} of 65642"):
        assemble_pages([(0, silent)], expected, page_size=FULL)


def test_real_paging_notice_is_tolerated_but_partial_paging_still_fails() -> None:
    """The two real storage pages (length 3) carry 'incomplete return' (tolerated: length 3 <
    total 878), and hold 6 of 878 rows: stopping there is an incomplete-pagination failure."""
    pages = [
        (0, parse_data_page(eia_fixture("native_storage_page1"))),
        (3, parse_data_page(eia_fixture("native_storage_page2"))),
    ]
    with pytest.raises(EiaIncompleteError, match="received 6 of 878 rows"):
        assemble_pages(pages, expect("l48_gas_storage"), page_size=3)


def test_real_empty_response_for_a_catalogued_series_is_a_failure() -> None:
    page = parse_data_page(eia_fixture("native_unknown_facet_empty"))
    with pytest.raises(EiaEmptyResponseError, match="no data"):
        assemble_pages([(0, page)], expect("us_crude_stocks_ex_spr"), page_size=FULL)
    assert (
        assemble_pages(
            [(0, page)], expect("us_crude_stocks_ex_spr"), page_size=FULL, allow_empty=True
        )
        == []
    )


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
    periods: list[str],
    total: int,
    offset: int,
    warnings: list[dict[str, str]] | None = None,
    *,
    length: int,
) -> EiaDataPage:
    """A SYNTHETIC page as EIA would answer (offset, length): the echo shows both."""
    return parse_data_page(
        synthetic_page(
            [synthetic_row(p, str(i)) for i, p in enumerate(periods)],
            total=str(total),
            offset=offset,
            length=length,
            warnings=warnings,
        )
    )


def fridays(n: int) -> list[str]:
    start = date(1900, 1, 5)  # a Friday
    return [date.fromordinal(start.toordinal() + 7 * i).isoformat() for i in range(n)]


N = [INCOMPLETE_RETURN]  # EIA's verbatim pagination notice
SIX = [*FRIDAYS, "2026-02-06"]  # six SYNTHETIC weekly periods


def incomplete(pages: list[tuple[int, EiaDataPage]], page_size: int) -> list[str]:
    """Every problem an incomplete request reports, in order."""
    with pytest.raises(EiaIncompleteError) as info:
        assemble_pages(pages, SYNTH, page_size=page_size)
    return info.value.problems


def row_count(offset: int, rows: int, want: int, length: int, total: int) -> str:
    return f"synth: page at offset {offset} holds {rows} rows, expected {want} " + (
        f"(length {length}, total {total})"
    )


def received(got: int, total: int) -> str:
    return f"synth: received {got} of {total} rows (truncated or incomplete pagination)"


# --- the rule itself, at its boundaries ----------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "length", "expected"),
    [
        (5, 2, True),  # length below the total
        (5, 4, True),
        (2, 1, True),  # the probe's one-row request on a two-row series
        (6200, 5000, True),  # every page of a series over 5,000 rows
        (5, 5, False),  # length equal to the total (observed live: request C)
        (1, 1, False),
        (5, 6, False),  # length above the total (observed live: requests A and B)
        (5, 5000, False),
        (0, 1, False),  # zero total
        (0, 5000, False),
    ],
)
def test_pagination_notice_expected_at_its_boundaries(
    total: int, length: int, expected: bool
) -> None:
    assert pagination_notice_expected(total, length=length) is expected


@pytest.mark.parametrize("length", [0, -1, -5000])
def test_pagination_notice_expected_refuses_an_impossible_length(length: int) -> None:
    with pytest.raises(ValueError, match="request length must be at least 1"):
        pagination_notice_expected(5, length=length)


def test_pagination_notice_expected_requires_the_length() -> None:
    with pytest.raises(TypeError):
        pagination_notice_expected(5)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("total", "offset", "length", "expected"),
    [
        (5, 0, 3, 3),  # first page
        (9, 3, 3, 3),  # middle page
        (5, 3, 3, 2),  # final page: the rows that remain
        (5, 4, 1, 1),  # the probe's last-row request
        (6, 3, 3, 3),  # final page exactly full
        (6200, 5000, 5000, 1200),  # final page of a series over 5,000 rows
        (5, 5, 3, 0),  # offset at the total
        (5, 6, 3, 0),  # offset past the total
        (0, 0, 5000, 0),  # zero total
    ],
)
def test_expected_page_rows(total: int, offset: int, length: int, expected: int) -> None:
    assert expected_page_rows(total, offset=offset, length=length) == expected


# --- complete requests are accepted --------------------------------------------------------------


def test_complete_multi_page_assembly() -> None:
    rows = assemble_pages(
        [(0, page(FRIDAYS[:3], 5, 0, length=3)), (3, page(FRIDAYS[3:], 5, 3, length=3))],
        SYNTH,
        page_size=3,
    )
    assert [r.period for r in rows] == FRIDAYS


def test_a_full_response_without_a_warning_is_accepted() -> None:
    """length 5000 >= total 5: every row in one page, and no notice (observed live, A and C)."""
    rows = assemble_pages([(0, page(FRIDAYS, 5, 0, length=FULL))], SYNTH, page_size=FULL)
    assert [r.period for r in rows] == FRIDAYS


def test_the_notice_on_every_page_including_the_final_one_is_tolerated() -> None:
    """SYNTHETIC, with EIA's verbatim notice on EVERY page because length 2 < total 5 (observed
    live: the notice came with the final row too). The final page holds every remaining row."""
    rows = assemble_pages(
        [
            (0, page(FRIDAYS[:2], 5, 0, N, length=2)),
            (2, page(FRIDAYS[2:4], 5, 2, N, length=2)),
            (4, page(FRIDAYS[4:], 5, 4, N, length=2)),
        ],
        SYNTH,
        page_size=2,
    )
    assert [r.period for r in rows] == FRIDAYS


def test_one_row_pages_each_carrying_the_notice_are_all_accepted() -> None:
    """The probe's request shape (length 1 < total 5) for every row: five one-row pages, each
    with EIA's notice, first and last included. All five rows come back, in order."""
    pages = [(i, page([p], 5, i, N, length=1)) for i, p in enumerate(FRIDAYS)]
    rows = assemble_pages(pages, SYNTH, page_size=1)
    assert [r.period for r in rows] == FRIDAYS


def test_total_equal_to_the_page_size_is_one_page_without_a_notice() -> None:
    rows = assemble_pages([(0, page(FRIDAYS[:3], 3, 0, length=3))], SYNTH, page_size=3)
    assert [r.period for r in rows] == FRIDAYS[:3]


def test_total_one_above_the_page_size_ends_with_a_one_row_page() -> None:
    rows = assemble_pages(
        [(0, page(FRIDAYS[:3], 4, 0, N, length=3)), (3, page(FRIDAYS[3:4], 4, 3, N, length=3))],
        SYNTH,
        page_size=3,
    )
    assert [r.period for r in rows] == FRIDAYS[:4]


def test_more_than_5000_rows_with_the_notice_on_every_page_is_complete() -> None:
    """SYNTHETIC 6,200 rows: two pages of length 5000, both carrying the notice (5000 < 6200),
    including the final page of 1,200 rows; every row arrives exactly once."""
    periods = fridays(6200)
    rows = assemble_pages(
        [
            (0, page(periods[:5000], 6200, 0, N, length=FULL)),
            (5000, page(periods[5000:], 6200, 5000, N, length=FULL)),
        ],
        SYNTH,
        page_size=FULL,
    )
    assert [r.period for r in rows] == periods


# --- the notice where the request does not explain it, and other warnings -------------------


@pytest.mark.parametrize(
    ("periods", "total", "length"),
    [
        (FRIDAYS, 5, FULL),  # length above the total
        (FRIDAYS[:3], 3, 3),  # length equal to the total
    ],
)
def test_the_notice_when_the_length_covers_the_total_is_a_failure(
    periods: list[str], total: int, length: int
) -> None:
    """EIA does not send the notice there (observed live), so it is an anomaly even though the
    page is complete."""
    with pytest.raises(EiaWarningError) as info:
        assemble_pages([(0, page(periods, total, 0, N, length=length))], SYNTH, page_size=length)
    assert info.value.problems == [
        f"synth: EIA warning 'incomplete return' at offset 0 (length {length}, total {total}, "
        f"{len(periods)} rows): {INCOMPLETE_RETURN['description']}"
    ]


@pytest.mark.parametrize(
    "warning", [INCOMPLETE_RETURN, {"warning": "something new", "description": "unknown"}]
)
@pytest.mark.parametrize("allow_empty", [False, True])
def test_a_zero_total_response_with_a_warning_is_a_failure(
    warning: dict[str, str], allow_empty: bool
) -> None:
    """Nothing explains a warning on an empty response, not even when empty is allowed."""
    with pytest.raises(EiaWarningError, match=warning["warning"]):
        assemble_pages(
            [(0, page([], 0, 0, [warning], length=FULL))],
            SYNTH,
            page_size=FULL,
            allow_empty=allow_empty,
        )


@pytest.mark.parametrize(
    "warning",
    [
        {"warning": "parameter out of range: length", "description": "max 5000"},
        {"warning": "something new", "description": "unknown"},
    ],
)
@pytest.mark.parametrize("on_final_page", [False, True])
def test_any_other_warning_is_a_failure_on_any_page(
    warning: dict[str, str], on_final_page: bool
) -> None:
    first, final = (N, [*N, warning]) if on_final_page else ([*N, warning], N)
    with pytest.raises(EiaWarningError, match=warning["warning"]):
        assemble_pages(
            [
                (0, page(FRIDAYS[:3], 5, 0, first, length=3)),
                (3, page(FRIDAYS[3:], 5, 3, final, length=3)),
            ],
            SYNTH,
            page_size=3,
        )


# --- incomplete or inconsistent requests fail (exact problem lists) -------------------------


def test_a_short_page_fails_even_if_the_remaining_rows_follow() -> None:
    """Approved policy: a page with fewer rows than its request should hold is incomplete,
    whatever follows it, even though the rows that follow make the count add up."""
    assert incomplete(
        [
            (0, page(FRIDAYS[:1], 5, 0, N, length=2)),
            (1, page(FRIDAYS[1:3], 5, 1, N, length=2)),
            (3, page(FRIDAYS[3:], 5, 3, N, length=2)),
        ],
        page_size=2,
    ) == [row_count(0, 1, 2, 2, 5)]


def test_a_short_final_page_with_the_notice_is_truncation() -> None:
    assert incomplete(
        [
            (0, page(FRIDAYS[:2], 5, 0, N, length=2)),
            (2, page(FRIDAYS[2:4], 5, 2, N, length=2)),
            (4, page([], 5, 4, N, length=2)),
        ],
        page_size=2,
    ) == [row_count(4, 0, 1, 2, 5), received(4, 5)]


def test_more_than_5000_rows_with_a_truncated_final_page_fails() -> None:
    periods = fridays(6200)
    assert incomplete(
        [
            (0, page(periods[:5000], 6200, 0, N, length=FULL)),
            (5000, page(periods[5000:-1], 6200, 5000, N, length=FULL)),
        ],
        page_size=FULL,
    ) == [row_count(5000, 1199, 1200, FULL, 6200), received(6199, 6200)]


def test_an_understated_total_fails() -> None:
    """EIA says 4 rows but sends 5: the final page holds more than the total leaves room for."""
    assert incomplete(
        [(0, page(FRIDAYS[:3], 4, 0, N, length=3)), (3, page(FRIDAYS[3:], 4, 3, N, length=3))],
        page_size=3,
    ) == [row_count(3, 2, 1, 3, 4), received(5, 4)]


def test_an_overstated_total_fails() -> None:
    """EIA says 6 rows but sends 5: the final page is one row short."""
    assert incomplete(
        [(0, page(FRIDAYS[:3], 6, 0, N, length=3)), (3, page(FRIDAYS[3:], 6, 3, N, length=3))],
        page_size=3,
    ) == [row_count(3, 2, 3, 3, 6), received(5, 6)]


def test_an_overfull_final_page_fails() -> None:
    """Total 5 and pages of 3: the final page must hold 2 rows, not 3."""
    assert incomplete(
        [(0, page(SIX[:3], 5, 0, N, length=3)), (3, page(SIX[3:], 5, 3, N, length=3))],
        page_size=3,
    ) == [row_count(3, 3, 2, 3, 5), received(6, 5)]


@pytest.mark.parametrize(
    ("pages", "problems"),
    [
        (  # the total changes between pages (the second page is then also short for it)
            [(0, page(FRIDAYS[:3], 5, 0, length=3)), (3, page(FRIDAYS[3:], 6, 3, length=3))],
            ["synth: total changed between pages: [5, 6]", row_count(3, 2, 3, 3, 6)],
        ),
        (  # a page requested past the rows received: a row is skipped
            [(0, page(FRIDAYS[:3], 5, 0, length=3)), (4, page(FRIDAYS[4:], 5, 4, length=3))],
            ["synth: page requested at offset 4, but 3 rows had been received", received(4, 5)],
        ),
        (  # EIA executed another offset than requested
            [(0, page(FRIDAYS[:3], 5, 0, length=3)), (3, page(FRIDAYS[3:], 5, 4, length=3))],
            ["synth: EIA executed offset 4, requested 3"],
        ),
        (  # EIA executed another length than requested
            [(0, page(FRIDAYS[:3], 5, 0, length=3)), (3, page(FRIDAYS[3:], 5, 3, length=FULL))],
            ["synth: EIA executed length 5000, requested 3"],
        ),
        (  # an empty final page
            [(0, page(FRIDAYS[:3], 5, 0, length=3)), (3, page([], 5, 3, length=3))],
            [row_count(3, 0, 2, 3, 5), received(3, 5)],
        ),
        (  # a period sent twice, with the count still adding up
            [(0, page(FRIDAYS[:3], 5, 0, length=3)), (3, page(FRIDAYS[2:4], 5, 3, length=3))],
            ["synth: duplicate periods ['2026-01-16']"],
        ),
        ([], ["synth: no pages received"]),
    ],
)
def test_incomplete_or_inconsistent_paging_fails(
    pages: list[tuple[int, EiaDataPage]], problems: list[str]
) -> None:
    assert incomplete(pages, page_size=3) == problems


def test_page_size_must_be_a_valid_request_length() -> None:
    for bad in (0, 5001):
        with pytest.raises(ValueError, match="page_size"):
            assemble_pages([(0, page(FRIDAYS, 5, 0, length=FULL))], SYNTH, page_size=bad)


def test_page_size_is_required() -> None:
    with pytest.raises(TypeError):
        assemble_pages([(0, page(FRIDAYS, 5, 0, length=FULL))], SYNTH)  # type: ignore[call-arg]


def test_descending_order_is_rejected() -> None:
    with pytest.raises(EiaMetadataError, match="ascending"):
        assemble_pages(
            [(0, page(list(reversed(FRIDAYS)), 5, 0, length=FULL))], SYNTH, page_size=FULL
        )


# --- conversion keeps the source period and exact values --------------------------------------


def test_observations_from_real_fixture_keep_source_periods_and_exact_values() -> None:
    rows = assemble_pages(
        [(0, parse_data_page(eia_fixture("native_wpuleus3_2026_jan_feb")))],
        expect("us_refinery_utilization"),
        page_size=FULL,
    )
    obs = to_observations(rows, expect("us_refinery_utilization"))
    first = obs[0]
    assert first.source_period == "2026-01-02"
    assert (first.period_start, first.period_end) == (date(2025, 12, 27), date(2026, 1, 2))
    assert str(first.value) == "94.7" and first.status == "ok"


def test_monthly_observations_from_real_fixture() -> None:
    rows = assemble_pages(
        [(0, parse_data_page(eia_fixture("native_n9133us2_2025")))],
        expect("us_lng_exports"),
        page_size=FULL,
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
        page_size=FULL,
    )
    obs = to_observations(rows, SYNTH)
    assert (obs[0].value, obs[0].status) == (None, "missing_at_source")
    assert (obs[1].value, obs[1].status) == (Decimal("2.5"), "ok")


def test_misaligned_weekly_period_fails_conversion() -> None:
    from marketpulse.sources.eia.periods import PeriodError

    rows = assemble_pages(  # a Saturday
        [(0, page(["2026-01-03"], 1, 0, length=FULL))], SYNTH, page_size=FULL
    )
    with pytest.raises(PeriodError):
        to_observations(rows, SYNTH)
