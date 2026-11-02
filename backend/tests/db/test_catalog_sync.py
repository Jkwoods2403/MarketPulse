"""Catalog synchronization against PostgreSQL."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketpulse.db.models import Series, Source
from marketpulse.ingestion.catalog_sync import CatalogSyncError, sync_catalog
from marketpulse.sources.catalog import Catalog, SeriesSpec, load_catalog
from marketpulse.sources.catalog.schema import VintageBasis


@pytest.fixture
def catalog() -> Catalog:
    return load_catalog()


def without(catalog: Catalog, code: str) -> Catalog:
    return replace(
        catalog, series=tuple((s, spec) for s, spec in catalog.series if spec.code != code)
    )


def changed(catalog: Catalog, series_code: str, /, **updates: object) -> Catalog:
    def apply(spec: SeriesSpec) -> SeriesSpec:
        return spec.model_copy(update=updates) if spec.code == series_code else spec

    return replace(catalog, series=tuple((s, apply(spec)) for s, spec in catalog.series))


def count(session: Session, model: type[Source] | type[Series]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_initial_sync_creates_all_sources_and_series(session: Session, catalog: Catalog) -> None:
    report = sync_catalog(session, catalog)

    assert sorted(report.sources_created) == ["eia", "fred"]
    assert len(report.series_created) == 24
    assert count(session, Source) == 2
    assert count(session, Series) == 24


def test_database_matches_catalog_field_by_field(session: Session, catalog: Catalog) -> None:
    sync_catalog(session, catalog)
    session.expire_all()

    sources = {s.code: s for s in session.scalars(select(Source))}
    for code, spec in catalog.sources.items():
        row = sources[code]
        assert (row.name, row.homepage_url, row.terms_url, row.license, row.attribution_text) == (
            spec.name,
            spec.homepage_url,
            spec.terms_url,
            spec.license.value,
            spec.attribution_text,
        )

    rows = {s.code: s for s in session.scalars(select(Series))}
    for source_code, spec in catalog.series:
        row = rows[spec.code]
        assert sources[source_code].id == row.source_id
        assert row.source_series_id == spec.source_series_id
        assert row.name == spec.name
        assert row.category == spec.category.value
        assert row.frequency == spec.frequency.value
        assert row.period_convention == spec.period_convention
        assert row.units == spec.units
        assert row.original_source == spec.original_source
        assert row.release_name == spec.release_name
        assert row.reference_url == spec.reference_url
        assert row.license_override == (
            spec.license_override.value if spec.license_override else None
        )
        assert row.seasonal_adjustment == (
            spec.seasonal_adjustment.value if spec.seasonal_adjustment else None
        )
        assert row.max_staleness == timedelta(days=spec.max_staleness_days)
        assert row.notes == spec.notes
        assert row.is_active is True
        assert row.vintage_basis == catalog.sources[source_code].vintage_basis.value


def test_second_sync_is_idempotent(session: Session, catalog: Catalog) -> None:
    sync_catalog(session, catalog)
    report = sync_catalog(session, catalog)

    assert not report.has_changes
    assert len(report.series_unchanged) == 24
    assert sorted(report.sources_unchanged) == ["eia", "fred"]


def test_metadata_change_updates_in_place(session: Session, catalog: Catalog) -> None:
    sync_catalog(session, catalog)
    original_id = session.scalars(select(Series.id).where(Series.code == "ust_10y")).one()

    report = sync_catalog(
        session, changed(catalog, "ust_10y", units="Percent per annum", max_staleness_days=7)
    )

    assert report.series_updated == {"ust_10y": ["units", "max_staleness"]}
    row = session.scalars(select(Series).where(Series.code == "ust_10y")).one()
    assert row.id == original_id
    assert row.units == "Percent per annum"
    assert row.max_staleness == timedelta(days=7)


def test_removed_series_is_deactivated_not_deleted(session: Session, catalog: Catalog) -> None:
    sync_catalog(session, catalog)
    report = sync_catalog(session, without(catalog, "us_lng_exports"))

    assert report.series_deactivated == ["us_lng_exports"]
    assert count(session, Series) == 24
    row = session.scalars(select(Series).where(Series.code == "us_lng_exports")).one()
    assert row.is_active is False


def test_re_added_series_is_reactivated(session: Session, catalog: Catalog) -> None:
    sync_catalog(session, catalog)
    sync_catalog(session, without(catalog, "us_lng_exports"))
    report = sync_catalog(session, catalog)

    assert report.series_updated == {"us_lng_exports": ["is_active"]}
    row = session.scalars(select(Series).where(Series.code == "us_lng_exports")).one()
    assert row.is_active is True


def test_identity_change_is_refused_and_nothing_is_written(
    session: Session, catalog: Catalog
) -> None:
    sync_catalog(session, catalog)
    tampered = changed(catalog, "ust_10y", source_series_id="DGS5", units="Changed")

    with pytest.raises(CatalogSyncError, match="identity changes are not allowed"):
        sync_catalog(session, tampered)

    row = session.scalars(select(Series).where(Series.code == "ust_10y")).one()
    assert (row.source_series_id, row.units) == ("DGS10", "Percent")


def test_renaming_a_series_code_is_refused(session: Session, catalog: Catalog) -> None:
    sync_catalog(session, catalog)
    with pytest.raises(CatalogSyncError, match="renaming series codes is not allowed"):
        sync_catalog(session, changed(catalog, "ust_10y", code="treasury_10y"))


def test_source_metadata_change_updates_source(session: Session, catalog: Catalog) -> None:
    sync_catalog(session, catalog)
    eia = catalog.sources["eia"].model_copy(update={"notes": "Updated notes"})
    report = sync_catalog(session, replace(catalog, sources={**catalog.sources, "eia": eia}))

    assert report.sources_updated == ["eia"]
    assert (
        session.scalars(select(Source.notes).where(Source.code == "eia")).one() == "Updated notes"
    )


def test_dry_run_rollback_leaves_database_unchanged(session: Session, catalog: Catalog) -> None:
    report = sync_catalog(session, catalog)
    assert report.has_changes
    session.rollback()  # what `catalog sync --dry-run` does

    assert count(session, Source) == 0
    assert count(session, Series) == 0


def test_series_take_their_sources_vintage_basis(session: Session, catalog: Catalog) -> None:
    sync_catalog(session, catalog)
    rows = session.execute(
        select(Source.code, Series.vintage_basis).join(Source, Source.id == Series.source_id)
    ).all()
    assert {code: basis for code, basis in rows} == {
        "fred": "source_vintage",
        "eia": "first_seen",
    }


def test_vintage_basis_change_is_refused_and_nothing_is_written(
    session: Session, catalog: Catalog
) -> None:
    sync_catalog(session, catalog)
    flipped = replace(
        catalog,
        sources={
            **catalog.sources,
            "eia": catalog.sources["eia"].model_copy(
                update={"vintage_basis": VintageBasis.SOURCE_VINTAGE}
            ),
        },
    )
    with pytest.raises(CatalogSyncError, match="vintage_basis cannot change"):
        sync_catalog(session, flipped)
    session.expire_all()
    bases = set(
        session.scalars(
            select(Series.vintage_basis)
            .join(Source, Source.id == Series.source_id)
            .where(Source.code == "eia")
        )
    )
    assert bases == {"first_seen"}
