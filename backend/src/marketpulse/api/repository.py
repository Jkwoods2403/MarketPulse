"""Where the data API reads series and observations, and the licensing filter it always applies.

A series is served only if it is **permitted**:
- it is active in ref.series;
- its effective license (the series' `license_override`, else its source's license) is not
  `personal_use_only` (ADR-0008);
- its source is not in the source-level ingestion block list (`core.source_access`, ADR-0014),
  which currently blocks ERCOT.

The filter is applied in the SQL query and checked again on every returned row, so a series
that is not permitted can never reach a response. There is no parameter to bypass it; an
unknown and a non-permitted series code are indistinguishable to callers.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, sessionmaker

from marketpulse.analytics.point_in_time import series_as_of
from marketpulse.api.schemas import DataMode, LatestObservation, Observation, SourceInfo
from marketpulse.core.source_access import INGESTION_BLOCKED_SOURCES, ingestion_block_reason
from marketpulse.db.models import Observation as ObservationRow
from marketpulse.db.models import Series, Source

RESTRICTED_LICENSES = frozenset({"personal_use_only"})


@dataclass(frozen=True)
class SeriesMeta:
    """A series' metadata, as served (everything except observations)."""

    id: int
    code: str
    name: str
    description: str | None
    category: str
    frequency: str
    period_convention: str
    units: str
    seasonal_adjustment: str | None
    original_source: str
    reference_url: str
    vintage_basis: str
    license: str  # effective license
    max_staleness: timedelta
    is_active: bool
    source: SourceInfo


def is_permitted(meta: SeriesMeta) -> bool:
    """The licensing rule, checked on every series a repository returns."""
    return (
        meta.is_active
        and meta.license not in RESTRICTED_LICENSES
        and ingestion_block_reason(meta.source.code) is None
    )


@dataclass(frozen=True)
class ObservationPage:
    total: int
    observations: list[Observation]


class SeriesRepository(Protocol):
    data_mode: DataMode

    def list_series(self) -> list[tuple[SeriesMeta, LatestObservation | None]]:
        """Every permitted series, with its latest current observation that has a value."""
        ...

    def get_series(self, code: str) -> tuple[SeriesMeta, LatestObservation | None] | None:
        """One permitted series, or None if it is unknown or not permitted."""
        ...

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
        """Observations of a permitted series by period_start, oldest first."""
        ...


def _require_permitted(metas: Sequence[SeriesMeta]) -> None:
    leaked = [m.code for m in metas if not is_permitted(m)]
    if leaked:  # pragma: no cover  (the query already excludes them)
        raise RuntimeError(f"licensing filter failed for series {leaked}")


class DatabaseSeriesRepository:
    """Reads ref.series, ref.sources, and obs.observations (current versions)."""

    data_mode: DataMode = "database"

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def _metas(self, session: Session, code: str | None = None) -> list[SeriesMeta]:
        effective_license = func.coalesce(Series.license_override, Source.license)
        query = (
            select(Series, Source, effective_license)
            .join(Source, Source.id == Series.source_id)
            .where(
                Series.is_active.is_(True),
                effective_license.not_in(sorted(RESTRICTED_LICENSES)),
                func.lower(Source.code).not_in(sorted(INGESTION_BLOCKED_SOURCES)),
            )
            .order_by(Source.code, Series.code)
        )
        if code is not None:
            query = query.where(Series.code == code)
        metas = [
            SeriesMeta(
                id=series.id,
                code=series.code,
                name=series.name,
                description=series.description,
                category=series.category,
                frequency=series.frequency,
                period_convention=series.period_convention,
                units=series.units,
                seasonal_adjustment=series.seasonal_adjustment,
                original_source=series.original_source,
                reference_url=series.reference_url,
                vintage_basis=series.vintage_basis,
                license=license_,
                max_staleness=series.max_staleness,
                is_active=series.is_active,
                source=SourceInfo(
                    code=source.code,
                    name=source.name,
                    attribution=source.attribution_text,
                    terms_url=source.terms_url,
                ),
            )
            for series, source, license_ in session.execute(query)
        ]
        _require_permitted(metas)
        return metas

    @staticmethod
    def _latest(session: Session, series_ids: Sequence[int]) -> dict[int, LatestObservation]:
        if not series_ids:
            return {}
        o = ObservationRow
        query = (
            select(o)
            .where(o.series_id.in_(series_ids), o.vintage_end.is_(None), o.value.is_not(None))
            .order_by(o.series_id, o.period_start.desc())
            .ext(distinct_on(o.series_id))
        )
        latest: dict[int, LatestObservation] = {}
        for row in session.scalars(query):
            if row.value is None:  # pragma: no cover  (excluded by the query)
                continue
            latest[row.series_id] = LatestObservation(
                period_start=row.period_start,
                period_end=row.period_end,
                source_period=row.source_period,
                value=row.value,
                status=row.status,
                vintage_start=row.vintage_start,
                first_retrieved_at=row.first_retrieved_at,
                last_confirmed_at=row.last_confirmed_at,
            )
        return latest

    def list_series(self) -> list[tuple[SeriesMeta, LatestObservation | None]]:
        with self._factory() as session:
            metas = self._metas(session)
            latest = self._latest(session, [m.id for m in metas])
        return [(m, latest.get(m.id)) for m in metas]

    def get_series(self, code: str) -> tuple[SeriesMeta, LatestObservation | None] | None:
        with self._factory() as session:
            metas = self._metas(session, code)
            if not metas:
                return None
            meta = metas[0]
            return meta, self._latest(session, [meta.id]).get(meta.id)

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
        _require_permitted([meta])
        with self._factory() as session:
            if as_of is not None:
                result = series_as_of(session, meta.id, as_of, period_start=start, period_end=end)
                rows = [
                    Observation(
                        period_start=r.period_start,
                        period_end=r.period_end,
                        source_period=r.source_period,
                        value=r.value,
                        status=r.status,
                        vintage_start=r.vintage_start,
                        vintage_end=r.vintage_end,
                    )
                    for r in result.observations
                ]
                return ObservationPage(len(rows), rows[offset : offset + limit])

            o = ObservationRow
            conditions = [o.series_id == meta.id, o.vintage_end.is_(None)]
            if start is not None:
                conditions.append(o.period_start >= start)
            if end is not None:
                conditions.append(o.period_start <= end)
            where = and_(*conditions)
            total = session.scalar(select(func.count()).select_from(o).where(where)) or 0
            page = session.scalars(
                select(o).where(where).order_by(o.period_start).limit(limit).offset(offset)
            )
            return ObservationPage(
                total,
                [
                    Observation(
                        period_start=r.period_start,
                        period_end=r.period_end,
                        source_period=r.source_period,
                        value=r.value,
                        status=r.status,
                        vintage_start=r.vintage_start,
                        vintage_end=r.vintage_end,
                    )
                    for r in page
                ],
            )


# Re-exported for typing in other API modules.
__all__ = [
    "DatabaseSeriesRepository",
    "ObservationPage",
    "SeriesMeta",
    "SeriesRepository",
    "is_permitted",
]
