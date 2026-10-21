"""scripts/fetch_lines.py CLI wiring, especially the --api-only deploy path.

The real fetch hits 20fl, so every test stubs the network boundary; the point
is to prove the cloudy branches (skip when fresh, no browser, direct API) go
where they should.
"""

from __future__ import annotations

import sys

import pytest

import scripts.fetch_lines as fetch_lines

_ROUTE = {
    "line": "811",
    "routeID": "R1",
    "direction": "1",
    "source": "Tel Aviv",
    "dest": "Shefayim",
}
_STOP = {
    "stopID": "1",
    "code": "1",
    "he": "תחנה",
    "en": "Stop One",
    "lat": 32.0,
    "lon": 34.7,
    "index": 1,
    "stopType": 0,
}


async def _api_payload() -> dict:
    return {"routes": [_ROUTE], "stops_by_route": {"R1": [_STOP]}}


def _run_main(monkeypatch: pytest.MonkeyPatch, *args: str, fresh: bool = False) -> int:
    monkeypatch.setattr(sys, "argv", ["fetch_lines.py", *args])
    monkeypatch.setattr(fetch_lines, "cache_is_fresh", lambda _path=None: fresh)
    return fetch_lines.main()


def test_fresh_cache_skips_any_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode():
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(fetch_lines, "fetch_via_api", explode)
    monkeypatch.setattr(fetch_lines, "scrape_with_playwright", explode)
    assert _run_main(monkeypatch, "--api-only", fresh=True) == 0


def test_api_only_uses_the_direct_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    saved: list[list[dict]] = []

    def save(records, schedule=None):
        saved.append(records)

    monkeypatch.setattr(fetch_lines, "fetch_via_api", _api_payload)
    monkeypatch.setattr(fetch_lines.config, "STOPS_CACHE", tmp_path / "stops.json")

    async def no_browser():
        raise AssertionError("the browser must not be used")

    monkeypatch.setattr(fetch_lines, "scrape_with_playwright", no_browser)
    monkeypatch.setattr(fetch_lines, "save", save)

    assert _run_main(monkeypatch, "--api-only", fresh=False) == 0
    assert len(saved) == 1
    assert saved[0][0]["line"] == "811"
    assert saved[0][0]["stop_count"] == 1


def test_browser_failure_falls_back_to_the_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    saved: list[list[dict]] = []

    def save(records, schedule=None):
        saved.append(records)

    async def broken_browser():
        raise RuntimeError("chromium missing")

    monkeypatch.setattr(fetch_lines, "scrape_with_playwright", broken_browser)
    monkeypatch.setattr(fetch_lines, "fetch_via_api", _api_payload)
    monkeypatch.setattr(fetch_lines.config, "STOPS_CACHE", tmp_path / "stops.json")
    monkeypatch.setattr(fetch_lines, "save", save)

    assert _run_main(monkeypatch, fresh=False) == 0
    assert saved[0][0]["stop_count"] == 1


def test_no_lines_in_the_response_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async def empty_payload() -> dict:
        return {"routes": [], "stops_by_route": {}}

    caught: list[list[dict]] = []

    def save(records, schedule=None):
        caught.append(records)

    monkeypatch.setattr(fetch_lines, "fetch_via_api", empty_payload)
    monkeypatch.setattr(fetch_lines, "scrape_with_playwright", empty_payload)
    monkeypatch.setattr(fetch_lines.config, "STOPS_CACHE", tmp_path / "stops.json")
    monkeypatch.setattr(fetch_lines, "save", save)

    assert _run_main(monkeypatch, "--api-only", fresh=False) == 1
    assert caught == []

def test_a_stored_timetable_survives_a_rebuild() -> None:
    """The 50 km/h guess must not overwrite the operator's real minutes.

    scripts/fetch_schedule.py stamps the cache once it has written the
    timetable. Rebuilding the routes regenerates ride_min from the road
    distance, so the stamp is what tells this script to copy the real numbers
    back instead.
    """
    previous = {
        "schedule_fetched_at": "2026-10-21T11:36:22+03:00",
        "lines": [
            {
                "line": "812",
                "stops": [
                    {"code": 20314, "ride_min": 12.5, "return_min": 19.2},
                ],
            }
        ],
    }
    records = [
        {
            "line": "812",
            "stops": [
                {"code": 20314, "ride_min": 18.0, "return_min": 22.0},
                {"code": 28567, "ride_min": 18.2, "return_min": 21.0},
            ],
        }
    ]

    schedule = fetch_lines.carry_over_schedule(records, previous)

    assert records[0]["stops"][0]["ride_min"] == 12.5
    assert records[0]["stops"][0]["return_min"] == 19.2
    # A stop the old cache never saw keeps the fresh estimate.
    assert records[0]["stops"][1]["ride_min"] == 18.2
    assert schedule == {"schedule_fetched_at": previous["schedule_fetched_at"]}


def test_a_cache_without_a_schedule_is_not_carried_over() -> None:
    previous = {"lines": [{"line": "812", "stops": [{"code": 20314, "ride_min": 12.5}]}]}
    records = [{"line": "812", "stops": [{"code": 20314, "ride_min": 18.0}]}]

    assert fetch_lines.carry_over_schedule(records, previous) is None
    assert records[0]["stops"][0]["ride_min"] == 18.0
    assert fetch_lines.carry_over_schedule(records, None) is None
