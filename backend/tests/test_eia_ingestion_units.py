"""EIA job pieces that need no database: page-to-row provenance mapping, exact request checks,
failure diagnostics, windows, boundary rows, and exit codes. All values are SYNTHETIC; page URLs
come from real client requests against the simulator, so they have the exact redacted form that
is stored."""

import dataclasses
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from sqlalchemy.exc import OperationalError

from marketpulse.ingestion.eia import (
    DATA_ERROR_CHECKS,
    REQUEST_REJECTED,
    UNFINALIZED,
    EiaIngestionSummary,
    EiaProvenanceError,
    RecordedPage,
    SeriesOutcome,
    choose_window,
    drop_before,
    failure_diagnostics,
    map_page_provenance,
    window_start,
)
from marketpulse.ingestion.snapshot_writer import ProvenanceError, SnapshotTimeError
from marketpulse.ingestion.snapshots import SnapshotObservation, SnapshotScope
from marketpulse.sources.eia.client import EiaAPIError, EiaFetchedPage
from marketpulse.sources.eia.parse import EiaResponseError
from marketpulse.sources.eia.periods import PeriodError
from marketpulse.sources.eia.validate import (
    EiaDataError,
    EiaEmptyResponseError,
    EiaIncompleteError,
    EiaMetadataError,
    EiaSeriesExpectation,
    EiaWarningError,
    expectation_for,
)
from marketpulse.sources.http import (
    ProviderHTTPError,
    ProviderTransportError,
    ResponseTooLargeError,
)
from test_eia_client import SPECS, STORAGE, Harness

CRUDE_EX_SPR = expectation_for(SPECS["us_crude_stocks_ex_spr"])  # another weekly series
PAGE_SIZE = 4
WINDOW_START = "2010-01-15"  # an EIA `start` within the synthetic storage periods


def storage_pages(
    n: int = 10, page_size: int = PAGE_SIZE, start: str | None = None
) -> list[EiaFetchedPage]:
    h = Harness()
    h.add_storage(n)
    return h.client.fetch_series_pages(STORAGE, start=start, page_size=page_size)


def map_pages(
    pages: list[EiaFetchedPage],
    lookup: dict[tuple[str, str], RecordedPage],
    expected: EiaSeriesExpectation,
    *,
    start: str | None = None,
    page_size: int = PAGE_SIZE,
) -> list[SnapshotObservation]:
    return map_page_provenance(pages, lookup, expected, start=start, page_size=page_size)


def with_params(page: EiaFetchedPage, changes: dict[str, str | None]) -> EiaFetchedPage:
    """The page as if its request had different query parameters (None removes one)."""
    parts = urlsplit(page.url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    for name, value in changes.items():
        if value is None:
            params.pop(name, None)
        else:
            params[name] = value
    return dataclasses.replace(page, url=urlunsplit(parts._replace(query=urlencode(params))))


def with_path(page: EiaFetchedPage, path: str) -> EiaFetchedPage:
    return dataclasses.replace(page, url=urlunsplit(urlsplit(page.url)._replace(path=path)))


def recorded(
    pages: list[EiaFetchedPage], first_id: int = 100
) -> dict[tuple[str, str], RecordedPage]:
    return {
        (p.url, p.sha256): RecordedPage(first_id + i, p.sha256, p.retrieved_at, p.url)
        for i, p in enumerate(pages)
    }


# --- provenance mapping -------------------------------------------------------------------------


def test_every_row_maps_to_the_raw_payload_of_its_own_page() -> None:
    pages = storage_pages()
    observations = map_pages(pages, recorded(pages), STORAGE)
    assert [o.raw_payload_id for o in observations] == [100] * 4 + [101] * 4 + [102] * 2
    assert [o.source_period for o in observations] == [r.period for p in pages for r in p.rows]
    first = observations[0]
    assert first.period_end - first.period_start == timedelta(days=6)  # week ending Friday
    assert first.value == Decimal("3000") and first.status == "ok"


def test_page_without_a_recorded_payload_is_refused() -> None:
    pages = storage_pages()
    lookup = recorded(pages)
    del lookup[(pages[1].url, pages[1].sha256)]
    with pytest.raises(EiaProvenanceError, match="offset 4 has no recorded raw payload"):
        map_pages(pages, lookup, STORAGE)


def test_page_whose_bytes_differ_from_the_recorded_payload_is_refused() -> None:
    pages = storage_pages()
    lookup = recorded(pages)
    tampered = [pages[0], dataclasses.replace(pages[1], sha256="0" * 64), pages[2]]
    with pytest.raises(EiaProvenanceError, match="no recorded raw payload"):
        map_pages(tampered, lookup, STORAGE)


def test_page_requested_for_another_series_is_refused() -> None:
    pages = storage_pages()
    with pytest.raises(EiaProvenanceError, match="not requested for this series"):
        map_pages(pages, recorded(pages), CRUDE_EX_SPR)


def test_page_offset_must_match_its_request() -> None:
    pages = storage_pages()
    moved = dataclasses.replace(pages[1], offset=5)
    with pytest.raises(EiaProvenanceError, match="offset"):
        map_pages([pages[0], moved, pages[2]], recorded([pages[0], moved, pages[2]]), STORAGE)


def test_one_raw_payload_cannot_cover_two_pages() -> None:
    pages = storage_pages()
    lookup = recorded(pages)
    lookup[(pages[1].url, pages[1].sha256)] = lookup[(pages[0].url, pages[0].sha256)]
    with pytest.raises(EiaProvenanceError):
        map_pages(pages, lookup, STORAGE)


def test_retrieval_time_must_match_the_recorded_payload() -> None:
    pages = storage_pages()
    lookup = recorded(pages)
    key = (pages[2].url, pages[2].sha256)
    lookup[key] = dataclasses.replace(
        lookup[key], retrieved_at=pages[2].retrieved_at + timedelta(seconds=1)
    )
    with pytest.raises(EiaProvenanceError, match="retrieval time"):
        map_pages(pages, lookup, STORAGE)


# --- exact request checks -----------------------------------------------------------------------


def refused(pages: list[EiaFetchedPage], *, start: str | None = None) -> str:
    with pytest.raises(EiaProvenanceError, match="not requested for this series") as info:
        map_pages(pages, recorded(pages), STORAGE, start=start)
    return str(info.value)


def test_real_full_and_windowed_requests_pass() -> None:
    full = storage_pages()
    assert len(map_pages(full, recorded(full), STORAGE)) == 10
    windowed = storage_pages(start=WINDOW_START)
    assert map_pages(windowed, recorded(windowed), STORAGE, start=WINDOW_START)


@pytest.mark.parametrize(
    "path",
    [
        f"/v2/extra/{STORAGE.route}/data",  # the old suffix match accepted a prefixed route
        f"/{STORAGE.route}/data",  # base path missing
        f"/v2/{STORAGE.route}/data/",  # trailing slash
        f"/v2/{STORAGE.route}",  # not the data endpoint
    ],
)
def test_path_must_be_exactly_the_series_data_route(path: str) -> None:
    pages = storage_pages()
    assert "path" in refused([with_path(pages[0], path), *pages[1:]])


def test_host_must_be_eia() -> None:
    page = storage_pages()[0]
    moved = dataclasses.replace(page, url=page.url.replace("api.eia.gov", "api.example.org", 1))
    assert "host" in refused([moved])


def test_start_must_match_the_window() -> None:
    windowed = storage_pages(start=WINDOW_START)
    assert "start" in refused(windowed, start="2010-01-22")  # a different window
    assert "unexpected parameters ['start']" in refused(windowed, start=None)  # full mode
    full = storage_pages()
    assert "start None, expected" in refused(full, start=WINDOW_START)  # window not applied


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"end": "2022-03-01"}, "unexpected parameters ['end']"),
        ({"facets[series][0]": "OTHER_FACET"}, "facets[series][0]"),
        ({"frequency": "monthly"}, "frequency"),
        ({"sort[0][direction]": "desc"}, "sort[0][direction]"),
        ({"sort[0][column]": None}, "sort[0][column]"),
        ({"data[0]": "other"}, "data[0]"),
        ({"length": "5000"}, "length"),
        ({"offset": None}, "offset"),
        ({"facets[duoarea][0]": "R48"}, "unexpected parameters ['facets[duoarea][0]']"),
    ],
)
def test_every_parameter_must_match_the_request(
    changes: dict[str, str | None], problem: str
) -> None:
    pages = storage_pages()
    assert problem in refused([with_params(pages[0], changes), *pages[1:]])


def test_repeated_parameters_are_refused() -> None:
    page = storage_pages()[0]
    doubled = dataclasses.replace(page, url=f"{page.url}&frequency=weekly")
    assert "frequency" in refused([doubled])


def test_request_problems_never_report_the_api_key_value() -> None:
    page = storage_pages()[0]
    leaked = with_params(page, {"api_key": "should-never-appear-123456", "end": "x"})
    message = refused([leaked])
    assert "should-never-appear-123456" not in message and "end" in message


# --- failure diagnostics ------------------------------------------------------------------------


class FutureEiaDataError(EiaDataError):
    """A subclass added later without its own check name."""


URL = "https://api.eia.gov/v2/natural-gas/stor/wkly/data?api_key=REDACTED"


@pytest.mark.parametrize(
    ("exc", "check"),
    [
        (EiaEmptyResponseError(["no rows"]), "eia_empty_response"),
        (EiaIncompleteError(["short"]), "eia_incomplete"),
        (EiaWarningError(["warned"]), "eia_warning"),
        (EiaMetadataError(["units"]), "eia_metadata"),
        (FutureEiaDataError(["new"]), "eia_data_error"),  # no StopIteration, base's name
        (EiaResponseError("data", ["not valid JSON"]), "eia_malformed_response"),
        (PeriodError("expected a YYYY-MM-DD period, got '2022-13'"), "eia_invalid_row"),
        (EiaAPIError(400, "Invalid frequency", url=URL), REQUEST_REJECTED),
        (EiaAPIError(404, "Not found", url=URL), REQUEST_REJECTED),
        (ProviderHTTPError(404, url=URL, body_excerpt="<html>"), REQUEST_REJECTED),
    ],
)
def test_data_failures_have_a_check_name(exc: Exception, check: str) -> None:
    diagnosis = failure_diagnostics(exc)
    assert diagnosis is not None and diagnosis[0] == check
    assert diagnosis[1]  # at least one problem to record


@pytest.mark.parametrize(
    "exc",
    [
        EiaAPIError(403, "API_KEY_INVALID: An invalid api_key was supplied.", url=URL),
        ProviderHTTPError(429, url=URL, body_excerpt="slow down"),
        ProviderHTTPError(500, url=URL, body_excerpt="oops"),
        ProviderHTTPError(503, url=URL, body_excerpt="unavailable"),
        ProviderTransportError("ConnectError requesting ...", url=URL),
        ResponseTooLargeError("too large", url=URL),
        EiaProvenanceError("page has no recorded raw payload"),
        ProvenanceError("raw payload belongs to another run"),
        SnapshotTimeError("clock went backwards"),
        OperationalError("SELECT 1", {}, Exception("connection refused")),
        RuntimeError("unexpected"),
        ValueError("a plain ValueError is not an EIA data failure"),
    ],
)
def test_operational_failures_have_no_check_name(exc: Exception) -> None:
    assert failure_diagnostics(exc) is None


def test_rejected_requests_are_not_described_as_catalog_drift() -> None:
    diagnosis = failure_diagnostics(EiaAPIError(404, "Not found", url=URL))
    assert diagnosis is not None
    (detail,) = diagnosis[1]
    assert "HTTP 404" in detail and "Not found" in detail and "drift" not in detail


def test_every_data_error_subclass_is_mapped() -> None:
    for kind in (EiaEmptyResponseError, EiaIncompleteError, EiaWarningError, EiaMetadataError):
        assert kind in DATA_ERROR_CHECKS
    assert EiaDataError in DATA_ERROR_CHECKS  # the fallback


# --- windows and boundary rows ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("latest", "frequency", "w", "before"),
    [
        ("2026-10-30", "daily", "2026-08-01", "2026-07-31"),  # 90 days
        ("2026-10-23", "weekly", "2026-04-24", "2026-04-17"),  # 26 weeks, Fridays
        ("2026-09", "monthly", "2024-07", "2024-06"),  # 26 months
        ("2026-01", "monthly", "2023-11", "2023-10"),  # across a year boundary
        ("2026-02", "monthly", "2023-12", "2023-11"),
    ],
)
def test_window_start_and_the_period_before_it(
    latest: str, frequency: str, w: str, before: str
) -> None:
    assert window_start(latest, frequency) == (w, before)


def test_unknown_frequency_has_no_window() -> None:
    with pytest.raises(ValueError, match="no lookback"):
        window_start("2026", "annual")


def test_modes() -> None:
    first = choose_window(None, "weekly", full=False)
    assert (first.mode, first.fetch_start, first.scope, first.window_start) == (
        "full",
        None,
        SnapshotScope(),
        None,
    )
    incremental = choose_window("2026-10-23", "weekly", full=False)
    assert (incremental.mode, incremental.fetch_start, incremental.window_start) == (
        "incremental",
        "2026-04-17",  # the period immediately before W
        "2026-04-24",
    )
    assert incremental.scope == SnapshotScope(start="2026-04-24")  # the scope starts at W
    reconcile = choose_window("2026-10-23", "weekly", full=True)
    assert (reconcile.mode, reconcile.fetch_start, reconcile.scope, reconcile.window_start) == (
        "full",
        None,
        SnapshotScope(),
        "2026-04-24",  # only for revision reports
    )


def _obs(period: str) -> SnapshotObservation:
    d = date.fromisoformat(period)
    return SnapshotObservation(period, d, d, Decimal("1"), "ok", 1)


def test_rows_before_the_window_are_dropped_whether_or_not_eia_returned_them() -> None:
    inclusive = [_obs(p) for p in ("2026-04-17", "2026-04-24", "2026-05-01")]
    kept, dropped = drop_before(inclusive, "2026-04-24")
    assert ([o.source_period for o in kept], dropped) == (["2026-04-24", "2026-05-01"], 1)
    kept, dropped = drop_before(inclusive[1:], "2026-04-24")  # exclusive start: nothing to drop
    assert ([o.source_period for o in kept], dropped) == (["2026-04-24", "2026-05-01"], 0)


# --- summary ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "code"),
    [
        (["succeeded", "succeeded"], 0),
        (["succeeded", "partial"], 2),
        (["partial", "failed"], 1),
        (["succeeded", UNFINALIZED], 1),
        (["partial", UNFINALIZED], 1),
    ],
)
def test_exit_codes(statuses: list[str], code: int) -> None:
    summary = EiaIngestionSummary(
        [SeriesOutcome(f"s{i}", "X", s, None, "full") for i, s in enumerate(statuses)]
    )
    assert summary.exit_code == code
