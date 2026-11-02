"""The provider-neutral compare-and-plan engine (pure; synthetic versions)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from marketpulse.core.versions import ObservationVersion
from marketpulse.ingestion.versions import ERROR, WARNING, StoredVersion, plan_changes

P = date(2024, 1, 1)  # one observation period


def t(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, 6, tzinfo=UTC)


def new(
    start: datetime, end: datetime | None, value: str | None, period: date = P
) -> ObservationVersion:
    return ObservationVersion(
        period,
        period,
        start,
        end,
        Decimal(value) if value else None,
        "ok" if value else "missing_at_source",
    )


def stored(
    id_: int, start: datetime, end: datetime | None, value: str | None, period: date = P
) -> StoredVersion:
    return StoredVersion(
        id_,
        period,
        period,
        start,
        end,
        Decimal(value) if value else None,
        "ok" if value else "missing_at_source",
    )


def checks(plan_issues: list) -> list[str]:  # type: ignore[type-arg]
    return [i.check_name for i in plan_issues]


# --- basic outcomes ----------------------------------------------------------------------------


def test_first_ingestion_inserts_everything() -> None:
    incoming = [new(t(2024, 4, 25), t(2024, 5, 30), "1.0"), new(t(2024, 5, 30), None, "1.1")]
    plan = plan_changes([], incoming)
    assert plan.inserts == incoming
    assert plan.closes == [] and plan.confirms == [] and plan.issues == []


def test_identical_repeat_only_confirms() -> None:
    s = [stored(1, t(2024, 4, 25), t(2024, 5, 30), "1.0"), stored(2, t(2024, 5, 30), None, "1.1")]
    plan = plan_changes(
        s, [new(t(2024, 4, 25), t(2024, 5, 30), "1.0"), new(t(2024, 5, 30), None, "1.1")]
    )
    assert plan.confirms == [1, 2]
    assert plan.inserts == [] and plan.closes == [] and plan.issues == []


def test_revision_closes_old_current_and_inserts_new() -> None:
    s = [stored(1, t(2024, 4, 25), None, "1.0")]
    plan = plan_changes(
        s, [new(t(2024, 4, 25), t(2024, 5, 30), "1.0"), new(t(2024, 5, 30), None, "1.1")]
    )
    assert plan.closes == [(1, t(2024, 5, 30))]
    assert [v.value for v in plan.inserts] == [Decimal("1.1")]
    assert plan.confirms == [] and plan.issues == []


def test_missing_value_is_a_value_like_any_other() -> None:
    s = [stored(1, t(2025), None, None)]
    plan = plan_changes(s, [new(t(2025), None, None)])
    assert plan.confirms == [1] and plan.issues == []


def test_decimal_precision_change_is_not_a_revision() -> None:
    s = [stored(1, t(2025), None, "1.0")]
    assert plan_changes(s, [new(t(2025), None, "1.00")]).confirms == [1]


# --- incremental fetches (start cut to coverage_start) ---------------------------------------


def test_cut_start_matches_covering_version_as_continuation() -> None:
    s = [stored(1, t(2005, 6, 28), None, "4.06")]
    cs = t(2026, 10, 1)
    plan = plan_changes(s, [new(cs, None, "4.06")], coverage_start=cs)
    assert plan.confirms == [1]
    assert plan.inserts == [] and plan.issues == []


def test_cut_start_with_new_revision_closes_and_inserts() -> None:
    s = [stored(1, t(2005, 6, 28), None, "4.06")]
    cs = t(2026, 10, 1)
    plan = plan_changes(
        s, [new(cs, t(2026, 10, 15), "4.06"), new(t(2026, 10, 15), None, "4.07")], coverage_start=cs
    )
    assert plan.closes == [(1, t(2026, 10, 15))]
    assert [v.vintage_start for v in plan.inserts] == [t(2026, 10, 15)]


def test_cut_start_with_a_different_value_is_a_conflict() -> None:
    s = [stored(1, t(2005, 6, 28), None, "4.06")]
    cs = t(2026, 10, 1)
    plan = plan_changes(s, [new(cs, None, "9.99")], coverage_start=cs)
    assert checks(plan.issues) == ["value_conflict"]
    assert plan.inserts == [] and plan.closes == [] and plan.periods_skipped == 1


def test_versions_entirely_before_coverage_are_out_of_scope() -> None:
    s = [stored(1, t(2010), t(2015), "1.0"), stored(2, t(2015), None, "1.1")]
    cs = t(2026, 10, 1)
    plan = plan_changes(s, [new(cs, None, "1.1")], coverage_start=cs)
    assert plan.confirms == [2]
    assert plan.issues == []  # version 1 ended before the fetched range: not "vanished"


def test_new_period_in_incremental_fetch_is_inserted() -> None:
    cs = t(2026, 10, 1)
    plan = plan_changes(
        [], [new(t(2026, 10, 2), None, "4.1", period=date(2026, 10, 1))], coverage_start=cs
    )
    assert len(plan.inserts) == 1


# --- conflicts are reported, never "fixed" -----------------------------------------------------


def test_changed_value_for_an_existing_vintage_is_an_error_and_period_untouched() -> None:
    s = [stored(1, t(2024, 4, 25), None, "1.0")]
    plan = plan_changes(s, [new(t(2024, 4, 25), None, "2.0")])
    assert checks(plan.issues) == ["value_conflict"]
    assert plan.issues[0].severity == ERROR
    assert plan.confirms == [] and plan.inserts == [] and plan.closes == []


def test_reopening_a_closed_version_is_an_error() -> None:
    s = [stored(1, t(2024), t(2025), "1.0"), stored(2, t(2025), None, "1.1")]
    plan = plan_changes(s, [new(t(2024), None, "1.0")])
    assert "vintage_end_conflict" in checks(plan.issues)
    assert plan.periods_skipped == 1


def test_different_end_for_a_closed_version_is_an_error() -> None:
    s = [stored(1, t(2024), t(2025), "1.0"), stored(2, t(2025), None, "1.1")]
    plan = plan_changes(s, [new(t(2024), t(2025, 6), "1.0"), new(t(2025, 6), None, "1.1")])
    assert "vintage_end_conflict" in checks(plan.issues)


def test_new_current_version_without_closing_the_stored_one_is_rejected() -> None:
    s = [stored(1, t(2024), None, "1.0")]
    plan = plan_changes(s, [new(t(2025), None, "1.1")])  # provider omitted the old version
    assert "inconsistent_history" in checks(plan.issues)
    assert plan.inserts == []


def test_overlapping_incoming_versions_are_rejected() -> None:
    plan = plan_changes([], [new(t(2024), t(2026), "1.0"), new(t(2025), None, "1.1")])
    assert checks(plan.issues) == ["incoming_overlap"]
    assert plan.inserts == []


def test_two_current_incoming_versions_are_rejected() -> None:
    plan = plan_changes([], [new(t(2024), None, "1.0"), new(t(2025), None, "1.1")])
    assert checks(plan.issues) == ["incoming_overlap"]


def test_vanished_version_is_a_warning_and_kept() -> None:
    s = [stored(1, t(2024), t(2025), "1.0"), stored(2, t(2025), None, "1.1")]
    plan = plan_changes(s, [new(t(2025), None, "1.1")])
    assert [(i.check_name, i.severity) for i in plan.issues] == [("vanished_version", WARNING)]
    assert plan.confirms == [2]
    assert not plan.has_errors


def test_whole_period_missing_from_provider_is_reported() -> None:
    s = [stored(1, t(2024), None, "1.0", period=date(2020, 1, 1))]
    plan = plan_changes(s, [new(t(2024), None, "1.0")])
    assert [i.period_start for i in plan.issues] == [date(2020, 1, 1)]
    assert plan.issues[0].check_name == "vanished_version"


def test_error_in_one_period_does_not_block_other_periods() -> None:
    other = date(2024, 4, 1)
    s = [stored(1, t(2024), None, "1.0"), stored(2, t(2024), None, "5.0", period=other)]
    plan = plan_changes(
        s,
        [
            new(t(2024), None, "9.0"),  # conflict in P
            new(t(2024), t(2025), "5.0", period=other),  # revision in `other`
            new(t(2025), None, "5.1", period=other),
        ],
    )
    assert plan.periods_skipped == 1
    assert plan.closes == [(2, t(2025))]
    assert [v.period_start for v in plan.inserts] == [other]


def test_period_end_change_is_an_error() -> None:
    s = [stored(1, t(2024), None, "1.0")]
    changed = ObservationVersion(P, date(2024, 3, 31), t(2024), None, Decimal("1.0"), "ok")
    assert checks(plan_changes(s, [changed]).issues) == ["period_end_conflict"]


# --- ObservationVersion invariants ------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"vintage_start": datetime(2024, 1, 1)},  # noqa: DTZ001  (naive on purpose)
        {"vintage_end": t(2023)},  # ends before it starts
        {"value": None},  # None but status ok
        {"status": "missing_at_source"},  # value but missing status
        {"value": Decimal("NaN")},
        {"period_end": date(2023, 12, 31)},
    ],
)
def test_observation_version_rejects_invalid_values(kwargs: dict) -> None:  # type: ignore[type-arg]
    fields = {
        "period_start": P,
        "period_end": P,
        "vintage_start": t(2024),
        "vintage_end": None,
        "value": Decimal("1"),
        "status": "ok",
    } | kwargs
    with pytest.raises(ValueError):
        ObservationVersion(**fields)
