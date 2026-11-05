"""The session-wide test isolation (tests/isolation.py) is really in force.

None of these tests reaches the network or a database: every guard refuses before connecting.
"""

import os
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest
from sqlalchemy import create_engine

import isolation
from isolation import (
    APPROVED_DB_HOSTS,
    CREDENTIAL_VARS,
    LIBPQ_DEFAULT_PORT,
    LIBPQ_ENV_VARS,
    REPO_DATA_DIR,
    IsolationError,
    approved_db_port,
    as_test_database,
    check_connection_target,
    check_database_host,
    check_database_name,
    check_database_port,
    data_dir_fingerprint,
    fingerprint_changes,
    maintenance_connection,
)
from marketpulse.core.config import Settings, get_settings
from marketpulse.sources.eia.client import EiaClient, EiaConfigError
from marketpulse.sources.fred.client import FredClient, FredConfigError

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
    assert get_settings().postgres_db.endswith("_test")


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
# The approved port (the session's POSTGRES_PORT) and one that is not approved.
PORT = str(approved_db_port())
OTHER_PORT = str(approved_db_port() + 1)

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
# once with no port (libpq then uses its default, 5432), so these tests hold whatever port the
# session is configured for (5432 normally, 5433 for staging).
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
    """No port means libpq's default (5432): accepted only if that is the approved port.
    With any other approved port (e.g. staging's 5433) it is refused, never silently allowed."""
    if approved_db_port() == LIBPQ_DEFAULT_PORT:
        check_connection_target(params)
    else:
        with pytest.raises(IsolationError, match="refused port"):
            check_connection_target(params)


def test_approved_port_is_the_configured_postgres_port() -> None:
    # POSTGRES_PORT may be unset (no .env, nothing exported): the Settings default applies then.
    configured = os.environ.get("POSTGRES_PORT") or Settings.model_fields["postgres_port"].default
    assert approved_db_port() == int(configured) == get_settings().postgres_port
    check_database_port(get_settings().postgres_port)  # what the database fixture checks


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
    if approved_db_port() == LIBPQ_DEFAULT_PORT:
        check_database_port(port)
    else:
        with pytest.raises(IsolationError, match="refused port"):
            check_database_port(port)


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

APPROVED_PORTS = [5432, 5433, 6543]  # libpq's default, staging, an arbitrary other port


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
    if approved == LIBPQ_DEFAULT_PORT:
        check_connection_target(params)
    else:  # never silently "upgraded" to the approved port
        with pytest.raises(IsolationError, match="refused port"):
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
    assert not [var for var in LIBPQ_ENV_VARS if var in os.environ]


# Set in the child session's environment, so the child does not start a child of its own.
CHILD_SESSION_VAR = "MARKETPULSE_TEST_CHILD_SESSION"


def test_the_config_and_isolation_tests_pass_under_a_non_default_port() -> None:
    """End to end: a separate pytest session whose POSTGRES_PORT is 6543 (neither 5432 nor
    staging's 5433), started with libpq defaults inherited from the shell (which the session
    must remove), runs test_config.py and this file. No database test runs and nothing
    connects: every check here passes or refuses before connecting."""
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
    assert "1 skipped" in result.stdout  # this test, inside the child: no further child


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
