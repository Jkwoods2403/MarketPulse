"""Run tracking, rollback on failure, and concurrent writers, with real commits.

These tests commit across separate connections (the only way to observe run records that
survive a rollback, or two writers contending for a lock), so every table they touch is
truncated afterwards. Data is synthetic.
"""

import threading
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

import marketpulse.ingestion.observation_writer as writer_module
from marketpulse.core.versions import ObservationVersion
from marketpulse.db.models import IngestionRun, Observation, Series, Source
from marketpulse.db.session import make_session_factory
from marketpulse.ingestion.observation_writer import write_versions
from marketpulse.ingestion.runs import IngestionRunTracker, describe_error, record_series_versions
from marketpulse.sources.fred.versions import vintage_instant

T1 = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
P = date(2026, 7, 1)
A, B = vintage_instant(date(2026, 10, 29)), vintage_instant(date(2026, 11, 26))


def v(start: datetime, end: datetime | None, value: str) -> ObservationVersion:
    return ObservationVersion(P, date(2026, 9, 30), start, end, Decimal(value), "ok")


@pytest.fixture
def factory(migrated_engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield make_session_factory(migrated_engine)
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE obs.observations, ops.data_quality_issues, ops.raw_payloads, "
                "ops.ingestion_runs, ref.series, ref.sources RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def ids(factory: sessionmaker[Session]) -> tuple[int, int]:
    with factory() as s, s.begin():
        source = Source(
            code="fred_test",
            name="FRED (test)",
            homepage_url="https://x.invalid",
            license="attribution_required",
        )
        s.add(source)
        s.flush()
        series = Series(
            source_id=source.id,
            source_series_id="TEST1",
            code="test_series",
            name="Synthetic",
            category="macro",
            frequency="quarterly",
            period_convention="quarter",
            units="Index",
            original_source="Synthetic",
            reference_url="https://x.invalid/TEST1",
            max_staleness=timedelta(days=125),
        )
        s.add(series)
        s.flush()
        return source.id, series.id


def run_row(factory: sessionmaker[Session], run_id: int) -> IngestionRun:
    with factory() as s:
        row = s.get(IngestionRun, run_id)
        assert row is not None
        return row


def observations(factory: sessionmaker[Session]) -> list[Observation]:
    with factory() as s:
        return list(s.scalars(select(Observation).order_by(Observation.vintage_start)))


# --- run tracking ----------------------------------------------------------------------------


def test_successful_run_records_status_counts_and_timestamps(
    factory: sessionmaker[Session], ids: tuple[int, int]
) -> None:
    source_id, series_id = ids
    run_id, _ = record_series_versions(
        factory,
        job="test.observations",
        source_id=source_id,
        series_id=series_id,
        versions=[v(A, B, "1.0"), v(B, None, "1.1")],
        retrieved_at=T1,
        params={"series_code": "test_series"},
    )
    run = run_row(factory, run_id)
    assert run.status == "succeeded"
    assert (run.rows_fetched, run.rows_inserted, run.rows_revised, run.rows_unchanged) == (
        2,
        2,
        0,
        0,
    )
    assert run.finished_at is not None and run.finished_at >= run.started_at
    assert run.params == {"series_id": series_id, "series_code": "test_series"}
    assert run.source_id == source_id
    assert run.code_version and run.code_version.startswith("0.1.0")
    assert run.error_message is None
    assert len(observations(factory)) == 2


def test_repeat_run_counts_unchanged_rows(
    factory: sessionmaker[Session], ids: tuple[int, int]
) -> None:
    source_id, series_id = ids
    kwargs = {
        "job": "test.observations",
        "source_id": source_id,
        "series_id": series_id,
        "versions": [v(A, None, "1.0")],
    }
    record_series_versions(factory, retrieved_at=T1, **kwargs)  # type: ignore[arg-type]
    run_id, _ = record_series_versions(factory, retrieved_at=T1 + timedelta(days=1), **kwargs)  # type: ignore[arg-type]
    run = run_row(factory, run_id)
    assert (run.status, run.rows_inserted, run.rows_unchanged) == ("succeeded", 0, 1)


def test_conflict_marks_run_partial(factory: sessionmaker[Session], ids: tuple[int, int]) -> None:
    source_id, series_id = ids
    common = {
        "job": "test.observations",
        "source_id": source_id,
        "series_id": series_id,
        "retrieved_at": T1,
    }
    record_series_versions(factory, versions=[v(A, None, "1.0")], **common)  # type: ignore[arg-type]
    run_id, result = record_series_versions(factory, versions=[v(A, None, "9.9")], **common)  # type: ignore[arg-type]
    assert result.has_errors
    assert run_row(factory, run_id).status == "partial"
    assert [o.value for o in observations(factory)] == [Decimal("1.0")]


# --- failures roll back completely -----------------------------------------------------------


def test_failure_mid_write_rolls_back_everything_and_records_failure(
    factory: sessionmaker[Session], ids: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id, series_id = ids
    record_series_versions(
        factory,
        job="test.observations",
        source_id=source_id,
        series_id=series_id,
        versions=[v(A, None, "1.0")],
        retrieved_at=T1,
    )

    # Fail *after* the close of the old version has been executed, during the insert.
    def exploding_insert(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated failure during insert, api_key=SHOULD-BE-REDACTED-123")

    monkeypatch.setattr(writer_module, "insert", exploding_insert)
    with pytest.raises(RuntimeError, match="simulated failure"):
        record_series_versions(
            factory,
            job="test.observations",
            source_id=source_id,
            series_id=series_id,
            versions=[v(A, B, "1.0"), v(B, None, "1.1")],
            retrieved_at=T1 + timedelta(days=1),
        )

    (only,) = observations(factory)  # the close was rolled back: still current, unchanged
    assert (only.value, only.vintage_end, only.last_confirmed_at) == (Decimal("1.0"), None, T1)

    with factory() as s:
        failed = s.scalars(select(IngestionRun).order_by(IngestionRun.id.desc())).first()
    assert failed is not None and failed.status == "failed"
    assert failed.finished_at is not None
    assert failed.error_message is not None
    assert failed.error_message.startswith("RuntimeError: simulated failure")
    assert "SHOULD-BE-REDACTED" not in failed.error_message
    assert (failed.rows_inserted, failed.rows_revised) == (0, 0)


def test_tracker_records_failure_for_any_exception(factory: sessionmaker[Session]) -> None:
    tracker = IngestionRunTracker(factory, job="test.job")
    with pytest.raises(KeyError), tracker:
        raise KeyError("boom")
    run = run_row(factory, tracker.run_id or 0)
    assert run.status == "failed" and run.error_message == "KeyError: 'boom'"


def test_running_row_is_committed_before_work_starts(factory: sessionmaker[Session]) -> None:
    with IngestionRunTracker(factory, job="test.job") as run:
        assert run_row(factory, run.run_id or 0).status == "running"  # visible to others
    assert run_row(factory, run.run_id or 0).status == "succeeded"


def test_long_error_messages_are_truncated() -> None:
    message = describe_error(RuntimeError("x" * 10_000))
    assert len(message) == 2_000 and message.endswith("(truncated)")
    assert describe_error(RuntimeError("short")) == "RuntimeError: short"


# --- concurrency -----------------------------------------------------------------------------


def test_concurrent_writers_for_one_series_are_serialized(
    factory: sessionmaker[Session], ids: tuple[int, int]
) -> None:
    """Writer A holds the series lock; writer B must wait, then see A's committed rows."""
    _, series_id = ids
    with factory() as s, s.begin():
        run = IngestionRun(job="test.observations")
        s.add(run)
        s.flush()
        run_id = run.id

    a_holds_lock = threading.Event()
    release_a = threading.Event()
    b_done = threading.Event()
    results: dict[str, object] = {}

    def writer_a() -> None:
        with factory() as s, s.begin():
            results["a"] = write_versions(
                s,
                series_id=series_id,
                ingestion_run_id=run_id,
                versions=[v(A, None, "1.0")],
                retrieved_at=T1,
            )
            a_holds_lock.set()
            release_a.wait(timeout=10)

    def writer_b() -> None:
        a_holds_lock.wait(timeout=10)
        with factory() as s, s.begin():
            results["b"] = write_versions(
                s,
                series_id=series_id,
                ingestion_run_id=run_id,
                versions=[v(A, None, "1.0")],
                retrieved_at=T1,
            )
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

    a, b = results["a"], results["b"]
    assert (a.inserted, b.inserted, b.unchanged) == (1, 0, 1)  # type: ignore[attr-defined]
    assert len(observations(factory)) == 1  # no duplicate version


def test_writers_for_different_series_do_not_block_each_other(
    factory: sessionmaker[Session], ids: tuple[int, int]
) -> None:
    source_id, series_id = ids
    with factory() as s, s.begin():
        other = Series(
            source_id=source_id,
            source_series_id="TEST2",
            code="test_series_2",
            name="Synthetic 2",
            category="macro",
            frequency="quarterly",
            period_convention="quarter",
            units="Index",
            original_source="Synthetic",
            reference_url="https://x.invalid/TEST2",
            max_staleness=timedelta(days=125),
        )
        run = IngestionRun(job="test.observations")
        s.add_all([other, run])
        s.flush()
        other_id, run_id = other.id, run.id

    with factory() as first, first.begin():
        write_versions(
            first,
            series_id=series_id,
            ingestion_run_id=run_id,
            versions=[v(A, None, "1.0")],
            retrieved_at=T1,
        )
        # Lock on series_id is held by `first`; a second session can still write another series.
        with factory() as second, second.begin():
            second.execute(text("SET LOCAL lock_timeout = '2s'"))
            result = write_versions(
                second,
                series_id=other_id,
                ingestion_run_id=run_id,
                versions=[v(A, None, "2.0")],
                retrieved_at=T1,
            )
        assert result.inserted == 1
