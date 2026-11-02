"""Synchronize the version-controlled catalog into ref.sources and ref.series.

Rules:
- Sources and series are matched by `code`. Metadata changes are applied in place.
- **Identity is immutable.** A series code must always map to the same (source, source series
  ID). Changing either would silently reassign stored observations, so it is refused; retire
  the old code and add a new one instead.
- **vintage_basis is immutable too.** Each series takes its source's vintage_basis when it is
  created. Changing it later would change what the stored version timestamps mean (provider
  vintages vs. first seen by Market Pulse), so it is refused.
- **Nothing is deleted.** Series removed from the catalog are marked inactive, because stored
  observations reference them. Re-adding a series reactivates it.
- The whole sync happens in the caller's transaction: all problems are detected before any
  write, and the caller commits (or rolls back for a dry run).
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketpulse.db.models import Series, Source
from marketpulse.sources.catalog import Catalog, SeriesSpec, SourceSpec

SOURCE_FIELDS = ("name", "homepage_url", "terms_url", "license", "attribution_text", "notes")
SERIES_FIELDS = (
    "name",
    "description",
    "category",
    "frequency",
    "period_convention",
    "units",
    "seasonal_adjustment",
    "original_source",
    "release_name",
    "reference_url",
    "notes",
    "license_override",
    "vintage_basis",
    "max_staleness",
    "is_active",
)


class CatalogSyncError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Catalog sync refused:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass
class SyncReport:
    sources_created: list[str] = field(default_factory=list)
    sources_updated: list[str] = field(default_factory=list)
    sources_unchanged: list[str] = field(default_factory=list)
    sources_not_in_catalog: list[str] = field(default_factory=list)
    series_created: list[str] = field(default_factory=list)
    series_updated: dict[str, list[str]] = field(default_factory=dict)  # code -> changed fields
    series_unchanged: list[str] = field(default_factory=list)
    series_deactivated: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.sources_created
            or self.sources_updated
            or self.series_created
            or self.series_updated
            or self.series_deactivated
        )


def _source_values(spec: SourceSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "homepage_url": spec.homepage_url,
        "terms_url": spec.terms_url,
        "license": spec.license.value,
        "attribution_text": spec.attribution_text,
        "notes": spec.notes,
    }


def _series_values(spec: SeriesSpec, source: SourceSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "category": spec.category.value,
        "frequency": spec.frequency.value,
        "period_convention": spec.period_convention,
        "units": spec.units,
        "seasonal_adjustment": spec.seasonal_adjustment.value if spec.seasonal_adjustment else None,
        "original_source": spec.original_source,
        "release_name": spec.release_name,
        "reference_url": spec.reference_url,
        "notes": spec.notes,
        "license_override": spec.license_override.value if spec.license_override else None,
        "vintage_basis": source.vintage_basis.value,
        "max_staleness": timedelta(days=spec.max_staleness_days),
        "is_active": True,
    }


def _apply(row: Source | Series, values: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    changed = [name for name in fields if getattr(row, name) != values[name]]
    for name in changed:
        setattr(row, name, values[name])
    return changed


def sync_catalog(session: Session, catalog: Catalog) -> SyncReport:
    """Apply `catalog` to the database within the session's transaction (caller commits)."""
    report = SyncReport()
    db_sources = {s.code: s for s in session.scalars(select(Source))}
    db_series = list(session.scalars(select(Series)))
    by_code = {s.code: s for s in db_series}
    by_identity = {(s.source_id, s.source_series_id): s for s in db_series}
    source_code_by_id = {s.id: s.code for s in db_sources.values()}

    # 1. Detect identity conflicts before writing anything.
    problems: list[str] = []
    for source_code, spec in catalog.series:
        existing = by_code.get(spec.code)
        if existing is not None:
            existing_source = source_code_by_id.get(existing.source_id)
            stored = f"{existing_source}:{existing.source_series_id}"
            wanted = f"{source_code}:{spec.source_series_id}"
            if stored != wanted:
                problems.append(
                    f"series {spec.code!r} is stored as {stored} but the catalog says {wanted}; "
                    "identity changes are not allowed (retire the code and add a new one)"
                )
            source_spec = catalog.sources.get(source_code)
            wanted_basis = source_spec.vintage_basis.value if source_spec else None
            if wanted_basis is not None and existing.vintage_basis != wanted_basis:
                problems.append(
                    f"series {spec.code!r} has vintage_basis {existing.vintage_basis!r} but its "
                    f"source now says {wanted_basis!r}; vintage_basis cannot change because it "
                    "defines what stored version timestamps mean"
                )
        db_source = db_sources.get(source_code)
        if db_source is not None:
            other = by_identity.get((db_source.id, spec.source_series_id))
            if other is not None and other.code != spec.code:
                problems.append(
                    f"{source_code}:{spec.source_series_id} is stored under code {other.code!r} "
                    f"but the catalog uses {spec.code!r}; renaming series codes is not allowed"
                )
    if problems:
        raise CatalogSyncError(problems)

    # 2. Sources.
    for code, source_spec in catalog.sources.items():
        values = _source_values(source_spec)
        source_row = db_sources.get(code)
        if source_row is None:
            source_row = Source(code=code, **values)
            session.add(source_row)
            db_sources[code] = source_row
            report.sources_created.append(code)
        elif _apply(source_row, values, SOURCE_FIELDS):
            source_row.updated_at = func.now()
            report.sources_updated.append(code)
        else:
            report.sources_unchanged.append(code)
    report.sources_not_in_catalog = sorted(set(db_sources) - set(catalog.sources))
    session.flush()

    # 3. Series.
    catalog_codes = set()
    for source_code, spec in catalog.series:
        catalog_codes.add(spec.code)
        values = _series_values(spec, catalog.sources[source_code])
        series_row = by_code.get(spec.code)
        if series_row is None:
            session.add(
                Series(
                    source_id=db_sources[source_code].id,
                    source_series_id=spec.source_series_id,
                    code=spec.code,
                    **values,
                )
            )
            report.series_created.append(spec.code)
        else:
            changed = _apply(series_row, values, SERIES_FIELDS)
            if changed:
                series_row.updated_at = func.now()
                report.series_updated[spec.code] = changed
            else:
                report.series_unchanged.append(spec.code)

    # 4. Deactivate series no longer in the catalog (never delete).
    for stale in db_series:
        if stale.code not in catalog_codes and stale.is_active:
            stale.is_active = False
            stale.updated_at = func.now()
            report.series_deactivated.append(stale.code)

    session.flush()
    return report
