"""Fixtures for tests that need a real PostgreSQL database.

These tests use a dedicated `<POSTGRES_DB>_test` database, recreated for each test session.
tests/isolation.py has already renamed POSTGRES_DB to that name. It refuses any connection to a
host that is not local, to a port other than POSTGRES_PORT, to ports 5432 and 5433 always, or to
a database whose name does not end in `_test` (except the `postgres` maintenance database, used
here only to create and drop the test database).
They run only in a session that opted in to a disposable server (MARKETPULSE_TEST_DB=disposable,
MARKETPULSE_TEST_DB_TOKEN, and all five POSTGRES_* variables set explicitly). Without the opt-in,
or if the server is not reachable, they are skipped without connecting, unless
MARKETPULSE_REQUIRE_DB=1 (set in CI), in which case they fail, so database tests can never be
skipped silently in CI.
Before anything is dropped or created, the maintenance connection verifies that the server holds
this session's marker database (`mp_disposable_<token>`); until it has, every other connection is
refused. A server without the marker is a hard failure, never a skip: the session was pointed at
the wrong server. The marker is checked again right before the teardown drop.
"""

import os
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from isolation import (
    check_database_host,
    check_database_name,
    check_database_port,
    database_disabled_reason,
    database_tests_enabled,
    drop_test_database,
    maintenance_connection,
    recreate_test_database,
    revoke_server_verification,
)
from marketpulse.core.config import Settings
from marketpulse.db import migrations
from marketpulse.db.session import make_engine, make_session_factory


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this directory as `db`, so `pytest -m "not db"` excludes them."""
    for item in items:
        if "tests/db/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.db)


def _admin_connect(settings: Settings) -> psycopg.Connection[tuple[object, ...]]:
    conninfo = psycopg.conninfo.make_conninfo(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        dbname="postgres",
        connect_timeout=3,
    )
    with maintenance_connection():
        return psycopg.connect(conninfo, autocommit=True)


@pytest.fixture(scope="session")
def db_settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def test_db_name(db_settings: Settings) -> Iterator[str]:
    """Create a fresh, empty test database for the session; drop it afterwards."""
    if not database_tests_enabled():  # decided before any connection is attempted
        message = (
            f"database tests need a disposable server ({database_disabled_reason()}). Set "
            "MARKETPULSE_TEST_DB=disposable, MARKETPULSE_TEST_DB_TOKEN (the token the server was "
            "created with, as POSTGRES_DB=mp_disposable_<token>) and "
            "POSTGRES_HOST/PORT/USER/PASSWORD/DB explicitly; "
            "ports 5432 (production) and 5433 (staging) are always refused"
        )
        if os.environ.get("MARKETPULSE_REQUIRE_DB") == "1":
            pytest.fail(message)
        pytest.skip(message)
    name = db_settings.postgres_db  # already `<POSTGRES_DB>_test` (tests/isolation.py)
    check_database_host(db_settings.postgres_host)  # fail fast on a remote server
    check_database_port(db_settings.postgres_port)  # ... or an unexpected port
    check_database_name(name)  # never create or drop anything else
    try:
        admin = _admin_connect(db_settings)
    except psycopg.OperationalError as exc:
        if os.environ.get("MARKETPULSE_REQUIRE_DB") == "1":
            pytest.fail(f"PostgreSQL required but unreachable: {exc}")
        pytest.skip("the disposable PostgreSQL server is not reachable (see README, Checks)")

    with admin:
        # Verifies the server's marker first: on a server without it nothing else runs, and
        # the error fails the session. Opens the connection gate only after success.
        recreate_test_database(admin, name)
    try:
        yield name
    finally:
        try:
            with _admin_connect(db_settings) as admin:
                drop_test_database(admin, name)  # verifies again, then drops; closes the gate
        finally:
            revoke_server_verification()  # also when the maintenance connection fails


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
