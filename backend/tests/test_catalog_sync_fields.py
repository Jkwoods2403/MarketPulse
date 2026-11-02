"""Guard: every metadata column is synchronized (no database needed).

If a column is added to ref.series or ref.sources, it must either be synced from the catalog
or be listed here as deliberately managed elsewhere.
"""

from marketpulse.db.models import Series, Source
from marketpulse.ingestion import catalog_sync
from marketpulse.sources.catalog import load_catalog

# Identity and bookkeeping columns, not copied as metadata.
NOT_SYNCED = {"id", "code", "source_id", "source_series_id", "created_at", "updated_at"}


def test_every_series_column_is_synced() -> None:
    columns = {c.name for c in Series.__table__.columns} - NOT_SYNCED
    assert set(catalog_sync.SERIES_FIELDS) == columns


def test_every_source_column_is_synced() -> None:
    columns = {c.name for c in Source.__table__.columns} - NOT_SYNCED
    assert set(catalog_sync.SOURCE_FIELDS) == columns


def test_value_builders_cover_their_field_lists() -> None:
    catalog = load_catalog()
    source_code, series_spec = catalog.series[0]
    source_spec = catalog.sources[source_code]
    assert set(catalog_sync._series_values(series_spec, source_spec)) == set(
        catalog_sync.SERIES_FIELDS
    )
    assert set(catalog_sync._source_values(catalog.sources[source_code])) == set(
        catalog_sync.SOURCE_FIELDS
    )
