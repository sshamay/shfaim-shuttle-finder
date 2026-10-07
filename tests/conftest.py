"""Shared fixtures.

Two conventions worth knowing:

- Tests never touch the network. External HTTP is mocked at the client
  boundary, so the suite is deterministic and offline-safe.
- ``stops_cache`` writes a small synthetic route set to a tmp path rather than
  reading ``data/stops.json``, so tests never depend on scraped data or on the
  network fetch that produces it.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Iterator

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run tests marked `live` that hit real Photon/Valhalla/20fl servers",
    )


def pytest_configure(config: pytest.Config) -> None:
    # Opt-in twice over: the flag, and an env var so a plain `pytest` in CI can
    # never reach a third-party server by accident.
    config.addinivalue_line("markers", "live: requires --run-live and RUN_LIVE=1")


def pytest_collection_modifyitems(config: pytest.Config, items: list[Any]) -> None:
    """Auto-mark by directory, and skip `live` unless explicitly enabled."""
    live_enabled = config.getoption("--run-live") and os.getenv("RUN_LIVE") == "1"

    for item in items:
        tier = Path(str(item.fspath)).parent.name
        if tier in {"unit", "integration", "e2e"}:
            item.add_marker(getattr(pytest.mark, tier))

        if item.get_closest_marker("live") and not live_enabled:
            item.add_marker(
                pytest.mark.skip(reason="live test, needs --run-live and RUN_LIVE=1")
            )


@pytest.fixture(autouse=True)
def _block_external_network() -> Iterator[None]:
    """Fail loudly if a test opens a non-localhost socket.

    Mocking at the httpx boundary is easy to forget in one place, and a test
    that quietly reaches 20fl is both slow and rate-limit hungry. This makes
    the offline policy enforced rather than aspirational.
    """
    real_connect = socket.socket.connect
    live_enabled = os.environ.get("RUN_LIVE") == "1"

    def guarded_connect(self, address, *a, **k):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("localhost", "127.0.0.1", "::1", "testserver"):
            if live_enabled:
                return real_connect(self, address, *a, **k)
            raise RuntimeError(
                f"test tried to open a socket to {host!r}; "
                "mock the HTTP client instead of calling a real service"
            )
        return real_connect(self, address, *a, **k)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _isolate_router_cache() -> None:
    """Clear the router cache so tests cannot leak state into each other."""
    from scripts import find

    find._ROUTER_CACHE = None
    find._ROUTER_CHECKED_AT = 0.0
    yield
    find._ROUTER_CACHE = None
    find._ROUTER_CHECKED_AT = 0.0


@pytest.fixture(autouse=True)
def _no_ors_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routing tests assume no OpenRouteService key unless one is set.

    A developer .env may configure OPENROUTESERVICE_API_KEY for local runs;
    pinning it off here keeps router-pick tests deterministic no matter what
    the machine has in its environment. Tests that want ORS set their own key.
    """
    from app import config

    monkeypatch.setattr(config, "ors_api_key", lambda: "")


# --- sample data -----------------------------------------------------------

# Two lines sharing one stop in common, so line-association merging is
# actually exercised rather than assumed. Mirrors the real data/stops.json
# shape, including stop_count, which /api/lines reads.
SAMPLE_LINES: list[dict[str, Any]] = [
    {
        "line": "813",
        "name": "Bagin-Hamasger",
        "stop_count": 2,
        "stops": [
            {
                "stop_id": "1",
                "code": "1",
                "name": "דרך מנחם בגין/החשמלה",
                "name_en": "Begin Road/HaMelacha",
                "lat": 32.0700,
                "lon": 34.7900,
                "index": 1,
                "is_park_and_ride": False,
            },
            {
                "stop_id": "2",
                "code": "2",
                "name": "קריית הממשלה/דרך מנחם בגין",
                "name_en": "Government Complex/Menahem Begin Road",
                "lat": 32.0747,
                "lon": 34.7920,
                "index": 2,
                "is_park_and_ride": False,
            },
        ],
    },
    {
        "line": "811",
        "name": "HaKiriya",
        "stop_count": 2,
        "stops": [
            # Same physical stop as 813's first stop, under the same key.
            {
                "stop_id": "1",
                "code": "1",
                "name": "דרך מנחם בגין/החשמלה",
                "name_en": "Begin Road/HaMelacha",
                "lat": 32.0700,
                "lon": 34.7900,
                "index": 1,
                "is_park_and_ride": False,
            },
            {
                "stop_id": "3",
                "code": "3",
                "name": "שדרות השלום",
                "name_en": "HaShalom Road",
                "lat": 32.0600,
                "lon": 34.7800,
                "index": 3,
                "is_park_and_ride": False,
            },
        ],
    },
]


@pytest.fixture
def sample_stops_payload() -> dict[str, Any]:
    return {
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "lines": SAMPLE_LINES,
    }


@pytest.fixture
def stops_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config.STOPS_CACHE at a synthetic dataset in tmp_path."""
    from app import config

    path = tmp_path / "stops.json"
    path.write_text(json.dumps({"fetched_at": "2026-01-01T00:00:00+00:00",
                                "lines": SAMPLE_LINES}))
    monkeypatch.setattr(config, "STOPS_CACHE", path)
    return path


@pytest.fixture
def no_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the Valhalla probe report no available host."""
    from scripts import find

    monkeypatch.setattr(find, "_pick_router", lambda: None)


def _stop(
    stop_id: str, name_en: str, lat: float, lon: float, index: int | None = None
) -> dict[str, Any]:
    return {
        "stop_id": stop_id,
        "code": stop_id,
        "name": f"תחנה {stop_id}",
        "name_en": name_en,
        "lat": lat,
        "lon": lon,
        "index": int(stop_id) if index is None else index,
        "is_park_and_ride": False,
    }


# Eight stops on a single line, spread north from the origin. Gives tests more
# stops than the default display limit of five, so `top` truncation is visible
# instead of being masked by a dataset smaller than the limit. All eight stay
# inside the 30-minute walk cutoff: the farthest (0.0016 * 7 deg) walks to
# ~26 min even with the 1.2 detour factor, so they all survive the filter.
MANY_STOPS: list[dict[str, Any]] = [
    _stop(str(i + 1), f"Stop {i + 1}", 32.0700 - i * 0.0016, 34.7900)
    for i in range(8)
]

# Two lines, three unique stops. Ordered by walk from the origin, nearest first:
#   Near   (~155 m) -> stop 1 on 811
#   Shared (~445 m) -> stop 2 on 811 but stop 1 on 813
# So 813 is reachable solely through a stop that also serves another line, 811
# has a closer stop of its own, and the shared stop carries a *different* number
# on each line. Aggregating only each stop's first line number would drop 813.
CROSS_SERVED_LINES: list[dict[str, Any]] = [
    {
        "line": "811",
        "name": "HaKiriya",
        "stop_count": 2,
        "stops": [
            _stop("10", "Near 811", 32.0714, 34.7900, index=1),
            _stop("11", "Shared 811 and 813", 32.0740, 34.7900, index=2),
        ],
    },
    {
        "line": "813",
        "name": "Bagin-Hamasger",
        "stop_count": 1,
        "stops": [
            _stop("11", "Shared 811 and 813", 32.0740, 34.7900, index=1),
        ],
    },
]


def write_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: list[dict[str, Any]]
) -> Path:
    """Write a route set to tmp_path and point config.STOPS_CACHE at it."""
    from app import config

    path = tmp_path / "stops.json"
    path.write_text(
        json.dumps({"fetched_at": "2026-01-01T00:00:00+00:00", "lines": lines})
    )
    monkeypatch.setattr(config, "STOPS_CACHE", path)
    return path


@pytest.fixture
def many_stops_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Eight stops, more than the five the UI shows by default."""
    return write_cache(
        monkeypatch,
        tmp_path,
        [{"line": "811", "name": "HaKiriya", "stop_count": 8, "stops": MANY_STOPS}],
    )


@pytest.fixture
def cross_served_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two lines whose closest stops are not the shared stop."""
    return write_cache(monkeypatch, tmp_path, CROSS_SERVED_LINES)


# Origin of the nearest fixtures is (32.0747, 34.7920). A stop 0.019° north
# walks to ~31.7 min at 80 m/min with the 1.2 detour factor — just beyond the
# 30-minute cutoff — while 0.0018° (~3.5 min) is comfortably inside it.
CUTOFF_LINES: list[dict[str, Any]] = [
    {
        "line": "811",
        "name": "HaKiriya",
        "stop_count": 1,
        "stops": [_stop("n1", "Near", 32.0729, 34.7920, index=1)],
    },
    {
        "line": "813",
        "name": "Bagin-Hamasger",
        "stop_count": 1,
        "stops": [_stop("f1", "Far", 32.0557, 34.7920, index=1)],
    },
]

# Every stop is beyond the cutoff, so `find_nearest` has nothing to rank.
ONLY_FAR_LINES: list[dict[str, Any]] = [
    {
        "line": "811",
        "name": "HaKiriya",
        "stop_count": 1,
        "stops": [_stop("f1", "Far", 32.0557, 34.7920, index=1)],
    },
]


@pytest.fixture
def cutoff_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One stop inside the 30-minute walk limit, one far beyond it."""
    return write_cache(monkeypatch, tmp_path, CUTOFF_LINES)


@pytest.fixture
def only_far_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A route set where no stop is within a 30-minute walk."""
    return write_cache(monkeypatch, tmp_path, ONLY_FAR_LINES)


@pytest.fixture
def routed_600m(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live router that reports 600 m for every stop.

    600 m at 80 m/min is 7.5 minutes, so it separates round() from int().
    """
    from scripts import find

    monkeypatch.setattr(find, "_pick_router", lambda: "http://router/route")

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return valhalla_response(0.6)

    class _Client:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *e: object) -> None:
            return None

        async def post(self, *a: object, **k: object) -> _Resp:
            return _Resp()

    monkeypatch.setattr(find.httpx, "AsyncClient", _Client)


def valhalla_response(length_km: float) -> dict[str, Any]:
    """A minimal Valhalla /route success payload."""
    return {"trip": {"summary": {"length": length_km}}}


def ors_response(distance_m: float) -> dict[str, Any]:
    """A minimal OpenRouteService directions success payload (metres)."""
    return {"routes": [{"summary": {"distance": distance_m, "duration": 0.0}}]}


def photon_feature(
    *,
    name: str = "",
    city: str = "",
    osm_value: str = "house",
    lat: float = 32.07,
    lon: float = 34.79,
    housenumber: str = "",
    state: str = "",
    street: str = "",
    osm_id: int = 1,
) -> dict[str, Any]:
    """One Photon GeoJSON feature, as the live API returns it."""
    return {
        "geometry": {"coordinates": [lon, lat]},
        "properties": {
            "name": name,
            "city": city,
            "state": state,
            "housenumber": housenumber,
            "street": street,
            "osm_value": osm_value,
            "osm_type": "W",
            "osm_id": osm_id,
            "osm_key": "building",
        },
    }


def photon_response(*features: dict[str, Any]) -> dict[str, Any]:
    return {"features": list(features)}
