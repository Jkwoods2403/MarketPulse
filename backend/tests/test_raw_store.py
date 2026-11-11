"""RawPayloadStore: integrity, atomicity, idempotence, and path safety. Payloads are synthetic.

The sample source is "eia", a permitted source: the store refuses blocked sources (ERCOT, FRED)
before writing anything (tests/test_source_ingestion_guard.py, tests/test_fred_source_block.py).
"""

import gzip
import hashlib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from marketpulse.ingestion.raw_store import (
    RawPayloadIntegrityError,
    RawPayloadStore,
    StoredPayload,
)
from marketpulse.sources.http import FetchResult

RETRIEVED = datetime(2026, 11, 1, 14, 30, 5, 123456, tzinfo=UTC)
BODY = b'{"observations": [{"date": "2026-10-30", "value": "4.1"}]}'  # synthetic test payload


def result(
    content: bytes = BODY,
    content_type: str | None = "application/json; charset=utf-8",
    retrieved_at: datetime = RETRIEVED,
) -> FetchResult:
    return FetchResult(
        content=content,
        status_code=200,
        content_type=content_type,
        url="https://api.example.com/v1/series?api_key=REDACTED",
        retrieved_at=retrieved_at,
        elapsed_seconds=0.1,
        attempts=1,
    )


def test_save_writes_compressed_payload_with_hash_of_original_bytes(tmp_path: Path) -> None:
    stored = RawPayloadStore(tmp_path).save("eia", "observations", result())

    digest = hashlib.sha256(BODY).hexdigest()
    assert stored.sha256 == digest
    assert stored.size_bytes == len(BODY)
    assert stored.relative_path == (
        f"raw/eia/observations/2026/11/20261101T143005123456Z_{digest[:12]}.json.gz"
    )
    assert gzip.decompress((tmp_path / stored.relative_path).read_bytes()) == BODY


def test_load_round_trips_and_verifies(tmp_path: Path) -> None:
    store = RawPayloadStore(tmp_path)
    stored = store.save("eia", "petroleum-weekly", result())
    assert store.load(stored) == BODY


def test_load_detects_tampering(tmp_path: Path) -> None:
    store = RawPayloadStore(tmp_path)
    stored = store.save("eia", "observations", result())
    (tmp_path / stored.relative_path).write_bytes(gzip.compress(b'{"tampered": true}'))

    with pytest.raises(RawPayloadIntegrityError):
        store.load(stored)


def test_saving_identical_payload_twice_is_idempotent(tmp_path: Path) -> None:
    store = RawPayloadStore(tmp_path)
    first = store.save("eia", "observations", result())
    second = store.save("eia", "observations", result())
    assert first == second
    assert len(list(tmp_path.rglob("*.gz"))) == 1


def test_never_overwrites_a_different_file_at_the_same_path(tmp_path: Path) -> None:
    store = RawPayloadStore(tmp_path)
    stored = store.save("eia", "observations", result())
    path = tmp_path / stored.relative_path
    path.write_bytes(gzip.compress(b"something else"))

    with pytest.raises(FileExistsError):
        store.save("eia", "observations", result())
    assert gzip.decompress(path.read_bytes()) == b"something else"


def test_compressed_output_is_deterministic(tmp_path: Path) -> None:
    a = RawPayloadStore(tmp_path / "a").save("eia", "obs", result())
    b = RawPayloadStore(tmp_path / "b").save("eia", "obs", result())
    assert (tmp_path / "a" / a.relative_path).read_bytes() == (
        tmp_path / "b" / b.relative_path
    ).read_bytes()


def test_no_temporary_files_left_behind(tmp_path: Path) -> None:
    RawPayloadStore(tmp_path).save("eia", "observations", result())
    assert [p.name for p in tmp_path.rglob(".tmp-*")] == []


def test_timestamp_is_normalized_to_utc(tmp_path: Path) -> None:
    central = timezone(timedelta(hours=-5))
    local = datetime(2026, 11, 1, 9, 30, 5, 123456, tzinfo=central)  # == RETRIEVED
    stored = RawPayloadStore(tmp_path).save("eia", "observations", result(retrieved_at=local))
    assert "/20261101T143005123456Z_" in stored.relative_path


def test_rejects_naive_timestamp(tmp_path: Path) -> None:
    naive = datetime(2026, 11, 1, 14, 30)  # noqa: DTZ001  (deliberately naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        RawPayloadStore(tmp_path).save("eia", "observations", result(retrieved_at=naive))


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [
        ("application/json", "json"),
        ("text/csv; charset=utf-8", "csv"),
        ("text/xml", "xml"),
        (None, "bin"),
        ("application/octet-stream", "bin"),
    ],
)
def test_extension_follows_content_type(
    tmp_path: Path, content_type: str | None, extension: str
) -> None:
    stored = RawPayloadStore(tmp_path).save("eia", "obs", result(content_type=content_type))
    assert stored.relative_path.endswith(f".{extension}.gz")


@pytest.mark.parametrize(
    "segment", ["../escape", "..", "a/b", "a\\b", "UPPER", "", " space", "x" * 65, "-leading"]
)
def test_rejects_unsafe_source_or_dataset_names(tmp_path: Path, segment: str) -> None:
    store = RawPayloadStore(tmp_path)
    with pytest.raises(ValueError):
        store.save(segment, "observations", result())
    with pytest.raises(ValueError):
        store.save("eia", segment, result())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "relative_path", ["../outside.json.gz", "/etc/passwd", "raw/../../outside.json.gz"]
)
def test_load_rejects_paths_outside_the_data_directory(tmp_path: Path, relative_path: str) -> None:
    store = RawPayloadStore(tmp_path / "data")
    with pytest.raises(ValueError):
        store.load(StoredPayload(relative_path, "0" * 64, 0))
