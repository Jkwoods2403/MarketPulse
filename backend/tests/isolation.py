"""Test-environment isolation (D5.3 P0), installed for every test session by tests/conftest.py.

Tests must never reach a real provider, a real credential, the real database, or the repository
data directory. Each guard below closes one path:

- **Configuration.** `Settings` normally reads the repository `.env`, which holds real API keys,
  the real data directory, and the real database's connection settings. During tests the `.env`
  file is never read, not even for PostgreSQL settings, and no missing setting is filled in.
  Inherited libpq defaults (`PGHOST`, `PGPORT`, `PGPASSWORD`, `PGSERVICE`, ...) are removed, so
  nothing but the explicitly exported `POSTGRES_*` variables decides where a test connects. Tests
  that check `Settings` defaults clear the `POSTGRES_*` variables themselves.
- **Disposable databases only.** Database connections are allowed only when the session is
  started with `MARKETPULSE_TEST_DB=disposable`, a well-formed `MARKETPULSE_TEST_DB_TOKEN`
  (12 to 40 lowercase letters or digits), AND all five of `POSTGRES_HOST`, `POSTGRES_PORT`,
  `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` set in the process environment.
  Otherwise every connection is refused before it is opened, and the database fixtures skip (or
  fail, under `MARKETPULSE_REQUIRE_DB=1`) without connecting. Ports 5432 (production, and libpq's
  default) and 5433 (staging) are never a test server: opting in with either stops the session
  before any test runs, and the connection guard refuses them in every case. CI's disposable
  service listens on 55432.
- **The server must prove it is this session's disposable server.** A local host, an approved
  port, and a `_test` name only say where a connection goes, not what listens there (a tunnel or
  port forward to a real server passes all three). Whoever creates a disposable server creates it
  with `POSTGRES_DB=mp_disposable_<token>` and gives the session the same token. Until the
  database fixtures have confirmed, over the maintenance connection, that the server holds that
  marker database, every other connection is refused (the connection gate); nothing is dropped
  or created on a server without it, and it is checked again right before the teardown drop.
  This binds the session to one server instance; it is an assertion of where the server came
  from, not a proof against someone deliberately creating the marker on a real server.
- **SCRAM authentication only.** An opted-in session sets `PGREQUIREAUTH=scram-sha-256`, and the
  guard refuses any connection whose `require_auth` is anything else, so a server that is not the
  expected one never receives the password in cleartext or as an MD5 hash.
- **Credentials.** Provider credentials (`EIA_API_KEY`, `FRED_API_KEY`, `ERCOT_*`) are removed
  from the process environment. Tests that need a key pass a synthetic one explicitly.
- **Database.** In an opted-in session, `POSTGRES_DB` becomes `<name>_test`. Every psycopg
  connection (direct, or through SQLAlchemy and Alembic; synchronous or asynchronous) is checked
  before it is opened, twice: once at `psycopg.connect` / `Connection.connect`, with the
  parameters as given, and again at `psycopg.generators.connect`, the last Python step before
  libpq, which both `Connection` and `AsyncConnection` use for every single attempt. The second
  check sees the final target of each attempt, including the address a host name resolved to
  (`hostaddr`), and its `IsolationError` (not a psycopg error) stops psycopg's attempt loop at
  once instead of moving on to the next host. Paths that cannot be guarded at runtime (libpq
  called directly, other PostgreSQL drivers) are kept out of the code base by a static test in
  tests/test_isolation.py. The rules for every connection:
  - its **host** must be local: an approved host name (`localhost`), a loopback address, or a
    Unix-domain socket. Every entry of a multi-host `host` or `hostaddr` is checked, including
    the `PGHOST`/`PGHOSTADDR` fallbacks. A connection `service` is refused because its host
    cannot be checked;
  - its **port** must be the approved port: the session's explicitly exported `POSTGRES_PORT`,
    which may never be 5432 or 5433. Every entry of a comma-separated `port` is checked, as is
    the `PGPORT` fallback; an empty entry means libpq's default, 5432, and is therefore refused;
  - its **database name** must end in `_test`. The only exception is the server's `postgres`
    maintenance database, and only inside `maintenance_connection()`, which the database
    fixtures use to create and drop the test database. That exception relaxes the name check
    only; the host and port checks still apply. It holds only in the current context: other
    threads never see it, but an asyncio task created inside the block copies the context and
    therefore inherits it.
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
import re
import shutil
import socket
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import psycopg
import psycopg.generators
from psycopg import sql

from marketpulse.core.config import REPO_ROOT, Settings, get_settings

TEST_DB_SUFFIX = "_test"
MAINTENANCE_DB = "postgres"
# Host names approved for test databases, besides loopback addresses and Unix-domain sockets.
APPROVED_DB_HOSTS = frozenset({"localhost"})
LIBPQ_DEFAULT_PORT = 5432  # what libpq uses when no port is given
# Never a test server, whatever is configured: production (also libpq's default) and staging.
PROTECTED_DB_PORTS = frozenset({5432, 5433})
# The explicit opt-in for database tests, and every setting that must accompany it.
DISPOSABLE_OPT_IN_VAR = "MARKETPULSE_TEST_DB"
DISPOSABLE_OPT_IN_VALUE = "disposable"
REQUIRED_DB_VARS = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
)
# The disposable server's token: the server was created with POSTGRES_DB=mp_disposable_<token>.
# Not a secret (it is visible in the server's database list), but never put in messages.
DISPOSABLE_TOKEN_VAR = "MARKETPULSE_TEST_DB_TOKEN"
DISPOSABLE_MARKER_PREFIX = "mp_disposable_"
_TOKEN_FORMAT = re.compile(r"[a-z0-9]{12,40}")  # the marker stays well under 63 bytes
TOKEN_FORMAT_HELP = "12 to 40 lowercase letters or digits"
# pg_database is readable by every role. The marker is always a bound parameter.
MARKER_QUERY = "SELECT 1 FROM pg_database WHERE datname = %s"
# The only authentication tests accept: the password never leaves the client in a reusable form.
REQUIRED_AUTH = "scram-sha-256"
REPO_DATA_DIR = REPO_ROOT / "data"

# Removed from the environment for the whole session.
CREDENTIAL_VARS = (
    "EIA_API_KEY",
    "FRED_API_KEY",
    "ERCOT_API_USERNAME",
    "ERCOT_API_PASSWORD",
    "ERCOT_API_SUBSCRIPTION_KEY",
)
# libpq's environment defaults, also removed for the session: inherited from the shell, they would
# silently fill in any connection parameter a test leaves out (host, port, password, service,
# ...). Tests connect only through the explicit POSTGRES_* settings above.
LIBPQ_ENV_VARS = (
    "PGHOST",
    "PGHOSTADDR",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGPASSFILE",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGOPTIONS",
    "PGAPPNAME",
    "PGSSLMODE",
    "PGREQUIRESSL",
    "PGSSLCERT",
    "PGSSLKEY",
    "PGSSLROOTCERT",
    "PGSSLCRL",
    "PGCONNECT_TIMEOUT",
    "PGTARGETSESSIONATTRS",
    "PGLOADBALANCEHOSTS",
    "PGCHANNELBINDING",
    "PGGSSENCMODE",
    "PGREQUIREAUTH",
    "PGSYSCONFDIR",
)


class IsolationError(RuntimeError):
    """A test tried to leave its sandbox (network, real database, ...)."""


# --- configuration --------------------------------------------------------------------------------


def as_test_database(name: str) -> str:
    """`name` itself if it already ends in `_test`, otherwise `name_test`."""
    return name if name.endswith(TEST_DB_SUFFIX) else f"{name}{TEST_DB_SUFFIX}"


def database_opt_in_problems(environ: Mapping[str, str]) -> list[str]:
    """Why database tests are disabled for `environ` (empty: they are enabled)."""
    problems: list[str] = []
    if environ.get(DISPOSABLE_OPT_IN_VAR) != DISPOSABLE_OPT_IN_VALUE:
        problems.append(f"{DISPOSABLE_OPT_IN_VAR}={DISPOSABLE_OPT_IN_VALUE} is not set")
    missing = [var for var in REQUIRED_DB_VARS if not environ.get(var, "").strip()]
    if missing:
        problems.append(f"not set explicitly: {', '.join(missing)}")
    token = environ.get(DISPOSABLE_TOKEN_VAR)
    if not token:
        problems.append(f"{DISPOSABLE_TOKEN_VAR} is not set")
    elif not _TOKEN_FORMAT.fullmatch(token):  # the value itself is never repeated
        problems.append(f"{DISPOSABLE_TOKEN_VAR} is malformed (expected {TOKEN_FORMAT_HELP})")
    return problems


def _isolate_environment(data_dir: Path) -> None:
    # Connection settings come only from the process environment. The repository `.env` is never
    # read and no missing value is filled in: a missing setting disables database tests instead.
    global _db_tests_enabled, _db_disabled_reason, _approved_db_port, _db_token
    # A new session never starts verified, whatever an earlier session (or test) left behind.
    revoke_server_verification()
    _db_tests_enabled, _approved_db_port, _db_token = False, None, None
    problems = database_opt_in_problems(os.environ)
    if problems:
        _db_disabled_reason = "; ".join(problems)
    else:
        check_database_host(os.environ["POSTGRES_HOST"])
        _approve_db_port(os.environ["POSTGRES_PORT"])  # refuses 5432 and 5433
        os.environ["POSTGRES_DB"] = as_test_database(os.environ["POSTGRES_DB"])
        _db_token = os.environ[DISPOSABLE_TOKEN_VAR]
        _db_tests_enabled, _db_disabled_reason = True, ""
    # Disabled: nothing is set or renamed. Settings may then use its default database name, but
    # the connection guard refuses every connection in such a session.
    for var in CREDENTIAL_VARS + LIBPQ_ENV_VARS:
        os.environ.pop(var, None)
    if _db_tests_enabled:
        # Read by libpq for every connection (psycopg, SQLAlchemy, Alembic); also enforced by the
        # connection guard, so neither a parameter nor a changed variable can weaken it.
        os.environ["PGREQUIREAUTH"] = REQUIRED_AUTH
    os.environ["MARKETPULSE_DATA_DIR"] = str(data_dir)
    # From here on, no Settings instance reads the repository `.env`.
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()


# --- database -------------------------------------------------------------------------------------

# Set only inside `maintenance_connection()`. A ContextVar, not a global: the permission is
# confined to the current thread's context. Limitation: an asyncio task created inside the block
# copies the context at creation and therefore inherits the permission (the host and port checks
# still apply to it).
_maintenance_allowed: ContextVar[bool] = ContextVar("maintenance_allowed", default=False)
_approved_db_port: int | None = None  # set when the guards are installed with an opt-in
_db_tests_enabled = False
_db_disabled_reason = "test isolation is not installed"
_db_token: str | None = None  # the opted-in session's disposable-server token
# The connection gate: False until the database fixtures have verified the server's marker.
# Process-wide on purpose (it describes the server, for every thread). Reset whenever the
# guards are installed or removed, on any failure while verifying or (re)creating the test
# database, and after the teardown drop.
_db_server_verified = False


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
    if port in PROTECTED_DB_PORTS:
        raise IsolationError(
            f"POSTGRES_PORT={port} is a protected server (5432: production, 5433: staging); "
            "database tests run only against a disposable server on another port"
        )
    _approved_db_port = port


def database_tests_enabled() -> bool:
    """Whether this session opted in to a disposable database (see the module docstring)."""
    return _db_tests_enabled


def database_disabled_reason() -> str:
    return _db_disabled_reason


def _require_database_tests() -> None:
    if not _db_tests_enabled:
        raise IsolationError(
            "database connections are disabled in this test session "
            f"({_db_disabled_reason}); set {DISPOSABLE_OPT_IN_VAR}={DISPOSABLE_OPT_IN_VALUE} "
            "and all of POSTGRES_HOST/PORT/USER/PASSWORD/DB for a disposable server"
        )


def approved_db_port() -> int:
    """The only PostgreSQL port tests may connect to (the session's POSTGRES_PORT)."""
    _require_database_tests()
    if _approved_db_port is None:
        raise IsolationError("test isolation is not installed")
    return _approved_db_port


@contextmanager
def maintenance_connection() -> Iterator[None]:
    """Allow connections to the `postgres` maintenance database (to create/drop the test DB).

    Only in the current context (see `_maintenance_allowed`); nested use restores the outer state.
    """
    token = _maintenance_allowed.set(True)
    try:
        yield
    finally:
        _maintenance_allowed.reset(token)


def check_database_name(name: str | None) -> None:
    if name and name.endswith(TEST_DB_SUFFIX):
        return
    if name == MAINTENANCE_DB and _maintenance_allowed.get():
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
    if parsed in PROTECTED_DB_PORTS:
        raise IsolationError(
            "tests may never connect to a protected PostgreSQL port (5432: production, "
            f"5433: staging); refused port {port!r}"
        )
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
    _require_database_tests()
    if params.get("service") or os.environ.get("PGSERVICE"):
        raise IsolationError("tests may not connect through a PostgreSQL service definition")
    name = params.get("dbname") or os.environ.get("PGDATABASE")
    check_database_name(None if name is None else str(name))
    for key, env in (("host", "PGHOST"), ("hostaddr", "PGHOSTADDR")):
        for host in _setting(params, key, env).split(","):  # libpq accepts a list
            check_database_host(host.strip())
    for port in _setting(params, "port", "PGPORT").split(","):  # one per host, or one for all
        check_database_port(port)
    auth = _setting(params, "require_auth", "PGREQUIREAUTH")
    if auth != REQUIRED_AUTH:
        raise IsolationError(
            f"tests may only connect with require_auth={REQUIRED_AUTH} (PGREQUIREAUTH); "
            f"refused require_auth={auth!r}"
        )
    if not _db_server_verified and not (_maintenance_allowed.get() and name == MAINTENANCE_DB):
        raise IsolationError(
            "database connections are refused until the disposable server has been verified: "
            "only the maintenance connection that verifies it may connect first"
        )


# --- the disposable server ----------------------------------------------------------------


def server_verified() -> bool:
    """Whether the connection gate is open (the server's marker has been verified)."""
    return _db_server_verified


def revoke_server_verification() -> None:
    """Close the connection gate."""
    global _db_server_verified
    _db_server_verified = False


def disposable_marker() -> str:
    """The marker database this session's disposable server was created with. Contains the
    token: use it only as a bound query parameter, never in a message."""
    _require_database_tests()
    if _db_token is None:
        raise IsolationError("test isolation is not installed")
    return f"{DISPOSABLE_MARKER_PREFIX}{_db_token}"


def verify_disposable_server(conn: Any) -> None:
    """Refuse the connected server unless it holds this session's marker database.

    `conn` is an open (maintenance) connection: anything whose `execute(query, params)` returns
    a cursor with `fetchone()`. Only reads `pg_database`; does not open the connection gate.
    """
    row = conn.execute(MARKER_QUERY, (disposable_marker(),)).fetchone()
    if row is None:
        raise IsolationError(
            f"the PostgreSQL server at {os.environ.get('POSTGRES_HOST')}:{approved_db_port()} is "
            f"not this session's disposable server: it has no marker database for "
            f"{DISPOSABLE_TOKEN_VAR}. Database tests run only against a server created with "
            f"POSTGRES_DB={DISPOSABLE_MARKER_PREFIX}<token> for this session's token; no database "
            "was dropped or created"
        )


def _require_test_database(name: str) -> sql.Identifier:
    # Strict: the maintenance exception never applies to what is dropped or created.
    if not name.endswith(TEST_DB_SUFFIX):
        raise IsolationError(
            f"only a database whose name ends in {TEST_DB_SUFFIX!r} may be dropped or created; "
            f"refused {name!r}"
        )
    return sql.Identifier(name)


def recreate_test_database(admin: Any, name: str) -> None:
    """Verify the server, then drop and create the session's test database, then open the gate.

    If verification fails, no SQL besides the marker query runs. On any failure the gate stays
    closed.
    """
    global _db_server_verified
    database = _require_test_database(name)
    revoke_server_verification()
    try:
        verify_disposable_server(admin)
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(database))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(database))
    except BaseException:
        revoke_server_verification()
        raise
    _db_server_verified = True


def drop_test_database(admin: Any, name: str) -> None:
    """Verify the server again, then drop the session's test database. Always closes the gate."""
    try:
        database = _require_test_database(name)
        verify_disposable_server(admin)
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(database))
    finally:
        revoke_server_verification()


_GUARD_MARKER = "_marketpulse_isolation_guard"


def _mark_guarded[F: Callable[..., Any]](func: F) -> F:
    setattr(func, _GUARD_MARKER, True)
    return func


def is_guarded(func: object) -> bool:
    """Whether `func` (a function, or a bound/class method) is one of the guards below."""
    return bool(getattr(getattr(func, "__func__", func), _GUARD_MARKER, False))


def _guard_psycopg() -> Callable[[], None]:
    real_connect = psycopg.Connection.connect.__func__  # type: ignore[attr-defined]
    module_connect = psycopg.connect
    # The last Python step before libpq: `BaseConnection._connect_gen` looks it up on the
    # `psycopg.generators` module at call time, for every attempt of every `Connection` and
    # `AsyncConnection` (psycopg 3.3: _connection_base.py). Asserted by tests/test_isolation.py.
    real_libpq_connect = psycopg.generators.connect

    @_mark_guarded
    def guarded(cls: type[psycopg.Connection[Any]], /, conninfo: str = "", **kwargs: Any) -> Any:
        check_connection_target(psycopg.conninfo.conninfo_to_dict(conninfo, **kwargs))
        return real_connect(cls, conninfo, **kwargs)

    @_mark_guarded
    def guarded_libpq_connect(conninfo: str, *args: Any, **kwargs: Any) -> Any:
        # `conninfo` is one attempt's final connection string: keyword arguments are already
        # merged into it, there is one host, and a host name carries its resolved `hostaddr`.
        # IsolationError is not a psycopg error, so the attempt loop does not catch it and try
        # the next host: it propagates at once.
        check_connection_target(psycopg.conninfo.conninfo_to_dict(conninfo))
        return real_libpq_connect(conninfo, *args, **kwargs)

    psycopg.Connection.connect = classmethod(guarded)  # type: ignore[method-assign,assignment]
    # `psycopg.connect` is bound at import time; SQLAlchemy and the fixtures look it up here.
    psycopg.connect = psycopg.Connection.connect
    psycopg.generators.connect = guarded_libpq_connect

    def restore() -> None:
        psycopg.generators.connect = real_libpq_connect
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
        undo.callback(revoke_server_verification)  # never leaves a session verified
        _isolate_environment(data_dir)
        undo.callback(_guard_psycopg())
        undo.callback(_guard_network())
        yield data_dir
