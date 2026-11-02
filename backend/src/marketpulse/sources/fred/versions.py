"""Convert FRED observations into provider-neutral ObservationVersion values.

FRED real-time dates are date-only. Per the approved convention they are stored as midnight
US Central (FRED's local time, America/Chicago) on that date, converted to UTC. This is
date-level precision, not an exact publication timestamp: a value published at 3:16 pm on D is
treated as known from 00:00 Central on D. Daylight saving time is handled by the time zone
database (midnight CDT = 05:00 UTC, midnight CST = 06:00 UTC).

FRED periods are closed at both ends; ObservationVersion uses an exclusive end, so
vintage_end = midnight Central on (realtime_end + 1 day). FRED's open end (9999-12-31) means
"still current" and maps to None.
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from marketpulse.core.versions import STATUS_MISSING_AT_SOURCE, STATUS_OK, ObservationVersion
from marketpulse.sources.fred.parse import FredObservation
from marketpulse.sources.fred.periods import period_bounds

FRED_TIMEZONE = ZoneInfo("America/Chicago")


def vintage_instant(day: date) -> datetime:
    """Midnight US Central on `day`, as an aware UTC datetime."""
    return datetime.combine(day, time(0), tzinfo=FRED_TIMEZONE).astimezone(UTC)


def to_version(observation: FredObservation, frequency: str) -> ObservationVersion:
    start, end = period_bounds(observation.observation_date, frequency)
    vintage_end = (
        None
        if observation.is_current
        else vintage_instant(observation.realtime_end + timedelta(days=1))
    )
    return ObservationVersion(
        period_start=start,
        period_end=end,
        vintage_start=vintage_instant(observation.realtime_start),
        vintage_end=vintage_end,
        value=observation.value,
        status=STATUS_MISSING_AT_SOURCE if observation.value is None else STATUS_OK,
    )


def to_versions(
    observations: Iterable[FredObservation], frequency: str
) -> list[ObservationVersion]:
    return [to_version(observation, frequency) for observation in observations]
