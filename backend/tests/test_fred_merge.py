"""Rejoining real-time periods split at window boundaries."""

from datetime import date
from decimal import Decimal

import pytest

from marketpulse.sources.fred import REALTIME_OPEN_END
from marketpulse.sources.fred.merge import MergeConflictError, merge_window_splits
from marketpulse.sources.fred.parse import FredObservation
from marketpulse.sources.fred.windows import plan_windows

D = date


def obs(day: date, start: date, end: date, value: str | None) -> FredObservation:
    return FredObservation(day, start, end, Decimal(value) if value is not None else None)


# Real data: the six real-time periods FRED returned for GDPC1, observation date 2024-01-01,
# when queried on 2026-11-01 (values verbatim from the API response).
GDPC1_2024Q1 = [
    obs(D(2024, 1, 1), D(2024, 4, 25), D(2024, 5, 29), "22768.866"),
    obs(D(2024, 1, 1), D(2024, 5, 30), D(2024, 6, 26), "22749.846"),
    obs(D(2024, 1, 1), D(2024, 6, 27), D(2024, 9, 25), "22758.752"),
    obs(D(2024, 1, 1), D(2024, 9, 26), D(2025, 9, 24), "23053.545"),
    obs(D(2024, 1, 1), D(2025, 9, 25), D(2026, 9, 29), "23082.119"),
    obs(D(2024, 1, 1), D(2026, 9, 30), REALTIME_OPEN_END, "23115.975"),
]


def test_real_revision_history_is_preserved_unchanged() -> None:
    assert merge_window_splits(GDPC1_2024Q1) == GDPC1_2024Q1


def test_real_revision_history_split_at_a_window_boundary_is_restored() -> None:
    # Simulate a window boundary on 2025-01-01 cutting the 4th period in two.
    split = [
        *GDPC1_2024Q1[:3],
        obs(D(2024, 1, 1), D(2024, 9, 26), D(2024, 12, 31), "23053.545"),
        obs(D(2024, 1, 1), D(2025, 1, 1), D(2025, 9, 24), "23053.545"),
        *GDPC1_2024Q1[4:],
    ]
    assert merge_window_splits(reversed(split)) == GDPC1_2024Q1  # input order does not matter


def test_unchanged_value_split_across_many_windows_becomes_one_period() -> None:
    windows = plan_windows(
        [D(2005, 6, 28), D(2010, 1, 1), D(2015, 1, 1), D(2020, 1, 1)], max_vintages=1
    )
    pieces = [obs(D(1962, 1, 2), w.start, w.end, "4.06") for w in windows]
    assert merge_window_splits(pieces) == [
        obs(D(1962, 1, 2), D(2005, 6, 28), REALTIME_OPEN_END, "4.06")
    ]


def test_missing_values_merge_like_any_other_value() -> None:
    pieces = [
        obs(D(2024, 1, 1), D(2025, 1, 1), D(2025, 6, 30), None),
        obs(D(2024, 1, 1), D(2025, 7, 1), REALTIME_OPEN_END, None),
    ]
    assert merge_window_splits(pieces) == [
        obs(D(2024, 1, 1), D(2025, 1, 1), REALTIME_OPEN_END, None)
    ]


def test_adjacent_periods_with_different_values_stay_separate() -> None:
    pieces = [
        obs(D(2024, 1, 1), D(2025, 1, 1), D(2025, 6, 30), "1.0"),
        obs(D(2024, 1, 1), D(2025, 7, 1), REALTIME_OPEN_END, "1.1"),
    ]
    assert merge_window_splits(pieces) == pieces


def test_missing_and_numeric_values_never_merge() -> None:
    pieces = [
        obs(D(2024, 1, 1), D(2025, 1, 1), D(2025, 6, 30), None),
        obs(D(2024, 1, 1), D(2025, 7, 1), REALTIME_OPEN_END, "0"),
    ]
    assert merge_window_splits(pieces) == pieces


def test_numerically_equal_decimals_with_different_precision_are_treated_as_equal() -> None:
    # Decimal("1.0") == Decimal("1.00"); the first-seen representation is kept.
    pieces = [
        obs(D(2024, 1, 1), D(2025, 1, 1), D(2025, 6, 30), "1.0"),
        obs(D(2024, 1, 1), D(2025, 7, 1), REALTIME_OPEN_END, "1.00"),
    ]
    merged = merge_window_splits(pieces)
    assert len(merged) == 1 and str(merged[0].value) == "1.0"


def test_gaps_are_never_filled() -> None:
    pieces = [
        obs(D(2024, 1, 1), D(2025, 1, 1), D(2025, 6, 29), "1.0"),
        obs(D(2024, 1, 1), D(2025, 7, 1), REALTIME_OPEN_END, "1.0"),
    ]  # 2025-06-30 missing
    assert merge_window_splits(pieces) == pieces


def test_exact_duplicates_collapse() -> None:
    piece = obs(D(2024, 1, 1), D(2025, 1, 1), REALTIME_OPEN_END, "1.0")
    assert merge_window_splits([piece, piece, piece]) == [piece]


def test_different_dates_are_independent_and_output_is_sorted() -> None:
    a = obs(D(2024, 2, 1), D(2025, 1, 1), REALTIME_OPEN_END, "2")
    b = obs(D(2024, 1, 1), D(2025, 1, 1), REALTIME_OPEN_END, "1")
    assert merge_window_splits([a, b]) == [b, a]


def test_nothing_follows_an_open_ended_period() -> None:
    # An open period ends at date.max; a later row for the same date overlaps it.
    with pytest.raises(MergeConflictError):
        merge_window_splits(
            [
                obs(D(2024, 1, 1), D(2025, 1, 1), REALTIME_OPEN_END, "1"),
                obs(D(2024, 1, 1), D(2026, 1, 1), REALTIME_OPEN_END, "1"),
            ]
        )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # same start, different values
        ((D(2025, 1, 1), D(2025, 6, 30), "1"), (D(2025, 1, 1), D(2025, 6, 30), "2")),
        # partial overlap, same value
        ((D(2025, 1, 1), D(2025, 6, 30), "1"), (D(2025, 6, 1), D(2025, 12, 31), "1")),
        # same start, one missing
        ((D(2025, 1, 1), D(2025, 6, 30), None), (D(2025, 1, 1), D(2025, 6, 30), "1")),
    ],
)
def test_overlaps_raise_instead_of_choosing(
    first: tuple[date, date, str | None], second: tuple[date, date, str | None]
) -> None:
    with pytest.raises(MergeConflictError) as info:
        merge_window_splits([obs(D(2024, 1, 1), *first), obs(D(2024, 1, 1), *second)])
    assert "2024-01-01" in info.value.conflicts[0]


def test_empty_input() -> None:
    assert merge_window_splits([]) == []
