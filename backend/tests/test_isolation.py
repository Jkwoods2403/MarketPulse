"""The session-wide test isolation (tests/isolation.py) is really in force.

None of these tests reaches the network or a database: every guard refuses before connecting.

Most guard tests run against a SYNTHETIC disposable configuration (approved port 55432, nothing
listening), installed per test by the autouse fixture below, so they behave the same whether or
not the session itself opted in to a disposable database. Tests about the session's real state
use the values captured at import (REAL_*), before any test changed them.
"""

import ast
import asyncio
import builtins
import importlib.metadata
import inspect
import io
import os
import socket
import subprocess
import sys
import threading
import uuid
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import psycopg.generators
import psycopg.waiting
import pytest
from psycopg._connection_base import BaseConnection
from sqlalchemy import create_engine

import isolation
from isolation import (
    APPROVED_DB_HOSTS,
    CREDENTIAL_VARS,
    DISPOSABLE_MARKER_PREFIX,
    DISPOSABLE_OPT_IN_VAR,
    DISPOSABLE_TOKEN_VAR,
    LIBPQ_DEFAULT_PORT,
    LIBPQ_ENV_VARS,
    MARKER_QUERY,
    PROTECTED_DB_PORTS,
    REPO_DATA_DIR,
    REQUIRED_AUTH,
    REQUIRED_DB_VARS,
    IsolationError,
    approved_db_port,
    as_test_database,
    check_connection_target,
    check_database_host,
    check_database_name,
    check_database_port,
    data_dir_fingerprint,
    database_opt_in_problems,
    database_tests_enabled,
    disposable_marker,
    drop_test_database,
    fingerprint_changes,
    maintenance_connection,
    recreate_test_database,
    server_verified,
    verify_disposable_server,
)
from marketpulse.core.config import Settings, get_settings
from marketpulse.sources.eia.client import EiaClient, EiaConfigError
from marketpulse.sources.fred.client import FredClient, FredConfigError

# Set in a child session's environment, so the child does not start a child of its own.
CHILD_SESSION_VAR = "MARKETPULSE_TEST_CHILD_SESSION"

# The session's real database state, captured before any test patches it.
REAL_DB_ENABLED = database_tests_enabled()
REAL_APPROVED_PORT = isolation._approved_db_port
REAL_PGREQUIREAUTH = os.environ.get("PGREQUIREAUTH")

# A synthetic disposable server for testing the guard logic. Nothing listens there, and no test
# below connects: every check passes or refuses before connecting.
DISPOSABLE_PORT = 55432
SYNTHETIC_TOKEN = "synthetictoken0001"  # never a real server's token


@pytest.fixture(autouse=True)
def synthetic_disposable_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthetic opted-in session whose server has already been VERIFIED (the connection gate
    is open), so the host/port/name/auth rules can be tested on their own. Tests of the gate
    itself close it explicitly."""
    monkeypatch.setattr(isolation, "_db_tests_enabled", True)
    monkeypatch.setattr(isolation, "_db_disabled_reason", "")
    monkeypatch.setattr(isolation, "_approved_db_port", DISPOSABLE_PORT)
    monkeypatch.setattr(isolation, "_db_token", SYNTHETIC_TOKEN)
    monkeypatch.setattr(isolation, "_db_server_verified", True)
    monkeypatch.setenv("PGREQUIREAUTH", REQUIRED_AUTH)


# --- credentials and configuration -------------------------------------------------------------


def test_provider_credentials_are_not_in_the_environment() -> None:
    assert not any(var in os.environ for var in CREDENTIAL_VARS)


def test_settings_do_not_read_the_repository_env_file() -> None:
    assert Settings.model_config["env_file"] is None
    settings = get_settings()
    assert settings.eia_api_key is None
    assert settings.fred_api_key is None


def test_provider_clients_cannot_be_built_from_the_test_settings() -> None:
    with pytest.raises(EiaConfigError, match="EIA_API_KEY"):
        EiaClient.from_settings(get_settings())
    with pytest.raises(FredConfigError, match="FRED_API_KEY"):
        FredClient.from_settings(get_settings())


def test_data_directory_is_a_temporary_directory() -> None:
    data_dir = get_settings().marketpulse_data_dir
    assert data_dir.is_dir()
    assert data_dir.resolve() != REPO_DATA_DIR.resolve()
    assert REPO_DATA_DIR.resolve() not in data_dir.resolve().parents


def test_configured_database_is_a_test_database() -> None:
    """Opted in: the exported database was renamed to `<name>_test`. Not opted in: nothing was
    renamed or supplied (no connection is possible; see the disabled-session tests)."""
    if REAL_DB_ENABLED:
        assert os.environ["POSTGRES_DB"].endswith("_test")
        assert get_settings().postgres_db == os.environ["POSTGRES_DB"]
    else:
        assert get_settings().postgres_db == os.environ.get(
            "POSTGRES_DB", Settings.model_fields["postgres_db"].default
        )


@pytest.mark.parametrize(
    ("name", "expected"),
    [("marketpulse", "marketpulse_test"), ("marketpulse_test", "marketpulse_test")],
)
def test_as_test_database(name: str, expected: str) -> None:
    assert as_test_database(name) == expected


# --- database guard ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["marketpulse", "marketpulse_testing", "test", "", None])
def test_only_test_database_names_are_allowed(name: str | None) -> None:
    with pytest.raises(IsolationError, match="_test"):
        check_database_name(name)


def test_maintenance_database_only_inside_its_context() -> None:
    with pytest.raises(IsolationError):
        check_database_name("postgres")
    with maintenance_connection():
        check_database_name("postgres")
    with pytest.raises(IsolationError):
        check_database_name("postgres")


def test_psycopg_refuses_a_real_database_before_connecting() -> None:
    # Port 9 (discard) on loopback: even if the guard failed, nothing real would be reached.
    with pytest.raises(IsolationError, match="'marketpulse'"):
        psycopg.connect("host=127.0.0.1 port=9 dbname=marketpulse connect_timeout=1")
    with pytest.raises(IsolationError, match="'marketpulse'"):
        psycopg.Connection.connect(host="127.0.0.1", port=9, dbname="marketpulse")


def test_sqlalchemy_refuses_a_real_database_before_connecting() -> None:
    engine = create_engine("postgresql+psycopg://user:pw@127.0.0.1:9/marketpulse")
    try:
        with pytest.raises(IsolationError, match="'marketpulse'"):
            engine.connect()
    finally:
        engine.dispose()


# Remote hosts used below are reserved and never real: 192.0.2.0/24 and 203.0.113.0/24
# (documentation ranges, RFC 5737) and the `.invalid` top-level domain (RFC 2606).
REMOTE_HOSTS = [
    "db.example.invalid",
    "192.0.2.10",
    "203.0.113.7",
    "10.0.0.5",  # private, but not this machine
    "2001:db8::1",  # IPv6 documentation range
    "::ffff:192.0.2.10",  # IPv4-mapped, not loopback
    "localhost.example.invalid",  # starts like "localhost", but is not it
]
# The approved (synthetic disposable) port and one that is not approved.
PORT = str(DISPOSABLE_PORT)
OTHER_PORT = str(DISPOSABLE_PORT + 1)

LOCAL_HOSTS: list[str | None] = [
    None,
    "",
    "localhost",
    "LOCALHOST",
    "127.0.0.1",
    "127.0.0.2",
    "::1",
]
SOCKET_HOSTS: list[str | None] = ["/var/run/postgresql", "/run/postgresql", "@abstract-socket"]


@pytest.mark.parametrize("host", REMOTE_HOSTS)
def test_remote_database_hosts_are_refused(host: str) -> None:
    with pytest.raises(IsolationError, match="refused host"):
        check_database_host(host)


@pytest.mark.parametrize("host", LOCAL_HOSTS + SOCKET_HOSTS)
def test_local_database_hosts_are_accepted(host: str | None) -> None:
    check_database_host(host)


def test_configured_database_host_is_local() -> None:
    assert "localhost" in APPROVED_DB_HOSTS
    check_database_host(get_settings().postgres_host)


@pytest.mark.parametrize("host", REMOTE_HOSTS)
def test_a_test_database_name_does_not_make_a_remote_host_acceptable(host: str) -> None:
    with pytest.raises(IsolationError, match="refused host"):
        psycopg.connect(host=host, port=9, dbname="marketpulse_test", connect_timeout=1)


def test_maintenance_connection_does_not_bypass_the_host_check() -> None:
    with maintenance_connection():
        # name relaxed (the port is the approved one, so only the name rule is being exercised)
        check_connection_target({"host": "localhost", "port": PORT, "dbname": "postgres"})
        with pytest.raises(IsolationError, match="refused host"):
            psycopg.connect(host="192.0.2.10", port=9, dbname="postgres", connect_timeout=1)
        with pytest.raises(IsolationError, match="refused host"):
            check_connection_target({"host": "db.example.invalid", "dbname": "postgres"})


@pytest.mark.parametrize(
    "params",
    [
        {"host": "127.0.0.1,192.0.2.10", "dbname": "marketpulse_test"},  # any entry of a list
        {"host": "localhost", "hostaddr": "192.0.2.10", "dbname": "marketpulse_test"},
        {"hostaddr": "127.0.0.1,203.0.113.7", "dbname": "marketpulse_test"},
    ],
)
def test_every_host_and_hostaddr_entry_is_checked(params: dict[str, str]) -> None:
    with pytest.raises(IsolationError, match="refused host"):
        check_connection_target(params)


def test_pghost_fallback_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "192.0.2.10")
    with pytest.raises(IsolationError, match="refused host"):
        psycopg.connect("port=9 dbname=marketpulse_test connect_timeout=1")
    monkeypatch.delenv("PGHOST")
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.7")
    with pytest.raises(IsolationError, match="refused host"):
        check_connection_target({"dbname": "marketpulse_test"})


def test_service_definitions_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(IsolationError, match="service"):
        check_connection_target({"service": "prod", "dbname": "marketpulse_test"})
    monkeypatch.setenv("PGSERVICE", "prod")
    with pytest.raises(IsolationError, match="service"):
        check_connection_target({"host": "localhost", "dbname": "marketpulse_test"})


def test_sqlalchemy_refuses_a_remote_host() -> None:
    engine = create_engine("postgresql+psycopg://user:pw@192.0.2.10:9/marketpulse_test")
    try:
        with pytest.raises(IsolationError, match="refused host"):
            engine.connect()
    finally:
        engine.dispose()


# Local configurations, WITHOUT a port. Each is checked below once with the approved port and
# once with no port (libpq then uses its default, 5432, a protected port: always refused).
LOCAL_CONFIGS = [
    {"host": "localhost", "dbname": "marketpulse_test"},
    {"host": "127.0.0.1", "dbname": "marketpulse_test"},
    {"host": "::1", "dbname": "marketpulse_test"},
    {"host": "localhost", "hostaddr": "127.0.0.1", "dbname": "marketpulse_test"},
    {"host": "localhost,127.0.0.1", "dbname": "marketpulse_test"},
    {"host": "/var/run/postgresql", "dbname": "marketpulse_test"},
    {"dbname": "marketpulse_test"},  # libpq default host: local socket or localhost
]
LOCAL_IDS = ["localhost", "ipv4", "ipv6", "hostaddr", "host-list", "socket", "no-host"]


@pytest.mark.parametrize("params", LOCAL_CONFIGS, ids=LOCAL_IDS)
def test_local_test_database_configurations_are_accepted(params: dict[str, str]) -> None:
    check_connection_target({**params, "port": PORT})


@pytest.mark.parametrize("params", LOCAL_CONFIGS, ids=LOCAL_IDS)
def test_a_local_configuration_without_a_port_uses_the_libpq_default(
    params: dict[str, str],
) -> None:
    """No port means libpq's default (5432), the protected production port: always refused,
    never silently allowed."""
    with pytest.raises(IsolationError, match="protected"):
        check_connection_target(params)


def test_the_session_state_matches_its_explicit_configuration() -> None:
    """Without the opt-in and all five POSTGRES_* variables, no port is approved at all.
    With them, the approved port is exactly the exported POSTGRES_PORT, never 5432 or 5433."""
    if REAL_DB_ENABLED:
        assert (
            REAL_APPROVED_PORT == int(os.environ["POSTGRES_PORT"]) == get_settings().postgres_port
        )
        assert REAL_APPROVED_PORT not in PROTECTED_DB_PORTS
    else:
        assert REAL_APPROVED_PORT is None


@pytest.mark.parametrize("port", [PORT, int(PORT), f" {PORT} "])
def test_the_approved_port_is_accepted(port: str | int) -> None:
    check_database_port(port)


@pytest.mark.parametrize(
    "port", [OTHER_PORT, "9", "0", "-1", "65536", "70000", "abc", f"{PORT}x", "5432.0"]
)
def test_unexpected_ports_are_refused(port: str) -> None:
    with pytest.raises(IsolationError, match="refused port"):
        check_database_port(port)


@pytest.mark.parametrize("port", [None, "", " "])
def test_an_empty_port_means_the_libpq_default(port: str | None) -> None:
    with pytest.raises(IsolationError, match="protected"):  # 5432
        check_database_port(port)


@pytest.mark.parametrize("port", sorted(PROTECTED_DB_PORTS))
def test_protected_ports_are_refused_even_when_approved(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: even if 5432 or 5433 somehow became the approved port, the connection
    guard still refuses it before connecting."""
    monkeypatch.setattr(isolation, "_approved_db_port", port)
    for value in (port, str(port), f" {port} "):
        with pytest.raises(IsolationError, match="protected"):
            check_database_port(value)
    with pytest.raises(IsolationError, match="protected"):
        psycopg.connect(host="127.0.0.1", port=port, dbname="marketpulse_test", connect_timeout=1)
    with maintenance_connection(), pytest.raises(IsolationError, match="protected"):
        check_connection_target({"host": "localhost", "port": str(port), "dbname": "postgres"})


def test_an_unexpected_port_is_refused_before_connecting() -> None:
    with pytest.raises(IsolationError, match="refused port"):
        psycopg.connect(
            host="127.0.0.1", port=OTHER_PORT, dbname="marketpulse_test", connect_timeout=1
        )
    with pytest.raises(IsolationError, match="refused port"):
        psycopg.connect(f"host=localhost port={OTHER_PORT} dbname=marketpulse_test")
    engine = create_engine(f"postgresql+psycopg://user:pw@127.0.0.1:{OTHER_PORT}/marketpulse_test")
    try:
        with pytest.raises(IsolationError, match="refused port"):
            engine.connect()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "ports", [f"{PORT},{OTHER_PORT}", f"{OTHER_PORT},{PORT}", f"{PORT},{PORT},abc"]
)
def test_every_entry_of_a_port_list_is_checked(ports: str) -> None:
    params = {"host": "localhost,127.0.0.1,::1", "port": ports, "dbname": "marketpulse_test"}
    with pytest.raises(IsolationError, match="refused port"):
        check_connection_target(params)


def test_a_port_list_of_approved_ports_is_accepted() -> None:
    check_connection_target(
        {"host": "localhost,127.0.0.1", "port": f"{PORT},{PORT}", "dbname": "marketpulse_test"}
    )


def test_pgport_fallback_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPORT", OTHER_PORT)
    with pytest.raises(IsolationError, match="refused port"):
        psycopg.connect("host=localhost dbname=marketpulse_test connect_timeout=1")
    with pytest.raises(IsolationError, match="refused port"):
        check_connection_target({"host": "localhost", "port": "", "dbname": "marketpulse_test"})
    # An explicit port takes precedence over PGPORT, as in libpq.
    check_connection_target({"host": "localhost", "port": PORT, "dbname": "marketpulse_test"})
    monkeypatch.setenv("PGPORT", PORT)
    check_connection_target({"host": "localhost", "dbname": "marketpulse_test"})


def test_maintenance_connection_does_not_bypass_the_port_check() -> None:
    with maintenance_connection():
        check_connection_target({"host": "localhost", "port": PORT, "dbname": "postgres"})
        with pytest.raises(IsolationError, match="refused port"):
            psycopg.connect(host="localhost", port=OTHER_PORT, dbname="postgres")


# --- the guard under a default and non-default approved port -------------------------------------
# Regression for the staging run (POSTGRES_PORT=5433): the rules must hold for ANY approved port.
# The approved port is swapped only for the duration of one test (monkeypatch restores it); no test
# here connects anywhere: every check either passes or refuses before connecting.

APPROVED_PORTS = [DISPOSABLE_PORT, 6543, 15432]  # disposable ports; 5432/5433 never are


@pytest.fixture(params=APPROVED_PORTS, ids=[f"approved-{p}" for p in APPROVED_PORTS])
def approved(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> int:
    port = int(request.param)
    monkeypatch.setattr(isolation, "_approved_db_port", port)
    assert approved_db_port() == port
    return port


def test_the_approved_port_is_accepted_and_any_other_refused(approved: int) -> None:
    check_connection_target({"host": "127.0.0.1", "port": str(approved), "dbname": "x_test"})
    for other in {approved - 1, approved + 1, LIBPQ_DEFAULT_PORT} - {approved}:
        with pytest.raises(IsolationError, match="refused port"):
            check_connection_target({"host": "127.0.0.1", "port": str(other), "dbname": "x_test"})


@pytest.mark.parametrize("port", [None, ""], ids=["absent", "empty"])
def test_an_omitted_port_is_the_libpq_default_for_every_approved_port(
    approved: int, port: str | None
) -> None:
    params = {"host": "localhost", "dbname": "x_test"}
    if port is not None:
        params["port"] = port
    assert approved != LIBPQ_DEFAULT_PORT
    with pytest.raises(IsolationError, match="refused port"):  # never "upgraded" to approved
        check_connection_target(params)


def test_the_pgport_fallback_under_every_approved_port(
    approved: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGPORT", str(approved))
    check_connection_target({"host": "localhost", "dbname": "x_test"})
    monkeypatch.setenv("PGPORT", str(approved + 1))
    with pytest.raises(IsolationError, match="refused port"):
        check_connection_target({"host": "localhost", "dbname": "x_test"})


def test_maintenance_connection_under_every_approved_port(approved: int) -> None:
    with maintenance_connection():
        check_connection_target({"host": "localhost", "port": str(approved), "dbname": "postgres"})
        with pytest.raises(IsolationError, match="refused port"):
            check_connection_target(
                {"host": "localhost", "port": str(approved + 1), "dbname": "postgres"}
            )
        with pytest.raises(IsolationError, match="refused host"):
            check_connection_target(
                {"host": "192.0.2.10", "port": str(approved), "dbname": "postgres"}
            )
    with pytest.raises(IsolationError, match="_test"):  # outside the context: name rule again
        check_connection_target({"host": "localhost", "port": str(approved), "dbname": "postgres"})


def test_names_and_hosts_are_refused_under_every_approved_port(approved: int) -> None:
    port = str(approved)
    with pytest.raises(IsolationError, match="'marketpulse'"):
        check_connection_target({"host": "localhost", "port": port, "dbname": "marketpulse"})
    with pytest.raises(IsolationError, match="refused host"):
        check_connection_target({"host": "db.example.invalid", "port": port, "dbname": "x_test"})


def test_inherited_libpq_defaults_are_removed_for_the_session() -> None:
    # PGREQUIREAUTH is the one exception: an opted-in session sets it itself (SCRAM only). The
    # autouse fixture sets it for every test here, so the session's own value was captured at
    # import, before any test ran.
    assert not [var for var in LIBPQ_ENV_VARS if var != "PGREQUIREAUTH" and var in os.environ]
    assert (REQUIRED_AUTH if REAL_DB_ENABLED else None) == REAL_PGREQUIREAUTH


# --- the disposable opt-in --------------------------------------------------------------------

COMPLETE = {
    DISPOSABLE_OPT_IN_VAR: "disposable",
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": str(DISPOSABLE_PORT),
    "POSTGRES_USER": "synthetic_user",
    "POSTGRES_PASSWORD": "synthetic-password",
    "POSTGRES_DB": "marketpulse",
    DISPOSABLE_TOKEN_VAR: SYNTHETIC_TOKEN,
}


def test_the_opt_in_needs_the_flag_and_all_five_settings() -> None:
    assert database_opt_in_problems(COMPLETE) == []
    assert set(REQUIRED_DB_VARS) == set(COMPLETE) - {DISPOSABLE_OPT_IN_VAR, DISPOSABLE_TOKEN_VAR}
    assert database_opt_in_problems({}) == [
        f"{DISPOSABLE_OPT_IN_VAR}=disposable is not set",
        "not set explicitly: " + ", ".join(REQUIRED_DB_VARS),
        f"{DISPOSABLE_TOKEN_VAR} is not set",
    ]
    for var in REQUIRED_DB_VARS:
        for value in (None, "", "  "):
            env = {k: v for k, v in COMPLETE.items() if k != var}
            if value is not None:
                env[var] = value
            assert database_opt_in_problems(env) == [f"not set explicitly: {var}"]
    for flag in ("1", "true", "yes", "Disposable", ""):
        assert database_opt_in_problems({**COMPLETE, DISPOSABLE_OPT_IN_VAR: flag}) == [
            f"{DISPOSABLE_OPT_IN_VAR}=disposable is not set"
        ]


class _EnvFileOpenedError(AssertionError):
    pass


def _refuse_env_file(path: Any) -> None:
    if Path(str(path)).name.startswith(".env"):
        raise _EnvFileOpenedError(f"tests must never open {path}")


def _install(environ: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run the session's environment set-up on `environ`; restore everything afterwards.

    Any attempt to open a file named .env* fails the test.
    """
    for name in (
        "_db_tests_enabled",
        "_db_disabled_reason",
        "_approved_db_port",
        "_db_token",
        "_db_server_verified",
    ):
        monkeypatch.setattr(isolation, name, getattr(isolation, name))
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    saved = dict(os.environ)
    real_open = builtins.open

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        _refuse_env_file(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    os.environ.clear()
    os.environ.update(environ)
    try:
        isolation._isolate_environment(tmp_path)
    finally:
        after = dict(os.environ)
        os.environ.clear()
        os.environ.update(saved)
        monkeypatch.setattr(isolation, "_last_install_environ", after, raising=False)


def test_without_any_settings_database_tests_are_disabled_and_nothing_is_filled_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install({}, monkeypatch, tmp_path)
    after = isolation._last_install_environ  # type: ignore[attr-defined]
    assert not database_tests_enabled()
    assert "MARKETPULSE_TEST_DB=disposable is not set" in isolation.database_disabled_reason()
    # No database setting was supplied or renamed: all five stay absent.
    assert not set(REQUIRED_DB_VARS) & set(after)
    with pytest.raises(IsolationError, match="disabled"):
        approved_db_port()


def test_settings_without_the_flag_do_not_enable_database_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = {k: v for k, v in COMPLETE.items() if k != DISPOSABLE_OPT_IN_VAR}
    _install(env, monkeypatch, tmp_path)
    after = isolation._last_install_environ  # type: ignore[attr-defined]
    assert not database_tests_enabled()
    assert after["POSTGRES_DB"] == "marketpulse"  # not renamed: the session is disabled
    with pytest.raises(IsolationError, match="disabled"):
        check_connection_target({"host": "127.0.0.1", "port": PORT, "dbname": "x_test"})


def test_the_complete_opt_in_enables_exactly_the_exported_disposable_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(COMPLETE, monkeypatch, tmp_path)
    after = isolation._last_install_environ  # type: ignore[attr-defined]
    assert database_tests_enabled()
    assert approved_db_port() == DISPOSABLE_PORT
    assert after["POSTGRES_DB"] == "marketpulse_test"
    assert after["PGREQUIREAUTH"] == REQUIRED_AUTH
    target = {"host": "127.0.0.1", "port": PORT, "dbname": "marketpulse_test"}
    # A new session starts with the gate closed, even though the previous state was verified.
    assert not server_verified()
    with pytest.raises(IsolationError, match="verified"):
        check_connection_target(target)
    monkeypatch.setattr(isolation, "_db_server_verified", True)  # as after the fixture's check
    check_connection_target(target)


@pytest.mark.parametrize("port", sorted(PROTECTED_DB_PORTS))
def test_opting_in_with_a_protected_port_stops_the_session(
    port: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(IsolationError, match="protected"):
        _install({**COMPLETE, "POSTGRES_PORT": str(port)}, monkeypatch, tmp_path)


def test_opting_in_with_a_remote_host_stops_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(IsolationError, match="refused host"):
        _install({**COMPLETE, "POSTGRES_HOST": "192.0.2.10"}, monkeypatch, tmp_path)


def test_a_repository_env_file_is_never_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The set-up must not open .env even when a setting is missing (the incident's path).
    `_install` makes any attempt to open a .env* file fail the test."""
    for env in ({}, {DISPOSABLE_OPT_IN_VAR: "disposable"}, COMPLETE):
        _install(dict(env), monkeypatch, tmp_path)
    assert "_PostgresConnection" not in vars(isolation)


def test_when_disabled_every_connection_is_refused_before_libpq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(isolation, "_db_tests_enabled", False)
    monkeypatch.setattr(isolation, "_db_disabled_reason", "synthetic: no opt-in")

    def must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a connection reached libpq")

    monkeypatch.setattr(psycopg.Connection, "_connect_gen", classmethod(must_not_run))
    targets = [
        {"host": "127.0.0.1", "port": PORT, "dbname": "marketpulse_test"},
        {"host": "127.0.0.1", "port": "5432", "dbname": "marketpulse_test"},
        {"host": "127.0.0.1", "port": "5433", "dbname": "marketpulse_test"},
        {"host": "localhost", "port": "5432", "dbname": "marketpulse"},  # Settings' defaults
    ]
    for target in targets:
        with pytest.raises(IsolationError, match="disabled"):
            psycopg.connect(**target, connect_timeout=1)
    engine = create_engine(f"postgresql+psycopg://u:p@127.0.0.1:{PORT}/marketpulse_test")
    try:
        with pytest.raises(IsolationError, match="disabled"):
            engine.connect()
    finally:
        engine.dispose()
    # A disabled session leaves POSTGRES_* unset, so Settings falls back to its defaults
    # (localhost:5432/marketpulse). Even an engine built from those defaults cannot connect.
    for var in REQUIRED_DB_VARS:
        monkeypatch.delenv(var, raising=False)
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (defaults.postgres_port, defaults.postgres_db) == (5432, "marketpulse")
    engine = create_engine(defaults.database_url)
    try:
        with pytest.raises(IsolationError, match="disabled"):
            engine.connect()
    finally:
        engine.dispose()


def test_database_tests_skip_without_connecting_when_not_opted_in() -> None:
    """A separate pytest session without the opt-in runs one database test file: the fixture
    skips it (and fails it under MARKETPULSE_REQUIRE_DB=1) without any connection attempt; an
    attempt would have raised IsolationError instead."""
    if os.environ.get(CHILD_SESSION_VAR) == "1":
        pytest.skip("inside a child session: it must not start another one")
    backend = Path(__file__).resolve().parents[1]
    base = {
        k: v
        for k, v in os.environ.items()
        if k not in REQUIRED_DB_VARS and k not in (DISPOSABLE_OPT_IN_VAR, "MARKETPULSE_REQUIRE_DB")
    }
    base[CHILD_SESSION_VAR] = "1"

    def child(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # fixed argv: this interpreter running pytest
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-rs",
                "tests/db/test_constraints.py",
            ],
            cwd=backend,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    skipped = child(base)
    output = skipped.stdout + skipped.stderr
    assert skipped.returncode == pytest.ExitCode.OK, output[-3000:]
    assert "skipped" in output and "passed" not in output and "error" not in output.lower()
    assert "database tests need a disposable server" in output
    assert "IsolationError" not in output

    required = child({**base, "MARKETPULSE_REQUIRE_DB": "1"})
    output = required.stdout + required.stderr
    assert required.returncode != pytest.ExitCode.OK
    assert "database tests need a disposable server" in output
    assert "IsolationError" not in output and "passed" not in output


def test_the_config_and_isolation_tests_pass_under_a_non_default_port() -> None:
    """End to end: a separate pytest session whose POSTGRES_PORT is 6543 (neither 5432 nor
    staging's 5433), started with libpq defaults inherited from the shell (which the session
    must remove), runs test_config.py and this file. No database test runs and nothing
    connects: every check here passes or refuses before connecting. (Whether the child opted in
    depends on the parent session; this file passes either way.)"""
    if os.environ.get(CHILD_SESSION_VAR) == "1":
        pytest.skip("inside the child session: it must not start another one")
    backend = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        CHILD_SESSION_VAR: "1",
        "POSTGRES_PORT": "6543",
        "PGPORT": "5432",  # inherited libpq defaults: removed by the child session's isolation
        "PGHOST": "192.0.2.10",
        "PGPASSWORD": "inherited-not-used",
    }
    result = subprocess.run(  # fixed argv: this interpreter running pytest
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_config.py",
            "tests/test_isolation.py",
            "-k",
            "not end_of_session",  # those three start sessions of their own; not needed here
        ],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout[-3000:] + result.stderr[-2000:]
    assert " passed" in result.stdout and "failed" not in result.stdout
    assert "2 skipped" in result.stdout  # the two child-starting tests, inside the child


# --- every connection path, synchronous and asynchronous ----------------------------------------
# The tests below do try to connect, so each one runs over a TRIPWIRE: `psycopg.generators.connect`
# (the last Python step before libpq) is replaced by a function that records the attempt and
# raises, and a fresh guard is installed ABOVE it. A refused attempt must raise IsolationError
# without reaching the tripwire; an allowed one reaches the tripwire, never libpq. Even a broken
# guard therefore cannot open a connection. Asynchronous connections run on a selector event loop
# (psycopg refuses Windows' default proactor loop before any guard would be exercised).


class ReachedLibpqError(Exception):
    """Raised by the tripwire: an attempt got past every guard. Not a psycopg error, so psycopg's
    attempt loop does not swallow it and move on to another host."""


@pytest.fixture
def libpq_tripwire(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every connection string that got past the guards (and stopped at the tripwire)."""
    reached: list[str] = []

    def tripwire(conninfo: str, *args: Any, **kwargs: Any) -> Any:
        reached.append(conninfo)
        raise ReachedLibpqError(conninfo)

    monkeypatch.setattr(psycopg.generators, "connect", tripwire)
    restore = isolation._guard_psycopg()  # guards above the tripwire, not above libpq
    try:
        yield reached
    finally:
        restore()


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def connect_with(mode: str, conninfo: str = "", **kwargs: Any) -> Any:
    kwargs.setdefault("connect_timeout", 1)
    if mode == "sync":
        return psycopg.Connection.connect(conninfo, **kwargs)
    return run_async(psycopg.AsyncConnection.connect(conninfo, **kwargs))


MODES = ["sync", "async"]
REFUSED_TARGETS = [
    pytest.param({"host": "192.0.2.10", "port": PORT}, "refused host", id="remote-host"),
    pytest.param({"host": "127.0.0.1", "port": "5432"}, "protected", id="production-port"),
    pytest.param({"host": "127.0.0.1", "port": "5433"}, "protected", id="staging-port"),
    pytest.param({"host": "127.0.0.1", "port": OTHER_PORT}, "refused port", id="other-port"),
    pytest.param(
        {"host": "127.0.0.1", "port": PORT, "dbname": "marketpulse"}, "_test", id="non-test-name"
    ),
    pytest.param({"service": "prod"}, "service", id="service"),
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("target", "reason"), REFUSED_TARGETS)
def test_refused_targets_never_reach_libpq(
    mode: str, target: dict[str, str], reason: str, libpq_tripwire: list[str]
) -> None:
    """T1: includes AsyncConnection.connect, which had no guard before the libpq-step guard."""
    with pytest.raises(IsolationError, match=reason):
        connect_with(mode, **{"dbname": "marketpulse_test", **target})
    assert libpq_tripwire == []


@pytest.mark.parametrize("mode", MODES)
def test_when_disabled_no_connection_reaches_libpq_in_either_mode(
    mode: str, monkeypatch: pytest.MonkeyPatch, libpq_tripwire: list[str]
) -> None:
    monkeypatch.setattr(isolation, "_db_tests_enabled", False)
    monkeypatch.setattr(isolation, "_db_disabled_reason", "synthetic: no opt-in")
    with pytest.raises(IsolationError, match="disabled"):
        connect_with(mode, host="127.0.0.1", port=PORT, dbname="marketpulse_test")
    assert libpq_tripwire == []


@pytest.mark.parametrize("mode", MODES)
def test_the_approved_target_reaches_only_the_tripwire(
    mode: str, libpq_tripwire: list[str]
) -> None:
    """T2: the guards do not over-block; the attempt stops at the tripwire, not at libpq."""
    with pytest.raises(ReachedLibpqError):
        connect_with(mode, host="127.0.0.1", port=PORT, dbname="marketpulse_test")
    assert len(libpq_tripwire) == 1
    assert "127.0.0.1" in libpq_tripwire[0] and f"port={PORT}" in libpq_tripwire[0]


def test_a_libpq_step_refusal_stops_the_attempt_loop(libpq_tripwire: list[str]) -> None:
    """T3: the first attempt is refused at the libpq step; the second, allowed, host is never
    tried, so the refusal was not collected as a connection error and retried. (Asynchronous:
    the synchronous outer guard would refuse the whole host list before any attempt.)"""
    with pytest.raises(IsolationError, match=r"refused host '192\.0\.2\.10'"):
        connect_with("async", host="192.0.2.10,127.0.0.1", port=PORT, dbname="marketpulse_test")
    assert libpq_tripwire == []


@pytest.mark.parametrize("mode", MODES)
def test_a_localhost_that_resolves_elsewhere_is_refused(
    mode: str, monkeypatch: pytest.MonkeyPatch, libpq_tripwire: list[str]
) -> None:
    """T3b: `localhost` passes the outer check by name; the libpq step checks the address it
    resolved to (a redirected hosts file, say). The fake resolver returns a reserved address."""

    def redirected(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.10", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", redirected)
    with pytest.raises(IsolationError, match=r"refused host '192\.0\.2\.10'"):
        connect_with(mode, host="localhost", port=PORT, dbname="marketpulse_test")
    assert libpq_tripwire == []


def test_a_real_localhost_resolves_to_loopback_and_reaches_only_the_tripwire(
    libpq_tripwire: list[str],
) -> None:
    """T3c: no regression for the usual `POSTGRES_HOST=localhost`."""
    with pytest.raises(ReachedLibpqError):
        connect_with("sync", host="localhost", port=PORT, dbname="marketpulse_test")
    assert len(libpq_tripwire) == 1 and "hostaddr=" in libpq_tripwire[0]


def test_connect_gen_called_directly_is_refused(libpq_tripwire: list[str]) -> None:
    """T4a: driving psycopg's connection generator directly skips Connection.connect."""
    gen = psycopg.Connection._connect_gen(f"host=192.0.2.10 port={PORT} dbname=marketpulse_test")
    with pytest.raises(IsolationError, match="refused host"):
        psycopg.waiting.wait_conn(gen, timeout=1)
    assert libpq_tripwire == []


def test_the_libpq_step_called_directly_is_refused(libpq_tripwire: list[str]) -> None:
    """T4b"""
    with pytest.raises(IsolationError, match="refused host"):
        psycopg.generators.connect(f"host=192.0.2.10 port={PORT} dbname=marketpulse_test")
    assert libpq_tripwire == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("uri", "reason"),
    [
        (f"postgresql:///marketpulse_test?host=192.0.2.10&port={PORT}", "refused host"),
        ("postgresql://[::1]:5432/marketpulse_test", "protected"),
        (f"postgresql://127.0.0.1:{PORT}/marketpulse", "_test"),
    ],
)
def test_uri_connection_strings_are_checked(
    mode: str, uri: str, reason: str, libpq_tripwire: list[str]
) -> None:
    """T4c"""
    with pytest.raises(IsolationError, match=reason):
        connect_with(mode, uri)
    assert libpq_tripwire == []


def test_sqlalchemy_async_engine_is_refused(libpq_tripwire: list[str]) -> None:
    """T5b: SQLAlchemy's async psycopg dialect calls AsyncConnection.connect. Async engines need
    greenlet: while it is not installed, SQLAlchemy 2.1 cannot even import its asyncio extension,
    so no async engine can exist; once it is, the guards must refuse. Either way nothing reaches
    libpq. (No skip: the child session above counts its skipped tests exactly.)"""
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
    except ImportError as exc:
        assert "greenlet" in str(exc)
        assert libpq_tripwire == []
        return

    async def connect() -> None:
        engine = create_async_engine(
            f"postgresql+psycopg_async://u:p@192.0.2.10:{PORT}/marketpulse_test"
        )
        try:
            async with engine.connect():
                pass
        finally:
            await engine.dispose()

    with pytest.raises(IsolationError, match="refused host"):
        run_async(connect())
    assert libpq_tripwire == []


def test_every_connection_entry_point_is_guarded() -> None:
    """T7: the session's guards are in place, and psycopg still routes every attempt of both
    connection classes through the guarded libpq step. Fails loudly if a psycopg upgrade moves
    that step, instead of silently leaving asynchronous connections unguarded."""
    assert isolation.is_guarded(psycopg.connect)
    assert isolation.is_guarded(psycopg.Connection.connect)
    assert isolation.is_guarded(psycopg.generators.connect)
    shared = BaseConnection._connect_gen.__func__  # type: ignore[attr-defined]
    assert psycopg.Connection._connect_gen.__func__ is shared  # type: ignore[attr-defined]
    assert psycopg.AsyncConnection._connect_gen.__func__ is shared  # type: ignore[attr-defined]
    assert "generators.connect(" in inspect.getsource(shared)
    # AsyncConnection.connect itself is psycopg's own: it reaches libpq only via that step.
    assert not isolation.is_guarded(psycopg.AsyncConnection.connect)


def test_restoring_the_guards_restores_every_entry_point() -> None:
    before = (psycopg.connect, psycopg.Connection.connect, psycopg.generators.connect)
    restore = isolation._guard_psycopg()
    assert psycopg.generators.connect is not before[2]
    restore()
    after = (psycopg.connect, psycopg.Connection.connect, psycopg.generators.connect)
    assert after[2] is before[2]
    assert after[0].__func__ is before[0].__func__  # type: ignore[attr-defined]
    assert after[1].__func__ is before[1].__func__  # type: ignore[attr-defined]


# --- the maintenance-database permission is confined to its context -----------------------------


def test_maintenance_permission_does_not_leak_to_other_threads() -> None:
    """T6a"""
    seen_by_other_thread: list[BaseException | None] = []

    def other_thread() -> None:
        try:
            check_database_name("postgres")
            seen_by_other_thread.append(None)
        except IsolationError as exc:
            seen_by_other_thread.append(exc)

    with maintenance_connection():
        check_database_name("postgres")
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(timeout=10)
    assert len(seen_by_other_thread) == 1
    assert isinstance(seen_by_other_thread[0], IsolationError)


def test_nested_maintenance_contexts_restore_the_outer_state() -> None:
    """T6b"""
    with maintenance_connection():
        with maintenance_connection():
            check_database_name("postgres")
        check_database_name("postgres")  # still inside the outer block
    with pytest.raises(IsolationError):
        check_database_name("postgres")


def test_maintenance_permission_is_reset_after_an_exception() -> None:
    """T6c"""
    with pytest.raises(ValueError, match="boom"), maintenance_connection():
        raise ValueError("boom")
    with pytest.raises(IsolationError):
        check_database_name("postgres")


def test_an_asyncio_task_created_inside_the_block_inherits_the_permission() -> None:
    """Documents the ContextVar limitation (see isolation._maintenance_allowed): a task copies
    the context when it is created. Only the name rule is relaxed; host and port still apply."""

    async def in_task() -> bool:
        check_database_name("postgres")
        with pytest.raises(IsolationError, match="refused host"):
            check_connection_target({"host": "192.0.2.10", "port": PORT, "dbname": "postgres"})
        return True

    with maintenance_connection():
        assert run_async(in_task())
    with pytest.raises(IsolationError):
        check_database_name("postgres")


# --- static: no connection path around the guards -----------------------------------------------
# The runtime guards cover psycopg's connect entry points and the libpq step every psycopg attempt
# goes through. What they cannot cover is kept out of the code base by this scan instead:
# - libpq called directly: psycopg.pq.PGconn.connect / connect_start (a C type: not patchable),
#   psycopg's C module (_psycopg), or a C library loaded with ctypes/cffi;
# - psycopg internals driven by hand (_connect_gen, wait_conn) or imported by name, which would
#   capture the real functions if imported before the guards are installed;
# - captured references to psycopg's connect functions (`creator=psycopg.connect`, `c = ...`),
#   which keep the unguarded function when taken before the guards are installed;
# - other PostgreSQL drivers, which never go through psycopg at all.

BACKEND_DIR = Path(__file__).resolve().parents[1]
STATIC_SCAN_DIRS = ("src", "tests", "scripts", "alembic")
# The only files allowed to use what the scan forbids, and why. Keep this list short.
STATIC_SCAN_ALLOWLIST = {
    "tests/isolation.py": "implements the guards: wraps psycopg's connect functions",
    "tests/test_isolation.py": "tests the guards by calling the internals they protect",
}
FORBIDDEN_MODULES = frozenset(
    {
        # other PostgreSQL drivers (import names)
        "asyncpg", "psycopg2", "pg8000", "aiopg", "pgdb", "pg", "postgresql",
        # psycopg's compiled implementations, which call libpq directly
        "psycopg_binary", "psycopg_c",
        # loading a C library such as libpq by hand
        "ctypes", "cffi",
    }
)  # fmt: skip
FORBIDDEN_PSYCOPG_MODULES = frozenset(
    {"psycopg.generators", "psycopg.pq", "psycopg.waiting", "psycopg._psycopg", "psycopg._cmodule"}
)
FORBIDDEN_PSYCOPG_NAMES = frozenset({"connect", "generators", "pq", "waiting", "_psycopg"})
FORBIDDEN_ATTRIBUTES = frozenset(
    {"connect_start", "PGconn", "_connect_gen", "wait_conn", "wait_conn_async", "_psycopg"}
)
FORBIDDEN_URL_DRIVERS = ("+asyncpg", "+psycopg2", "+pg8000", "+pygresql", "+aiopg")
CONNECT_OWNERS = frozenset({"Connection", "AsyncConnection"})
INSTALLED_DRIVER_DISTRIBUTIONS = (
    "asyncpg", "psycopg2", "psycopg2-binary", "pg8000", "aiopg", "PyGreSQL", "py-postgresql",
)  # fmt: skip


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def unguarded_connection_paths(source: str) -> list[str]:
    """Every use in `source` of a connection path the runtime guards cannot cover."""
    tree = ast.parse(source)
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    found: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES or any(
                    alias.name == m or alias.name.startswith(f"{m}.")
                    for m in FORBIDDEN_PSYCOPG_MODULES
                ):
                    found.append(f"line {line}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if module.split(".")[0] in FORBIDDEN_MODULES or any(
                module == m or module.startswith(f"{m}.") for m in FORBIDDEN_PSYCOPG_MODULES
            ):
                found.append(f"line {line}: from {module} import ...")
            elif module == "psycopg":
                found.extend(
                    f"line {line}: from psycopg import {alias.name}"
                    for alias in node.names
                    if alias.name in FORBIDDEN_PSYCOPG_NAMES
                )
        elif isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if node.attr in FORBIDDEN_ATTRIBUTES:
                found.append(f"line {line}: {dotted or node.attr}")
            elif node.attr == "connect" and dotted.endswith("generators.connect"):
                found.append(f"line {line}: {dotted}")
            elif (
                node.attr == "connect"
                and id(node) not in called
                and (
                    dotted == "psycopg.connect"
                    or (isinstance(node.value, ast.Attribute) and node.value.attr in CONNECT_OWNERS)
                )
            ):
                found.append(f"line {line}: reference to {dotted} (not a call)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for driver in FORBIDDEN_URL_DRIVERS:
                if driver in node.value:
                    found.append(f"line {line}: driver {driver!r} in a string")
    return found


def _scanned_files() -> list[Path]:
    return [
        path
        for directory in STATIC_SCAN_DIRS
        for path in sorted((BACKEND_DIR / directory).rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def test_no_connection_path_around_the_guards() -> None:
    """T5"""
    scanned = _scanned_files()
    assert len(scanned) > 50  # the scan really saw the code base
    rel = {path.relative_to(BACKEND_DIR).as_posix() for path in scanned}
    assert set(STATIC_SCAN_ALLOWLIST) <= rel  # no stale allowlist entries
    findings = {
        name: hits
        for path in scanned
        if (name := path.relative_to(BACKEND_DIR).as_posix()) not in STATIC_SCAN_ALLOWLIST
        if (hits := unguarded_connection_paths(path.read_text(encoding="utf-8")))
    }
    assert findings == {}


@pytest.mark.parametrize(
    "source",
    [
        "import asyncpg",
        "import psycopg2.extras",
        "from pg8000 import native",
        "import ctypes",
        "import psycopg_binary",
        "import psycopg.pq",
        "from psycopg.pq import PGconn",
        "from psycopg.generators import connect",
        "from psycopg import connect",
        "from psycopg import generators",
        "import psycopg\npsycopg.pq.PGconn.connect(b'')",
        "conn.pgconn.connect_start(b'')",
        "psycopg.Connection._connect_gen('')",
        "psycopg.waiting.wait_conn(gen)",
        "psycopg.generators.connect('')",
        "creator = psycopg.connect",
        "create_engine(url, creator=psycopg.Connection.connect)",
        "f = psycopg.AsyncConnection.connect",
        "url = 'postgresql+asyncpg://u@h/db_test'",
    ],
)
def test_the_static_scan_detects(source: str) -> None:
    """The scan itself works: each of these is a path around the guards."""
    assert unguarded_connection_paths(source)


def test_the_static_scan_accepts_ordinary_database_code() -> None:
    source = (
        "import psycopg\nfrom psycopg import sql\nfrom sqlalchemy import create_engine\n"
        "conn = psycopg.connect('dbname=x_test')\n"
        "aconn = psycopg.AsyncConnection.connect('dbname=x_test')\n"
        "engine = create_engine('postgresql+psycopg://u@localhost/x_test')\n"
    )
    assert unguarded_connection_paths(source) == []


@pytest.mark.parametrize("distribution", INSTALLED_DRIVER_DISTRIBUTIONS)
def test_no_other_postgresql_driver_is_installed(distribution: str) -> None:
    with pytest.raises(importlib.metadata.PackageNotFoundError):
        importlib.metadata.distribution(distribution)


# --- D1: the server must prove it is this session's disposable server ---------------------------
# None of these tests connects. Verification runs against FakeAdmin, which records every
# statement and runs none; connection tests run over the libpq tripwire above.

REAL_SERVER_VERIFIED_AT_IMPORT = isolation._db_server_verified


class FakeCursor:
    def __init__(self, row: tuple[int] | None) -> None:
        self.row = row

    def fetchone(self) -> tuple[int] | None:
        return self.row


class FakeAdmin:
    """Stands in for the maintenance connection. `databases` is the server's database list."""

    def __init__(self, databases: set[str], fail_on: str | None = None) -> None:
        self.databases = databases
        self.fail_on = fail_on
        self.executed: list[tuple[str, Any]] = []

    def execute(self, query: Any, params: Any = None) -> FakeCursor:
        statement = query if isinstance(query, str) else query.as_string(None)
        self.executed.append((statement, params))
        if self.fail_on and self.fail_on in statement:
            raise RuntimeError(f"synthetic failure in {self.fail_on}")
        exists = statement == MARKER_QUERY and params is not None and params[0] in self.databases
        return FakeCursor((1,) if exists else None)

    def statements(self) -> list[str]:
        return [statement for statement, _ in self.executed]


DROP_TEST_DB = 'DROP DATABASE IF EXISTS "marketpulse_test" WITH (FORCE)'
CREATE_TEST_DB = 'CREATE DATABASE "marketpulse_test"'
OTHER_SERVER_MARKER = f"{DISPOSABLE_MARKER_PREFIX}othertoken00000001"


@pytest.fixture
def unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    """The session's gate is closed, as at the start of every session."""
    monkeypatch.setattr(isolation, "_db_server_verified", False)


def marked_server() -> FakeAdmin:
    return FakeAdmin({"postgres", "template0", "template1", disposable_marker()})


def test_a_session_starts_unverified() -> None:
    assert REAL_SERVER_VERIFIED_AT_IMPORT is False


# D1-1: the token -------------------------------------------------------------------------------

MALFORMED_TOKENS = [
    "short0000",  # too short
    "UPPERCASETOKEN01",
    "a" * 41,  # too long
    "token;drop0001",
    "token with space",
    "tökentoken00001",
    " synthetictoken0001",
    "synthetictoken0001\n",
]


@pytest.mark.parametrize("token", [None, "", *MALFORMED_TOKENS])
def test_a_missing_or_malformed_token_disables_database_tests(
    token: str | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = {k: v for k, v in COMPLETE.items() if k != DISPOSABLE_TOKEN_VAR}
    if token is not None:
        env[DISPOSABLE_TOKEN_VAR] = token
    problems = database_opt_in_problems(env)
    assert len(problems) == 1 and DISPOSABLE_TOKEN_VAR in problems[0]
    if token:
        assert token not in problems[0]  # the value is never repeated
    _install(env, monkeypatch, tmp_path)
    after = isolation._last_install_environ  # type: ignore[attr-defined]
    assert not database_tests_enabled()
    assert "PGREQUIREAUTH" not in after
    if token:
        assert token not in isolation.database_disabled_reason()
    with pytest.raises(IsolationError, match="disabled"):
        check_connection_target({"host": "127.0.0.1", "port": PORT, "dbname": "marketpulse_test"})
    with pytest.raises(IsolationError, match="disabled"):
        disposable_marker()


@pytest.mark.parametrize("token", ["abcdefghijkl", "0" * 40, "ci12345678901x1"])
def test_well_formed_tokens_are_accepted(token: str) -> None:
    assert database_opt_in_problems({**COMPLETE, DISPOSABLE_TOKEN_VAR: token}) == []


# D1-2: the marker ------------------------------------------------------------------------------


def test_the_marker_is_derived_from_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    assert disposable_marker() == f"{DISPOSABLE_MARKER_PREFIX}{SYNTHETIC_TOKEN}"
    assert disposable_marker() == disposable_marker()
    monkeypatch.setattr(isolation, "_db_token", "z" * 40)  # the longest token
    assert len(disposable_marker().encode()) <= 63  # PostgreSQL's identifier limit


def test_there_is_no_marker_without_an_opted_in_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(isolation, "_db_tests_enabled", False)
    with pytest.raises(IsolationError, match="disabled"):
        disposable_marker()


# D1-3, D1-4: verifying the server --------------------------------------------------------------


@pytest.mark.parametrize(
    "databases",
    [
        pytest.param({"postgres", "template0", "template1", "marketpulse"}, id="no-marker"),
        pytest.param({"postgres", OTHER_SERVER_MARKER}, id="another-servers-marker"),
        pytest.param(set(), id="empty"),
    ],
)
def test_a_server_without_this_sessions_marker_is_refused(
    databases: set[str], monkeypatch: pytest.MonkeyPatch, unverified: None
) -> None:
    password = "synthetic-password-must-not-appear"
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PASSWORD", password)
    admin = FakeAdmin(databases)
    with pytest.raises(IsolationError, match="not this session's disposable server") as refused:
        verify_disposable_server(admin)
    message = str(refused.value)
    for secret in (SYNTHETIC_TOKEN, disposable_marker(), password):
        assert secret not in message
    assert f"127.0.0.1:{DISPOSABLE_PORT}" in message
    assert admin.executed == [(MARKER_QUERY, (disposable_marker(),))]
    assert not server_verified()


def test_a_server_with_the_marker_is_accepted_through_a_bound_parameter(unverified: None) -> None:
    admin = marked_server()
    verify_disposable_server(admin)
    assert admin.executed == [(MARKER_QUERY, (disposable_marker(),))]
    assert "%s" in MARKER_QUERY and SYNTHETIC_TOKEN not in MARKER_QUERY  # never formatted in
    assert not server_verified()  # verifying alone does not open the gate


# D1-5 to D1-7: the connection gate -------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_before_verification_no_test_database_connection_reaches_libpq(
    mode: str, unverified: None, libpq_tripwire: list[str]
) -> None:
    """D1-5"""
    with pytest.raises(IsolationError, match="verified"):
        connect_with(mode, host="127.0.0.1", port=PORT, dbname="marketpulse_test")
    engine = create_engine(f"postgresql+psycopg://u:p@127.0.0.1:{PORT}/marketpulse_test")
    try:
        with pytest.raises(IsolationError, match="verified"):
            engine.connect()
    finally:
        engine.dispose()
    assert libpq_tripwire == []


@pytest.mark.parametrize("mode", MODES)
def test_before_verification_only_the_maintenance_connection_reaches_libpq(
    mode: str, unverified: None, libpq_tripwire: list[str]
) -> None:
    """D1-6: the connection that verifies the server is the only one allowed first."""
    target = {"host": "127.0.0.1", "port": PORT}
    with pytest.raises(IsolationError, match="_test"):  # outside the maintenance context
        connect_with(mode, dbname="postgres", **target)
    with maintenance_connection(), pytest.raises(ReachedLibpqError):
        connect_with(mode, dbname="postgres", **target)
    with maintenance_connection(), pytest.raises(IsolationError, match="verified"):
        connect_with(mode, dbname="marketpulse_test", **target)  # maintenance means `postgres`
    with maintenance_connection(), pytest.raises(IsolationError, match="refused host"):
        connect_with(mode, dbname="postgres", host="192.0.2.10", port=PORT)
    assert len(libpq_tripwire) == 1 and "dbname=postgres" in libpq_tripwire[0]


@pytest.mark.parametrize("mode", MODES)
def test_after_verification_the_rules_still_apply(
    mode: str, unverified: None, libpq_tripwire: list[str]
) -> None:
    """D1-7"""
    recreate_test_database(marked_server(), "marketpulse_test")
    assert server_verified()
    with pytest.raises(ReachedLibpqError):
        connect_with(mode, host="127.0.0.1", port=PORT, dbname="marketpulse_test")
    with pytest.raises(IsolationError, match="refused host"):
        connect_with(mode, host="192.0.2.10", port=PORT, dbname="marketpulse_test")
    with pytest.raises(IsolationError, match="protected"):
        connect_with(mode, host="127.0.0.1", port="5432", dbname="marketpulse_test")
    with pytest.raises(IsolationError, match="_test"):
        connect_with(mode, host="127.0.0.1", port=PORT, dbname="marketpulse")
    assert len(libpq_tripwire) == 1


def test_installing_or_removing_the_guards_closes_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verified state can never carry over into another session (set-up) or outlive one."""
    for name in (
        "_db_tests_enabled",
        "_db_disabled_reason",
        "_approved_db_port",
        "_db_token",
        "_db_server_verified",
    ):
        monkeypatch.setattr(isolation, name, getattr(isolation, name))
    assert server_verified()  # the synthetic session
    with isolation.isolated():
        assert not server_verified()  # a new session starts closed
        monkeypatch.setattr(isolation, "_db_server_verified", True)
    assert not server_verified()  # and removing the guards closes it again


# D1-8: SCRAM only ------------------------------------------------------------------------------


def test_an_opted_in_session_requires_scram(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install({**COMPLETE, "PGREQUIREAUTH": "password"}, monkeypatch, tmp_path)  # inherited
    after = isolation._last_install_environ  # type: ignore[attr-defined]
    assert after["PGREQUIREAUTH"] == REQUIRED_AUTH


def test_a_disabled_session_sets_no_auth_requirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = {k: v for k, v in COMPLETE.items() if k != DISPOSABLE_OPT_IN_VAR}
    _install({**env, "PGREQUIREAUTH": "md5"}, monkeypatch, tmp_path)
    after = isolation._last_install_environ  # type: ignore[attr-defined]
    assert "PGREQUIREAUTH" not in after  # removed with the other inherited libpq defaults


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("auth", ["password", "md5", "none", "scram-sha-256,password"])
def test_a_weaker_require_auth_parameter_is_refused(
    mode: str, auth: str, libpq_tripwire: list[str]
) -> None:
    with pytest.raises(IsolationError, match="require_auth"):
        connect_with(
            mode, host="127.0.0.1", port=PORT, dbname="marketpulse_test", require_auth=auth
        )
    assert libpq_tripwire == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("auth", [None, "", "password"])
def test_a_removed_or_changed_pgrequireauth_is_refused(
    mode: str, auth: str | None, monkeypatch: pytest.MonkeyPatch, libpq_tripwire: list[str]
) -> None:
    if auth is None:
        monkeypatch.delenv("PGREQUIREAUTH")
    else:
        monkeypatch.setenv("PGREQUIREAUTH", auth)
    with pytest.raises(IsolationError, match="require_auth"):
        connect_with(mode, host="127.0.0.1", port=PORT, dbname="marketpulse_test")
    assert libpq_tripwire == []


# D1-9: the database fixtures verify before anything destructive --------------------------------


@pytest.mark.parametrize(
    "databases",
    [
        pytest.param({"postgres", "marketpulse", "marketpulse_test"}, id="no-marker"),
        pytest.param({"postgres", OTHER_SERVER_MARKER}, id="another-servers-marker"),
    ],
)
def test_a_missing_marker_prevents_all_destructive_sql(
    databases: set[str], unverified: None
) -> None:
    """Neither the set-up nor the teardown runs anything but the marker query on such a server,
    and the gate stays closed."""
    setup = FakeAdmin(databases)
    with pytest.raises(IsolationError, match="not this session's disposable server"):
        recreate_test_database(setup, "marketpulse_test")
    assert setup.statements() == [MARKER_QUERY]
    assert not server_verified()
    teardown = FakeAdmin(databases)
    with pytest.raises(IsolationError, match="not this session's disposable server"):
        drop_test_database(teardown, "marketpulse_test")
    assert teardown.statements() == [MARKER_QUERY]
    assert not server_verified()


def test_setup_verifies_then_drops_and_creates_then_opens_the_gate(unverified: None) -> None:
    admin = marked_server()
    recreate_test_database(admin, "marketpulse_test")
    assert admin.statements() == [MARKER_QUERY, DROP_TEST_DB, CREATE_TEST_DB]
    assert admin.executed[0][1] == (disposable_marker(),)
    assert server_verified()


def test_teardown_verifies_again_right_before_the_drop_and_closes_the_gate() -> None:
    assert server_verified()  # as after a successful set-up
    admin = marked_server()
    drop_test_database(admin, "marketpulse_test")
    assert admin.statements() == [MARKER_QUERY, DROP_TEST_DB]
    assert not server_verified()


def test_a_server_that_lost_its_marker_is_not_dropped_at_teardown() -> None:
    """Verified at set-up, but the port now leads elsewhere (a re-pointed tunnel, say)."""
    assert server_verified()
    admin = FakeAdmin({"postgres", "marketpulse_test"})
    with pytest.raises(IsolationError, match="not this session's disposable server"):
        drop_test_database(admin, "marketpulse_test")
    assert admin.statements() == [MARKER_QUERY]
    assert not server_verified()


@pytest.mark.parametrize("failing", ["DROP DATABASE", "CREATE DATABASE"])
def test_a_failure_while_recreating_leaves_the_gate_closed(failing: str, unverified: None) -> None:
    admin = FakeAdmin(marked_server().databases, fail_on=failing)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        recreate_test_database(admin, "marketpulse_test")
    assert not server_verified()


def test_a_failure_while_dropping_still_closes_the_gate() -> None:
    admin = FakeAdmin(marked_server().databases, fail_on="DROP DATABASE")
    with pytest.raises(RuntimeError, match="synthetic failure"):
        drop_test_database(admin, "marketpulse_test")
    assert not server_verified()


@pytest.mark.parametrize("name", ["marketpulse", "postgres", "marketpulse_testx", "x_test_db"])
def test_only_test_databases_are_dropped_or_created(name: str, unverified: None) -> None:
    for helper in (recreate_test_database, drop_test_database):
        admin = marked_server()
        with maintenance_connection(), pytest.raises(IsolationError, match="_test"):
            helper(admin, name)
        assert admin.executed == []  # refused before any statement, even the marker query
        assert not server_verified()


def test_the_database_fixtures_go_through_the_verified_helpers() -> None:
    """tests/db/conftest.py drops and creates only through the helpers above."""
    source = (BACKEND_DIR / "tests" / "db" / "conftest.py").read_text(encoding="utf-8")
    called = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"recreate_test_database", "drop_test_database"} <= called
    assert "DROP DATABASE" not in source.upper() and "CREATE DATABASE" not in source.upper()
    assert ".execute(" not in source.split("def test_db_name", 1)[1].split("@pytest.fixture")[0]


# --- network guard -----------------------------------------------------------------------------


def test_external_host_names_are_not_resolved() -> None:
    with pytest.raises(IsolationError, match=r"api\.eia\.gov"):
        socket.getaddrinfo("api.eia.gov", 443)


def test_external_addresses_are_refused() -> None:
    # 192.0.2.0/24 is reserved for documentation (TEST-NET-1): never a real host.
    with pytest.raises(IsolationError, match=r"192\.0\.2\.1"):
        socket.create_connection(("192.0.2.1", 443), timeout=1)
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(IsolationError),
    ):
        sock.connect_ex(("192.0.2.1", 443))


def test_http_requests_to_providers_fail_without_retrying_as_network_errors() -> None:
    # IsolationError is not an OSError, so httpx does not turn it into a retryable ConnectError.
    with httpx.Client() as client, pytest.raises(IsolationError):
        client.get("https://api.eia.gov/v2/")


def test_loopback_connections_still_work() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
        with socket.create_connection(("localhost", port), timeout=5):
            pass


# --- repository data directory ------------------------------------------------------------------


def test_fingerprint_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert data_dir_fingerprint(tmp_path / "missing") == {}


def test_fingerprint_detects_additions_modifications_and_deletions(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    kept, changed, removed = (tmp_path / "raw" / f"{n}.json.gz" for n in ("a", "b", "c"))
    for page in (kept, changed, removed):
        page.write_bytes(b"synthetic test bytes")
    before = data_dir_fingerprint(tmp_path)
    assert fingerprint_changes(before, data_dir_fingerprint(tmp_path)) == []

    changed.write_bytes(b"synthetic, changed")
    removed.unlink()
    (tmp_path / "raw" / "new.json.gz").write_bytes(b"synthetic, added")
    assert fingerprint_changes(before, data_dir_fingerprint(tmp_path)) == [
        "added: raw/new.json.gz",
        "modified: raw/b.json.gz",
        "deleted: raw/c.json.gz",
    ]


def test_fingerprint_detects_a_rewrite_that_keeps_size_and_mtime(tmp_path: Path) -> None:
    page = tmp_path / "page.json.gz"
    page.write_bytes(b"synthetic-AAAA")
    stat = page.stat()
    before = data_dir_fingerprint(tmp_path)
    page.write_bytes(b"synthetic-BBBB")  # same size
    os.utime(page, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same modification time
    assert fingerprint_changes(before, data_dir_fingerprint(tmp_path)) == ["modified: page.json.gz"]


@pytest.fixture
def repo_data_probe() -> Iterator[Path]:
    """A new, uniquely named file path in the real repository data/ directory.

    The probe never touches an existing file. Whatever the test does, the probe is removed
    afterwards, and the directory must then match its fingerprint from before the test exactly.
    `/data/*` is gitignored, so the probe can never be committed.
    """
    if not REPO_DATA_DIR.is_dir():
        pytest.skip(f"no repository data directory at {REPO_DATA_DIR}")
    probe = REPO_DATA_DIR / f".isolation-selftest-{uuid.uuid4().hex}"
    assert not probe.exists()
    before = data_dir_fingerprint()
    try:
        yield probe
    finally:
        probe.unlink(missing_ok=True)
    assert fingerprint_changes(before, data_dir_fingerprint()) == []


def test_session_guard_detects_an_added_repository_file(repo_data_probe: Path) -> None:
    before = data_dir_fingerprint()
    repo_data_probe.write_bytes(b"isolation self-test (synthetic)")
    assert fingerprint_changes(before, data_dir_fingerprint()) == [f"added: {repo_data_probe.name}"]


def test_session_guard_detects_a_modified_repository_file(repo_data_probe: Path) -> None:
    repo_data_probe.write_bytes(b"isolation self-test (synthetic)")
    stat = repo_data_probe.stat()
    before = data_dir_fingerprint()
    repo_data_probe.write_bytes(b"isolation self-test (SYNTHETIC)")  # same size
    os.utime(repo_data_probe, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same mtime
    assert fingerprint_changes(before, data_dir_fingerprint()) == [
        f"modified: {repo_data_probe.name}"
    ]


def test_session_guard_detects_a_deleted_repository_file(repo_data_probe: Path) -> None:
    repo_data_probe.write_bytes(b"isolation self-test (synthetic)")
    before = data_dir_fingerprint()
    repo_data_probe.unlink()
    assert fingerprint_changes(before, data_dir_fingerprint()) == [
        f"deleted: {repo_data_probe.name}"
    ]


# The real end-of-session check, run in a separate pytest session. Its conftest is a verbatim copy
# of tests/conftest.py, so the inner session installs the same isolation and the same
# pytest_sessionfinish hook that guards this session.
INNER_TEST = """
import os
from pathlib import Path


def test_change_repository_data() -> None:
    probe = Path(os.environ["MARKETPULSE_SELFTEST_PROBE"])
    action = os.environ["MARKETPULSE_SELFTEST_ACTION"]
    if action == "add":
        probe.write_bytes(b"isolation self-test (synthetic), added")
    elif action == "modify":
        probe.write_bytes(b"isolation self-test (synthetic), MODIFIED")
    # "none": change nothing (the control run)
"""


def run_inner_session(tmp_path: Path, probe: Path, action: str) -> subprocess.CompletedProcess[str]:
    tests_dir = Path(__file__).resolve().parent
    project = tmp_path / "inner"
    project.mkdir()
    (project / "conftest.py").write_text(
        (tests_dir / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (project / "test_inner.py").write_text(INNER_TEST, encoding="utf-8")
    (project / "pytest.ini").write_text(
        f"[pytest]\npythonpath = {tests_dir.as_posix()}\n", encoding="utf-8"
    )
    env = {
        **os.environ,  # already isolated: no credentials, a _test database, a temp data dir
        "MARKETPULSE_SELFTEST_PROBE": str(probe),
        "MARKETPULSE_SELFTEST_ACTION": action,
    }
    return subprocess.run(  # fixed argv: this interpreter running pytest
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "pytest.ini"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_end_of_session_check_passes_when_nothing_changes(
    repo_data_probe: Path, tmp_path: Path
) -> None:
    result = run_inner_session(tmp_path, repo_data_probe, "none")
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "repository data directory" not in result.stdout


def test_end_of_session_check_fails_the_session_for_an_added_file(
    repo_data_probe: Path, tmp_path: Path
) -> None:
    result = run_inner_session(tmp_path, repo_data_probe, "add")
    assert repo_data_probe.exists()  # the inner test really changed the repository data/
    # The test itself passed; the session fails only because of the end-of-session check.
    assert "1 passed" in result.stdout
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "ERROR: tests changed the repository data directory" in result.stdout
    assert f"added: {repo_data_probe.name}" in result.stdout


def test_end_of_session_check_fails_the_session_for_a_modified_file(
    repo_data_probe: Path, tmp_path: Path
) -> None:
    repo_data_probe.write_bytes(b"isolation self-test (synthetic), original")  # before the session
    result = run_inner_session(tmp_path, repo_data_probe, "modify")
    assert "1 passed" in result.stdout
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert f"modified: {repo_data_probe.name}" in result.stdout
    assert "added:" not in result.stdout and "deleted:" not in result.stdout
