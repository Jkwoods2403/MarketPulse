"""Version-controlled catalog of data sources and tracked series.

The YAML files in `data/` are the single, reviewed definition of what Market Pulse tracks:

    data/sources.yaml          one entry per provider (terms, license, attribution)
    data/series/<source>.yaml  one file per provider, listing its tracked series

To add an indicator: verify it against the provider's official page, add an entry to the
provider's file, run `marketpulse catalog validate`, then `marketpulse catalog sync`.
"""

from marketpulse.sources.catalog.loader import Catalog, CatalogError, load_catalog
from marketpulse.sources.catalog.schema import SeriesSpec, SourceSpec

__all__ = ["Catalog", "CatalogError", "SeriesSpec", "SourceSpec", "load_catalog"]
