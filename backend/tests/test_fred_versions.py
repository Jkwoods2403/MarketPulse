"""FRED observations -> ObservationVersion (date-level vintages at midnight US Central)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from fred_mock import fixture
from marketpulse.sources.fred import REALTIME_OPEN_END
from marketpulse.sources.fred.parse import FredObservation, parse_observations
from marketpulse.sources.fred.versions import to_version, to_versions, vintage_instant


def test_vintage_instant_is_midnight_central_with_daylight_saving() -> None:
    assert vintage_instant(date(2024, 4, 25)) == datetime(2024, 4, 25, 5, tzinfo=UTC)  # CDT
    assert vintage_instant(date(2024, 1, 15)) == datetime(2024, 1, 15, 6, tzinfo=UTC)  # CST
    # US DST 2024 began 2024-03-10: midnight that day is still CST.
    assert vintage_instant(date(2024, 3, 10)) == datetime(2024, 3, 10, 6, tzinfo=UTC)
    assert vintage_instant(date(2024, 3, 11)) == datetime(2024, 3, 11, 5, tzinfo=UTC)


def test_closed_fred_period_becomes_exclusive_end() -> None:
    obs = FredObservation(
        date(2024, 1, 1), date(2024, 4, 25), date(2024, 5, 29), Decimal("22768.866")
    )
    version = to_version(obs, "quarterly")
    assert version.vintage_start == vintage_instant(date(2024, 4, 25))
    assert version.vintage_end == vintage_instant(date(2024, 5, 30))  # day after realtime_end
    assert (version.period_start, version.period_end) == (date(2024, 1, 1), date(2024, 3, 31))
    assert version.value == Decimal("22768.866") and version.status == "ok"


def test_open_fred_period_is_current() -> None:
    obs = FredObservation(date(2024, 1, 1), date(2026, 9, 30), REALTIME_OPEN_END, Decimal("1"))
    assert to_version(obs, "quarterly").vintage_end is None


def test_consecutive_fred_periods_are_contiguous() -> None:
    page1 = parse_observations(fixture("observations_gdpc1_2024q1q2_page1"))
    page2 = parse_observations(fixture("observations_gdpc1_2024q1q2_page2"))
    versions = to_versions(page1.observations + page2.observations, "quarterly")
    q1 = sorted(
        (v for v in versions if v.period_start == date(2024, 1, 1)), key=lambda v: v.vintage_start
    )
    for earlier, later in pairwise(q1):
        assert earlier.vintage_end == later.vintage_start  # no gap, no overlap
    assert q1[-1].vintage_end is None


def test_real_missing_values_become_missing_at_source() -> None:
    page = parse_observations(fixture("observations_dgs10_holidays_window"))
    versions = {v.period_start: v for v in to_versions(page.observations, "daily")}
    christmas = versions[date(2025, 12, 25)]
    assert christmas.value is None and christmas.status == "missing_at_source"
    assert versions[date(2025, 12, 24)].value == Decimal("4.15")


def test_exact_decimal_string_is_preserved() -> None:
    obs = FredObservation(
        date(2026, 9, 1), date(2026, 10, 14), REALTIME_OPEN_END, Decimal("336.298")
    )
    assert str(to_version(obs, "monthly").value) == "336.298"


def test_latest_possible_real_time_end_does_not_overflow() -> None:
    obs = FredObservation(
        date(2024, 1, 1), date(2024, 1, 1), REALTIME_OPEN_END - timedelta(days=1), Decimal("1")
    )
    assert to_version(obs, "daily").vintage_end is not None
