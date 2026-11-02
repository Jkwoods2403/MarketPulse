"""Integrity of the recorded FRED fixtures: hashes, labels, parseability, and no secrets."""

import hashlib
import json
import re

import pytest

from fred_mock import FIXTURES
from marketpulse.sources.fred.parse import (
    parse_observations,
    parse_series_metadata,
    parse_vintage_dates,
)

MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
ENTRIES = {entry["file"]: entry for entry in MANIFEST}


def test_manifest_lists_exactly_the_fixture_files() -> None:
    on_disk = {p.name for p in FIXTURES.glob("*.json")} - {"manifest.json"}
    assert set(ENTRIES) == on_disk


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_fixture_is_unmodified_and_labelled(name: str) -> None:
    entry = ENTRIES[name]
    body = (FIXTURES / name).read_bytes()
    assert hashlib.sha256(body).hexdigest() == entry["sha256"]
    assert len(body) == entry["size_bytes"]
    assert entry["purpose"]
    assert entry["request_url"].startswith("https://api.stlouisfed.org/fred/")
    assert entry["retrieved_at"].startswith("2026-")


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_no_api_key_anywhere_in_fixtures(name: str) -> None:
    entry = ENTRIES[name]
    # The only api_key value allowed in a recorded URL is the redaction marker.
    assert re.findall(r"api_key=([^&]*)", entry["request_url"]) == ["REDACTED"]
    body = (FIXTURES / name).read_text()
    assert "api_key" not in body
    assert not re.search(r"\b[0-9a-f]{32}\b", body)  # FRED keys are 32 hex characters


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_every_success_fixture_parses_and_every_error_fixture_is_fred_json(name: str) -> None:
    entry, body = ENTRIES[name], (FIXTURES / name).read_bytes()
    if entry["http_status"] != 200:
        data = json.loads(body)
        assert data["error_code"] == entry["http_status"]
        assert data["error_message"]
    elif name.startswith("series_"):
        parse_series_metadata(body)
    elif name.startswith("vintagedates_"):
        parse_vintage_dates(body)
    else:
        parse_observations(body)
