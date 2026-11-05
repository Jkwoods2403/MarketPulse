"""Controlled failures for EIA data: metadata drift, warnings, incomplete pages, empty responses.

Each check raises a specific error with every problem listed; nothing is skipped or repaired.

- EiaMetadataError: the response's frequency, date format, series, description, or units differ
  from the verified catalog entry (drift), or the data is not in ascending period order.
- EiaWarningError: EIA reported a warning. The only tolerated warning is EIA's pagination
  notice ("incomplete return"), and only where the request explains it (see
  `pagination_notice_expected`). Any other warning (e.g. "parameter out of range: length"), the
  notice where the request does not explain it, or an unrecognized warning is a failure.
- EiaIncompleteError: rows received != total reported, a page holds other than
  min(length, total - offset) rows, pages are not contiguous, the executed offset or length
  differs from the request, totals change between pages, or a period appears twice.
  Completeness is always checked against `total` and row counts, never inferred from warnings
  (the series-ID route truncates without any warning at all).
- EiaEmptyResponseError: a catalogued series returned no data (EIA answers HTTP 200 with total
  "0" for unknown series on native routes).

The pagination notice is not evidence of truncation. Observed live (the 2026-11-01 fixtures; the
nine-series probe and a three-request check of us_lng_exports on 2026-11-05), EIA attaches it
whenever the requested `length` is smaller than the reported `total`, even on a page that holds
every remaining row (offset total - 1, length 1, one row returned: notice present), and omits it
when `length` is at least the total, whatever the offset (offset 0 or total - 1 with length
5000, and offset 0 with length == total: no notice). This is the rule that fits every
observation so far, on the routes observed; it is not documented by EIA or guaranteed for every
route. Completeness never relies on it, and a notice the request does not explain is still a
failure.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from marketpulse.sources.catalog.schema import SeriesSpec, eia_facet_series
from marketpulse.sources.eia import MAX_ROWS_PER_REQUEST
from marketpulse.sources.eia.parse import EiaDataPage, EiaRow
from marketpulse.sources.eia.periods import DATE_FORMATS


class EiaDataError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        shown = "\n".join(f"  - {p}" for p in problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        super().__init__(f"{type(self).__name__}: {len(problems)} problem(s):\n{shown}{more}")


class EiaMetadataError(EiaDataError):
    """The response does not match the verified catalog metadata."""


class EiaWarningError(EiaDataError):
    """EIA reported a warning that is not an expected pagination notice."""


# EIA's pagination notice (observed live; see the module docstring for when it appears).
PAGINATION_NOTICE = "incomplete return"


class EiaIncompleteError(EiaDataError):
    """Pagination is incomplete or the data were truncated."""


class EiaEmptyResponseError(EiaDataError):
    """A catalogued series returned no data."""


@dataclass(frozen=True)
class EiaSeriesExpectation:
    """What a response for one catalogued series must look like."""

    code: str
    route: str
    facet_series: str
    frequency: str
    date_format: str
    description: str
    units: str
    period_convention: str


def expectation_for(spec: SeriesSpec) -> EiaSeriesExpectation:
    if spec.api_route is None or spec.api_description is None or spec.api_units is None:
        raise ValueError(f"{spec.code}: catalog entry lacks verified EIA API metadata")
    frequency = spec.frequency.value
    if frequency not in DATE_FORMATS:
        raise ValueError(f"{spec.code}: EIA frequency {frequency!r} is not supported yet")
    return EiaSeriesExpectation(
        code=spec.code,
        route=spec.api_route,
        facet_series=eia_facet_series(spec.source_series_id),
        frequency=frequency,
        date_format=DATE_FORMATS[frequency],
        description=spec.api_description,
        units=spec.api_units,
        period_convention=spec.period_convention,
    )


def check_page_metadata(page: EiaDataPage, expected: EiaSeriesExpectation) -> list[str]:
    """Drift problems in one page (empty list when everything matches the catalog)."""
    problems: list[str] = []
    where = expected.code
    if page.frequency != expected.frequency:
        problems.append(f"{where}: frequency {page.frequency!r}, expected {expected.frequency!r}")
    if page.date_format != expected.date_format:
        problems.append(
            f"{where}: date format {page.date_format!r}, expected {expected.date_format!r}"
        )
    for label, attr, want in (
        ("series", "series", expected.facet_series),
        ("description", "description", expected.description),
        ("units", "units", expected.units),
    ):
        seen = sorted({getattr(row, attr) for row in page.rows})
        unexpected = [value for value in seen if value != want]
        if unexpected:
            problems.append(f"{where}: {label} {unexpected!r}, expected {want!r}")
    return problems


def expected_page_rows(total: int, *, offset: int, length: int) -> int:
    """How many rows a complete response to (offset, length) holds when `total` rows match."""
    return max(0, min(length, total - offset))


def pagination_notice_expected(total: int, *, length: int) -> bool:
    """Whether the request explains EIA's "incomplete return" notice: the requested `length` is
    below the reported total (when EIA attaches it, observed live).

    This only decides that the notice is not an anomaly. A page carrying it is accepted only if
    it also holds exactly `expected_page_rows` rows and passes every other check, so a short
    (genuinely incomplete) page still fails, as incomplete. A request length below 1 is not a
    request EIA could have answered, so it is refused rather than evaluated.
    """
    if length < 1:
        raise ValueError(f"request length must be at least 1, got {length}")
    return length < total


def assemble_pages(
    pages: Sequence[tuple[int, EiaDataPage]],
    expected: EiaSeriesExpectation,
    *,
    page_size: int,
    allow_empty: bool = False,
) -> list[EiaRow]:
    """Validate the pages of one request (each with the offset it was requested at, all with
    the requested `page_size` as their length) and return all rows in ascending period order.
    Raises instead of returning partial data."""
    if not 1 <= page_size <= MAX_ROWS_PER_REQUEST:
        raise ValueError(f"page_size must be between 1 and {MAX_ROWS_PER_REQUEST}")
    if not pages:
        raise EiaIncompleteError([f"{expected.code}: no pages received"])

    metadata = [p for _, page in pages for p in check_page_metadata(page, expected)]
    if metadata:
        raise EiaMetadataError(metadata)

    warning_problems: list[str] = []
    for requested_offset, page in pages:
        explained = pagination_notice_expected(page.total, length=page_size)
        for w in page.warnings:
            if w.warning == PAGINATION_NOTICE and explained:
                continue  # length < total; each page's row count is checked below
            where = f"offset {requested_offset} (length {page_size}, total {page.total}, "
            where += f"{len(page.rows)} rows)"
            warning_problems.append(
                f"{expected.code}: EIA warning {w.warning!r} at {where}"
                + (f": {w.description}" if w.description else "")
            )
    if warning_problems:
        raise EiaWarningError(warning_problems)

    problems: list[str] = []
    totals = {page.total for _, page in pages}
    if len(totals) > 1:
        problems.append(f"{expected.code}: total changed between pages: {sorted(totals)}")
    received = 0
    for requested_offset, page in pages:
        if requested_offset != received:
            problems.append(
                f"{expected.code}: page requested at offset {requested_offset}, "
                f"but {received} rows had been received"
            )
        echoed = page.echo.get("offset")
        if echoed is not None and int(echoed) != requested_offset:
            problems.append(
                f"{expected.code}: EIA executed offset {echoed}, requested {requested_offset}"
            )
        echoed_length = page.echo.get("length")
        if echoed_length is not None and str(echoed_length) != str(page_size):
            problems.append(
                f"{expected.code}: EIA executed length {echoed_length}, requested {page_size}"
            )
        if len(page.rows) > MAX_ROWS_PER_REQUEST:
            problems.append(
                f"{expected.code}: page has {len(page.rows)} rows, above the "
                f"{MAX_ROWS_PER_REQUEST}-row limit"
            )
        want = expected_page_rows(page.total, offset=requested_offset, length=page_size)
        if len(page.rows) != want:  # a short (or overfull) page is incomplete, notice or not
            problems.append(
                f"{expected.code}: page at offset {requested_offset} holds {len(page.rows)} "
                f"rows, expected {want} (length {page_size}, total {page.total})"
            )
        received += len(page.rows)

    total = pages[0][1].total
    rows = [row for _, page in pages for row in page.rows]
    if total == 0 and not problems:
        if allow_empty:
            return []
        raise EiaEmptyResponseError([f"{expected.code}: EIA returned no data (total 0)"])
    if received != total:
        problems.append(
            f"{expected.code}: received {received} of {total} rows (truncated or "
            "incomplete pagination)"
        )
    periods = [row.period for row in rows]
    duplicates = sorted(p for p, n in Counter(periods).items() if n > 1)
    if duplicates:
        problems.append(f"{expected.code}: duplicate periods {duplicates[:5]}")
    if problems:
        raise EiaIncompleteError(problems)
    if periods != sorted(periods):
        raise EiaMetadataError(
            [
                f"{expected.code}: rows are not in ascending period order "
                "(the request must sort by period ascending)"
            ]
        )
    return rows
