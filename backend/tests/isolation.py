"""Test-environment isolation (D5.3 P0), installed for every test session by tests/conftest.py.

Tests must never reach a real provider, a real credential, the real database, or the repository
data directory. Each guard below closes one path:

- **Configuration.** `Settings` normally reads the repository `.env`, which holds real API keys,
  the real data directory, and the real database name. During tests the `.env` file is not read
  at all. Only the PostgreSQL connection settings (user, password, host, port, database) are
  copied from it, so database tests can still reach the local server.
- **Credentials.** Provider credentials (`EIA_API_KEY`, `FRED_API_KEY`, `ERCOT_*`) are removed
  from the process environment. Tests that need a key pass a synthetic one explicitly.
- **Database.** `POSTGRES_DB` becomes `<name>_test`. Every psycopg connection (direct, or through
  SQLAlchemy and Alembic) is checked before it is opened:
  - its **host** must be local: an approved host name (`localhost`), a loopback address, or a
    Unix-domain socket. Every entry of a multi-host `host` or `hostaddr` is checked, including
    the `PGHOST`/`PGHOSTADDR` fallbacks. A connection `service` is refused because its host
    cannot be checked;
  - its **port** must be the approved port: the session's configured `POSTGRES_PORT` (the
    port docker-compose.yml publishes; 5432 by default). Every entry of a comma-separated
    `port` is checked, as is the `PGPORT` fallback; an empty entry means libpq's default, 5432;
  - its **database name** must end in `_test`. The only exception is the server's `postgres`
    maintenance database, and only inside `maintenance_connection()`, which the database
    fixtures use to create and drop the test database. That exception relaxes the name check
    only; the host check still applies.
- **Data directory.** `MARKETPULSE_DATA_DIR` points to a temporary directory for the session.
  The repository `data/` directory is fingerprinted (size, mtime, SHA-256 per file) at the
  start of the session and compared at the end; any added, modified, or deleted file fails the
  session.
- **Network.** Python sockets may connect only to loopback addresses, and host names other than
  `localhost` are not resolved. Provider HTTP clients therefore fail at once instead of reaching
  the internet. PostgreSQL connections go through libpq, not Python sockets, so they are governed
  by the database guard instead.

The guards raise `IsolationError`, a `RuntimeError`, not an `OSError`, so an HTTP client cannot
mistake a blocked connection for a transient network failure and retry it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import shutil
import socket
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import psycopg
from pydantic_settings import BaseSettings, SettingsConfigDict

from marketpulse.core.config import REPO_ROOT, Settings, get_settings

TEST_DB_SUFFIX = "_test"
MAINTENANCE_DB = "postgres"
# Host names approved for test databases, besides loopback addresses and Unix-domain sockets.
APPROVED_DB_HOSTS = frozenset({"localhost"})
LIBPQ_DEFAULT_PORT = 5432  # what libpq uses when no port is given
REPO_DATA_DIR = REPO_ROOT / "data"

# Removed from the environment for the whole session.
CREDENTIAL_VARS = (
    "EIA_API_KEY",
    "FRED_API_KEY",
    "ERCOT_API_USERNAME",
    "ERCOT_API_PASSWORD",
    "ERCOT_API_SUBSCRIPTION_KEY",
)


class IsolationError(RuntimeError):
    """A test tried to leave its sandbox (network, real database, ...)."""


# --- configuration --------------------------------------------------------------------------------


class _PostgresConnection(BaseSettings):
    """The only values tests may take from the repository `.env`: how to reach PostgreSQL."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    postgres_user: str | None = None
    postgres_password: str | None = None
    postgres_db: str | None = None
    postgres_host: str | None = None
    postgres_port: str | None = None


def as_test_database(name: str) -> str:
    """`name` itself if it already ends in `_test`, otherwise `name_test`."""
    return name if name.endswith(TEST_DB_SUFFIX) else f"{name}{TEST_DB_SUFFIX}"


def _isolate_environment(data_dir: Path) -> None:
    # Environment variables take precedence over `.env`, as in Settings; nothing else is copied.
    connection = _PostgresConnection().model_dump(exclude_none=True)
    for field, value in connection.items():
        os.environ.setdefault(field.upper(), value)
    os.environ["POSTGRES_DB"] = as_test_database(
        os.environ.get("POSTGRES_DB") or str(Settings.model_fields["postgres_db"].default)
    )
    _approve_db_port(
        os.environ.get("POSTGRES_PORT") or str(Settings.model_fields["postgres_port"].default)
    )
    for var in CREDENTIAL_VARS:
        os.environ.pop(var, None)
    os.environ["MARKETPULSE_DATA_DIR"] = str(data_dir)
    # From here on, no Settings instance reads the repository `.env`.
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()


# --- database -------------------------------------------------------------------------------------

_maintenance_allowed = False
_approved_db_port: int | None = None  # set when the guards are installed


def _parse_port(value: str) -> int | None:
    try:
        port = int(value.strip())
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


def _approve_db_port(configured: str) -> None:
    global _approved_db_port
    port = _parse_port(configured)
    if port is None:
        raise IsolationError(f"POSTGRES_PORT is not a valid port: {configured!r}")
    _approved_db_port = port


def approved_db_port() -> int:
    """The only PostgreSQL port tests may connect to (the session's POSTGRES_PORT)."""
    if _approved_db_port is None:
        raise IsolationError("test isolation is not installed")
    return _approved_db_port


@contextmanager
def maintenance_connection() -> Iterator[None]:
    """Allow connections to the `postgres` maintenance database (to create/drop the test DB)."""
    global _maintenance_allowed
    previous, _maintenance_allowed = _maintenance_allowed, True
    try:
        yield
    finally:
        _maintenance_allowed = previous


def check_database_name(name: str | None) -> None:
    if name and name.endswith(TEST_DB_SUFFIX):
        return
    if name == MAINTENANCE_DB and _maintenance_allowed:
        return
    raise IsolationError(
        f"tests may only connect to databases whose name ends in {TEST_DB_SUFFIX!r}; "
        f"refused {name!r}"
    )


def check_database_host(host: str | None) -> None:
    """Refuse any PostgreSQL host that is not local."""
    if not host:  # libpq default: the local Unix-domain socket (or localhost on Windows)
        return
    if host.startswith(("/", "@")):  # Unix-domain socket directory or abstract socket
        return
    if host.lower() in APPROVED_DB_HOSTS or _is_loopback(host):
        return
    raise IsolationError(
        f"tests may only connect to a local PostgreSQL server (approved hosts "
        f"{sorted(APPROVED_DB_HOSTS)}, loopback addresses, Unix sockets); refused host {host!r}"
    )


def check_database_port(port: str | int | None) -> None:
    """Refuse any port but the approved one; empty means libpq's default port."""
    text = "" if port is None else str(port).strip()
    parsed = LIBPQ_DEFAULT_PORT if text == "" else _parse_port(text)
    if parsed != approved_db_port():
        raise IsolationError(
            f"tests may only connect to the approved PostgreSQL port {approved_db_port()} "
            f"(POSTGRES_PORT); refused port {port!r}"
        )


def _setting(params: Mapping[str, object], key: str, env: str) -> str:
    """A libpq parameter, falling back to its environment variable when absent or empty."""
    value = params.get(key)
    return str(value) if value not in (None, "") else os.environ.get(env, "")


def check_connection_target(params: Mapping[str, object]) -> None:
    """Check the database name, host(s), and port(s) libpq would use, before connecting."""
    if params.get("service") or os.environ.get("PGSERVICE"):
        raise IsolationError("tests may not connect through a PostgreSQL service definition")
    name = params.get("dbname") or os.environ.get("PGDATABASE")
    check_database_name(None if name is None else str(name))
    for key, env in (("host", "PGHOST"), ("hostaddr", "PGHOSTADDR")):
        for host in _setting(params, key, env).split(","):  # libpq accepts a list
            check_database_host(host.strip())
    for port in _setting(params, "port", "PGPORT").split(","):  # one per host, or one for all
        check_database_port(port)


def _guard_psycopg() -> Callable[[], None]:
    real_connect = psycopg.Connection.connect.__func__  # type: ignore[attr-defined]
    module_connect = psycopg.connect

    def guarded(cls: type[psycopg.Connection[Any]], /, conninfo: str = "", **kwargs: Any) -> Any:
        check_connection_target(psycopg.conninfo.conninfo_to_dict(conninfo, **kwargs))
        return real_connect(cls, conninfo, **kwargs)

    psycopg.Connection.connect = classmethod(guarded)  # type: ignore[method-assign,assignment]
    # `psycopg.connect` is bound at import time; SQLAlchemy and the fixtures look it up here.
    psycopg.connect = psycopg.Connection.connect

    def restore() -> None:
        psycopg.Connection.connect = classmethod(real_connect)  # type: ignore[method-assign,assignment]
        psycopg.connect = module_connect

    return restore


# --- network --------------------------------------------------------------------------------------


def _is_loopback(host: object) -> bool:
    if host is None:  # getaddrinfo(None, ...) means the local host
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False  # any other host name


def _check_address(family: int, address: object) -> None:
    if family == getattr(socket, "AF_UNIX", object()):
        return
    if (
        family in (socket.AF_INET, socket.AF_INET6)
        and isinstance(address, tuple)
        and _is_loopback(address[0])
    ):
        return
    raise IsolationError(
        f"network access is blocked during tests; refused connection to {address!r}"
    )


def _guard_network() -> Callable[[], None]:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        _check_address(self.family, address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check_address(self.family, address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(host):
            raise IsolationError(
                f"network access is blocked during tests; refused to resolve {host!r}"
            )
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = getaddrinfo

    def restore() -> None:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo

    return restore


# --- repository data directory --------------------------------------------------------------------


Fingerprint = dict[str, tuple[int, int, str]]


def data_dir_fingerprint(root: Path = REPO_DATA_DIR) -> Fingerprint:
    """(size, mtime_ns, SHA-256) of every file under `root`, to detect writes by tests.

    The hash catches a rewrite that keeps both the size and the modification time.
    """
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): (
            stat.st_size,
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
        for stat in (path.stat(),)
    }


def fingerprint_changes(before: Fingerprint, after: Fingerprint) -> list[str]:
    """Human-readable list of added, modified, and deleted files (empty if none)."""
    return (
        [f"added: {name}" for name in sorted(after.keys() - before.keys())]
        + [
            f"modified: {name}"
            for name in sorted(before.keys() & after.keys())
            if before[name] != after[name]
        ]
        + [f"deleted: {name}" for name in sorted(before.keys() - after.keys())]
    )


# --- install --------------------------------------------------------------------------------------


@contextmanager
def isolated() -> Iterator[Path]:
    """Install every guard; yield the temporary data directory. Undone on exit."""
    saved_env = dict(os.environ)
    saved_env_file = Settings.model_config.get("env_file")

    def restore_configuration() -> None:
        Settings.model_config["env_file"] = saved_env_file
        os.environ.clear()
        os.environ.update(saved_env)
        get_settings.cache_clear()

    with ExitStack() as undo:  # undone in reverse order
        data_dir = Path(tempfile.mkdtemp(prefix="marketpulse-test-data-"))
        undo.callback(shutil.rmtree, data_dir, ignore_errors=True)
        undo.callback(restore_configuration)
        _isolate_environment(data_dir)
        undo.callback(_guard_psycopg())
        undo.callback(_guard_network())
        yield data_dir
