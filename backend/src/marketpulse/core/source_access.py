"""Source-level ingestion block (ADR-0014).

A source listed in INGESTION_BLOCKED_SOURCES is never ingested through the application: no
ingestion run, raw payload (file or row), or observation may be created for it. This is separate
from, and in addition to, the series-level license guard of the EIA job (ADR-0008), which is
unchanged.

The list is a code constant, deliberately: it is not read from the environment, settings, the
command line, or the series catalog, so no flag, variable, configuration file, or catalog passed
at runtime can lift it. Removing a source from it is a reviewed code change that must cite the
recorded licensing answer and the project owner's decision (ADR-0014, docs/data-sources.md).

Checked at every shared write path (run tracker, raw payload store and row, observation
writers) and by each job's entry point before it creates a provider client.

A blocked source is also never served: the data API's repository excludes every series of a
blocked source (api/repository.py), so observations already stored for it (for example FRED
data in production) stay in the database untouched but never reach a response. Read-only
verification (`marketpulse verify`, including --replay) still checks them; it writes nothing and
contacts no provider.

Blocked sources:
- ercot: licensing clarification pending (ADR-0014).
- fred: retired as a data provider (project owner's decision, 2026-11-11): Market Pulse obtains
  data only directly from the original publishing organizations. Independently, FRED's terms
  require the Federal Reserve Bank of St. Louis's prior written consent to store FRED content in
  a database (docs/data-sources.md). The FRED connector and its stored records are kept, not
  deleted, pending a separately approved plan.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

INGESTION_BLOCKED_SOURCES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ercot": (
            "ERCOT licensing clarification is pending (ERCOT ticket SS0007218). ADR-0014 forbids "
            "any ERCOT ingestion, staging included, until ERCOT's written answer and the project "
            "owner's decision are recorded in docs/data-sources.md"
        ),
        "fred": (
            "FRED is retired as a data provider (project owner's decision, 2026-11-11): Market "
            "Pulse obtains data only directly from the original publishing organizations. FRED's "
            "terms also require the Federal Reserve Bank of St. Louis's prior written consent to "
            "store FRED content in a database (docs/data-sources.md). Stored FRED records are "
            "kept unchanged and are never served"
        ),
    }
)


class SourceIngestionBlockedError(RuntimeError):
    """Ingestion was attempted for a source whose ingestion is blocked (ADR-0014)."""

    def __init__(self, source_code: str, reason: str) -> None:
        self.source_code = source_code
        self.reason = reason
        super().__init__(f"ingestion of source {source_code!r} is blocked: {reason}")


def _normalize(source_code: str) -> str:
    return source_code.strip().casefold()


def ingestion_block_reason(source_code: str) -> str | None:
    """Why ingestion of `source_code` is blocked, or None when it is not (case-insensitive)."""
    return INGESTION_BLOCKED_SOURCES.get(_normalize(source_code))


def require_source_ingestion_permitted(source_code: str) -> None:
    """Raise SourceIngestionBlockedError if ingestion of `source_code` is blocked."""
    reason = ingestion_block_reason(source_code)
    if reason is not None:
        raise SourceIngestionBlockedError(_normalize(source_code), reason)
