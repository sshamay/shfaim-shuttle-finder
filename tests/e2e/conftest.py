"""E2E tier fixtures: an offline local server plus a real Playwright browser.

The e2e tier stays true to the repo's offline policy. The FastAPI app runs on
127.0.0.1 in a background thread with Photon geocoding and the stops cache
replaced by deterministic stubs, and the browser is told to abort any
non-local request (tile images, etc.) so nothing ever leaves the machine. No
``RUN_LIVE`` flag is needed, and the pytest socket guard in ``tests/conftest.py``
keeps quiet because no outgoing socket is opened by the Python process.

Routing is covered three ways. The default ``app_url`` stubs the router probe
out, exercising the degraded estimates path the banner warns about. The
``app_url_valhalla`` and ``app_url_ors`` variants point the app at local
look-alikes of the two real backends, so the full probe-then-route chain runs
against a real HTTP server for each without any network.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from typing import Any, Callable, Iterator

import pytest
import uvicorn

from app import config, geocode
from scripts import find
from tests.conftest import SAMPLE_LINES, ors_response, valhalla_response


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


def _stubs_cache(tmp_path) -> Path:
    """Point config.STOPS_CACHE at SAMPLE_LINES plus two stops near the house.

    SAMPLE_LINES covers central Tel Aviv; the Marcel Janco house is in its
    north, which the 30-minute walk cutoff would otherwise drain of results.
    A stop near the house keeps the happy path a real happy path.
    """
    cache = tmp_path / "stops.json"
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
    return cache


async def _fake_search(query: str, *, limit: int = 5) -> list[dict]:
    """The "Marsel yanko 10" fixture: one firm local address, plus same-number
    POI and a homonym abroad. Exactly one local address-type candidate, which
    is the condition the UI's auto-select branch requires.
    """
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


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    valhalla_at: str | None = None,
    ors_at: str | None = None,
) -> Iterator[str]:
    """Start the real FastAPI app on a free localhost port with geocode stubbed.

    Router selection mirrors the three live states:
    - neither backend configured: the probe is stubbed out entirely, so every
      walk time is an estimate (the degraded path the banner warns about);
    - ``valhalla_at``: VALHALLA_URLS is pointed at the fake host;
    - ``ors_at``: VALHALLA_URLS is a dead port and ORS is pointed at the fake
      host with a test API key, so the app must reach ORS through the real
      probe-selection code.
    """
    from app.web import app as fastapi_app

    monkeypatch.setattr(config, "STOPS_CACHE", _stubs_cache(tmp_path))
    monkeypatch.setattr(geocode, "search", _fake_search)

    if valhalla_at is None and ors_at is None:
        monkeypatch.setattr(find, "_pick_router", lambda: None)
    if valhalla_at is not None:
        monkeypatch.setattr(config, "VALHALLA_URLS", [valhalla_at])
    if ors_at is not None:
        monkeypatch.setattr(config, "VALHALLA_URLS", ["http://127.0.0.1:1/route"])
        monkeypatch.setattr(config, "OPENROUTESERVICE_URL", ors_at)
        monkeypatch.setattr(config, "ors_api_key", lambda: "e2e-test-key")

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
def app_url(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[str]:
    """The degraded app: no router, so every walk time is an estimate."""
    yield from _serve(monkeypatch, tmp_path)


@pytest.fixture
def app_url_valhalla(
    monkeypatch: pytest.MonkeyPatch, tmp_path, local_valhalla: str
) -> Iterator[str]:
    """The app with a reachable local Valhalla look-alike."""
    yield from _serve(monkeypatch, tmp_path, valhalla_at=local_valhalla)


@pytest.fixture
def app_url_ors(
    monkeypatch: pytest.MonkeyPatch, tmp_path, local_ors: str
) -> Iterator[str]:
    """The app pointing at a reachable ORS look-alike, Valhalla dead."""
    yield from _serve(monkeypatch, tmp_path, ors_at=local_ors)


def _fake_router_server(
    *, endpoint: str, payload: Callable[[], dict[str, Any]]
) -> Iterator[str]:
    """A local stand-in for a routing backend answering with ``payload``.

    Valhalla is probed on GET /status and routes on POST /route; ORS is only
    ever GET, probe and route alike. ``endpoint`` is the suffix the fixture
    yields (``/route`` for Valhalla, "" for ORS) so the probe URL derivation
    matches the real backends' URL shapes.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def _respond(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._respond(json.dumps(payload()).encode())

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._respond(json.dumps(payload()).encode())

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}{endpoint}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def local_valhalla() -> Iterator[str]:
    """A Valhalla-shaped fake serving a 556 m walk (~7 min) for every stop."""
    yield from _fake_router_server(endpoint="/route", payload=lambda: valhalla_response(0.556))


@pytest.fixture
def local_ors() -> Iterator[str]:
    """An ORS-shaped fake serving a 492 m walk (~6 min) for every destination."""
    yield from _fake_router_server(endpoint="", payload=lambda: ors_response(492.0))


def _page_for(base_url: str) -> Iterator:
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
                if route.request.url.startswith(base_url)
                else route.abort()
            ),
        )
        page.goto(base_url, wait_until="load")
        try:
            yield page
        finally:
            browser.close()


@pytest.fixture
def page(app_url: str):
    """Browser against the degraded (estimate) app."""
    yield from _page_for(app_url)


@pytest.fixture
def page_valhalla(app_url_valhalla: str):
    """Browser against the app backed by the local Valhalla look-alike."""
    yield from _page_for(app_url_valhalla)


@pytest.fixture
def page_ors(app_url_ors: str):
    """Browser against the app backed by the local ORS look-alike."""
    yield from _page_for(app_url_ors)