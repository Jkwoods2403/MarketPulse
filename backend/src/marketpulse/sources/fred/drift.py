"""Compare a catalog entry with FRED's live series metadata.

Errors (ingestion of the series must stop):
- the series ID differs;
- the frequency differs, or FRED reports a frequency code we do not recognize;
- the units differ (exact string comparison: a units change can silently rescale data);
- the seasonal adjustment differs from what the catalog records.

Warnings (logged, not blocking):
- FRED's title differs from the catalog name (titles are sometimes reworded);
- the catalog does not record a seasonal adjustment that FRED reports.
"""

from dataclasses import dataclass, field

from marketpulse.sources.catalog.schema import Frequency, SeasonalAdjustment, SeriesSpec
from marketpulse.sources.fred.parse import FredSeriesMetadata

FREQUENCY_CODES: dict[str, Frequency] = {
    "D": Frequency.DAILY,
    "W": Frequency.WEEKLY,
    "M": Frequency.MONTHLY,
    "Q": Frequency.QUARTERLY,
    "A": Frequency.ANNUAL,
}
SEASONAL_ADJUSTMENT_CODES: dict[str, SeasonalAdjustment] = {
    "NSA": SeasonalAdjustment.NOT_SEASONALLY_ADJUSTED,
    "SA": SeasonalAdjustment.SEASONALLY_ADJUSTED,
    "SAAR": SeasonalAdjustment.SEASONALLY_ADJUSTED_ANNUAL_RATE,
}


@dataclass(frozen=True)
class DriftReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_catalog_drift(spec: SeriesSpec, metadata: FredSeriesMetadata) -> DriftReport:
    errors: list[str] = []
    warnings: list[str] = []
    where = f"{spec.code} ({spec.source_series_id})"

    if metadata.series_id != spec.source_series_id:
        errors.append(f"{where}: FRED returned series {metadata.series_id!r}")

    frequency = FREQUENCY_CODES.get(metadata.frequency_short)
    if frequency is None:
        errors.append(f"{where}: unrecognized FRED frequency code {metadata.frequency_short!r}")
    elif frequency is not spec.frequency:
        errors.append(
            f"{where}: frequency is {frequency.value} at FRED but {spec.frequency.value} "
            "in the catalog"
        )

    if metadata.units.strip() != spec.units.strip():
        errors.append(
            f"{where}: units are {metadata.units!r} at FRED but {spec.units!r} in the catalog"
        )

    adjustment = SEASONAL_ADJUSTMENT_CODES.get(metadata.seasonal_adjustment_short)
    if spec.seasonal_adjustment is None:
        warnings.append(
            f"{where}: catalog records no seasonal adjustment; FRED reports "
            f"{metadata.seasonal_adjustment_short!r}"
        )
    elif adjustment is None:
        errors.append(
            f"{where}: unrecognized FRED seasonal adjustment code "
            f"{metadata.seasonal_adjustment_short!r}"
        )
    elif adjustment is not spec.seasonal_adjustment:
        errors.append(
            f"{where}: seasonal adjustment is {adjustment.value} at FRED but "
            f"{spec.seasonal_adjustment.value} in the catalog"
        )

    if metadata.title.strip() != spec.name.strip():
        warnings.append(f"{where}: FRED title {metadata.title!r} differs from catalog name")

    return DriftReport(errors=errors, warnings=warnings)
