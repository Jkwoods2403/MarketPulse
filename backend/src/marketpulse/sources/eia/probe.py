"""Read-only live probe of the catalogued EIA series (D5.3 P1).

Checks that each catalogued series still exists where the catalog says, with the metadata the
catalog was verified against, and reports its coverage. It stores nothing:

- **No database.** This module is in the `sources` layer, which may not import `marketpulse.db`
  or `marketpulse.ingestion` (an import-linter contract), so it cannot open a database
  connection, create an ingestion run, or record a raw payload.
- **No raw responses are kept.** Responses are parsed in memory and dropped; no `on_response`
  hook saves them.
- **No data values are reported, for any series.** The report holds only metadata, row counts,
  and period strings. Problem texts that could quote a value (a malformed `value` field) have the
  value withheld. Restricted series (Refinitiv/LSEG spot prices, `personal_use_only`) are
  flagged in the report so it is clear their values are licensed for personal use only.

Per series, at most four requests (route metadata is fetched once per route):

1. route metadata: the route still offers the series' frequency with the catalogued date
   format, a `series` facet, and a `value` data column;
2. the series-ID route maps the catalogued EIA series ID to the catalogued native route,
   series facet, and frequency (`EiaClient.verify_route`);
3. the first row (ascending, offset 0, length 1): the total row count, the first period, and
   the catalogued frequency, date format, series, description, and units (drift);
4. the last row (offset total - 1, length 1): the last period, checked the same way.

Each sampled page must hold exactly one row. EIA's "incomplete return" notice is expected on
these one-row requests whenever the series has more than one row (EIA attaches it when the
requested length is below the total; see validate.pagination_notice_expected), so it is accepted
there and nowhere else; any other warning fails the series.

Results per series: `fail` (anything that would stop ingestion: drift, a changed route, no
data, EIA errors, unexpected warnings, malformed responses or periods), `warn` (the latest
period is older than the catalog's `max_staleness_days`), else `ok`.

An invalid key (HTTP 401/403) or exhausted rate limit (HTTP 429 after retries) stops the probe at
once: the remaining series are reported as not probed instead of sending more requests that
would fail the same way.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from marketpulse.core.redaction import redact_text
from marketpulse.sources.catalog import Catalog, SeriesSpec
from marketpulse.sources.catalog.schema import License
from marketpulse.sources.eia.client import EiaAPIError, EiaClient
from marketpulse.sources.eia.parse import EiaDataPage, EiaRouteMetadata
from marketpulse.sources.eia.periods import PeriodError, period_bounds
from marketpulse.sources.eia.validate import (
    PAGINATION_NOTICE,
    EiaSeriesExpectation,
    check_page_metadata,
    expectation_for,
    expected_page_rows,
    pagination_notice_expected,
)
from marketpulse.sources.http import ProviderHTTPError

SOURCE_CODE = "eia"
SAMPLE_LENGTH = 1  # rows requested for the first and last row
RESTRICTED_LICENSES = frozenset({License.PERSONAL_USE_ONLY, License.NO_REDISTRIBUTION})

OK, WARN, FAIL = "ok", "warn", "fail"
STOP_STATUSES = frozenset({401, 403, 429})  # stop the whole probe: further requests are futile

# A malformed `value` field is reported by the parser as "data[i].value: ... <the raw value>".
_VALUE_PROBLEM = re.compile(r"(\.value: ).*")
VALUE_WITHHELD = "<value withheld>"


class EiaProbeConfigError(ValueError):
    """The probe cannot start (unknown series code, catalog entry without EIA metadata)."""


class _StopProbeError(Exception):
    """An authentication or rate-limit failure: no further requests are made."""


def withhold_values(text: str) -> str:
    """Remove any data value a problem text might quote, and any credential."""
    return redact_text(_VALUE_PROBLEM.sub(rf"\g<1>{VALUE_WITHHELD}", text))


@dataclass
class SeriesProbe:
    code: str
    source_series_id: str
    route: str
    frequency: str
    license: str  # effective license (series override, else the source's)
    restricted: bool
    max_staleness_days: int
    total: int | None = None
    first_period: str | None = None
    last_period: str | None = None
    last_period_end: date | None = None
    age_days: int | None = None  # days from the last period's end to the probe date
    problems: list[str] = field(default_factory=list)  # each makes the series `fail`
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.problems:
            return FAIL
        return WARN if self.warnings else OK


@dataclass
class EiaProbeReport:
    probed_on: date
    series: list[SeriesProbe] = field(default_factory=list)
    responses: int = 0  # successful responses received (and discarded)

    @property
    def exit_code(self) -> int:
        """1 any series failed; 2 warnings only; 0 every series ok."""
        statuses = {s.status for s in self.series}
        if FAIL in statuses:
            return 1
        return 2 if WARN in statuses else 0

    def count(self, status: str) -> int:
        return sum(s.status == status for s in self.series)


def effective_license(catalog: Catalog, spec: SeriesSpec) -> License:
    return spec.license_override or catalog.sources[SOURCE_CODE].license


def select_series(catalog: Catalog, codes: Sequence[str] | None) -> list[SeriesSpec]:
    available = {spec.code: spec for spec in catalog.series_for(SOURCE_CODE)}
    if not codes:
        return list(available.values())
    unknown = sorted(set(codes) - set(available))
    if unknown:
        raise EiaProbeConfigError(
            f"unknown EIA series code(s) {unknown}; available: {sorted(available)}"
        )
    return [available[code] for code in dict.fromkeys(codes)]


def probe_eia(
    client: EiaClient,
    catalog: Catalog,
    *,
    series_codes: Sequence[str] | None = None,
    today: date | None = None,
    responses: Callable[[], int] = lambda: 0,
) -> EiaProbeReport:
    """Probe the selected catalogued EIA series (default: all). Never raises per series.

    `responses` reports how many responses the client has received so far (the caller counts
    them in the client's `on_response` hook, which keeps nothing).
    """
    specs = select_series(catalog, series_codes)
    try:
        expectations = {spec.code: expectation_for(spec) for spec in specs}
    except ValueError as exc:
        raise EiaProbeConfigError(str(exc)) from None

    report = EiaProbeReport(probed_on=today or datetime.now(UTC).date())
    route_metadata: dict[str, EiaRouteMetadata | Exception] = {}
    stopped: str | None = None
    for spec in specs:
        license_ = effective_license(catalog, spec)
        probe = SeriesProbe(
            code=spec.code,
            source_series_id=spec.source_series_id,
            route=expectations[spec.code].route,
            frequency=spec.frequency.value,
            license=license_.value,
            restricted=license_ in RESTRICTED_LICENSES,
            max_staleness_days=spec.max_staleness_days,
        )
        report.series.append(probe)
        if stopped is not None:
            probe.problems.append(f"not probed: {stopped}")
            continue
        try:
            _probe_series(client, probe, expectations[spec.code], route_metadata, report.probed_on)
        except _StopProbeError as exc:
            stopped = f"the probe stopped after {spec.code} ({exc})"
    report.responses = responses()
    return report


def _probe_series(
    client: EiaClient,
    probe: SeriesProbe,
    expected: EiaSeriesExpectation,
    route_metadata: dict[str, EiaRouteMetadata | Exception],
    today: date,
) -> None:
    def failed(step: str, exc: Exception) -> None:
        probe.problems.append(withhold_values(f"{step}: {type(exc).__name__}: {exc}"))
        if isinstance(exc, (ProviderHTTPError, EiaAPIError)) and exc.status_code in STOP_STATUSES:
            raise _StopProbeError(f"HTTP {exc.status_code} at {step}") from None

    # 1. Route metadata (once per route; a failure is reported for every series on the route).
    if expected.route not in route_metadata:
        try:
            route_metadata[expected.route] = client.route_metadata(expected.route)
        except Exception as exc:
            route_metadata[expected.route] = exc
    metadata = route_metadata[expected.route]
    if isinstance(metadata, Exception):
        failed("route metadata", metadata)
    else:
        probe.problems.extend(_route_problems(metadata, expected))

    # 2. The series-ID route still maps to the catalogued route, facet, and frequency.
    try:
        probe.problems.extend(client.verify_route(expected, probe.source_series_id))
    except Exception as exc:
        failed("series-ID route", exc)

    # 3. and 4. The first and last rows: total, coverage, and metadata drift.
    try:
        first = client.fetch_page(expected, offset=0, length=SAMPLE_LENGTH)
    except Exception as exc:
        failed("first row", exc)
        return
    probe.total = first.total
    if first.total == 0 or not first.rows:
        probe.problems.append(f"{expected.code}: EIA returned no data (total {first.total})")
        return
    probe.problems.extend(_page_problems(first, expected, offset=0))
    probe.first_period = first.rows[0].period

    last = first
    if first.total > 1:
        try:
            last = client.fetch_page(expected, offset=first.total - 1, length=SAMPLE_LENGTH)
        except Exception as exc:
            failed("last row", exc)
            return
        if last.total != first.total:
            probe.problems.append(
                f"{expected.code}: total changed between requests "
                f"({first.total}, then {last.total})"
            )
        probe.problems.extend(_page_problems(last, expected, offset=first.total - 1))
        if not last.rows:
            probe.problems.append(f"{expected.code}: no row at offset {first.total - 1}")
            return
    probe.last_period = last.rows[-1].period

    try:
        period_bounds(probe.first_period, expected.frequency, expected.period_convention)
        _, probe.last_period_end = period_bounds(
            probe.last_period, expected.frequency, expected.period_convention
        )
    except PeriodError as exc:
        probe.problems.append(f"{expected.code}: {exc}")
        return
    if probe.last_period < probe.first_period:
        probe.problems.append(
            f"{expected.code}: last period {probe.last_period} is before the first "
            f"{probe.first_period} (rows are not in ascending order)"
        )
    probe.age_days = (today - probe.last_period_end).days
    if probe.age_days > probe.max_staleness_days:
        probe.warnings.append(
            f"{expected.code}: latest period {probe.last_period} ended {probe.age_days} days "
            f"ago, more than the catalog's max_staleness_days ({probe.max_staleness_days})"
        )


def _route_problems(metadata: EiaRouteMetadata, expected: EiaSeriesExpectation) -> list[str]:
    problems = []
    date_format = metadata.frequencies.get(expected.frequency)
    if date_format is None:
        problems.append(
            f"{expected.code}: route {expected.route} no longer offers frequency "
            f"{expected.frequency!r} (offers {sorted(metadata.frequencies)})"
        )
    elif date_format != expected.date_format:
        problems.append(
            f"{expected.code}: route {expected.route} reports date format {date_format!r} for "
            f"{expected.frequency}, expected {expected.date_format!r}"
        )
    if "series" not in metadata.facets:
        problems.append(f"{expected.code}: route {expected.route} has no 'series' facet")
    if "value" not in metadata.data_columns:
        problems.append(f"{expected.code}: route {expected.route} has no 'value' data column")
    return problems


def _page_problems(page: EiaDataPage, expected: EiaSeriesExpectation, *, offset: int) -> list[str]:
    """Drift, row count, and warnings in one sampled page (SAMPLE_LENGTH rows at `offset`)."""
    problems = check_page_metadata(page, expected)
    want = expected_page_rows(page.total, offset=offset, length=SAMPLE_LENGTH)
    if len(page.rows) != want:
        problems.append(
            f"{expected.code}: {len(page.rows)} row(s) at offset {offset}, expected {want} "
            f"(length {SAMPLE_LENGTH}, total {page.total})"
        )
    explained = pagination_notice_expected(page.total, length=SAMPLE_LENGTH)
    for w in page.warnings:
        if w.warning == PAGINATION_NOTICE and explained:
            continue  # length 1 below the total: EIA's notice, not truncation
        problems.append(
            withhold_values(
                f"{expected.code}: EIA warning {w.warning!r} at offset {offset}"
                + (f": {w.description}" if w.description else "")
            )
        )
    return problems
