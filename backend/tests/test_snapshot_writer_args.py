"""Snapshot writer pieces that need no database. All values are SYNTHETIC."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from marketpulse.db.enums import VintageBasis
from marketpulse.db.models import Series
from marketpulse.ingestion.snapshot_writer import (
    FUTURE_TOLERANCE,
    SnapshotWriteResult,
    write_snapshot,
)
from marketpulse.ingestion.snapshots import SnapshotIssue, SnapshotScope
from marketpulse.ingestion.version_apply import VintageBasisMismatchError, require_vintage_basis


class NoDatabase:
    """A session stand-in that fails on any use: argument errors must come first."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the database was used ({name}) before arguments were validated")


def call(**overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "series_id": 1,
        "ingestion_run_id": 1,
        "snapshot_at": datetime(2026, 10, 5, tzinfo=UTC),
        "scope": SnapshotScope(),
        "period_format": "YYYY-MM-DD",
        "incoming": [],
    }
    kwargs.update(overrides)
    write_snapshot(cast(Session, NoDatabase()), **kwargs)


def test_naive_snapshot_time_is_refused_before_any_database_access() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        call(snapshot_at=datetime(2026, 10, 5))  # noqa: DTZ001  (naive on purpose)


def test_unknown_period_format_is_refused_before_any_database_access() -> None:
    with pytest.raises(ValueError, match="unknown period format"):
        call(period_format="YYYY")


def test_future_tolerance_is_five_minutes() -> None:
    assert timedelta(minutes=5) == FUTURE_TOLERANCE  # approved D3 decision


@pytest.mark.parametrize(
    ("basis", "expected", "ok"),
    [
        ("first_seen", VintageBasis.FIRST_SEEN, True),
        ("source_vintage", VintageBasis.SOURCE_VINTAGE, True),
        ("source_vintage", VintageBasis.FIRST_SEEN, False),
        ("first_seen", VintageBasis.SOURCE_VINTAGE, False),
    ],
)
def test_label_guard(basis: str, expected: VintageBasis, ok: bool) -> None:
    series = Series(id=1, code="synthetic", vintage_basis=basis)
    if ok:
        require_vintage_basis(series, expected)
    else:
        with pytest.raises(VintageBasisMismatchError, match=expected.value):
            require_vintage_basis(series, expected)


def test_result_reports_errors_only_for_error_issues() -> None:
    info = SnapshotIssue(None, "revision_outside_window", "x", "info")
    error = SnapshotIssue(None, "stale_snapshot", "x", "error")
    assert not SnapshotWriteResult(issues=[info]).has_errors
    assert SnapshotWriteResult(issues=[info, error]).has_errors
