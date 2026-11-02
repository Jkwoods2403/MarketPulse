"""Integrity of the recorded EIA fixtures: hashes, labels, no secrets, and no Refinitiv data."""

import gzip
import hashlib
import json
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "eia"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
ENTRIES = {entry["file"]: entry for entry in MANIFEST}
# Refinitiv-sourced EIA series (personal_use_only): must never be committed.
REFINITIV_SERIES = ("RWTC", "RBRTE", "RNGWHHD")


def body(name: str) -> bytes:
    raw = (FIXTURES / name).read_bytes()
    return gzip.decompress(raw) if ENTRIES[name]["compressed"] else raw


def test_manifest_lists_exactly_the_fixture_files() -> None:
    on_disk = {p.name for p in FIXTURES.iterdir()} - {"manifest.json", "README.md"}
    assert set(ENTRIES) == on_disk


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_fixture_is_unmodified_and_labelled(name: str) -> None:
    entry = ENTRIES[name]
    content = body(name)
    assert hashlib.sha256(content).hexdigest() == entry["sha256"]
    assert len(content) == entry["size_bytes"]
    assert entry["purpose"]
    assert entry["request_url"].startswith("https://api.eia.gov/v2/")
    assert entry["retrieved_at"].startswith("2026-")


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_no_api_key_anywhere(name: str) -> None:
    entry = ENTRIES[name]
    assert re.findall(r"api_key=([^&]*)", entry["request_url"]) == ["REDACTED"]
    assert not re.search(r"\b[A-Za-z0-9]{40}\b", body(name).decode())  # EIA keys: 40 characters


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_no_refinitiv_sourced_data_is_committed(name: str) -> None:
    text = body(name).decode() + ENTRIES[name]["request_url"] + ENTRIES[name]["purpose"]
    for series in REFINITIV_SERIES:
        assert series not in text, f"{name} contains Refinitiv-sourced series {series}"


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_success_and_error_fixtures_have_expected_shape(name: str) -> None:
    entry, data = ENTRIES[name], json.loads(body(name))
    if entry["http_status"] == 200:
        assert "response" in data and "request" in data
    else:
        assert "error" in data
