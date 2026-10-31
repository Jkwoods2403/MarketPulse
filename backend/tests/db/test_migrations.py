"""Migrations against a live PostgreSQL database."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text

from marketpulse.db import migrations
from marketpulse.db.base import SCHEMAS, Base


def test_models_match_migrated_schema_exactly(migrated_engine: Engine) -> None:
    """Fails if a model changes without a corresponding migration (or vice versa)."""
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"include_schemas": True, "compare_server_default": True},
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == []


def test_sessions_use_utc(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        assert connection.execute(text("SHOW timezone")).scalar_one() == "UTC"


def test_downgrade_to_base_then_upgrade_again(test_db_url: str, migrated_engine: Engine) -> None:
    migrations.downgrade(test_db_url, "base")
    engine = create_engine(test_db_url)
    try:
        remaining = set(inspect(engine).get_schema_names()) & set(SCHEMAS)
        assert remaining == set()
    finally:
        engine.dispose()

    migrations.upgrade(test_db_url)
    engine = create_engine(test_db_url)
    try:
        assert set(SCHEMAS) <= set(inspect(engine).get_schema_names())
    finally:
        engine.dispose()
