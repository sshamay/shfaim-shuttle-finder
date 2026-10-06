"""E2E tier fixtures: an offline local server plus a real Playwright browser.

The e2e tier stays true to the repo's offline policy. The FastAPI app runs on
127.0.0.1 in a background thread with Photon geocoding, the Valhalla router
probe, and the stops cache all replaced by deterministic stubs, and the browser
is told to abort any non-local request (tile images, etc.) so nothing ever
leaves the machine. No ``RUN_LIVE`` flag is needed, and the pytest socket guard
in ``tests/conftest.py`` keeps quiet because no outgoing socket is opened by
the Python process.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Iterator

import pytest
import uvicorn

from app import config, geocode
from scripts import find
from tests.conftest import SAMPLE_LINES


def _candidate(
    *,
    label: str,
    city: str,
    housenumber: str,
    lat: float,
    lon: float,
    type_: str,
    out_of_area: bool,
) -> dict:
    """One geocoder answer shaped exactly like the live ``geocode.search`` output."""
    return {
        "label": label,
        "street": label,
        "housenumber": housenumber,
        "city": city,
        "state": "",
        "lat": lat,
        "lon": lon,
        "out_of_area": out_of_area,
        "type": type_,
        "osm_type": "W",
        "osm_id": 1,
        "osm_key": "building",
    }


@pytest.fixture
def app_url(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[str]:
    """Start the real FastAPI app on a free localhost port, fully stubbed.

    The scenario mirrors the live happy path for "Marsel yanko 10": one firm
    local address (the Marcel Janco house), a same-house-number POI on a local
    street, and a homonym abroad. Exactly one local address-type candidate,
    which is the condition the UI's auto-select branch requires.
    """
    from app.web import app as fastapi_app

    cache = tmp_path / "stops.json"
    # SAMPLE_LINES covers central Tel Aviv; the Marcel Janco house is in its
    # north, which the 30-minute walk cutoff would otherwise drain of results.
    # A stop near the house keeps the happy path a real happy path.
    cache.write_text(
        json.dumps({"fetched_at": "2026-01-01T00:00:00+00:00", "lines": SAMPLE_LINES + [
            {
                "line": "811",
                "name": "HaKiriya",
                "stop_count": 1,
                "stops": [{
                    "stop_id": "n1",
                    "code": "711",
                    "name": "שדרות אבא אבן",
                    "name_en": "Abba Hillel Road",
                    "lat": 32.1147,
                    "lon": 34.8240,
                    "index": 5,
                    "is_park_and_ride": False,
                }],
            },
            {
                "line": "815",
                "name": "Kiryat Atidim",
                "stop_count": 1,
                "stops": [{
                    "stop_id": "n2",
                    "code": "712",
                    "name": "דרך אבוקה",
                    "name_en": "Derech Abuka",
                    "lat": 32.1110,
                    "lon": 34.8360,
                    "index": 4,
                    "is_park_and_ride": False,
                }],
            },
        ]})
    )
    monkeypatch.setattr(config, "STOPS_CACHE", cache)
    monkeypatch.setattr(find, "_pick_router", lambda: None)

    async def fake_search(query: str, *, limit: int = 5) -> list[dict]:
        return [
            _candidate(
                label="Marcel Janco 10",
                city="Tel Aviv",
                housenumber="10",
                lat=32.117517,
                lon=34.824959,
                type_="house",
                out_of_area=False,
            ),
            _candidate(
                label="Marsel Brothers 10",
                city="Holon",
                housenumber="10",
                lat=32.02,
                lon=34.77,
                type_="butcher",
                out_of_area=False,
            ),
            _candidate(
                label="Yanko Avenue",
                city="Sydney",
                housenumber="",
                lat=-33.8,
                lon=151.2,
                type_="house",
                out_of_area=True,
            ),
        ]

    monkeypatch.setattr(geocode, "search", fake_search)

    server = uvicorn.Server(
        uvicorn.Config(fastapi_app, host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.025)
        if not server.started:
            raise RuntimeError("the test server did not start in time")
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def page(app_url: str):
    """A headless Chromium page that cannot leave the machine.

    Anything outside the app origin (OSM tiles, etc.) is aborted, so the test
    is deterministic regardless of connectivity and never depends on a remote
    server.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except PlaywrightError as exc:
            pytest.skip(
                f"chromium unavailable: {exc}. Run `python -m playwright install chromium`"
            )
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route(
            "**/*",
            lambda route: (
                route.continue_()
                if route.request.url.startswith(app_url)
                else route.abort()
            ),
        )
        page.goto(app_url, wait_until="load")
        try:
            yield page
        finally:
            browser.close()