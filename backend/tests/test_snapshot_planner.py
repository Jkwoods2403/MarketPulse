"""The pure first-seen snapshot planner (ingestion.snapshots). All values are SYNTHETIC."""

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from marketpulse.ingestion.snapshots import (
    SnapshotObservation,
    SnapshotPlan,
    SnapshotScope,
    StoredSnapshotVersion,
    plan_snapshot,
)

T1 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # earlier snapshot
T2 = T1 + timedelta(days=1)  # this snapshot
FULL = SnapshotScope()
DAY = "YYYY-MM-DD"


def fri(i: int) -> date:
    return date(2026, 1, 2) + timedelta(weeks=i)


def obs(
    i: int, value: str | None, *, page: int | None = 7, source_period: str | None = None
) -> SnapshotObservation:
    d = fri(i)
    return SnapshotObservation(
        source_period=d.isoformat() if source_period is None else source_period,
        period_start=d - timedelta(days=6),
        period_end=d,
        value=None if value is None else Decimal(value),
        status="missing_at_source" if value is None else "ok",
        raw_payload_id=page,
    )


def stored(
    i: int,
    value: str | None,
    *,
    id: int | None = None,
    start: datetime = T1,
    end: datetime | None = None,
    confirmed: datetime | None = None,
    source_period: str | None = "",
) -> StoredSnapshotVersion:
    o = obs(i, value)
    return StoredSnapshotVersion(
        id=id if id is not None else 100 + i,
        period_start=o.period_start,
        period_end=o.period_end,
        source_period=o.source_period if source_period == "" else source_period,
        value=o.value,
        status=o.status,
        vintage_start=start,
        vintage_end=end,
        last_confirmed_at=confirmed or start,
    )


def plan(
    history: Iterable[StoredSnapshotVersion],
    incoming: Iterable[SnapshotObservation],
    *,
    scope: SnapshotScope = FULL,
    at: datetime = T2,
    window: str | None = None,
    period_format: str = DAY,
) -> SnapshotPlan:
    return plan_snapshot(
        history,
        incoming,
        snapshot_at=at,
        scope=scope,
        period_format=period_format,
        incremental_window_start=window,
    )


def checks(p: SnapshotPlan) -> list[str]:
    return [i.check_name for i in p.issues]


def assert_no_actions(p: SnapshotPlan) -> None:
    assert p.rejected
    assert (p.inserts, p.closes, p.confirms, p.vanished, p.new_periods) == ([], [], [], [], 0)


# --- insert / confirm / revise ------------------------------------------------------------------


def test_first_snapshot_inserts_every_period_at_snapshot_time() -> None:
    incoming = [obs(i, f"{i}.5") for i in range(4)]
    p = plan([], incoming)
    assert not p.rejected
    assert p.inserts == incoming
    assert (p.closes, p.confirms, p.vanished, p.new_periods) == ([], [], [], 4)


def test_unchanged_values_are_confirmed_without_new_versions() -> None:
    p = plan([stored(0, "1.5"), stored(1, "2")], [obs(0, "1.5"), obs(1, "2")])
    assert (p.inserts, p.closes, p.confirms) == ([], [], [100, 101])


def test_representation_only_change_is_not_a_new_version() -> None:
    p = plan([stored(0, "1.0")], [obs(0, "1.00")])
    assert (p.inserts, p.confirms) == ([], [100])


def test_exact_decimal_comparison_detects_tiny_changes() -> None:
    p = plan([stored(0, "0.1")], [obs(0, "0.10000000000000001")])
    assert p.closes == [100]  # float would call these equal
    assert p.inserts[0].value == Decimal("0.10000000000000001")


def test_changed_value_closes_current_and_inserts_new_version() -> None:
    new = obs(0, "1.6")
    p = plan([stored(0, "1.5")], [new])
    assert (p.closes, p.inserts, p.confirms, p.revisions) == ([100], [new], [], 1)
    assert p.new_periods == 0


@pytest.mark.parametrize(
    ("before", "after", "outcome"),
    [
        ("1.5", None, "revised"),  # value -> null
        (None, "1.5", "revised"),  # null -> value
        (None, None, "confirmed"),  # null -> null
    ],
)
def test_null_transitions(before: str | None, after: str | None, outcome: str) -> None:
    p = plan([stored(0, before)], [obs(0, after)])
    if outcome == "revised":
        assert (p.closes, len(p.inserts)) == ([100], 1)
        assert p.inserts[0].value == (None if after is None else Decimal(after))
    else:
        assert (p.closes, p.inserts, p.confirms) == ([], [], [100])


def test_closed_versions_are_never_touched() -> None:
    history = [
        stored(0, "1.0", id=1, start=T1 - timedelta(days=9), end=T1),
        stored(0, "1.5", id=2, start=T1),
    ]
    p = plan(history, [obs(0, "1.0")])
    assert p.closes == [2]  # only the current version; the old value returning is a revision
    assert p.confirms == []


def test_revision_before_the_incremental_window_is_reported() -> None:
    history = [stored(i, "1") for i in range(3)]
    p = plan(history, [obs(0, "1.1"), obs(1, "1"), obs(2, "1.2")], window=fri(2).isoformat())
    assert not p.rejected  # info, not an error
    assert [(i.check_name, i.severity, i.period_start) for i in p.issues] == [
        ("revision_outside_window", "info", obs(0, "1").period_start)
    ]
    assert p.closes == [100, 102]


# --- vanished periods ---------------------------------------------------------------------------


def test_vanished_period_is_kept_and_not_confirmed() -> None:
    history = [stored(i, "1") for i in range(200)]
    incoming = [obs(i, "1") for i in range(200) if i != 50]
    p = plan(history, incoming)
    assert not p.rejected
    assert [v.stored_id for v in p.vanished] == [150]
    assert 150 not in p.confirms and 150 not in p.closes
    assert len(p.confirms) == 199


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
def test_vanished_limit(stored_count: int, vanished: int, rejected: bool) -> None:
    history = [stored(i, "1") for i in range(stored_count)]
    incoming = [obs(i, "1") for i in range(vanished, stored_count)]
    p = plan(history, incoming)
    assert p.rejected is rejected
    assert ("vanished_limit" in checks(p)) is rejected
    assert p.in_scope == stored_count
    if rejected:
        assert_no_actions(p)


def test_vanished_period_and_revision_in_the_same_snapshot() -> None:
    history = [stored(i, "1") for i in range(200)]
    incoming = [obs(i, "2" if i == 10 else "1") for i in range(200) if i != 50]
    p = plan(history, incoming)
    assert not p.rejected
    assert [v.stored_id for v in p.vanished] == [150]
    assert (p.closes, [o.value for o in p.inserts]) == ([110], [Decimal("2")])
    assert len(p.confirms) == 198


def test_vanished_detection_is_limited_to_the_scope() -> None:
    history = [stored(i, "1") for i in range(10)]
    window = SnapshotScope(start=fri(8).isoformat())
    p = plan(history, [obs(8, "1"), obs(9, "2")], scope=window)
    assert not p.rejected
    assert (p.in_scope, p.vanished, p.confirms, p.closes) == (2, [], [108], [109])


def test_vanished_inside_a_bounded_scope_counts_against_that_scope() -> None:
    # 1,000 stored periods; the scope covers 300 of them (100..399). Periods outside the scope
    # that are absent from the snapshot are not vanished; the limits apply to the 300 in scope.
    history = [stored(i, "1") for i in range(1000)]
    scope = SnapshotScope(start=fri(100).isoformat(), end=fri(399).isoformat())

    within = plan(history, [obs(i, "1") for i in range(103, 400)], scope=scope)  # 3 = 1%
    assert not within.rejected
    assert within.in_scope == 300
    assert [v.stored_id for v in within.vanished] == [200, 201, 202]

    over = plan(history, [obs(i, "1") for i in range(104, 400)], scope=scope)  # 4 > 1%
    assert checks(over) == ["vanished_limit"]
    assert over.in_scope == 300
    assert_no_actions(over)


def test_empty_response_against_full_history_is_rejected() -> None:
    p = plan([stored(i, "1") for i in range(500)], [])
    assert checks(p) == ["vanished_limit"]
    assert p.in_scope == 500
    assert_no_actions(p)
    assert p.vanished_detected == 500


def test_vanished_count_survives_rejection_without_the_periods() -> None:
    history = [stored(i, "1") for i in range(200)]
    accepted = plan(history, [obs(i, "1") for i in range(200) if i != 7])  # 1 of 200 = 0.5%
    assert (accepted.rejected, accepted.vanished_detected, len(accepted.vanished)) == (False, 1, 1)

    rejected = plan(history, [obs(i, "1") for i in range(200) if not 10 <= i < 16])  # 6 > 5
    assert checks(rejected) == ["vanished_limit"]
    assert rejected.vanished_detected == 6
    assert_no_actions(rejected)  # the count is reported; the periods cannot be applied

    none_missing = plan(history, [obs(i, "1") for i in range(200)])
    assert none_missing.vanished_detected == 0  # zero is known, not unknown


def test_vanished_count_is_known_when_another_per_period_error_rejects() -> None:
    history = [stored(i, "1") for i in range(200)]
    incoming = [obs(i, "1") for i in range(200) if i != 3]
    incoming[0] = obs(0, "1", source_period="2026-01-03")  # well-formed, but relabelled
    p = plan(history, incoming)
    assert checks(p) == ["source_period_conflict"]
    assert p.vanished_detected == 1  # periods were compared, so the count is known


def test_vanished_count_is_unknown_when_rejected_before_periods_are_compared() -> None:
    history = [stored(i, "1", start=T2) for i in range(40)]
    stale = plan(history, [obs(i, "1") for i in range(30)], at=T1)  # older than stored history
    assert "stale_snapshot" in checks(stale)
    assert stale.vanished_detected is None  # not determined, which is different from 0

    bad_scope = plan(
        history, [obs(i, "1") for i in range(40)], scope=SnapshotScope(start="2026-01")
    )
    assert "scope_format_mismatch" in checks(bad_scope)
    assert bad_scope.vanished_detected is None


# --- whole-snapshot rejections ------------------------------------------------------------------


def test_period_outside_scope_rejects_the_snapshot() -> None:
    window = SnapshotScope(start=fri(5).isoformat(), end=fri(6).isoformat())
    p = plan([], [obs(4, "1"), obs(5, "1")], scope=window)
    assert checks(p) == ["period_outside_scope"]
    assert_no_actions(p)


def test_duplicate_periods_reject_the_snapshot() -> None:
    p = plan([], [obs(0, "1"), obs(0, "2")])
    assert "duplicate_period" in checks(p)
    assert_no_actions(p)


def test_duplicate_current_versions_in_storage_are_rejected() -> None:
    history = [stored(0, "1", id=1), stored(0, "2", id=2), stored(1, "1")]
    p = plan(history, [obs(0, "1"), obs(1, "1")])
    assert checks(p) == ["duplicate_current_version"]
    assert p.issues[0].period_start == obs(0, "1").period_start
    assert_no_actions(p)


@pytest.mark.parametrize(
    ("scope", "window"),
    [
        (SnapshotScope(start="2026-01"), None),  # monthly bound on a daily-format series
        (SnapshotScope(end="2026-03"), None),
        (SnapshotScope(start="2026-1-02"), None),
        (FULL, "2026-01"),  # incremental window start is compared the same way
    ],
)
def test_scope_bounds_must_match_the_period_format(
    scope: SnapshotScope, window: str | None
) -> None:
    p = plan([stored(0, "1")], [obs(0, "1")], scope=scope, window=window)
    assert "scope_format_mismatch" in checks(p)
    assert_no_actions(p)


def test_monthly_scope_is_accepted_for_a_monthly_series() -> None:
    monthly = SnapshotObservation(
        "2026-03", date(2026, 3, 1), date(2026, 3, 31), Decimal("1"), "ok", 3
    )
    p = plan([], [monthly], scope=SnapshotScope(start="2026-01"), period_format="YYYY-MM")
    assert not p.rejected
    assert p.inserts == [monthly]


def test_source_periods_must_match_the_period_format() -> None:
    p = plan([], [obs(0, "1")], period_format="YYYY-MM")  # daily strings on a monthly series
    assert checks(p) == ["source_period_format"]
    stored_bad = plan([stored(0, "1", source_period="2026-W01")], [obs(1, "1")])
    assert checks(stored_bad) == ["source_period_format"]


def test_unknown_period_format_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown period format"):
        plan([], [], period_format="YYYY")


# --- stale and conflicting snapshots ------------------------------------------------------------


@pytest.mark.parametrize(
    "history",
    [
        [stored(0, "1", start=T2 + timedelta(seconds=1))],  # newer version exists
        [stored(0, "1", confirmed=T2 + timedelta(seconds=1))],  # confirmed later
        [stored(0, "1", start=T2)],  # a version from this same instant
        [stored(0, "1", confirmed=T2)],  # already confirmed at this same instant
    ],
)
def test_snapshot_not_newer_than_stored_history_is_rejected(
    history: list[StoredSnapshotVersion],
) -> None:
    p = plan(history, [obs(0, "1")])  # same value: still rejected, it is not newer
    assert checks(p) == ["stale_snapshot"]
    assert_no_actions(p)


def test_replaying_a_snapshot_at_the_same_instant_is_rejected() -> None:
    history = [stored(0, "1", start=T2), stored(1, "2", start=T1, confirmed=T2)]
    p = plan(history, [obs(0, "1"), obs(1, "2")])
    assert checks(p) == ["stale_snapshot"]
    assert_no_actions(p)


def test_conflicting_snapshot_at_the_same_instant_after_confirmation() -> None:
    # Confirmed at T2 with value 1; another snapshot also at T2 reports 2.
    p = plan([stored(0, "1", start=T1, confirmed=T2)], [obs(0, "2")])
    assert checks(p) == ["stale_snapshot", "conflicting_snapshot"]
    assert p.issues[1].period_start == obs(0, "1").period_start
    assert_no_actions(p)


def test_conflicting_value_at_the_same_instant_as_a_version() -> None:
    p = plan([stored(0, "1", start=T2)], [obs(0, "2")])
    assert checks(p) == ["stale_snapshot", "conflicting_snapshot"]
    assert_no_actions(p)


def test_stale_detection_uses_closed_versions() -> None:
    # Only a closed version is later than the snapshot (its start and end both after T2).
    history = [
        stored(0, "1", id=1, start=T2 + timedelta(hours=1), end=T2 + timedelta(hours=2)),
        stored(1, "1", start=T1),
    ]
    p = plan(history, [obs(1, "1")])
    assert checks(p) == ["stale_snapshot"]
    assert_no_actions(p)

    # A closed version's end alone also counts.
    history = [
        stored(0, "1", id=1, start=T1 - timedelta(days=1), end=T2 + timedelta(hours=1)),
        stored(1, "1", start=T1),
    ]
    assert checks(plan(history, [obs(1, "1")])) == ["stale_snapshot"]


# --- period conflicts and provenance ------------------------------------------------------------


def test_one_bad_period_rejects_the_whole_snapshot() -> None:
    bad = SnapshotObservation(
        source_period=fri(1).isoformat(),
        period_start=fri(1) - timedelta(days=6),
        period_end=fri(1) + timedelta(days=1),  # end changed
        value=Decimal("2"),
        status="ok",
        raw_payload_id=7,
    )
    p = plan([stored(0, "1"), stored(1, "2")], [obs(0, "9"), bad])
    assert checks(p) == ["period_end_conflict"]
    assert_no_actions(p)  # the valid revision of period 0 is not left in the plan


def test_source_period_conflict_is_an_error() -> None:
    renamed = obs(0, "1", source_period=(fri(0) + timedelta(days=1)).isoformat())
    p = plan([stored(0, "1")], [renamed])
    assert checks(p) == ["source_period_conflict"]
    assert_no_actions(p)


def test_stored_version_without_source_period_is_missing_provenance() -> None:
    p = plan([stored(0, "1", source_period=None)], [obs(0, "1")])
    assert checks(p) == ["missing_provenance"]
    assert_no_actions(p)


@pytest.mark.parametrize(
    ("page", "source_period", "missing"),
    [
        (None, None, "raw_payload_id"),
        (7, "", "source_period"),
        (7, "   ", "source_period"),
        (None, "", "source_period or raw_payload_id"),
    ],
)
def test_incoming_observation_without_provenance_is_rejected(
    page: int | None, source_period: str | None, missing: str
) -> None:
    p = plan([], [obs(0, "1"), obs(1, "1", page=page, source_period=source_period)])
    assert checks(p) == ["missing_provenance"]
    assert p.issues[0].detail.endswith(f"has no {missing}")
    assert_no_actions(p)


# --- input validation ---------------------------------------------------------------------------


def test_snapshot_time_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        plan([], [], at=datetime(2026, 11, 2))  # noqa: DTZ001


def test_observation_rejects_inconsistent_null_and_non_finite_values() -> None:
    with pytest.raises(ValueError, match="missing_at_source"):
        SnapshotObservation("2026-01-02", date(2026, 1, 2), date(2026, 1, 2), None, "ok", 1)
    with pytest.raises(ValueError, match="finite"):
        SnapshotObservation(
            "2026-01-02", date(2026, 1, 2), date(2026, 1, 2), Decimal("NaN"), "ok", 1
        )
