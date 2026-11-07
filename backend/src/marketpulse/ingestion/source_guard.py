"""Database-side checks of the source-level ingestion block (core.source_access, ADR-0014).

Used by every shared ingestion write path (run tracker, raw payload rows, observation writers),
so a blocked source is refused before any row is written, whichever job tries.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketpulse.core.source_access import require_source_ingestion_permitted
from marketpulse.db.models import Source


def require_source_id_permitted(session: Session, source_id: int | None) -> None:
    """Raise SourceIngestionBlockedError if `source_id` belongs to a blocked source.

    A run without a source (`None`) is not tied to any provider and is allowed. An unknown id is
    left to the foreign key, which rejects it when the row is written.
    """
    if source_id is None:
        return
    code = session.scalar(select(Source.code).where(Source.id == source_id))
    if code is not None:
        require_source_ingestion_permitted(code)
