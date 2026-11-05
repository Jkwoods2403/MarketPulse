"""Session-wide test isolation: no real credentials, database, data directory, or network.

See tests/isolation.py for what each guard does and why.
"""

from collections.abc import Iterator
from contextlib import ExitStack

import pytest

from isolation import (
    REPO_DATA_DIR,
    Fingerprint,
    data_dir_fingerprint,
    fingerprint_changes,
    isolated,
)
from marketpulse.core.config import get_settings

_stack = ExitStack()
_repo_data_before: Fingerprint = {}


def pytest_configure(config: pytest.Config) -> None:
    # Installed before collection, so even import-time code runs isolated.
    global _repo_data_before
    _repo_data_before = data_dir_fingerprint()
    _stack.enter_context(isolated())


def pytest_unconfigure(config: pytest.Config) -> None:
    _stack.close()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    changes = fingerprint_changes(_repo_data_before, data_dir_fingerprint())
    if changes:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                f"ERROR: tests changed the repository data directory {REPO_DATA_DIR}:",
                red=True,
            )
            for change in changes:
                reporter.write_line(f"  {change}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """No test sees settings cached by another test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
