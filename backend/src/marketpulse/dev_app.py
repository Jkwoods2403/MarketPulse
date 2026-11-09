"""Development-only API serving SYNTHETIC fixtures (`marketpulse serve --synthetic`).

Used to build the dashboard while no single database holds both EIA and FRED data. It reads
series metadata from the version-controlled catalog, serves generated values from memory
(never a database), and marks every response `data_mode = "synthetic"`. It refuses to start
unless MARKETPULSE_ENV is "development".
"""

from datetime import datetime, timedelta

from fastapi import FastAPI

from marketpulse.api.main import create_app
from marketpulse.api.repository import SeriesMeta
from marketpulse.api.schemas import SourceInfo
from marketpulse.api.synthetic import SyntheticSeriesRepository
from marketpulse.core.config import Environment, Settings, get_settings
from marketpulse.sources.catalog import Catalog, load_catalog


class SyntheticModeError(RuntimeError):
    """Synthetic fixtures were requested outside the development environment."""


def catalog_metas(catalog: Catalog) -> list[SeriesMeta]:
    """Every catalogued series as API metadata (the repository applies the licensing filter)."""
    metas = []
    for index, (source_code, spec) in enumerate(catalog.series, start=1):
        source = catalog.sources[source_code]
        metas.append(
            SeriesMeta(
                id=-index,  # not a database id
                code=spec.code,
                name=spec.name,
                description=spec.description,
                category=spec.category.value,
                frequency=spec.frequency.value,
                period_convention=spec.period_convention,
                units=spec.units,
                seasonal_adjustment=(
                    spec.seasonal_adjustment.value if spec.seasonal_adjustment else None
                ),
                original_source=spec.original_source,
                reference_url=spec.reference_url,
                vintage_basis=(spec.vintage_basis or source.vintage_basis).value,
                license=(spec.license_override or source.license).value,
                max_staleness=timedelta(days=spec.max_staleness_days),
                is_active=True,
                source=SourceInfo(
                    code=source.code,
                    name=source.name,
                    attribution=source.attribution_text,
                    terms_url=source.terms_url,
                ),
            )
        )
    return metas


def create_synthetic_app(
    settings: Settings | None = None, *, now: datetime | None = None
) -> FastAPI:
    settings = settings or get_settings()
    if settings.marketpulse_env is not Environment.DEVELOPMENT:
        raise SyntheticModeError(
            "synthetic fixtures are only served in the development environment "
            f"(MARKETPULSE_ENV is {settings.marketpulse_env.value!r})"
        )
    repository = SyntheticSeriesRepository(catalog_metas(load_catalog()), now=now)
    return create_app(settings, repository=repository)
