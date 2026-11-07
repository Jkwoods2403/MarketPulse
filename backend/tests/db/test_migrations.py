"""Migrations against a live PostgreSQL database."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

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


SEED_0002 = """
INSERT INTO ref.sources (id, code, name, homepage_url, license)
VALUES (1, 'fred', 'FRED (synthetic test row)', 'https://example.invalid', 'attribution_required'),
       (2, 'eia', 'EIA (synthetic test row)', 'https://example.invalid', 'public_domain');
INSERT INTO ref.series (id, source_id, source_series_id, code, name, category, frequency,
                        period_convention, units, max_staleness, original_source, reference_url)
VALUES (1, 1, 'TEST1', 'fred_test', 'Synthetic', 'macro', 'monthly', 'month', 'Index',
        interval '40 days', 'Synthetic', 'https://example.invalid/1'),
       (2, 2, 'PET.TEST1.W', 'eia_test', 'Synthetic', 'crude_oil', 'weekly',
        'week_ending_friday', 'MBBL', interval '10 days', 'Synthetic', 'https://example.invalid/2');
INSERT INTO ops.ingestion_runs (id, job, status) VALUES (1, 'test.job', 'succeeded');
INSERT INTO obs.observations (series_id, period_start, period_end, value, status, vintage_start,
                              vintage_end, first_retrieved_at, last_confirmed_at,
                              ingestion_run_id)
VALUES (1, '2026-01-01', '2026-01-31', 1.10, 'ok', '2026-02-10 06:00+00', '2026-03-10 05:00+00',
        '2026-11-01 12:00+00', '2026-11-01 12:00+00', 1),
       (1, '2026-01-01', '2026-01-31', 1.125, 'ok', '2026-03-10 05:00+00', NULL,
        '2026-11-01 12:00+00', '2026-11-01 12:00+00', 1),
       (1, '2026-02-01', '2026-02-28', NULL, 'missing_at_source', '2026-03-10 05:00+00', NULL,
        '2026-11-01 12:00+00', '2026-11-01 12:00+00', 1);
INSERT INTO ops.data_quality_issues (ingestion_run_id, series_id, period_start, check_name,
                                     detail, severity, created_at)
VALUES (1, 1, '2026-01-01', 'vanished_version', 'synthetic', 'warning', '2026-11-01 12:00+00');
"""
OBSERVATIONS_0002 = (
    "SELECT id, series_id, period_start, period_end, value::text, status, vintage_start, "
    "vintage_end, first_retrieved_at, last_confirmed_at, ingestion_run_id "
    "FROM obs.observations ORDER BY id"
)
TRUNCATE_ALL = (
    "TRUNCATE obs.observations, ops.data_quality_issues, ops.raw_payloads, ops.ingestion_runs, "
    "ref.series, ref.sources RESTART IDENTITY CASCADE"
)


def test_0003_preserves_existing_rows_and_labels_series(
    test_db_url: str, migrated_engine: Engine
) -> None:
    migrations.downgrade(test_db_url, "0002")
    engine = create_engine(test_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(SEED_0002))
            before = conn.execute(text(OBSERVATIONS_0002)).all()

        migrations.upgrade(test_db_url, "0003")

        with engine.begin() as conn:
            assert conn.execute(text(OBSERVATIONS_0002)).all() == before  # byte-for-byte
            new_columns = conn.execute(
                text(
                    "SELECT source_period, raw_payload_id, last_confirmed_run_id "
                    "FROM obs.observations"
                )
            ).all()
            assert set(new_columns) == {(None, None, None)}  # "not recorded", never invented
            bases = dict(conn.execute(text("SELECT code, vintage_basis FROM ref.series")).all())
            assert bases == {"fred_test": "source_vintage", "eia_test": "first_seen"}
            tracking = conn.execute(
                text(
                    "SELECT last_detected_at = created_at, occurrences, last_detected_run_id, "
                    "resolved_at, resolved_by_run_id FROM ops.data_quality_issues"
                )
            ).one()
            assert tuple(tracking) == (True, 1, None, None, None)
            conn.execute(text(TRUNCATE_ALL))
    finally:
        engine.dispose()
        migrations.upgrade(test_db_url)


def test_0003_refuses_to_guess_the_vintage_basis(test_db_url: str, migrated_engine: Engine) -> None:
    migrations.downgrade(test_db_url, "0002")
    engine = create_engine(test_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ref.sources (id, code, name, homepage_url, license) VALUES "
                    "(1, 'other', 'Synthetic', 'https://example.invalid', 'public_domain');"
                    "INSERT INTO ref.series (id, source_id, source_series_id, code, name, "
                    "category, frequency, period_convention, units, max_staleness, "
                    "original_source, reference_url) VALUES (1, 1, 'X', 'other_test', 'S', "
                    "'macro', 'daily', 'calendar_day', 'Index', interval '4 days', 'S', "
                    "'https://example.invalid/x')"
                )
            )
        with pytest.raises(IntegrityError):
            migrations.upgrade(test_db_url, "0003")
        with engine.connect() as conn:  # the failed upgrade was rolled back entirely
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002"
            columns = {c["name"] for c in inspect(conn).get_columns("series", schema="ref")}
            assert "vintage_basis" not in columns
        with engine.begin() as conn:
            conn.execute(text(TRUNCATE_ALL))
    finally:
        engine.dispose()
        migrations.upgrade(test_db_url)


# --- 0004: source access requirement and interval frequencies ----------------------------------

SEED_0003 = """
INSERT INTO ref.sources (id, code, name, homepage_url, license)
VALUES (1, 'fred', 'FRED (synthetic test row)', 'https://example.invalid', 'attribution_required'),
       (2, 'eia', 'EIA (synthetic test row)', 'https://example.invalid', 'public_domain');
INSERT INTO ref.series (id, source_id, source_series_id, code, name, category, frequency,
                        period_convention, units, max_staleness, original_source, reference_url,
                        vintage_basis)
VALUES (1, 1, 'TEST1', 'fred_test', 'Synthetic', 'macro', 'monthly', 'month', 'Index',
        interval '40 days', 'Synthetic', 'https://example.invalid/1', 'source_vintage'),
       (2, 2, 'PET.TEST1.W', 'eia_test', 'Synthetic', 'crude_oil', 'weekly',
        'week_ending_friday', 'MBBL', interval '10 days', 'Synthetic', 'https://example.invalid/2',
        'first_seen');
"""
SOURCES_0003 = (
    "SELECT id, code, name, homepage_url, terms_url, license, attribution_text, notes, "
    "created_at, updated_at FROM ref.sources ORDER BY id"
)
SERIES_ALL = "SELECT * FROM ref.series ORDER BY id"
HOURLY_SERIES = (
    "INSERT INTO ref.series (id, source_id, source_series_id, code, name, category, frequency, "
    "period_convention, units, max_staleness, original_source, reference_url, vintage_basis) "
    "VALUES (3, 2, 'SYNTHETIC-H', 'hourly_test', 'Synthetic', 'electricity', 'hourly', "
    "'hour_ending', 'MW', interval '2 days', 'Synthetic', 'https://example.invalid/3', "
    "'first_seen')"
)


def source_columns(conn: Connection) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns("sources", schema="ref")}


def test_0004_classifies_existing_sources_and_preserves_rows(
    test_db_url: str, migrated_engine: Engine
) -> None:
    migrations.downgrade(test_db_url, "0003")
    engine = create_engine(test_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(SEED_0003))
            sources_before = conn.execute(text(SOURCES_0003)).all()
            series_before = conn.execute(text(SERIES_ALL)).all()

        migrations.upgrade(test_db_url, "0004")

        with engine.begin() as conn:
            assert conn.execute(text(SOURCES_0003)).all() == sources_before  # unchanged
            assert conn.execute(text(SERIES_ALL)).all() == series_before
            access = dict(
                conn.execute(text("SELECT code, access_requirement FROM ref.sources")).all()
            )
            assert access == {"fred": "registration_required", "eia": "registration_required"}
            conn.execute(text(HOURLY_SERIES))  # the widened frequency constraint accepts it
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE ref.sources SET access_requirement = 'free' WHERE id = 1"))
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE ref.sources SET access_requirement = NULL WHERE id = 1"))
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE ref.series SET frequency = 'five_minute' WHERE id = 3"))
        with engine.begin() as conn:
            conn.execute(text(TRUNCATE_ALL))
    finally:
        engine.dispose()
        migrations.upgrade(test_db_url)


def test_0004_refuses_to_guess_an_access_requirement(
    test_db_url: str, migrated_engine: Engine
) -> None:
    migrations.downgrade(test_db_url, "0003")
    engine = create_engine(test_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ref.sources (id, code, name, homepage_url, license) VALUES "
                    "(1, 'other', 'Synthetic', 'https://example.invalid', 'public_domain')"
                )
            )
        with pytest.raises(IntegrityError):
            migrations.upgrade(test_db_url, "0004")
        with engine.connect() as conn:  # the failed upgrade was rolled back entirely
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0003"
            assert "access_requirement" not in source_columns(conn)
        with engine.begin() as conn:
            conn.execute(text(TRUNCATE_ALL))
    finally:
        engine.dispose()
        migrations.upgrade(test_db_url)


def test_0004_downgrade_refuses_while_interval_frequencies_are_in_use(
    test_db_url: str, migrated_engine: Engine
) -> None:
    engine = create_engine(test_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ref.sources (id, code, name, homepage_url, license, "
                    "access_requirement) VALUES (2, 'eia', 'Synthetic', "
                    "'https://example.invalid', 'public_domain', 'registration_required')"
                )
            )
            conn.execute(text(HOURLY_SERIES))
        with pytest.raises(RuntimeError, match="1 series use a frequency added in 0004"):
            migrations.downgrade(test_db_url, "0003")
        with engine.connect() as conn:  # nothing was downgraded
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0004"
            assert "access_requirement" in source_columns(conn)
        with engine.begin() as conn:
            conn.execute(text(TRUNCATE_ALL))
    finally:
        engine.dispose()
        migrations.upgrade(test_db_url)


def test_0004_downgrade_restores_the_0003_schema(test_db_url: str, migrated_engine: Engine) -> None:
    migrations.downgrade(test_db_url, "0003")
    engine = create_engine(test_db_url)
    try:
        with engine.connect() as conn:
            assert "access_requirement" not in source_columns(conn)
        with engine.begin() as conn:
            conn.execute(text(SEED_0003))
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(HOURLY_SERIES))  # the 0003 frequency constraint is back
        with engine.begin() as conn:
            conn.execute(text(TRUNCATE_ALL))
    finally:
        engine.dispose()
        migrations.upgrade(test_db_url)
