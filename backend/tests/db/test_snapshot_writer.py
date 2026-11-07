"""write_snapshot against PostgreSQL, inside one rolled-back test transaction. All values are
SYNTHETIC (no real EIA data)."""

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

import marketpulse.ingestion.snapshot_writer as snapshot_writer_module
from marketpulse.core.versions import ObservationVersion
from marketpulse.db.models import (
    DataQualityIssue,
    IngestionRun,
    Observation,
    RawPayload,
    Series,
    Source,
)
from marketpulse.ingestion.observation_writer import write_versions
from marketpulse.ingestion.snapshot_writer import (
    ProvenanceError,
    SnapshotTimeError,
    SnapshotWriteResult,
    write_snapshot,
)
from marketpulse.ingestion.snapshots import SnapshotObservation, SnapshotScope
from marketpulse.ingestion.version_apply import (
    OBSERVATION_LOCK_NAMESPACE,
    VintageBasisMismatchError,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)  # runs in these tests start here
T1 = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
T2, T3, T4, T5 = (T1 + timedelta(days=n) for n in (1, 2, 3, 4))
FULL = SnapshotScope()
DAY = "YYYY-MM-DD"


def fri(i: int) -> date:
    return date(2020, 1, 3) + timedelta(weeks=i)


def make_source(session: Session, code: str) -> int:
    source = Source(
        code=code,
        name=f"{code} (test)",
        homepage_url="https://example.invalid",
        license="public_domain",
        access_requirement="registration_required",
    )
    session.add(source)
    session.flush()
    return source.id


def make_series(session: Session, source_id: int, code: str, vintage_basis: str) -> int:
    series = Series(
        source_id=source_id,
        source_series_id=code.upper(),
        code=code,
        name="Synthetic weekly series",
        category="energy",
        frequency="weekly",
        period_convention="week_ending",
        units="Synthetic units",
        original_source="Synthetic",
        reference_url="https://example.invalid/series",
        max_staleness=timedelta(days=14),
        vintage_basis=vintage_basis,
    )
    session.add(series)
    session.flush()
    return series.id


@pytest.fixture
def source_id(session: Session) -> int:
    return make_source(session, "eia_test")


@pytest.fixture
def series_id(session: Session, source_id: int) -> int:
    return make_series(session, source_id, "eia_weekly_test", "first_seen")


def new_run(session: Session, *, started: datetime = T0, status: str = "running") -> int:
    run = IngestionRun(job="test.eia", started_at=started, status=status)
    session.add(run)
    session.flush()
    return run.id


def new_page(session: Session, run_id: int, source_id: int, *, retrieved: datetime) -> int:
    page = RawPayload(
        ingestion_run_id=run_id,
        source_id=source_id,
        request_url="https://example.invalid/eia/data",
        http_status=200,
        retrieved_at=retrieved,
        file_path=f"raw/test/{run_id}_{retrieved:%Y%m%dT%H%M%S%f}.json.gz",
        sha256="0" * 64,
        size_bytes=1,
    )
    session.add(page)
    session.flush()
    return page.id


def obs(i: int, value: str | None, page: int | None) -> SnapshotObservation:
    d = fri(i)
    return SnapshotObservation(
        source_period=d.isoformat(),
        period_start=d - timedelta(days=6),
        period_end=d,
        value=None if value is None else Decimal(value),
        status="missing_at_source" if value is None else "ok",
        raw_payload_id=page,
    )


def snap(
    session: Session,
    series_id: int,
    source_id: int,
    values: Mapping[int, str | None],
    at: datetime,
    *,
    scope: SnapshotScope = FULL,
    window: str | None = None,
) -> tuple[int, SnapshotWriteResult]:
    """Write one snapshot in a new run whose single page was retrieved a minute before `at`."""
    run = new_run(session)
    page = new_page(session, run, source_id, retrieved=at - timedelta(minutes=1))
    result = write_snapshot(
        session,
        series_id=series_id,
        ingestion_run_id=run,
        snapshot_at=at,
        scope=scope,
        period_format=DAY,
        incoming=[obs(i, v, page) for i, v in values.items()],
        incremental_window_start=window,
    )
    return run, result


def rows(session: Session, series_id: int) -> list[Observation]:
    session.expire_all()
    return list(
        session.scalars(
            select(Observation)
            .where(Observation.series_id == series_id)
            .order_by(Observation.period_start, Observation.vintage_start)
        )
    )


def checksum(session: Session, series_id: int) -> str:
    """Every column of every observation row of the series."""
    session.flush()
    return str(
        session.scalar(
            text(
                "SELECT coalesce(md5(string_agg(o::text, '|' ORDER BY o.id)), 'empty') "
                "FROM obs.observations o WHERE o.series_id = :s"
            ),
            {"s": series_id},
        )
    )


def issues(session: Session, series_id: int, check: str | None = None) -> list[DataQualityIssue]:
    session.expire_all()
    query = select(DataQualityIssue).where(DataQualityIssue.series_id == series_id)
    if check is not None:
        query = query.where(DataQualityIssue.check_name == check)
    return list(session.scalars(query.order_by(DataQualityIssue.id)))


def as_of(session: Session, series_id: int, period: date, instant: datetime) -> Decimal | None:
    return session.scalar(
        text(
            "SELECT value FROM obs.observations WHERE series_id = :s AND period_start = :p "
            "AND vintage_start <= :t AND (vintage_end IS NULL OR vintage_end > :t)"
        ),
        {"s": series_id, "p": period, "t": instant},
    )


def week(i: int) -> date:
    return fri(i) - timedelta(days=6)


# --- insert / confirm / revise ------------------------------------------------------------------


def test_first_snapshot_inserts_versions_with_provenance_at_snapshot_time(
    session: Session, series_id: int, source_id: int
) -> None:
    run, result = snap(session, series_id, source_id, {0: "1.5", 1: None}, T1)
    assert not result.rejected
    assert (result.fetched, result.inserted, result.new_periods, result.confirmed) == (2, 2, 2, 0)
    page = session.scalar(select(RawPayload.id).where(RawPayload.ingestion_run_id == run))
    first, second = rows(session, series_id)
    for row in (first, second):
        assert (row.vintage_start, row.vintage_end) == (T1, None)
        assert (row.first_retrieved_at, row.last_confirmed_at) == (T1, T1)
        assert (row.ingestion_run_id, row.last_confirmed_run_id, row.raw_payload_id) == (
            run,
            run,
            page,
        )
    assert (first.source_period, first.value, first.status) == (
        fri(0).isoformat(),
        Decimal("1.5"),
        "ok",
    )
    assert (second.value, second.status) == (None, "missing_at_source")
    assert issues(session, series_id) == []


def test_identical_snapshot_only_confirms(session: Session, series_id: int, source_id: int) -> None:
    run1, _ = snap(session, series_id, source_id, {0: "1.5", 1: "2"}, T1)
    before = [(r.id, r.value, r.vintage_start, r.raw_payload_id) for r in rows(session, series_id)]
    run2, result = snap(session, series_id, source_id, {0: "1.5", 1: "2"}, T2)
    assert (result.inserted, result.revised, result.confirmed) == (0, 0, 2)
    after = rows(session, series_id)
    assert [(r.id, r.value, r.vintage_start, r.raw_payload_id) for r in after] == before
    for row in after:
        assert (row.last_confirmed_at, row.last_confirmed_run_id) == (T2, run2)
        assert (row.first_retrieved_at, row.ingestion_run_id, row.vintage_end) == (T1, run1, None)


def test_revision_closes_without_confirming_and_inserts_a_new_version(
    session: Session, series_id: int, source_id: int
) -> None:
    run1, _ = snap(session, series_id, source_id, {0: "1.5", 1: "2"}, T1)
    run2, result = snap(session, series_id, source_id, {0: "1.6", 1: "2"}, T2)
    assert (result.inserted, result.new_periods, result.revised, result.confirmed) == (1, 0, 1, 1)
    old, new, other = rows(session, series_id)
    # The closed version only gained vintage_end; a replaced value is not "confirmed".
    assert (old.value, old.vintage_start, old.vintage_end) == (Decimal("1.5"), T1, T2)
    assert (old.last_confirmed_at, old.last_confirmed_run_id) == (T1, run1)
    assert (new.value, new.vintage_start, new.vintage_end, new.ingestion_run_id) == (
        Decimal("1.6"),
        T2,
        None,
        run2,
    )
    assert other.last_confirmed_at == T2
    # Point in time: the old value until T2, the new value from T2.
    assert as_of(session, series_id, week(0), T2 - timedelta(seconds=1)) == Decimal("1.5")
    assert as_of(session, series_id, week(0), T2) == Decimal("1.6")


@pytest.mark.parametrize(("before", "after"), [("1.5", None), (None, "1.5")])
def test_null_transitions_create_versions(
    session: Session, series_id: int, source_id: int, before: str | None, after: str | None
) -> None:
    snap(session, series_id, source_id, {0: before}, T1)
    _, result = snap(session, series_id, source_id, {0: after}, T2)
    assert result.revised == 1
    old, new = rows(session, series_id)
    assert (old.status, new.status) == (
        "ok" if before else "missing_at_source",
        "ok" if after else "missing_at_source",
    )


def test_null_to_null_is_confirmed(session: Session, series_id: int, source_id: int) -> None:
    snap(session, series_id, source_id, {0: None}, T1)
    _, result = snap(session, series_id, source_id, {0: None}, T2)
    assert (result.inserted, result.confirmed) == (0, 1)


def test_values_round_trip_exactly_and_compare_as_decimals(
    session: Session, series_id: int, source_id: int
) -> None:
    long = "123456789012345678901234567890.123456789012345"
    snap(session, series_id, source_id, {0: long, 1: "1.0", 2: "0.1"}, T1)
    _, result = snap(
        session, series_id, source_id, {0: long, 1: "1.00", 2: "0.10000000000000001"}, T2
    )
    assert (result.confirmed, result.revised) == (2, 1)  # "1.00" == "1.0"; 1e-17 is a change
    by_period = {}
    for row in rows(session, series_id):
        by_period.setdefault(row.period_start, []).append(row)
    assert str(by_period[week(0)][0].value) == long  # no float rounding anywhere
    assert [str(r.value) for r in by_period[week(1)]] == ["1.0"]  # stored text kept
    assert [str(r.value) for r in by_period[week(2)]] == ["0.1", "0.10000000000000001"]


def test_revision_outside_window_is_recorded_as_info(
    session: Session, series_id: int, source_id: int
) -> None:
    snap(session, series_id, source_id, {0: "1", 1: "1"}, T1)
    _, result = snap(session, series_id, source_id, {0: "2", 1: "1"}, T2, window=fri(1).isoformat())
    assert not result.rejected
    (info,) = issues(session, series_id)
    assert (info.check_name, info.severity, info.period_start) == (
        "revision_outside_window",
        "info",
        week(0),
    )


def test_existing_rows_are_never_rewritten(
    session: Session, series_id: int, source_id: int
) -> None:
    """Append-only: across revisions and confirmations, pre-existing rows keep every
    immutable column; only vintage_end (once) and confirmation fields change."""
    snap(session, series_id, source_id, {i: "1" for i in range(5)}, T1)
    immutable = [
        "id",
        "period_start",
        "period_end",
        "value",
        "status",
        "vintage_start",
        "first_retrieved_at",
        "ingestion_run_id",
        "source_period",
        "raw_payload_id",
    ]

    def frozen() -> dict[int, tuple[object, ...]]:
        return {r.id: tuple(getattr(r, c) for c in immutable) for r in rows(session, series_id)}

    start = frozen()
    snap(session, series_id, source_id, {0: "2", 1: "1", 2: "3", 3: "1", 4: "1"}, T2)
    snap(session, series_id, source_id, {0: "2", 1: "4", 2: "3", 3: "1", 4: "1"}, T3)
    end = frozen()
    assert all(end[i] == start[i] for i in start)  # nothing rewritten
    assert len(end) == len(start) + 3  # three revisions appended


# --- rejected snapshots change nothing ----------------------------------------------------------


def test_rejected_snapshot_changes_no_observations_or_tracked_issues(
    session: Session, series_id: int, source_id: int
) -> None:
    # 200 stored periods; one vanishes (1 of 200 is within the limit) and opens a tracked issue.
    snap(session, series_id, source_id, {i: "1" for i in range(200)}, T1)
    snap(session, series_id, source_id, {i: "1" for i in range(200) if i != 7}, T2)
    before, open_before = checksum(session, series_id), issues(session, series_id)
    assert [i.check_name for i in open_before] == ["vanished_period"]
    tracked = [(i.id, i.occurrences, i.last_detected_at) for i in open_before]

    # Three more vanish (4 of 200 > 1%): rejected.
    _, result = snap(
        session, series_id, source_id, {i: "2" for i in range(200) if i not in (7, 8, 9, 10)}, T3
    )
    assert result.rejected
    assert [i.check_name for i in result.issues] == ["vanished_limit"]
    assert (result.inserted, result.revised, result.confirmed, result.vanished) == (0, 0, 0, 0)
    assert result.vanished_detected == 4  # reported although nothing was applied
    assert (result.vanished_opened, result.vanished_updated) == (0, 0)
    assert checksum(session, series_id) == before
    after = issues(session, series_id)
    assert [(i.id, i.occurrences, i.last_detected_at) for i in after[:1]] == tracked
    assert [(i.check_name, i.severity) for i in after[1:]] == [("vanished_limit", "error")]


def test_period_end_conflict_rejects_without_changes(
    session: Session, series_id: int, source_id: int
) -> None:
    snap(session, series_id, source_id, {0: "1", 1: "1"}, T1)
    before = checksum(session, series_id)
    run = new_run(session)
    page = new_page(session, run, source_id, retrieved=T2 - timedelta(minutes=1))
    moved = SnapshotObservation(
        fri(1).isoformat(), week(1), fri(1) + timedelta(days=1), Decimal("1"), "ok", page
    )
    result = write_snapshot(
        session,
        series_id=series_id,
        ingestion_run_id=run,
        snapshot_at=T2,
        scope=FULL,
        period_format=DAY,
        incoming=[obs(0, "9", page), moved],  # period 0 would be a valid revision
    )
    assert result.rejected and [i.check_name for i in result.issues] == ["period_end_conflict"]
    assert checksum(session, series_id) == before


def test_missing_provenance_rejects_without_changes(
    session: Session, series_id: int, source_id: int
) -> None:
    run = new_run(session)
    result = write_snapshot(
        session,
        series_id=series_id,
        ingestion_run_id=run,
        snapshot_at=T1,
        scope=FULL,
        period_format=DAY,
        incoming=[obs(0, "1", None)],
    )
    assert result.rejected and [i.check_name for i in result.issues] == ["missing_provenance"]
    assert rows(session, series_id) == []


def test_exception_mid_apply_rolls_back_everything(
    session: Session, series_id: int, source_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap(session, series_id, source_id, {0: "1", 1: "1"}, T1)
    before = checksum(session, series_id)

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated failure after closes, confirms, and inserts")

    monkeypatch.setattr(snapshot_writer_module, "_track_vanished", explode)
    with pytest.raises(RuntimeError, match="simulated failure"), session.begin_nested():
        snap(session, series_id, source_id, {0: "2", 1: "1"}, T2)
    assert checksum(session, series_id) == before


# --- stale and duplicate snapshots --------------------------------------------------------------


def test_duplicate_application_is_rejected_as_stale(
    session: Session, series_id: int, source_id: int
) -> None:
    snap(session, series_id, source_id, {0: "1"}, T1)
    snap(session, series_id, source_id, {0: "1"}, T2)
    before = checksum(session, series_id)
    _, again = snap(session, series_id, source_id, {0: "1"}, T2)  # same snapshot, same instant
    assert again.rejected and [i.check_name for i in again.issues] == ["stale_snapshot"]
    _, conflicting = snap(session, series_id, source_id, {0: "5"}, T2)
    assert [i.check_name for i in conflicting.issues] == ["stale_snapshot", "conflicting_snapshot"]
    assert checksum(session, series_id) == before


def test_older_snapshot_is_rejected_as_stale(
    session: Session, series_id: int, source_id: int
) -> None:
    snap(session, series_id, source_id, {0: "1"}, T2)
    before = checksum(session, series_id)
    _, result = snap(session, series_id, source_id, {0: "2"}, T1)
    assert result.rejected and "stale_snapshot" in [i.check_name for i in result.issues]
    assert checksum(session, series_id) == before


def test_database_stale_guard_covers_closed_versions(
    session: Session, series_id: int, source_id: int
) -> None:
    """Only a closed version (no current row for its period) is later than the snapshot; the
    planner only sees current versions, so the database guard must catch it."""
    run = new_run(session)
    session.add(
        Observation(
            series_id=series_id,
            period_start=week(5),
            period_end=fri(5),
            value=Decimal("1"),
            status="ok",
            vintage_start=T1,
            vintage_end=T3,
            first_retrieved_at=T1,
            last_confirmed_at=T1,
            ingestion_run_id=run,
            source_period=fri(5).isoformat(),
        )
    )
    session.flush()
    before = checksum(session, series_id)
    _, result = snap(session, series_id, source_id, {0: "1"}, T2)
    assert result.rejected
    assert [i.check_name for i in result.issues] == ["stale_snapshot"]
    assert checksum(session, series_id) == before


# --- time and provenance guards -----------------------------------------------------------------


def test_snapshot_time_far_ahead_of_the_database_clock_is_refused(
    session: Session, series_id: int, source_id: int
) -> None:
    run = new_run(session)
    future = datetime.now(UTC) + timedelta(minutes=10)
    page = new_page(session, run, source_id, retrieved=T1)
    with pytest.raises(SnapshotTimeError, match="ahead of the database clock"):
        write_snapshot(
            session,
            series_id=series_id,
            ingestion_run_id=run,
            snapshot_at=future,
            scope=FULL,
            period_format=DAY,
            incoming=[obs(0, "1", page)],
        )


def test_snapshot_time_within_the_tolerance_is_accepted(
    session: Session, series_id: int, source_id: int
) -> None:
    at = datetime.now(UTC) + timedelta(minutes=1)
    _, result = snap(session, series_id, source_id, {0: "1"}, at)
    assert not result.rejected and result.inserted == 1


def test_snapshot_time_before_the_run_started_is_refused(
    session: Session, series_id: int, source_id: int
) -> None:
    run = new_run(session, started=T2)
    page = new_page(session, run, source_id, retrieved=T2)
    with pytest.raises(SnapshotTimeError, match="fresh snapshot time"):
        write_snapshot(
            session,
            series_id=series_id,
            ingestion_run_id=run,
            snapshot_at=T1,  # e.g. reused from an earlier attempt
            scope=FULL,
            period_format=DAY,
            incoming=[obs(0, "1", page)],
        )


def test_snapshot_time_before_a_page_was_retrieved_is_refused(
    session: Session, series_id: int, source_id: int
) -> None:
    run = new_run(session)
    early = new_page(session, run, source_id, retrieved=T1 - timedelta(minutes=2))
    late = new_page(session, run, source_id, retrieved=T1 + timedelta(seconds=1))
    with pytest.raises(SnapshotTimeError, match="after every page"):
        write_snapshot(
            session,
            series_id=series_id,
            ingestion_run_id=run,
            snapshot_at=T1,
            scope=FULL,
            period_format=DAY,
            incoming=[obs(0, "1", early), obs(1, "1", late)],
        )
    assert rows(session, series_id) == []


@pytest.mark.parametrize("problem", ["other_run", "other_source", "missing"])
def test_raw_payload_must_belong_to_this_run_and_source(
    session: Session, series_id: int, source_id: int, problem: str
) -> None:
    run = new_run(session)
    if problem == "other_run":
        page = new_page(session, new_run(session), source_id, retrieved=T0)
    elif problem == "other_source":
        page = new_page(session, run, make_source(session, "other_test"), retrieved=T0)
    else:
        page = 999_999
    with pytest.raises(ProvenanceError):
        write_snapshot(
            session,
            series_id=series_id,
            ingestion_run_id=run,
            snapshot_at=T1,
            scope=FULL,
            period_format=DAY,
            incoming=[obs(0, "1", page)],
        )
    assert rows(session, series_id) == []


# --- label guard and argument validation --------------------------------------------------------


def test_snapshot_writer_refuses_source_vintage_series(session: Session, source_id: int) -> None:
    fred_like = make_series(session, source_id, "vintaged_test", "source_vintage")
    run = new_run(session)
    with pytest.raises(VintageBasisMismatchError, match="first_seen"):
        write_snapshot(
            session,
            series_id=fred_like,
            ingestion_run_id=run,
            snapshot_at=T1,
            scope=FULL,
            period_format=DAY,
            incoming=[],
        )


def test_fred_writer_refuses_first_seen_series(session: Session, series_id: int) -> None:
    version = ObservationVersion(week(0), fri(0), T1, None, Decimal("1"), "ok")
    with pytest.raises(VintageBasisMismatchError, match="source_vintage"):
        write_versions(
            session,
            series_id=series_id,
            ingestion_run_id=new_run(session),
            versions=[version],
            retrieved_at=T1,
        )
    assert rows(session, series_id) == []


def test_run_must_exist_and_be_running(session: Session, series_id: int) -> None:
    finished = new_run(session, status="succeeded")
    for run_id, message in ((finished, "not running"), (999_999, "does not exist")):
        with pytest.raises(ValueError, match=message):
            write_snapshot(
                session,
                series_id=series_id,
                ingestion_run_id=run_id,
                snapshot_at=T1,
                scope=FULL,
                period_format=DAY,
                incoming=[],
            )


def test_invalid_arguments_are_refused(session: Session, series_id: int) -> None:
    run = new_run(session)
    common = {"ingestion_run_id": run, "scope": FULL, "incoming": []}
    with pytest.raises(ValueError, match="timezone-aware"):
        write_snapshot(
            session,
            series_id=series_id,
            snapshot_at=datetime(2026, 10, 5),  # noqa: DTZ001  (naive on purpose)
            period_format=DAY,
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="unknown period format"):
        write_snapshot(
            session,
            series_id=series_id,
            snapshot_at=T1,
            period_format="YYYY",
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="does not exist"):
        write_snapshot(
            session,
            series_id=999_999,
            snapshot_at=T1,
            period_format=DAY,
            **common,  # type: ignore[arg-type]
        )


def test_series_advisory_lock_is_held_until_transaction_ends(
    session: Session, series_id: int, source_id: int
) -> None:
    snap(session, series_id, source_id, {0: "1"}, T1)
    held = session.scalar(
        text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND classid = :ns AND objid = :sid AND pid = pg_backend_pid()"
        ),
        {"ns": OBSERVATION_LOCK_NAMESPACE, "sid": series_id},
    )
    assert held == 1


# --- vanished periods ---------------------------------------------------------------------------


def test_vanished_period_issue_lifecycle(session: Session, series_id: int, source_id: int) -> None:
    every = {i: "1" for i in range(200)}
    without_7 = {i: "1" for i in range(200) if i != 7}
    snap(session, series_id, source_id, every, T1)

    run2, r2 = snap(session, series_id, source_id, without_7, T2)  # opened
    assert (r2.vanished, r2.vanished_opened, r2.vanished_updated, r2.confirmed) == (1, 1, 0, 199)
    (issue,) = issues(session, series_id, "vanished_period")
    assert (issue.severity, issue.period_start, issue.occurrences) == ("warning", week(7), 1)
    assert (issue.ingestion_run_id, issue.last_detected_run_id, issue.last_detected_at) == (
        run2,
        run2,
        T2,
    )
    assert issue.resolved_at is None
    stored_7 = next(r for r in rows(session, series_id) if r.period_start == week(7))
    assert (stored_7.vintage_end, stored_7.last_confirmed_at) == (None, T1)  # kept, not confirmed

    run3, r3 = snap(session, series_id, source_id, without_7, T3)  # detected again
    assert (r3.vanished_opened, r3.vanished_updated) == (0, 1)
    (issue,) = issues(session, series_id, "vanished_period")
    assert (issue.occurrences, issue.last_detected_at, issue.last_detected_run_id) == (2, T3, run3)
    assert issue.ingestion_run_id == run2  # first detection kept

    run4, r4 = snap(session, series_id, source_id, every, T4)  # reported again: resolved
    assert r4.vanished_resolved == 1
    (issue,) = issues(session, series_id, "vanished_period")
    assert (issue.resolved_at, issue.resolved_by_run_id) == (T4, run4)

    _, r5 = snap(session, series_id, source_id, without_7, T5)  # vanishes again: new issue
    assert r5.vanished_opened == 1
    old, new = issues(session, series_id, "vanished_period")
    assert (old.resolved_at, new.resolved_at, new.occurrences) == (T4, None, 1)


def test_stale_snapshots_leave_open_vanished_issues_unchanged(
    session: Session, series_id: int, source_id: int
) -> None:
    every = {i: "1" for i in range(200)}
    without_7 = {i: "1" for i in range(200) if i != 7}
    snap(session, series_id, source_id, every, T1)
    run2, _ = snap(session, series_id, source_id, without_7, T2)  # opens the issue

    def tracked() -> list[tuple[object, ...]]:
        return [
            (
                i.id,
                i.period_start,
                i.occurrences,
                i.last_detected_at,
                i.last_detected_run_id,
                i.resolved_at,
                i.resolved_by_run_id,
                i.ingestion_run_id,
            )
            for i in issues(session, series_id, "vanished_period")
        ]

    before_issues, before_rows = tracked(), checksum(session, series_id)
    assert [(t[2], t[3], t[4], t[5]) for t in before_issues] == [(1, T2, run2, None)]
    total_before = len(issues(session, series_id))

    # Replay at the same instant (would update the issue if applied) and an older snapshot
    # that reports period 7 again (would resolve it if applied).
    for values, at in ((without_7, T2), (every, T2 - timedelta(hours=1))):
        _, result = snap(session, series_id, source_id, values, at)
        assert result.rejected
        assert [i.check_name for i in result.issues] == ["stale_snapshot"]
        assert (result.vanished_opened, result.vanished_updated, result.vanished_resolved) == (
            0,
            0,
            0,
        )

    assert tracked() == before_issues  # occurrences, detection, resolution all unchanged
    assert checksum(session, series_id) == before_rows
    stale_rows = issues(session, series_id, "stale_snapshot")
    assert len(stale_rows) == 2  # one per rejected snapshot, nothing else added
    assert len(issues(session, series_id)) == total_before + 2


def test_open_vanished_issue_outside_the_scope_is_untouched(
    session: Session, series_id: int, source_id: int
) -> None:
    snap(session, series_id, source_id, {i: "1" for i in range(200)}, T1)
    snap(session, series_id, source_id, {i: "1" for i in range(200) if i != 7}, T2)
    (before,) = issues(session, series_id, "vanished_period")
    window = SnapshotScope(start=fri(190).isoformat())
    _, result = snap(
        session, series_id, source_id, {i: "1" for i in range(190, 200)}, T3, scope=window
    )
    assert not result.rejected
    assert (result.vanished, result.vanished_resolved, result.confirmed) == (0, 0, 10)
    (after,) = issues(session, series_id, "vanished_period")
    assert after.id == before.id  # the same open issue
    assert (after.occurrences, after.last_detected_at, after.resolved_at) == (1, T2, None)


def test_only_one_open_vanished_issue_per_period(
    session: Session, series_id: int, source_id: int
) -> None:
    snap(session, series_id, source_id, {i: "1" for i in range(200)}, T1)
    for at in (T2, T3, T4):
        snap(session, series_id, source_id, {i: "1" for i in range(200) if i != 7}, at)
    open_rows = session.scalar(
        select(func.count())
        .select_from(DataQualityIssue)
        .where(
            DataQualityIssue.series_id == series_id,
            DataQualityIssue.check_name == "vanished_period",
            DataQualityIssue.resolved_at.is_(None),
        )
    )
    assert open_rows == 1
    (issue,) = issues(session, series_id, "vanished_period")
    assert issue.occurrences == 3


@pytest.mark.parametrize(
    ("stored_count", "vanished", "rejected"),
    [
        (1000, 5, False),  # 5 periods and 0.5%: within both limits
        (1000, 6, True),  # more than five periods
        (200, 2, False),  # exactly 1%
        (200, 3, True),  # more than 1%
        (50, 1, True),  # 2%: a single period in a small scope
    ],
)
def test_vanishing_threshold_through_the_writer(
    session: Session,
    series_id: int,
    source_id: int,
    stored_count: int,
    vanished: int,
    rejected: bool,
) -> None:
    snap(session, series_id, source_id, {i: "1" for i in range(stored_count)}, T1)
    before = checksum(session, series_id)
    _, result = snap(
        session, series_id, source_id, {i: "1" for i in range(vanished, stored_count)}, T2
    )
    assert result.rejected is rejected
    tracked = issues(session, series_id, "vanished_period")
    if rejected:
        assert checksum(session, series_id) == before
        assert tracked == []
    else:
        assert (result.vanished, len(tracked)) == (vanished, vanished)
