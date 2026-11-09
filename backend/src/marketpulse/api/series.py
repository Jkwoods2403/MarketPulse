"""Read-only series endpoints. Only permitted series are served (see api.repository)."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from marketpulse.api.freshness import assess_freshness
from marketpulse.api.repository import SeriesMeta, SeriesRepository
from marketpulse.api.schemas import (
    DATABASE_UNAVAILABLE,
    ErrorResponse,
    LatestObservation,
    ObservationsResponse,
    SeriesListResponse,
    SeriesSummary,
    ValidationErrorResponse,
)

router = APIRouter(tags=["series"])

MAX_LIMIT = 5000
NOT_FOUND = "series not found"  # same answer for unknown and not-permitted codes


def _repository(request: Request) -> SeriesRepository:
    repository: SeriesRepository = request.app.state.repository
    return repository


def _now(request: Request) -> datetime:
    clock: Callable[[], datetime] = request.app.state.clock
    return clock()


def _summary(
    repository: SeriesRepository,
    meta: SeriesMeta,
    latest: LatestObservation | None,
    today: date,
) -> SeriesSummary:
    return SeriesSummary(
        code=meta.code,
        name=meta.name,
        description=meta.description,
        category=meta.category,
        frequency=meta.frequency,
        period_convention=meta.period_convention,
        units=meta.units,
        seasonal_adjustment=meta.seasonal_adjustment,
        original_source=meta.original_source,
        reference_url=meta.reference_url,
        vintage_basis=meta.vintage_basis,
        license=meta.license,
        source=meta.source,
        synthetic=repository.data_mode == "synthetic",
        latest=latest,
        freshness=assess_freshness(
            latest.period_end if latest else None, meta.max_staleness, today
        ),
    )


@router.get("/series", response_model=SeriesListResponse, responses=DATABASE_UNAVAILABLE)
def list_series(request: Request) -> SeriesListResponse:
    """Every permitted series with its latest observation and freshness."""
    repository = _repository(request)
    now = _now(request)
    today = now.astimezone(UTC).date()
    return SeriesListResponse(
        data_mode=repository.data_mode,
        generated_at=now,
        series=[
            _summary(repository, meta, latest, today) for meta, latest in repository.list_series()
        ],
    )


@router.get(
    "/series/{code}/observations",
    response_model=ObservationsResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Unknown or not permitted series"},
        422: {"model": ValidationErrorResponse, "description": "Invalid parameters"},
        **DATABASE_UNAVAILABLE,
    },
)
def series_observations(
    request: Request,
    code: str,
    start: Annotated[date | None, Query(description="First period_start included")] = None,
    end: Annotated[date | None, Query(description="Last period_start included")] = None,
    as_of: Annotated[
        datetime | None,
        Query(description="Point-in-time: the versions active at this instant (timezone-aware)"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 1000,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ObservationsResponse:
    """Observations of one permitted series, oldest period first.

    Without `as_of`: the current version of each period. With `as_of`: the versions that were
    active at that instant (no look-ahead). Periods are filtered by `period_start`.
    """
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start must not be after end")
    if as_of is not None and as_of.tzinfo is None:
        raise HTTPException(status_code=422, detail="as_of must include a timezone")

    repository = _repository(request)
    found = repository.get_series(code)
    if found is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    meta, latest = found
    page = repository.observations(
        meta, start=start, end=end, as_of=as_of, limit=limit, offset=offset
    )
    today = _now(request).astimezone(UTC).date()
    return ObservationsResponse(
        data_mode=repository.data_mode,
        series=_summary(repository, meta, latest, today),
        as_of=as_of,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
        total=page.total,
        observations=page.observations,
    )
