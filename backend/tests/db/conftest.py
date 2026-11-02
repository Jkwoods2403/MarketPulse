"""Fixtures for tests that need a real PostgreSQL database.

These tests use a dedicated `<POSTGRES_DB>_test` database, recreated for each test session.
If PostgreSQL is not reachable they are skipped, unless MARKETPULSE_REQUIRE_DB=1 (set in CI),
in which case they fail, so database tests can never be skipped silently in CI.
"""

import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from marketpulse.core.config import Settings
from marketpulse.db import migrations
from marketpulse.db.session import make_engine, make_session_factory


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this directory as `db`, so `pytest -m "not db"` excludes them."""
    for item in items:
        if "tests/db/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.db)


def _admin_conninfo(settings: Settings) -> str:
    return psycopg.conninfo.make_conninfo(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        dbname="postgres",
        connect_timeout=3,
    )


@pytest.fixture(scope="session")
def db_settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def test_db_name(db_settings: Settings) -> Iterator[str]:
    """Create a fresh, empty test database for the session; drop it afterwards."""
    name = f"{db_settings.postgres_db}_test"
    try:
        admin = psycopg.connect(_admin_conninfo(db_settings), autocommit=True)
    except psycopg.OperationalError as exc:
        if os.environ.get("MARKETPULSE_REQUIRE_DB") == "1":
            pytest.fail(f"PostgreSQL required but unreachable: {exc}")
        pytest.skip("PostgreSQL not reachable; start it with `docker compose up -d --wait`")

    with admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        with psycopg.connect(_admin_conninfo(db_settings), autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture(scope="session")
def test_db_url(db_settings: Settings, test_db_name: str) -> str:
    return db_settings.database_url_for(test_db_name)


@pytest.fixture(scope="session")
def migrated_engine(db_settings: Settings, test_db_name: str, test_db_url: str) -> Iterator[Engine]:
    """Engine for the test database with all migrations applied."""
    migrations.upgrade(test_db_url)
    engine = make_engine(db_settings, test_db_name)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(migrated_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Session factory whose writes really commit (for multi-connection tests).

    Every table that such tests write to is truncated afterwards, so committed data never leaks
    into other tests.
    """
    yield make_session_factory(migrated_engine)
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE obs.observations, ops.data_quality_issues, ops.raw_payloads, "
                "ops.ingestion_runs, ref.series, ref.sources RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    """A session whose changes are rolled back after each test."""
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        factory = make_session_factory(migrated_engine)
        db_session = factory(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield db_session
        finally:
            db_session.close()
            transaction.rollback()
