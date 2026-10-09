"""HTTP surface, driven in-process with FastAPI's TestClient.

Still no network: Photon and Valhalla are mocked. These tests assert the shape
of what the browser receives, including the fields the Leaflet UI reads by name
and the routing-degraded flag.
"""

from __future__ import annotations

import json
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, geocode
from app.web import app
from scripts import find
from tests.conftest import (
    REAL_VALHALLA_SHAPE,
    ors_response,
    photon_feature,
    photon_response,
    valhalla_response,
)


@pytest.fixture
def client(stops_cache: Path) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _mock_photon(monkeypatch: pytest.MonkeyPatch, *features: dict) -> None:
    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return photon_response(*features)

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return None

        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(geocode.httpx, "AsyncClient", _Client)


def _mock_router(
    monkeypatch: pytest.MonkeyPatch,
    length_km: float = 0.402,
    shape: str | None = REAL_VALHALLA_SHAPE,
) -> None:
    monkeypatch.setattr(find, "_pick_router", lambda: "http://router/route")

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return valhalla_response(length_km, shape)

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return None

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(find.httpx, "AsyncClient", _Client)


def _mock_ors(monkeypatch: pytest.MonkeyPatch, distance_m: float = 402.0) -> None:
    """Back the app with an ORS-shaped GET, one of the walk-drawing backends."""
    monkeypatch.setattr(find, "_pick_router", lambda: find.config.OPENROUTESERVICE_URL)
    monkeypatch.setattr(config, "ors_api_key", lambda: "test-key")

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return ors_response(distance_m)

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return None

        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(find.httpx, "AsyncClient", _Client)


class TestStatic:
    def test_index_is_served(self, client: TestClient) -> None:
        assert client.get("/").status_code == 200

    def test_index_contains_the_search_box(self, client: TestClient) -> None:
        assert 'id="q"' in client.get("/").text

    def test_app_js_is_served(self, client: TestClient) -> None:
        assert client.get("/static/app.js").status_code == 200

    def test_app_css_is_served(self, client: TestClient) -> None:
        assert client.get("/static/app.css").status_code == 200

    def test_static_assets_are_not_cached(
        self, client: TestClient
    ) -> None:
        """Stale JS hid a bug for several rounds during development."""
        response = client.get("/static/app.js")
        assert response.headers.get("cache-control") == "no-store"

    def test_index_is_not_cached(self, client: TestClient) -> None:
        assert client.get("/").headers.get("cache-control") == "no-store"


class TestSearchEndpoint:
    def test_returns_candidates(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_photon(
            monkeypatch,
            photon_feature(name="Azrieli Center", city="Tel Aviv", osm_value="mall",
                           lat=32.0747, lon=34.7920),
        )
        response = client.get("/api/search", params={"q": "Azrieli Center"})
        assert response.status_code == 200
        assert response.json()["candidates"]

    def test_out_of_area_flag_reaches_the_client(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_photon(
            monkeypatch,
            photon_feature(name="Rothschild", city="London", osm_value="house",
                           lat=51.5, lon=-0.1),
        )
        candidate = client.get("/api/search", params={"q": "Rothschild"}).json()["candidates"][0]
        assert candidate["out_of_area"] is True, (
            "the UI marks far matches and needs this flag"
        )

    def test_short_query_is_rejected(self, client: TestClient) -> None:
        assert client.get("/api/search", params={"q": "a"}).status_code == 400

    def test_blank_query_is_rejected(self, client: TestClient) -> None:
        assert client.get("/api/search", params={"q": "  "}).status_code == 400

    def test_no_match_returns_404_with_a_reason(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_photon(monkeypatch)
        response = client.get("/api/search", params={"q": "Nowhere At All"})
        assert response.status_code == 404
        assert "detail" in response.json()

    def test_geocoder_failure_is_404_not_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad address is user error; it should not read as a server crash."""
        _mock_photon(monkeypatch)
        assert client.get("/api/search", params={"q": "Zzzz Qqqq"}).status_code == 404


class TestNearestEndpoint:
    def test_returns_a_winner(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_router(monkeypatch)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        assert body["best_stop"]["lines"]

    def test_one_entry_per_line(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_router(monkeypatch)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        lines = [entry["line"] for entry in body["lines"]]
        assert sorted(lines) == ["811", "813"]

    def test_stops_are_sorted_by_walk(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_router(monkeypatch, length_km=0.402)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        walks = [stop["walk_m"] for stop in body["stops"]]
        assert walks == sorted(walks)

    def test_ui_field_names_are_present(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """app.js reads these by name; a rename here silently blanks the UI."""
        _mock_router(monkeypatch)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        stop = body["best_stop"]
        for field in ("lat", "lon", "name_en", "name_he", "lines", "code",
                      "walk_min", "walk_m", "is_estimate"):
            assert field in stop, f"UI expects stop.{field}"

    def test_both_english_and_hebrew_names_are_sent(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hebrew labels are how you match a stop to the sign at the stop."""
        _mock_router(monkeypatch)
        stop = client.get(
            "/api/nearest", params={"lat": 32.0747, "lon": 34.7920}
        ).json()["best_stop"]
        assert stop["name_en"].isascii()
        assert not stop["name_he"].isascii(), "expected the Hebrew stop name"

    def test_routing_available_true_when_routed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_router(monkeypatch)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        assert body["routing_available"] is True
        assert body["best_stop"]["is_estimate"] is False

    def test_routing_available_false_when_down(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The UI banner depends on this flag; it drives the estimate warning."""
        monkeypatch.setattr(find, "_pick_router", lambda: None)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        assert body["routing_available"] is False
        assert body["best_stop"]["is_estimate"] is True

    def test_estimate_flag_is_per_stop(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(find, "_pick_router", lambda: None)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        assert all(stop["is_estimate"] for stop in body["stops"])

    def test_ors_stops_carry_walk_geometry_for_the_map(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The map draws the winner's walk; it reads best_stop.geometry."""
        _mock_ors(monkeypatch)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        geometry = body["best_stop"]["geometry"]
        assert geometry and len(geometry) >= 2, "a drawable path is needed"
        for lon, lat in geometry:
            assert isinstance(lon, float) and isinstance(lat, float)
        for stop in body["stops"]:
            assert stop["geometry"] == geometry, "every stop draws its own walk"

    def test_line_rows_omit_geometry_to_keep_the_payload_small(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Line rows are text-only; the path rides on the stop payloads once."""
        _mock_ors(monkeypatch)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        for entry in body["lines"]:
            assert entry["stop"]["geometry"] is None

    def test_valhalla_results_carry_walk_geometry(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The decoded polyline6 rides the same way ORS geometry does."""
        _mock_router(monkeypatch)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        geometry = body["best_stop"]["geometry"]
        assert geometry and len(geometry) >= 2, "a drawable path is needed"
        for stop in body["stops"]:
            assert stop["geometry"] == geometry, "every stop draws its own walk"

    def test_valhalla_without_a_shape_still_reports_distances(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A route that will not draw costs only the line, never the time."""
        _mock_router(monkeypatch, length_km=0.402, shape=None)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        assert body["routing_available"] is True
        assert body["best_stop"]["is_estimate"] is False
        assert all(stop["geometry"] is None for stop in body["stops"])

    def test_line_entries_include_route_name(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_router(monkeypatch)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        for entry in body["lines"]:
            assert entry["name"], f"line {entry['line']} needs a display name"

    def test_line_entries_include_their_stop(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_router(monkeypatch)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        for entry in body["lines"]:
            assert entry["stop"]["name_en"]
            assert entry["best_walk_min"] >= 0

    def test_origin_is_echoed_back(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The UI maps the returned origin to draw the pin."""
        _mock_router(monkeypatch)
        body = client.get("/api/nearest", params={"lat": 32.0747, "lon": 34.7920}).json()
        assert body["origin"] == {"lat": 32.0747, "lon": 34.7920}

    def test_missing_cache_returns_503_not_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "STOPS_CACHE", Path("/nonexistent/stops.json"))
        response = TestClient(app, raise_server_exceptions=False).get(
            "/api/nearest", params={"lat": 32.07, "lon": 34.79}
        )
        assert response.status_code == 503
        assert "fetch_lines.py" in response.json()["detail"]

    def test_garbage_coordinates_are_rejected(self, client: TestClient) -> None:
        response = client.get("/api/nearest", params={"lat": "abc", "lon": "34.79"})
        assert response.status_code in (400, 422)


class TestLinesEndpoint:
    def test_lists_each_line(self, client: TestClient) -> None:
        body = client.get("/api/lines").json()
        assert sorted(line["line"] for line in body["lines"]) == ["811", "813"]

    def test_each_line_has_a_name_and_count(self, client: TestClient) -> None:
        for line in client.get("/api/lines").json()["lines"]:
            assert line["name"]
            assert line["stop_count"] > 0

    def test_fetched_at_is_reported(self, client: TestClient) -> None:
        """Stop data goes stale; the UI needs to be able to show that."""
        assert client.get("/api/lines").json()["fetched_at"]


class TestRealStopData:
    """Checks against the actual scraped data/stops.json, not a fixture.

    The synthetic fixture drifted from the real schema once already, which broke
    /api/lines in tests only. These pin the contract against real data.
    """

    @pytest.fixture(autouse=True)
    def _require_real_cache(self) -> None:
        if not config.STOPS_CACHE.is_file():
            pytest.skip(
                f"{config.STOPS_CACHE} is missing; run scripts/fetch_lines.py"
            )

    def test_all_five_lines_are_present(self) -> None:
        payload = json.loads(config.STOPS_CACHE.read_text())
        assert sorted(line["line"] for line in payload["lines"]) == [
            "811", "812", "813", "814", "815",
        ]

    def test_every_line_has_its_stops_and_a_count(self) -> None:
        payload = json.loads(config.STOPS_CACHE.read_text())
        for line in payload["lines"]:
            assert line["name"], f"line {line['line']} has no display name"
            assert line["stops"], f"line {line['line']} has no stops"
            assert line["stop_count"] == len(line["stops"])

    def test_stops_load_and_merge(self) -> None:
        stops = find.load_stops()
        assert len(stops) >= 30, f"only {len(stops)} unique stops, expected 33"

    def test_every_stop_serves_at_least_one_line(self) -> None:
        for stop in find.load_stops():
            assert stop["lines"], f"{stop['name_en']} serves no line"

    def test_every_stop_has_both_names(self) -> None:
        for stop in find.load_stops():
            assert stop["name"].strip(), f"{stop} has no Hebrew name"
            assert stop["name_en"].strip(), f"{stop} has no English name"

    def test_english_names_contain_no_hebrew(self) -> None:
        """The UI stacks name_en over name_he; a mixed value breaks that."""
        for stop in find.load_stops():
            assert not any("\u0590" <= ch <= "\u05ff" for ch in stop["name_en"]), (
                f"name_en contains Hebrew: {stop['name_en']}"
            )

    def test_every_stop_is_inside_the_service_area(self) -> None:
        for stop in find.load_stops():
            assert geocode._meters_from_metro(stop["lat"], stop["lon"]) < 60_000, (
                f"{stop['name_en']} is implausibly far from Tel Aviv"
            )

    def test_all_stops_are_routed_not_estimated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_router(monkeypatch, length_km=0.5)
        result = find.find_nearest(32.0747, 34.7920)
        assert not any(stop["is_estimate"] for stop in result["all_stops"]), (
            "a mocked live router should route every stop"
        )

    def test_one_entry_per_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Per-line suggestions exist, never repeat, and respect the 30-min cutoff.

        The cutoff is origin-dependent, so on real data the contract is: no
        duplicate lines, nothing outside the five Shefayim lines, every
        suggestion within walking range, and the metro-line 811 always present
        from a central point.
        """
        monkeypatch.setattr(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        entries = result["lines"]

        assert len(entries) == len({e["line"] for e in entries}), "a line repeated"
        assert {e["line"] for e in entries} <= {"811", "812", "813", "814", "815"}
        assert all(e["best_stop"]["walk_min"] <= 30 for e in entries)
        assert any(e["line"] == "811" for e in entries)

    def test_each_line_suggestion_is_on_that_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(find, "_pick_router", lambda: None)
        for entry in find.find_nearest(32.0747, 34.7920)["lines"]:
            assert entry["line"] in entry["best_stop"]["lines"], (
                f"line {entry['line']} points at a stop it does not serve"
            )

    def test_line_entries_are_ordered_by_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(find, "_pick_router", lambda: None)
        walks = [e["best_walk_m"] for e in find.find_nearest(32.0747, 34.7920)["lines"]]
        assert walks == sorted(walks)

    def test_stops_are_ordered_by_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(find, "_pick_router", lambda: None)
        walks = [s["walk_m"] for s in find.find_nearest(32.0747, 34.7920)["stops"]]
        assert walks == sorted(walks)

    def test_the_park_and_ride_is_reachable_but_never_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shefayim P&R is 14km out; it must rank last, never near the top."""
        monkeypatch.setattr(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        names = [s["name_en"] for s in result["stops"]]
        assert not any("Park and Ride" in n for n in names), (
            f"distant P&R surfaced in the top {len(names)}: {names}"
        )

    def test_every_stop_has_a_sign_code(self) -> None:
        """A rider matches the stop to the sign by its code, so none may be blank."""
        for stop in find.load_stops():
            assert stop["code"] not in (None, ""), f"{stop['name_en']} has no code"

    def test_sign_code_and_position_are_different_numbers(self) -> None:
        """They look alike but mean different things, so neither may shadow the other."""
        for stop in find.load_stops():
            for number in stop["stop_numbers"].values():
                assert stop["code"] != number, (
                    f"{stop['name_en']}: code {stop['code']} collides with "
                    f"its position {number} on a line"
                )

    def test_813_stop_3_is_code_20726(self) -> None:
        """The worked example, pinned against the real feed.

        Menahem Begin Road/HaMelacha is index 4 in the feed but signed 3, and the
        sign carries 20726. If either the shift or the code mapping changes, this
        catches it against real data rather than a fixture that drifted with it.
        """
        matches = [s for s in find.load_stops() if s["code"] == 20726]
        assert len(matches) == 1, f"expected one stop with code 20726, got {matches}"
        stop = matches[0]
        assert stop["name_en"] == "Menahem Begin Road/HaMelacha"
        assert stop["stop_numbers"]["813"] == 3

    def test_the_code_is_the_same_for_every_line_serving_a_stop(self) -> None:
        """The code marks the physical sign, so it cannot vary with the line."""
        shared = [
            s for s in find.load_stops() if "Park and Ride/Shuttle Pickup" in s["name_en"]
        ]
        assert shared, "expected the park and ride pickup stop"
        for stop in shared:
            assert len(stop["lines"]) > 1, "this stop serves several lines"
            assert stop["code"]

    def test_answers_are_stable_across_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same address twice must give the same stop, cached or not."""
        monkeypatch.setattr(find, "_pick_router", lambda: None)
        first = find.find_nearest(32.0747, 34.7920)["best_stop"]["name_en"]
        find._ROUTER_CACHE = None  # simulate a restart
        second = find.find_nearest(32.0747, 34.7920)["best_stop"]["name_en"]
        assert first == second


class TestTableFormat:
    """Both result tables must present the same columns in the same order.

    They were built by hand in two places and had already drifted once. The row
    markup is generated in JavaScript, so the headers are pinned here as the
    cheapest thing that fails if the two tables stop matching.
    """

    @staticmethod
    def _tables(html: str) -> dict[str, list[str]]:
        from html.parser import HTMLParser

        class Tables(HTMLParser):
            """Collects each table's header cells, keyed by the tbody that follows."""

            def __init__(self) -> None:
                super().__init__()
                self.in_thead = False
                self.headers: list[str] = []
                self.pending: list[str] | None = None
                self.found: dict[str, list[str]] = {}

            def handle_starttag(self, tag, attrs):
                if tag == "thead":
                    self.in_thead = True
                    self.headers = []
                elif tag == "tbody":
                    body_id = dict(attrs).get("id")
                    if body_id:
                        self.found[body_id] = self.pending or []
                        self.pending = None

            def handle_endtag(self, tag):
                if tag == "thead":
                    self.in_thead = False
                    self.pending = list(self.headers)

            def handle_data(self, data):
                if self.in_thead and data.strip():
                    self.headers.append(data.strip())

        parser = Tables()
        parser.feed(html)
        return parser.found

    def test_both_tables_declare_the_same_columns(self, client: TestClient) -> None:
        tables = self._tables(client.get("/").text)
        assert set(tables) == {"lines", "stops"}
        assert tables["lines"] == tables["stops"], (
            f"column mismatch:\n  other lines: {tables['lines']}\n"
            f"  nearby stops: {tables['stops']}"
        )

    def test_columns_are_the_agreed_set(self, client: TestClient) -> None:
        assert self._tables(client.get("/").text)["stops"] == [
            "Line",
            "Route",
            "Closest stop",
            "No.",
            "Code",
            "Walk",
            "Ride",
            "Return",
        ]

    def test_each_row_body_has_one_cell_per_column(self, client: TestClient) -> None:
        """A row short a cell shifts every column after it."""
        html = client.get("/").text
        expected = len(self._tables(html)["stops"])
        assert html.count("<th>") == expected * 2, (
            "the two tables have different column counts"
        )

    def test_the_row_builder_emits_one_cell_per_column(
        self, client: TestClient
    ) -> None:
        """Pin the JavaScript row builder against the header count.

        The cells are created in app.js, so the headers alone cannot catch a row
        that appends a different number of cells than the table declares -- which
        shifts every column to the right of the missing one with no test failing.
        """
        js = client.get("/static/app.js").text
        match = re.search(r"tr\.append\((.*?)\);", js, re.S)
        assert match, "could not find the tr.append() call in app.js"

        cells = [c.strip() for c in match.group(1).split(",") if c.strip()]
        declared = len(self._tables(client.get("/").text)["stops"])
        assert len(cells) == declared, (
            f"row builds {len(cells)} cells ({cells}) "
            f"but the tables declare {declared} columns"
        )


class TestNoSecretsInResponses:
    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/static/app.js",
            "/static/app.css",
            "/api/lines",
            "/api/search?q=Azrieli",
            "/api/nearest?lat=32.07&lon=34.79",
        ],
    )
    def test_no_api_key_leaks_to_the_browser(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
    ) -> None:
        monkeypatch.setenv("SERPAPI_API_KEY", "test-secret-value-abc123")
        _mock_router(monkeypatch)
        _mock_photon(monkeypatch)

        body = client.get(path).text
        assert "test-secret-value-abc123" not in body, f"secret leaked via {path}"

    def test_response_never_carries_geocoder_internals(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_photon(
            monkeypatch,
            photon_feature(name="Azrieli Center", city="Tel Aviv", osm_value="mall"),
        )
        candidate = client.get("/api/search", params={"q": "Azrieli"}).json()["candidates"][0]
        assert "_rank" not in candidate


class TestRoutingStatusEndpoint:
    """The deploy diagnostic behind the "~" banner.

    It must answer with probe results for every backend while keeping the
    OpenRouteService key server-side, since the endpoint is public.
    """

    @staticmethod
    def _stub_probes(
        monkeypatch: pytest.MonkeyPatch, status: int
    ) -> list[tuple[str, dict]]:
        calls: list[tuple[str, dict]] = []

        def _get(url: str, **kwargs) -> object:
            calls.append((url, kwargs))

            class _Resp:
                status_code = status
                text = "backend unhappy"

            return _Resp()

        # Valhalla candidates probe with POST (a real route); ORS and BRouter
        # probe with GET.
        monkeypatch.setattr(find.httpx, "get", _get)
        monkeypatch.setattr(find.httpx, "post", _get)
        return calls

    def test_without_a_key_ors_is_skipped_and_reported(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_probes(monkeypatch, status=503)

        response = client.get("/api/routing-status")

        assert response.status_code == 200
        body = response.json()
        assert body["key_configured"] is False
        assert [c["kind"] for c in body["candidates"]] == [
            "valhalla",
            "ors",
            "brouter",
        ]
        assert body["candidates"][0]["http_status"] == 503
        assert "backend unhappy" in body["candidates"][0]["error"]
        ors = body["candidates"][1]
        assert ors["skipped"] is True
        assert "OPENROUTESERVICE_API_KEY" in ors["error"]
        assert body["candidates"][-1]["kind"] == "brouter"
        assert body["would_pick"] is None
        assert "router" in body["cached"]

    def test_with_a_key_ors_is_probed_but_the_key_stays_server_side(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._stub_probes(monkeypatch, status=403)
        monkeypatch.setattr(config, "ors_api_key", lambda: "integration-secret-key-9")

        response = client.get("/api/routing-status")

        assert response.status_code == 200
        assert "integration-secret-key-9" not in response.text, "key leaked"
        body = response.json()
        assert body["key_configured"] is True
        assert body["key_length"] == len("integration-secret-key-9")
        ors = body["candidates"][1]
        assert ors["kind"] == "ors"
        assert ors["ok"] is False
        assert ors["http_status"] == 403
        # The key did go out on the ORS probe itself (BRouter's probe, which
        # runs after it, deliberately carries no key).
        authorized = [
            kwargs for _, kwargs in calls if kwargs.get("headers", {}).get("Authorization")
        ]
        assert authorized, "the ORS probe must carry the key"
        assert authorized[0]["headers"]["Authorization"] == "integration-secret-key-9"


class TestUserJourney:
    def test_search_then_nearest_gives_a_consistent_answer(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors the browser flow: search an address, then route from it."""
        _mock_photon(
            monkeypatch,
            photon_feature(name="Azrieli Center", city="Tel Aviv", osm_value="mall",
                           lat=32.0747, lon=34.7920),
        )
        candidate = client.get(
            "/api/search", params={"q": "Azrieli Center Tel Aviv"}
        ).json()["candidates"][0]

        _mock_router(monkeypatch, length_km=0.402)
        body = client.get(
            "/api/nearest", params={"lat": candidate["lat"], "lon": candidate["lon"]}
        ).json()

        assert body["origin"]["lat"] == pytest.approx(candidate["lat"])
        assert body["best_stop"]["walk_m"] == pytest.approx(402.0)
        assert body["best_stop"]["is_estimate"] is False

    def test_a_far_address_is_reported_not_hidden(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Someone in London is flagged out-of-area at geocoding and gets no
        result, not a nonsense 40-hour walk shown as a suggestion."""
        _mock_photon(
            monkeypatch,
            photon_feature(name="Rothschild", city="London", osm_value="house",
                           lat=51.5, lon=-0.1),
        )
        candidate = client.get("/api/search", params={"q": "Rothschild"}).json()["candidates"][0]
        assert candidate["out_of_area"] is True

        monkeypatch.setattr(find, "_pick_router", lambda: None)
        body = client.get(
            "/api/nearest", params={"lat": candidate["lat"], "lon": candidate["lon"]}
        ).json()
        assert body["best_stop"] is None
        assert body["stops"] == []
        assert body["lines"] == []
