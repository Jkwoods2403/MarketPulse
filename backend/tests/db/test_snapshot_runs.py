"""Snapshot writes through the run tracker, rollback, and concurrent writers, with real commits.

Every table these tests touch is truncated afterwards (the `factory` fixture). All values are
SYNTHETIC.
"""

import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

import marketpulse.ingestion.snapshot_writer as snapshot_writer_module
from marketpulse.db.models import (
    DataQualityIssue,
    IngestionRun,
    Observation,
    RawPayload,
    Series,
    Source,
)
from marketpulse.ingestion.runs import IngestionRunTracker
from marketpulse.ingestion.snapshot_writer import SnapshotWriteResult, write_snapshot
from marketpulse.ingestion.snapshots import SnapshotObservation, SnapshotScope

FULL = SnapshotScope()
DAY = "YYYY-MM-DD"


def fri(i: int) -> date:
    return date(2020, 1, 3) + timedelta(weeks=i)


@pytest.fixture
def ids(factory: sessionmaker[Session]) -> tuple[int, int]:
    with factory() as s, s.begin():
        source = Source(
            code="eia_test",
            name="EIA (test)",
            homepage_url="https://example.invalid",
            license="public_domain",
            access_requirement="registration_required",
        )
        s.add(source)
        s.flush()
        series = Series(
            source_id=source.id,
            source_series_id="EIA.TEST",
            code="eia_weekly_test",
            name="Synthetic weekly series",
            category="energy",
            frequency="weekly",
            period_convention="week_ending",
            units="Synthetic units",
            original_source="Synthetic",
            reference_url="https://example.invalid/series",
            max_staleness=timedelta(days=14),
            vintage_basis="first_seen",
        )
        s.add(series)
        s.flush()
        return source.id, series.id


def record_page(factory: sessionmaker[Session], run_id: int, source_id: int) -> int:
    """A raw page committed in its own transaction, as the response hook does."""
    with factory() as s, s.begin():
        page = RawPayload(
            ingestion_run_id=run_id,
            source_id=source_id,
            request_url="https://example.invalid/eia/data",
            http_status=200,
            retrieved_at=datetime.now(UTC),
            file_path=f"raw/test/{run_id}_{time.monotonic_ns()}.json.gz",
            sha256="0" * 64,
            size_bytes=1,
        )
        s.add(page)
        s.flush()
        return page.id


def obs(i: int, value: str, page: int) -> SnapshotObservation:
    d = fri(i)
    return SnapshotObservation(d.isoformat(), d - timedelta(days=6), d, Decimal(value), "ok", page)


def tracked_snapshot(
    factory: sessionmaker[Session], ids: tuple[int, int], values: dict[int, str]
) -> tuple[IngestionRunTracker, SnapshotWriteResult]:
    source_id, series_id = ids
    tracker = IngestionRunTracker(factory, job="test.eia", source_id=source_id)
    with tracker as run:
        assert run.run_id is not None
        page = record_page(factory, run.run_id, source_id)
        snapshot_at = datetime.now(UTC)  # after every page was retrieved
        result = run.write_snapshot(
            series_id=series_id,
            snapshot_at=snapshot_at,
            scope=FULL,
            period_format=DAY,
            incoming=[obs(i, v, page) for i, v in values.items()],
        )
    return tracker, result


def run_row(factory: sessionmaker[Session], run_id: int | None) -> IngestionRun:
    with factory() as s:
        row = s.get(IngestionRun, run_id)
        assert row is not None
        return row


def checksum(factory: sessionmaker[Session]) -> str:
    with factory() as s:
        return str(
            s.scalar(
                text(
                    "SELECT coalesce(md5(string_agg(o::text, '|' ORDER BY o.id)), 'empty') "
                    "FROM obs.observations o"
                )
            )
        )


def observations(factory: sessionmaker[Session]) -> list[Observation]:
    with factory() as s:
        return list(s.scalars(select(Observation).order_by(Observation.id)))


# --- run tracking -------------------------------------------------------------------------------


def test_applied_snapshots_record_counts_and_succeed(
    factory: sessionmaker[Session], ids: tuple[int, int]
) -> None:
    first, r1 = tracked_snapshot(factory, ids, {0: "1", 1: "2"})
    run = run_row(factory, first.run_id)
    assert (run.status, run.rows_fetched, run.rows_inserted, run.rows_unchanged) == (
        "succeeded",
        2,
        2,
        0,
    )
    second, r2 = tracked_snapshot(factory, ids, {0: "1", 1: "3"})
    run = run_row(factory, second.run_id)
    assert (run.status, run.rows_inserted, run.rows_revised, run.rows_unchanged) == (
        "succeeded",
        1,
        1,
        1,
    )
    assert (r1.rejected, r2.rejected) == (False, False)
    current = {o.period_start: o for o in observations(factory) if o.vintage_end is None}
    assert {o.last_confirmed_run_id for o in current.values()} == {second.run_id}


def test_rejected_snapshot_commits_only_issues_and_marks_the_run_partial(
    factory: sessionmaker[Session], ids: tuple[int, int]
) -> None:
    tracked_snapshot(factory, ids, {i: "1" for i in range(50)})
    before = checksum(factory)
    tracker, result = tracked_snapshot(factory, ids, {i: "1" for i in range(1, 50)})  # 1 of 50
    assert result.rejected
    run = run_row(factory, tracker.run_id)
    assert (run.status, run.rows_inserted, run.rows_revised, run.rows_unchanged) == (
        "partial",
        0,
        0,
        0,
    )
    assert checksum(factory) == before
    with factory() as s:
        recorded = s.scalars(
            select(DataQualityIssue.check_name).where(
                DataQualityIssue.ingestion_run_id == tracker.run_id
            )
        ).all()
    assert recorded == ["vanished_limit"]


def test_failure_mid_apply_rolls_back_and_fails_the_run(
    factory: sessionmaker[Session], ids: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked_snapshot(factory, ids, {0: "1", 1: "1"})
    before = checksum(factory)

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated failure, api_key=SHOULD-BE-REDACTED-123")

    monkeypatch.setattr(snapshot_writer_module, "_track_vanished", explode)
    tracker = None
    with pytest.raises(RuntimeError, match="simulated failure"):
        tracker, _ = tracked_snapshot(factory, ids, {0: "2", 1: "1"})
    assert checksum(factory) == before  # the close and insert were rolled back
    with factory() as s:
        failed = s.scalars(select(IngestionRun).order_by(IngestionRun.id.desc())).first()
    assert tracker is None and failed is not None
    assert failed.status == "failed"
    assert failed.error_message is not None
    assert "SHOULD-BE-REDACTED" not in failed.error_message


def issue_state(factory: sessionmaker[Session]) -> list[tuple[object, ...]]:
    with factory() as s:
        return [
            tuple(row)
            for row in s.execute(
                select(
                    DataQualityIssue.id,
                    DataQualityIssue.check_name,
                    DataQualityIssue.period_start,
                    DataQualityIssue.occurrences,
                    DataQualityIssue.last_detected_at,
                    DataQualityIssue.last_detected_run_id,
                    DataQualityIssue.resolved_at,
                    DataQualityIssue.resolved_by_run_id,
                ).order_by(DataQualityIssue.id)
            )
        ]


@pytest.mark.parametrize("stage", ["_track_vanished", "_resolve_vanished"])
def test_failure_during_vanished_issue_processing_rolls_back_everything(
    factory: sessionmaker[Session],
    ids: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    every = {i: "1" for i in range(200)}
    tracked_snapshot(factory, ids, every)
    tracked_snapshot(factory, ids, {i: "1" for i in range(200) if i != 7})  # opens period 7
    rows_before, issues_before = checksum(factory), issue_state(factory)
    assert [(i[1], i[3], i[6]) for i in issues_before] == [("vanished_period", 1, None)]

    # This snapshot revises period 0, reports period 7 again (resolution), and loses period 8
    # (a new vanished issue: 1 of 200 is within the limit). The real stage runs first, so its
    # SQL has executed when the failure is raised.
    original = getattr(snapshot_writer_module, stage)

    executed: dict[str, object] = {}

    def run_then_fail(*args: object, **kwargs: object) -> object:
        outcome = original(*args, **kwargs)
        executed["outcome"], executed["result"] = outcome, args[4]
        raise RuntimeError(f"simulated failure after {stage}")

    monkeypatch.setattr(snapshot_writer_module, stage, run_then_fail)
    values = {i: ("2" if i == 0 else "1") for i in range(200) if i != 8}
    with pytest.raises(RuntimeError, match="simulated failure"):
        tracked_snapshot(factory, ids, values)

    # The stage really wrote before failing: the upsert opened period 8's issue, or the
    # resolution resolved period 7's issue.
    if stage == "_track_vanished":
        assert executed["result"].vanished_opened == 1  # type: ignore[attr-defined]
    else:
        assert executed["outcome"] == 1
    assert checksum(factory) == rows_before  # the close, confirms, and insert were rolled back
    assert issue_state(factory) == issues_before  # no new issue, update, or resolution kept
    with factory() as s:
        failed = s.scalars(select(IngestionRun).order_by(IngestionRun.id.desc())).first()
    assert failed is not None and failed.status == "failed"
    assert (failed.rows_inserted, failed.rows_revised, failed.rows_unchanged) == (0, 0, 0)
    assert failed.error_message == f"RuntimeError: simulated failure after {stage}"


# --- concurrency --------------------------------------------------------------------------------


def _start_run(factory: sessionmaker[Session], source_id: int) -> tuple[int, int]:
    with factory() as s, s.begin():
        run = IngestionRun(job="test.eia", source_id=source_id)
        s.add(run)
        s.flush()
        run_id = run.id
    return run_id, record_page(factory, run_id, source_id)


def test_concurrent_snapshot_writers_are_serialized(
    factory: sessionmaker[Session], ids: tuple[int, int]
) -> None:
    """Writer A holds the series lock; B must wait, then plan against A's committed rows."""
    source_id, series_id = ids
    run_a, page_a = _start_run(factory, source_id)
    run_b, page_b = _start_run(factory, source_id)
    at_a = datetime.now(UTC)
    at_b = at_a + timedelta(milliseconds=1)

    a_holds_lock = threading.Event()
    release_a = threading.Event()
    b_done = threading.Event()
    results: dict[str, SnapshotWriteResult] = {}

    def write(run_id: int, page: int, at: datetime, s: Session) -> SnapshotWriteResult:
        return write_snapshot(
            s,
            series_id=series_id,
            ingestion_run_id=run_id,
            snapshot_at=at,
            scope=FULL,
            period_format=DAY,
            incoming=[obs(0, "1", page)],
        )

    def writer_a() -> None:
        with factory() as s, s.begin():
            results["a"] = write(run_a, page_a, at_a, s)
            a_holds_lock.set()
            release_a.wait(timeout=10)

    def writer_b() -> None:
        a_holds_lock.wait(timeout=10)
        with factory() as s, s.begin():
            results["b"] = write(run_b, page_b, at_b, s)
        b_done.set()

    threads = [threading.Thread(target=writer_a), threading.Thread(target=writer_b)]
    for thread in threads:
        thread.start()
    assert a_holds_lock.wait(timeout=10)
    time.sleep(0.5)
    assert not b_done.is_set(), "writer B must block while A holds the series lock"
    release_a.set()
    for thread in threads:
        thread.join(timeout=10)
    assert b_done.is_set()

    assert (results["a"].inserted, results["b"].inserted, results["b"].confirmed) == (1, 0, 1)
    (only,) = observations(factory)  # B saw A's row: no duplicate version
    assert (only.last_confirmed_at, only.last_confirmed_run_id) == (at_b, run_b)


def test_older_concurrent_snapshot_is_rejected_after_the_newer_commits(
    factory: sessionmaker[Session], ids: tuple[int, int]
) -> None:
    source_id, series_id = ids
    run_old, page_old = _start_run(factory, source_id)
    run_new, page_new = _start_run(factory, source_id)
    at_old = datetime.now(UTC)
    at_new = at_old + timedelta(seconds=1)
    for run_id, page, at, value in (
        (run_new, page_new, at_new, "2"),
        (run_old, page_old, at_old, "1"),
    ):
        with factory() as s, s.begin():
            s.execute(text("SET LOCAL lock_timeout = '5s'"))
            result = write_snapshot(
                s,
                series_id=series_id,
                ingestion_run_id=run_id,
                snapshot_at=at,
                scope=FULL,
                period_format=DAY,
                incoming=[obs(0, value, page)],
            )
    assert result.rejected  # the older one, applied second
    assert [i.check_name for i in result.issues] == ["stale_snapshot", "conflicting_snapshot"]
    (only,) = observations(factory)
    assert (only.value, only.vintage_start) == (Decimal("2"), at_new)
