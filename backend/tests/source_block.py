"""TEST ONLY: lift the FRED source block inside one test, to exercise code kept behind it.

FRED is in the source block list (core/source_access.py). The FRED connector and job are kept,
unreachable, and production already holds FRED rows ingested before the block. Tests still need
to (a) keep the dormant FRED client and job tests meaningful and (b) create stored FRED rows the
way they were created before the block, so that read-only verification and the API filter can
be checked against them.

- `lift_fred_block(monkeypatch)` replaces the block list *in this process only*, until that
  monkeypatch is undone, with the same list minus FRED. ERCOT stays blocked. Use a
  function-scoped `monkeypatch` or `pytest.MonkeyPatch.context()`, never a wider scope.
- `with_fred_block_lifted(build)` lifts it only for one call, e.g. constructing a FredClient
  wired to a mock transport (the client checks the block in its constructor only).
- `assert_real_block_in_force()` is checked before and after every test by the autouse
  fixture in tests/conftest.py, so a lift that outlives its test fails the run.

Use the lift only around ingestion or client construction, never around API or verification
checks: the database API's SQL filter binds the list at import time, but its per-row
`is_permitted` check follows the lifted list. Nothing outside the test process (environment,
settings, files) is touched, and production code has no equivalent: the block can only be
lifted by a reviewed code change.
"""

from collections.abc import Callable
from types import MappingProxyType

import pytest

import marketpulse.core.source_access as source_access

REAL_BLOCK_LIST = source_access.INGESTION_BLOCKED_SOURCES
REAL_REQUIRE = source_access.require_source_ingestion_permitted
REAL_REASON = source_access.ingestion_block_reason


def lift_fred_block(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "fred" in REAL_BLOCK_LIST, "FRED is expected to be blocked"
    without_fred = {code: reason for code, reason in REAL_BLOCK_LIST.items() if code != "fred"}
    monkeypatch.setattr(source_access, "INGESTION_BLOCKED_SOURCES", MappingProxyType(without_fred))


def with_fred_block_lifted[T](build: Callable[[], T]) -> T:
    """Run `build()` with the FRED block lifted; the block is back in force when it returns."""
    with pytest.MonkeyPatch.context() as patch:
        lift_fred_block(patch)
        return build()


def assert_real_block_in_force() -> None:
    """Fail unless the block list and its check functions are the real, unmodified ones."""
    problems = []
    if source_access.INGESTION_BLOCKED_SOURCES is not REAL_BLOCK_LIST:
        problems.append("INGESTION_BLOCKED_SOURCES was replaced")
    if source_access.require_source_ingestion_permitted is not REAL_REQUIRE:
        problems.append("require_source_ingestion_permitted was replaced")
    if source_access.ingestion_block_reason is not REAL_REASON:
        problems.append("ingestion_block_reason was replaced")
    if not {"ercot", "fred"} <= set(REAL_BLOCK_LIST):
        problems.append(f"the real block list lost a source: {sorted(REAL_BLOCK_LIST)}")
    if problems:
        raise AssertionError("the source block is not in force: " + "; ".join(problems))
