"""The EIA simulator reproduces the real recorded responses exactly.

Each test loads the real rows from a fixture into the simulator (padding with synthetic rows
where the fixture shows only part of a series), sends the request that produced the fixture,
and compares the simulator's response with the real one: parsed JSON for data responses
(including warnings, totals as strings/numbers, echo parameters, and row field values), and
bytes for error bodies. If EIA's behavior and the simulator disagree, these tests fail.
"""

import json
from datetime import date, timedelta
from typing import Any

import httpx

from eia_mock import eia_fixture
from eia_sim import FakeEia, SimRoute, SimSeries, make_rows

KEY = "SIMULATED-EIA-KEY-0000000000000000000000"
WEEKLY_PETROLEUM = SimRoute([("weekly", "YYYY-MM-DD"), ("four-week-average", "YYYY-MM-DD")])


def fixture_json(name: str) -> dict[str, Any]:
    return json.loads(eia_fixture(name))


def send(fake: FakeEia, path: str, params: dict[str, str]) -> httpx.Response:
    with httpx.Client(transport=fake.transport(), base_url="https://api.eia.gov/v2/") as c:
        return c.get(path, params={**params, "api_key": KEY})


def fridays_after(last: str, n: int, value: str = "1") -> list[tuple[str, str | None]]:
    d = date.fromisoformat(last)
    return [((d + timedelta(weeks=i + 1)).isoformat(), value) for i in range(n)]


def test_native_weekly_window_matches_the_real_response() -> None:
    real = fixture_json("native_wcestus1_2026_jan_feb")
    rows = real["response"]["data"]
    fake = FakeEia(KEY)
    before = make_rows("WCESTUS1", rows[0]["series-description"], "MBBL", [("2025-12-26", "1")])
    after = make_rows(
        "WCESTUS1", rows[0]["series-description"], "MBBL", fridays_after(rows[-1]["period"], 3)
    )
    fake.add(
        "PET.WCESTUS1.W",
        SimSeries("petroleum/sum/sndw", "WCESTUS1", "weekly", before + rows + after),
        WEEKLY_PETROLEUM,
    )
    simulated = send(
        fake,
        "petroleum/sum/sndw/data",
        {
            "frequency": "weekly",
            "data[0]": "value",
            "facets[series][0]": "WCESTUS1",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "start": "2026-01-01",
            "end": "2026-02-28",
        },
    ).json()
    assert simulated == real


def test_native_monthly_window_matches_the_real_response() -> None:
    real = fixture_json("native_n9133us2_2025")
    rows = real["response"]["data"]
    fake = FakeEia(KEY)
    extra = make_rows(
        "N9133US2", rows[0]["series-description"], "MMCF", [("2024-12", "1"), ("2026-01", "2")]
    )
    fake.add(
        "NG.N9133US2.M",
        SimSeries("natural-gas/move/expc", "N9133US2", "monthly", [extra[0], *rows, extra[1]]),
        SimRoute([("monthly", "YYYY-MM"), ("annual", "YYYY")]),
    )
    simulated = send(
        fake,
        "natural-gas/move/expc/data",
        {
            "frequency": "monthly",
            "data[0]": "value",
            "facets[series][0]": "N9133US2",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "start": "2025-01",
            "end": "2025-12",
        },
    ).json()
    assert simulated == real


def storage_sim() -> FakeEia:
    """878 rows like the real series: the 6 real rows first, then synthetic Fridays."""
    page1 = fixture_json("native_storage_page1")["response"]["data"]
    page2 = fixture_json("native_storage_page2")["response"]["data"]
    real = page1 + page2
    padding = make_rows(
        "NW2_EPG0_SWO_R48_BCF",
        real[0]["series-description"],
        "BCF",
        fridays_after(real[-1]["period"], 878 - 6),
    )
    fake = FakeEia(KEY)
    fake.add(
        "NG.NW2_EPG0_SWO_R48_BCF.W",
        SimSeries("natural-gas/stor/wkly", "NW2_EPG0_SWO_R48_BCF", "weekly", real + padding),
        SimRoute([("weekly", "YYYY-MM-DD")]),
    )
    return fake


def test_native_paging_pages_match_the_real_responses_including_warnings() -> None:
    fake = storage_sim()
    for name, offset in (("native_storage_page1", "0"), ("native_storage_page2", "3")):
        simulated = send(
            fake,
            "natural-gas/stor/wkly/data",
            {
                "frequency": "weekly",
                "data[0]": "value",
                "facets[series][0]": "NW2_EPG0_SWO_R48_BCF",
                "sort[0][column]": "period",
                "sort[0][direction]": "asc",
                "offset": offset,
                "length": "3",
            },
        ).json()
        assert simulated == fixture_json(name)


def test_unknown_series_on_a_native_route_matches_the_real_empty_response() -> None:
    fake = FakeEia(KEY)
    fake.routes["petroleum/sum/sndw"] = WEEKLY_PETROLEUM
    simulated = send(
        fake,
        "petroleum/sum/sndw/data",
        {
            "frequency": "weekly",
            "data[0]": "value",
            "facets[series][0]": "MARKETPULSENOPE",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
        },
    ).json()
    assert simulated == fixture_json("native_unknown_facet_empty")


def test_series_id_route_matches_the_real_response() -> None:
    real = fixture_json("seriesid_wcestus1_sort_asc_ignored")
    newest = list(reversed(real["response"]["data"]))  # real rows, ascending
    first = date.fromisoformat(newest[0]["period"]) - timedelta(weeks=2300 - 3)
    older = make_rows(
        "WCESTUS1",
        newest[0]["series-description"],
        "MBBL",
        [((first + timedelta(weeks=i)).isoformat(), "1") for i in range(2300 - 3)],
    )
    as_strings = [{**r, "value": str(r["value"])} for r in newest]
    fake = FakeEia(KEY)
    fake.add(
        "PET.WCESTUS1.W",
        SimSeries(
            "petroleum/sum/sndw",
            "WCESTUS1",
            "weekly",
            older + as_strings,
            seriesid_description=real["response"]["description"],
        ),
        WEEKLY_PETROLEUM,
    )
    simulated = send(
        fake,
        "seriesid/PET.WCESTUS1.W",
        {"sort[0][column]": "period", "sort[0][direction]": "asc", "length": "3"},
    ).json()
    assert simulated == real  # JSON numbers, desc order despite asc, no warnings


def test_length_over_limit_reproduces_both_real_warnings() -> None:
    real = json.loads(eia_fixture("native_overlimit_length_6000"))
    fake = storage_sim()
    simulated = send(
        fake,
        "natural-gas/stor/wkly/data",
        {
            "frequency": "weekly",
            "data[0]": "value",
            "facets[series][0]": "NW2_EPG0_SWO_R48_BCF",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": "6000",
        },
    ).json()
    # 878 rows fit, so only the length warning applies here; add rows to exceed 5,000:
    assert simulated["warnings"] == [real["warnings"][0]]
    big = FakeEia(KEY)
    rows = make_rows("BIG1", "Synthetic (MBBL)", "MBBL", fridays_after("1900-01-05", 6000))
    big.add("PET.BIG1.W", SimSeries("petroleum/sum/sndw", "BIG1", "weekly", rows), WEEKLY_PETROLEUM)
    simulated = send(
        big,
        "petroleum/sum/sndw/data",
        {
            "frequency": "weekly",
            "data[0]": "value",
            "facets[series][0]": "BIG1",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": "6000",
        },
    ).json()
    assert simulated["warnings"] == real["warnings"]  # both, verbatim, in the same order
    assert (simulated["response"]["total"], len(simulated["response"]["data"])) == ("6000", 5000)


def test_error_bodies_match_the_real_responses_byte_for_byte() -> None:
    fake = FakeEia(KEY)
    fake.routes["petroleum/sum/sndw"] = WEEKLY_PETROLEUM
    cases = [
        ("seriesid/PET.MARKETPULSENOPE.W", {}, "error_unknown_series_id", 404),
        (
            "petroleum/sum/sndw/data",
            {"frequency": "weekly", "data[0]": "monsters", "facets[series][0]": "WCESTUS1"},
            "error_invalid_data_column",
            400,
        ),
        (
            "petroleum/sum/sndw/data",
            {"frequency": "hourly", "data[0]": "value", "facets[series][0]": "WCESTUS1"},
            "error_invalid_frequency",
            400,
        ),
    ]
    for path, params, name, status in cases:
        response = send(fake, path, params)
        assert response.status_code == status
        assert response.content == eia_fixture(name)
    with httpx.Client(transport=fake.transport(), base_url="https://api.eia.gov/v2/") as c:
        response = c.get("seriesid/PET.WCESTUS1.W", params={"api_key": "WRONG"})
    assert response.status_code == 403
    assert response.content == eia_fixture("error_invalid_api_key")
