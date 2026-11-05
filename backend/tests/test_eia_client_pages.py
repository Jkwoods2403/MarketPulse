"""EiaClient.fetch_series_pages: page-level provenance, and fetch_series behaving exactly as before.
All series values are SYNTHETIC."""

import hashlib
from typing import Any

import httpx
import pytest

from marketpulse.sources.eia.client import DATASET_DATA
from marketpulse.sources.eia.validate import EiaIncompleteError
from test_eia_client import STORAGE, Harness


def test_pages_keep_offset_url_hash_retrieval_time_and_their_own_rows() -> None:
    h = Harness()
    periods = h.add_storage(10)
    pages = h.client.fetch_series_pages(STORAGE, page_size=4)  # pages of 4, 4, 2

    assert [p.offset for p in pages] == [0, 4, 8]
    assert [[r.period for r in p.rows] for p in pages] == [periods[0:4], periods[4:8], periods[8:]]
    data = [result for dataset, result in h.raw if dataset == DATASET_DATA]
    assert len(data) == 3
    for page, result in zip(pages, data, strict=True):
        assert page.url == result.url  # the redacted URL that is stored
        assert "offset=" + str(page.offset) in page.url
        assert page.sha256 == hashlib.sha256(result.content).hexdigest()
        assert page.retrieved_at == result.retrieved_at
        assert page.retrieved_at.tzinfo is not None


def test_fetch_series_returns_the_concatenated_page_rows_with_the_same_requests() -> None:
    flat, paged = Harness(), Harness()
    flat.add_storage(10)
    paged.add_storage(10)
    rows = flat.client.fetch_series(STORAGE, page_size=4)
    pages = paged.client.fetch_series_pages(STORAGE, page_size=4)
    assert rows == [r for p in pages for r in p.rows]
    assert [str(r.url) for r in flat.fake.requests] == [str(r.url) for r in paged.fake.requests]


def test_windowed_pages_carry_the_window_in_their_urls() -> None:
    h = Harness()
    periods = h.add_storage(20)
    (page,) = h.client.fetch_series_pages(STORAGE, start=periods[5], end=periods[9])
    assert [r.period for r in page.rows] == periods[5:10]
    assert f"start={periods[5]}" in page.url


def test_short_page_is_followed_by_the_next_offset_but_refused_as_incomplete() -> None:
    """The client is not fooled into stopping early (it asks for the missing row at offset 9),
    but a page holding fewer rows than its request should is incomplete, so nothing is
    returned."""
    h = Harness()
    h.add_storage(10)

    def short_final_page(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any] | None:
        if request.url.params.get("offset") == "8":
            body["response"]["data"] = body["response"]["data"][:-1]
            return body
        return None

    h.fake.faults.append(short_final_page)
    with pytest.raises(EiaIncompleteError, match="page at offset 8 holds 1 rows, expected 2"):
        h.client.fetch_series_pages(STORAGE, page_size=4)
    assert [r.url.params["offset"] for r in h.fake.data_requests()] == ["0", "4", "8", "9"]


def test_truncated_pagination_raises_and_keeps_the_raw_pages() -> None:
    h = Harness()
    h.add_storage(10)

    def overstated_total(request: httpx.Request, body: dict[str, Any]) -> dict[str, Any] | None:
        body["response"]["total"] = "11"  # one row never arrives
        return body

    h.fake.faults.append(overstated_total)
    with pytest.raises(EiaIncompleteError, match="received 10 of 11"):
        h.client.fetch_series_pages(STORAGE, page_size=4)
    assert len([d for d, _ in h.raw if d == DATASET_DATA]) == 4  # raw kept for diagnosis
