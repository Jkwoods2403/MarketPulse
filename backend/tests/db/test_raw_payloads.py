"""Recording raw payload metadata in ops.raw_payloads (synthetic records)."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from marketpulse.db.models import IngestionRun, RawPayload, Source
from marketpulse.ingestion.raw_store import RawPayloadStore, record_raw_payload
from marketpulse.sources.http import FetchResult


def test_record_raw_payload_links_run_source_and_file(session: Session, tmp_path: Path) -> None:
    source = Source(
        code="test_source",
        name="Test",
        homepage_url="https://example.invalid",
        license="public_domain",
    )
    run = IngestionRun(job="test.job")
    session.add_all([source, run])
    session.flush()

    fetched = FetchResult(
        content=b'{"synthetic": true}',
        status_code=200,
        content_type="application/json",
        url="https://api.example.com/v1/series?series_id=X&api_key=REDACTED",
        retrieved_at=datetime(2026, 11, 1, 14, 30, tzinfo=UTC),
        elapsed_seconds=0.2,
        attempts=1,
    )
    stored = RawPayloadStore(tmp_path).save("test_source", "series", fetched)

    row = record_raw_payload(
        session, ingestion_run_id=run.id, source_id=source.id, result=fetched, stored=stored
    )
    session.expire_all()
    loaded = session.get(RawPayload, row.id)

    assert loaded is not None
    assert loaded.request_url == fetched.url
    assert loaded.file_path == stored.relative_path
    assert loaded.sha256 == stored.sha256
    assert loaded.size_bytes == len(fetched.content)
    assert loaded.http_status == 200
    assert loaded.retrieved_at == fetched.retrieved_at
