"""Catalog entry schemas and per-entry validation rules.

Controlled vocabularies are defined here (not in the database layer) because `sources` may only
import `core`. The values for license and frequency mirror the database CHECK constraints; a
test asserts the two stay identical.
"""

import re
from datetime import date
from enum import StrEnum
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from marketpulse.sources.fred import SERIES_ID_PATTERN as FRED_SERIES_ID_PATTERN

CODE_PATTERN = r"^[a-z][a-z0-9_]{1,62}$"
Code = Annotated[str, StringConstraints(pattern=CODE_PATTERN)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class License(StrEnum):
    PUBLIC_DOMAIN = "public_domain"
    ATTRIBUTION_REQUIRED = "attribution_required"
    PERSONAL_USE_ONLY = "personal_use_only"
    NO_REDISTRIBUTION = "no_redistribution"


class AccessRequirement(StrEnum):
    """How a source's data may be fetched, separate from its license (mirrors db.enums)."""

    OPEN = "open"
    REGISTRATION_REQUIRED = "registration_required"  # a free account or API key
    SUBSCRIPTION_REQUIRED = "subscription_required"  # an account plus a data-product subscription


class VintageBasis(StrEnum):
    """Meaning of a series' version timestamps (mirrors db.enums.VintageBasis)."""

    SOURCE_VINTAGE = "source_vintage"  # the provider publishes real-time vintages (FRED)
    FIRST_SEEN = "first_seen"  # first retrieved by Market Pulse; not a publication time (EIA)


class Frequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    HOURLY = "hourly"
    FIFTEEN_MINUTE = "fifteen_minute"


class Category(StrEnum):
    """Top-level grouping. Extend as new domains (electricity, weather, ...) are added."""

    RATES = "rates"
    MACRO = "macro"
    FX = "fx"
    CRUDE_OIL = "crude_oil"
    REFINED_PRODUCTS = "refined_products"
    REFINING = "refining"
    NATURAL_GAS = "natural_gas"
    ELECTRICITY = "electricity"
    WEATHER = "weather"


class SeasonalAdjustment(StrEnum):
    SEASONALLY_ADJUSTED = "seasonally_adjusted"
    SEASONALLY_ADJUSTED_ANNUAL_RATE = "seasonally_adjusted_annual_rate"
    NOT_SEASONALLY_ADJUSTED = "not_seasonally_adjusted"


# Which period conventions make sense for each frequency. A convention names how the provider
# labels a period. Sub-daily conventions follow the existing "<unit>_ending" style
# ("week_ending_friday"): "hour_ending" is a one-hour interval labeled by the clock hour in which
# it ends (ERCOT "hour ending 1-24"); "fifteen_minute_ending" is a 15-minute interval labeled by
# its end (ERCOT settlement intervals). Labels are in the provider's local market time; stored
# interval identity is UTC (ADR-0012, ADR-0013).
PERIOD_CONVENTIONS: dict[Frequency, frozenset[str]] = {
    Frequency.HOURLY: frozenset({"hour_ending"}),
    Frequency.FIFTEEN_MINUTE: frozenset({"fifteen_minute_ending"}),
    Frequency.DAILY: frozenset({"business_day", "calendar_day"}),
    Frequency.WEEKLY: frozenset(
        f"week_ending_{day}"
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    ),
    Frequency.MONTHLY: frozenset({"month"}),
    Frequency.QUARTERLY: frozenset({"quarter"}),
    Frequency.ANNUAL: frozenset({"year"}),
}


def _check_https(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"must be an https URL: {url!r}")
    return url


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceSpec(_Strict):
    """A data provider."""

    code: Code
    name: Text
    homepage_url: str
    terms_url: str
    license: License
    access_requirement: AccessRequirement
    # Default for the source's series; a series may override it (SeriesSpec.vintage_basis).
    vintage_basis: VintageBasis
    attribution_text: Text
    # Hosts allowed in series reference_url values for this source (official pages only).
    reference_hosts: tuple[Text, ...] = Field(min_length=1)
    notes: Text | None = None

    _https = field_validator("homepage_url", "terms_url")(_check_https)


class SeriesSpec(_Strict):
    """A tracked series. `name` is the provider's official title, verbatim."""

    code: Code
    source_series_id: Text
    name: Text
    description: Text | None = None
    category: Category
    frequency: Frequency
    period_convention: Text
    units: Text
    seasonal_adjustment: SeasonalAdjustment | None = None
    original_source: Text
    release_name: Text | None = None
    reference_url: str
    license_override: License | None = None
    # Overrides the source's vintage_basis for this series (e.g. ERCOT load forecasts, which
    # carry ERCOT's own posting times, under a first_seen source). Immutable once synced.
    vintage_basis: VintageBasis | None = None
    max_staleness_days: int = Field(ge=1, le=400)
    notes: Text | None = None
    # Provider API metadata, verified against the live API and enforced by drift checks.
    # Required for EIA series (see _eia_id_problems); unused by FRED, which has its own metadata
    # endpoint. Stored in the catalog only (not in ref.series).
    api_route: Text | None = None  # e.g. "petroleum/pri/spt" (EIA API v2 native route)
    api_description: Text | None = None  # exact "series-description" string the API returns
    api_units: Text | None = None  # exact "units" code the API returns, e.g. "$/BBL"

    _https = field_validator("reference_url")(_check_https)

    @model_validator(mode="after")
    def _period_convention_matches_frequency(self) -> Self:
        allowed = PERIOD_CONVENTIONS[self.frequency]
        if self.period_convention not in allowed:
            raise ValueError(
                f"period_convention {self.period_convention!r} is not valid for "
                f"{self.frequency.value} series (allowed: {sorted(allowed)})"
            )
        return self


class SeriesFile(_Strict):
    """One provider's series file."""

    source: Code
    verified_on: date  # when the entries were last checked against official pages
    series: tuple[SeriesSpec, ...] = Field(min_length=1)


class SourcesFile(_Strict):
    sources: tuple[SourceSpec, ...] = Field(min_length=1)


# --- provider-specific identifier rules --------------------------------------------------

_FRED_ID = FRED_SERIES_ID_PATTERN  # FRED's stated rule: 25 or fewer alphanumeric characters
# EIA APIv1-style series ID: CATEGORY.SERIES.FREQUENCY (accepted by APIv2's seriesid route).
_EIA_ID = re.compile(r"^(?P<category>[A-Z]+)\.(?P<series>[A-Z0-9_]+)\.(?P<freq>[DWMQA])$")
_EIA_FREQ = {
    "D": Frequency.DAILY,
    "W": Frequency.WEEKLY,
    "M": Frequency.MONTHLY,
    "Q": Frequency.QUARTERLY,
    "A": Frequency.ANNUAL,
}


def _fred_id_problems(spec: SeriesSpec) -> list[str]:
    if not _FRED_ID.fullmatch(spec.source_series_id):
        return [f"FRED series ID {spec.source_series_id!r} must be 1-25 uppercase letters/digits"]
    return []


_EIA_ROUTE = re.compile(r"^[a-z0-9-]+(?:/[a-z0-9-]+)+$")


def eia_facet_series(source_series_id: str) -> str:
    """The native-route `series` facet for an EIA ID: the middle part ('PET.RWTC.D' -> 'RWTC').

    Verified for all catalogued series on 2026-11-01: the series-ID route translates each ID to
    its native route with exactly this facet value.
    """
    match = _EIA_ID.fullmatch(source_series_id)
    if not match:
        raise ValueError(f"not an EIA series ID: {source_series_id!r}")
    return match["series"]


def _eia_id_problems(spec: SeriesSpec) -> list[str]:
    problems: list[str] = []
    match = _EIA_ID.fullmatch(spec.source_series_id)
    if not match:
        problems.append(f"EIA series ID {spec.source_series_id!r} must look like 'PET.RWTC.D'")
    else:
        implied = _EIA_FREQ[match["freq"]]
        if implied is not spec.frequency:
            problems.append(
                f"EIA series ID {spec.source_series_id!r} implies {implied.value}, "
                f"but frequency is {spec.frequency.value}"
            )
    for field in ("api_route", "api_description", "api_units"):
        if getattr(spec, field) is None:
            problems.append(f"EIA series must set {field} (verified against the live API)")
    if spec.api_route is not None and not _EIA_ROUTE.fullmatch(spec.api_route):
        problems.append(
            f"api_route {spec.api_route!r} must be a lowercase route like 'petroleum/pri/spt' "
            "(no leading slash, no '/data')"
        )
    if spec.api_route is not None and spec.api_route.endswith("/data"):
        problems.append("api_route must not include the trailing '/data' node")
    return problems


# Sources without a rule here accept any non-empty identifier.
SERIES_ID_RULES = {"fred": _fred_id_problems, "eia": _eia_id_problems}
