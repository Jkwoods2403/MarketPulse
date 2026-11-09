"""SYNTHETIC development fixtures: generated values for building the dashboard. Not market data.

Values exist only in memory, are never written to any database, and are served with
`data_mode = "synthetic"` and `synthetic = true` on every series. They are a deterministic walk
around 100 for every series regardless of its units (for example a "100.00 %" unemployment
rate), so they cannot pass for real figures. The same licensing filter as the database
repository applies: restricted series and blocked sources are never served.

Built by `marketpulse.dev_app`, which refuses to run outside the development environment.
"""

import calendar
import hashlib
import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from marketpulse.api.repository import ObservationPage, SeriesMeta, is_permitted
from marketpulse.api.schemas import DataMode, LatestObservation, Observation

HISTORY_PERIODS = 260


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _periods(meta: SeriesMeta, today: date) -> list[tuple[date, date, str]]:
    """(period_start, period_end, source_period) for the series' convention, oldest first."""
    convention = meta.period_convention
    out: list[tuple[date, date, str]] = []
    if convention in ("business_day", "calendar_day"):
        day = today - timedelta(days=1)
        while len(out) < HISTORY_PERIODS:
            if convention == "calendar_day" or day.weekday() < 5:
                out.append((day, day, day.isoformat()))
            day -= timedelta(days=1)
    elif convention.startswith("week_ending_"):
        weekday = list(calendar.day_name).index(convention.removeprefix("week_ending_").title())
        end = today - timedelta(days=5)
        end -= timedelta(days=(end.weekday() - weekday) % 7)
        for _ in range(HISTORY_PERIODS):
            out.append((end - timedelta(days=6), end, end.isoformat()))
            end -= timedelta(days=7)
    elif convention == "month":
        year, month = today.year, today.month - 2
        for _ in range(HISTORY_PERIODS // 4):
            if month <= 0:
                year, month = year - 1, month + 12
            out.append((date(year, month, 1), _month_end(year, month), f"{year}-{month:02d}"))
            month -= 1
    elif convention == "quarter":
        quarter = (today.month - 1) // 3  # the previous quarter
        year = today.year
        for _ in range(40):
            if quarter == 0:
                year, quarter = year - 1, 4
            first_month = 3 * (quarter - 1) + 1
            out.append(
                (
                    date(year, first_month, 1),
                    _month_end(year, first_month + 2),
                    f"{year}-Q{quarter}",
                )
            )
            quarter -= 1
    out.reverse()
    return out


def _walk(code: str, count: int) -> list[Decimal]:
    seed = int.from_bytes(hashlib.sha256(code.encode()).digest()[:8], "big")
    rng = random.Random(seed)  # noqa: S311 (deterministic fixtures, no security role)
    level = 100.0
    values = []
    for _ in range(count):
        level *= 1 + rng.uniform(-0.01, 0.01)
        values.append(Decimal(f"{level:.2f}"))
    return values


class SyntheticSeriesRepository:
    """Serves generated observations for the permitted series in `metas`."""

    data_mode: DataMode = "synthetic"

    def __init__(self, metas: Sequence[SeriesMeta], now: datetime | None = None) -> None:
        self._now = (now or datetime.now(UTC)).astimezone(UTC)
        self._metas = {m.code: m for m in metas if is_permitted(m)}  # same licensing rule
        self._observations = {
            code: self._generate(meta) for code, meta in sorted(self._metas.items())
        }

    def _generate(self, meta: SeriesMeta) -> list[Observation]:
        periods = _periods(meta, self._now.date())
        return [
            Observation(
                period_start=start,
                period_end=end,
                source_period=label,
                value=value,
                status="ok",
                vintage_start=self._now,
                vintage_end=None,
            )
            for (start, end, label), value in zip(
                periods, _walk(meta.code, len(periods)), strict=True
            )
        ]

    def _latest(self, code: str) -> LatestObservation | None:
        observations = self._observations.get(code) or []
        if not observations:
            return None
        last = observations[-1]
        if last.value is None:  # pragma: no cover  (generated values are never None)
            return None
        return LatestObservation(
            period_start=last.period_start,
            period_end=last.period_end,
            source_period=last.source_period,
            value=last.value,
            status=last.status,
            vintage_start=self._now,
            first_retrieved_at=self._now,
            last_confirmed_at=self._now,
        )

    def list_series(self) -> list[tuple[SeriesMeta, LatestObservation | None]]:
        return [(meta, self._latest(code)) for code, meta in sorted(self._metas.items())]

    def get_series(self, code: str) -> tuple[SeriesMeta, LatestObservation | None] | None:
        meta = self._metas.get(code)
        return None if meta is None else (meta, self._latest(code))

    def observations(
        self,
        meta: SeriesMeta,
        *,
        start: date | None,
        end: date | None,
        as_of: datetime | None,
        limit: int,
        offset: int,
    ) -> ObservationPage:
        if not is_permitted(meta) or meta.code not in self._metas:
            raise RuntimeError(f"series {meta.code!r} is not served")
        rows = [
            o
            for o in self._observations[meta.code]
            if (start is None or o.period_start >= start)
            and (end is None or o.period_start <= end)
            and (as_of is None or o.vintage_start <= as_of)
        ]
        return ObservationPage(len(rows), rows[offset : offset + limit])
