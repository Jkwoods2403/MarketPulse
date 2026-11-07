"""Raw provider payload storage (ADR-0007: raw payloads are retained for reprocessing).

Every provider response is saved, exactly as received, before parsing:

    <data_dir>/raw/<source>/<dataset>/<YYYY>/<MM>/<YYYYMMDDTHHMMSSffffffZ>_<sha256[:12]>.<ext>.gz

- `sha256` and `size_bytes` describe the *original, uncompressed* response body, so the hash
  identifies exactly what the provider sent, independent of compression.
- Writes are atomic (temp file + rename), so a crash never leaves a partial file.
- Existing files are never overwritten.
- Reading back verifies the hash, so tampering or corruption is detected.
- `source` and `dataset` are restricted to a safe character set, so paths cannot escape the
  storage directory.

The relative file path is recorded in `ops.raw_payloads` together with the redacted URL.
"""

import gzip
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from marketpulse.core.source_access import require_source_ingestion_permitted
from marketpulse.db.models import RawPayload
from marketpulse.ingestion.source_guard import require_source_id_permitted
from marketpulse.sources.http import FetchResult

_SAFE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_EXTENSIONS = {
    "application/json": "json",
    "text/csv": "csv",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/plain": "txt",
}


class RawPayloadIntegrityError(Exception):
    """A stored payload's content does not match its recorded hash."""


@dataclass(frozen=True)
class StoredPayload:
    relative_path: str  # POSIX-style, relative to the data directory
    sha256: str  # of the original, uncompressed body
    size_bytes: int  # of the original, uncompressed body


def _validate_segment(name: str, value: str) -> str:
    if not _SAFE_SEGMENT.fullmatch(value) or value in {".", ".."} or ".." in value:
        raise ValueError(f"Invalid {name} {value!r}: use lowercase letters, digits, _ . -")
    return value


def _extension(content_type: str | None) -> str:
    if not content_type:
        return "bin"
    media_type = content_type.split(";", 1)[0].strip().lower()
    return _EXTENSIONS.get(media_type, "bin")


class RawPayloadStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir.resolve()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def save(self, source: str, dataset: str, result: FetchResult) -> StoredPayload:
        """Save `result.content` compressed; return its path and hash.

        A blocked source (ADR-0014) is refused before any directory or file is created.
        """
        require_source_ingestion_permitted(source)
        _validate_segment("source", source)
        _validate_segment("dataset", dataset)
        retrieved = result.retrieved_at
        if retrieved.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        retrieved = retrieved.astimezone(UTC)

        digest = hashlib.sha256(result.content).hexdigest()
        relative = PurePosixPath(
            "raw",
            source,
            dataset,
            f"{retrieved:%Y}",
            f"{retrieved:%m}",
            f"{retrieved:%Y%m%dT%H%M%S%fZ}_{digest[:12]}.{_extension(result.content_type)}.gz",
        )
        target = self._resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            if self._read_verified(target, digest) is not None:
                return StoredPayload(str(relative), digest, len(result.content))
            raise FileExistsError(f"Refusing to overwrite existing payload: {relative}")

        # mtime=0 makes the compressed bytes deterministic for identical content.
        compressed = gzip.compress(result.content, compresslevel=6, mtime=0)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".gz")
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(compressed)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return StoredPayload(str(relative), digest, len(result.content))

    def load(self, stored: StoredPayload) -> bytes:
        """Return the original bytes, verifying them against the recorded hash."""
        content = self._read_verified(
            self._resolve(PurePosixPath(stored.relative_path)), stored.sha256
        )
        if content is None:
            raise RawPayloadIntegrityError(f"Hash mismatch for {stored.relative_path}")
        return content

    def _resolve(self, relative: PurePosixPath) -> Path:
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid payload path: {relative}")
        path = (self._data_dir / Path(*relative.parts)).resolve()
        if not path.is_relative_to(self._data_dir):
            raise ValueError(f"Payload path escapes the data directory: {relative}")
        return path

    @staticmethod
    def _read_verified(path: Path, expected_sha256: str) -> bytes | None:
        content = gzip.decompress(path.read_bytes())
        return content if hashlib.sha256(content).hexdigest() == expected_sha256 else None


def record_raw_payload(
    session: Session,
    *,
    ingestion_run_id: int,
    source_id: int,
    result: FetchResult,
    stored: StoredPayload,
) -> RawPayload:
    """Add the `ops.raw_payloads` row linking a run to its saved payload (caller commits).

    A blocked source (ADR-0014) is refused before the row is added.
    """
    require_source_id_permitted(session, source_id)
    row = RawPayload(
        ingestion_run_id=ingestion_run_id,
        source_id=source_id,
        request_url=result.url,  # already redacted by ProviderHttpClient
        http_status=result.status_code,
        retrieved_at=result.retrieved_at,
        file_path=stored.relative_path,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
    )
    session.add(row)
    session.flush()
    return row
