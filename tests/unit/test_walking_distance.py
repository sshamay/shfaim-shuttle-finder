"""Walking-distance math and stop loading.

The cases here are drawn from real failures during development: the OSRM
profile-ignoring bug, the Modi'in/Tel Aviv same-name collision, and the
straight-line fallback used when no router is available.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app import config
from scripts import find
from tests.conftest import valhalla_response, write_cache


class TestHaversine:
    def test_zero_distance_for_same_point(self) -> None:
        assert find.haversine_m(32.07, 34.79, 32.07, 34.79) == 0.0

    def test_known_short_distance_matches_osm_scale(self) -> None:
        # ~100 m north-south in Tel Aviv, the scale a stop ranking turns on.
        meters = find.haversine_m(32.0700, 34.7900, 32.0709, 34.7900)
        assert 95 < meters < 105, f"expected ~100 m, got {meters:.1f}"

    def test_symmetric(self) -> None:
        forward = find.haversine_m(32.0700, 34.7900, 32.0747, 34.7920)
        backward = find.haversine_m(32.0747, 34.7920, 32.0700, 34.7900)
        assert forward == pytest.approx(backward)

    def test_shefayim_to_tel_aviv_is_tens_of_km(self) -> None:
        meters = find.haversine_m(32.2130, 34.8351, 32.0747, 34.7920)
        assert 15_000 < meters < 20_000, f"got {meters:.0f} m"


class TestLoadStops:
    def test_merges_identical_stops_across_lines(self, stops_cache: Path) -> None:
        stops = find.load_stops()
        # 3 stop records, but the first is shared by 813 and 811.
        assert len(stops) == 3

    def test_shared_stop_carries_both_line_numbers(self, stops_cache: Path) -> None:
        shared = next(s for s in find.load_stops() if s["name_en"] == "Begin Road/HaMelacha")
        assert sorted(shared["lines"]) == ["811", "813"]

    def test_single_line_stop_is_not_duplicated(self, stops_cache: Path) -> None:
        stops = find.load_stops()
        only_813 = next(s for s in stops if s["name_en"] == "Government Complex/Menahem Begin Road")
        assert only_813["lines"] == ["813"]

    def test_keeps_english_and_hebrew_names(self, stops_cache: Path) -> None:
        stop = next(
            s for s in find.load_stops() if s["name_en"] == "Government Complex/Menahem Begin Road"
        )
        assert stop["name"] == "קריית הממשלה/דרך מנחם בגין"
        assert stop["name_en"].isascii()

    def test_missing_cache_raises_actionable_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "STOPS_CACHE", tmp_path / "nope.json")
        with pytest.raises(FileNotFoundError, match="fetch_lines.py"):
            find.load_stops()


class TestWalkSpeed:
    def test_ten_minute_walk_is_about_800m(self) -> None:
        assert find.WALK_M_PER_MIN * 10 == pytest.approx(800.0)

    def test_speed_is_a_plausible_4_8_kmh(self) -> None:
        assert find.WALK_SPEED_KMH == pytest.approx(4.8)


class TestWalkingDistancesRouterDown:
    def test_falls_back_to_straight_line_when_no_router(
        self, stops_cache: Path, no_router: None
    ) -> None:
        stops = find.load_stops()
        origin = (32.0747, 34.7920)

        results = find.asyncio.run(find.walking_distances(origin, stops))
        assert len(results) == len(stops)
        assert all(r["is_estimate"] for r in results), "no router means all estimates"

    def test_fallback_applies_detour_factor(
        self, stops_cache: Path, no_router: None
    ) -> None:
        """A street grid makes the walk longer than the crow flies."""
        stops = find.load_stops()
        origin = (32.0747, 34.7920)

        results = find.asyncio.run(find.walking_distances(origin, stops))
        for item in results:
            straight = find.haversine_m(origin[0], origin[1], item["stop"]["lat"], item["stop"]["lon"])
            assert item["walk_m"] == pytest.approx(
                straight * find.ESTIMATE_DETOUR_FACTOR, rel=1e-6
            )

    def test_detour_factor_is_above_one(self) -> None:
        assert find.ESTIMATE_DETOUR_FACTOR > 1.0


class TestWalkingDistancesRouted:
    def test_uses_router_distance_when_available(
        self, stops_cache: Path, mocker
    ) -> None:
        """Regression guard: routing must win over the straight-line estimate."""
        mocker.patch.object(find, "_pick_router", lambda: "http://router/route")
        posted: list[dict] = []

        def handler(request):
            posted.append(json.loads(request.content))
            return _FakeResponse(valhalla_response(0.402))

        mocker.patch.object(find.httpx, "AsyncClient", return_value=_FakeClient(handler))

        stops = find.load_stops()
        origin = (32.0747, 34.7920)
        results = find.asyncio.run(find.walking_distances(origin, stops))

        assert all(not r["is_estimate"] for r in results)
        assert all(r["walk_m"] == pytest.approx(402.0) for r in results)
        assert len(posted) == len(stops), "one route request per stop"

    def test_request_asks_for_pedestrian_costing(
        self, stops_cache: Path, mocker
    ) -> None:
        """The original bug was a server answering with car routes."""
        mocker.patch.object(find, "_pick_router", lambda: "http://router/route")
        sent: list[dict] = []

        def handler(request):
            sent.append(json.loads(request.content))
            return _FakeResponse(valhalla_response(0.1))

        mocker.patch.object(find.httpx, "AsyncClient", return_value=_FakeClient(handler))
        find.asyncio.run(find.walking_distances((32.07, 34.79), find.load_stops()))

        assert sent, "expected at least one route request"
        assert all(payload["costing"] == "pedestrian" for payload in sent)
        assert all(len(payload["locations"]) == 2 for payload in sent)

    def test_single_failing_stop_does_not_fail_the_batch(
        self, stops_cache: Path, mocker
    ) -> None:
        """One bad stop must not lose the other 32 results."""
        mocker.patch.object(find, "_pick_router", lambda: "http://router/route")
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResponse(None, status=500)
            return _FakeResponse(valhalla_response(0.300))

        mocker.patch.object(find.httpx, "AsyncClient", return_value=_FakeClient(handler))

        stops = find.load_stops()
        results = find.asyncio.run(find.walking_distances((32.07, 34.79), stops))

        assert len(results) == len(stops)
        assert sum(1 for r in results if r["is_estimate"]) == 1
        assert sum(1 for r in results if not r["is_estimate"]) == 2

    def test_error_payload_is_treated_as_failure(
        self, stops_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: "http://router/route")
        mocker.patch.object(
            find.httpx,
            "AsyncClient",
            return_value=_FakeClient(lambda r: _FakeResponse({"error": "no path"})),
        )
        results = find.asyncio.run(find.walking_distances((32.07, 34.79), find.load_stops()))
        assert all(r["is_estimate"] for r in results)

    def test_zero_length_route_is_rejected_as_garbage(
        self, stops_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: "http://router/route")
        mocker.patch.object(
            find.httpx,
            "AsyncClient",
            return_value=_FakeClient(lambda r: _FakeResponse(valhalla_response(0.0))),
        )
        results = find.asyncio.run(find.walking_distances((32.07, 34.79), find.load_stops()))
        assert all(r["is_estimate"] for r in results), "0 m is not a usable walking route"


class TestFindNearest:
    def test_ranks_closest_stop_first(self, stops_cache: Path, mocker) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)

        walks = [s["walk_m"] for s in result["stops"]]
        assert walks == sorted(walks), f"not sorted: {walks}"

    def test_winner_is_the_closest_stop(self, stops_cache: Path, mocker) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        assert result["best_stop"]["walk_m"] == result["stops"][0]["walk_m"]

    def test_reports_one_entry_per_line(self, stops_cache: Path, mocker) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        lines = [entry["line"] for entry in result["lines"]]
        assert sorted(lines) == ["811", "813"]

    def test_lines_sorted_by_their_own_closest_stop(
        self, stops_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        walks = [entry["best_walk_m"] for entry in result["lines"]]
        assert walks == sorted(walks)

    def test_every_line_entry_carries_a_route_name(
        self, stops_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        for entry in result["lines"]:
            assert entry["name"], f"line {entry['line']} has no display name"

    def test_best_stop_per_line_is_actually_on_that_line(
        self, stops_cache: Path, mocker
    ) -> None:
        """A per-line suggestion for a stop the line does not serve is a wrong answer."""
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        for entry in result["lines"]:
            assert entry["line"] in entry["best_stop"]["lines"], (
                f"line {entry['line']} suggested a stop it does not serve"
            )

    def test_walk_minutes_present_and_non_negative(
        self, stops_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        for stop in result["stops"]:
            assert stop["walk_min"] >= 0

    def test_top_argument_limits_returned_stops(
        self, stops_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920, top=1)
        assert len(result["stops"]) == 1

    def test_all_stops_kept_regardless_of_top(
        self, stops_cache: Path, mocker
    ) -> None:
        """top trims the display list, not the ranking input."""
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920, top=1)
        assert len(result["all_stops"]) == 3


class TestWalkTimeCutoff:
    """Stops beyond a 30-minute walk never surface in any result set."""

    def test_far_stop_is_dropped_everywhere(
        self, cutoff_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)

        assert result["best_stop"]["name_en"] == "Near"
        assert [s["name_en"] for s in result["stops"]] == ["Near"]
        assert [s["name_en"] for s in result["all_stops"]] == ["Near"]

    def test_line_only_reachable_via_a_far_stop_is_absent(
        self, cutoff_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        assert [e["line"] for e in result["lines"]] == ["811"]

    def test_no_stop_within_the_limit_returns_no_results(
        self, only_far_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        assert result["best_stop"] is None
        assert result["stops"] == []
        assert result["lines"] == []
        assert result["all_stops"] == []

    def test_cutoff_uses_raw_minutes_not_rounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker
    ) -> None:
        """29.4 min survives, 30.4 min is cut: the raw value decides."""
        mocker.patch.object(find, "_pick_router", lambda: None)

        def stop(stop_id: str, name: str, lat: float) -> dict[str, Any]:
            return {
                "stop_id": stop_id,
                "code": stop_id,
                "name": f"תחנה {name}",
                "name_en": name,
                "lat": lat,
                "lon": 34.7920,
                "index": int(stop_id),
                "is_park_and_ride": False,
            }

        lines = [
            {"line": "811", "name": "HaKiriya", "stop_count": 2, "stops": [
                stop("1", "JustInside", 32.0747 - 0.0176),
                stop("2", "JustOutside", 32.0747 - 0.0182),
            ]},
        ]
        write_cache(monkeypatch, tmp_path, lines)
        result = find.find_nearest(32.0747, 34.7920)

        assert [s["name_en"] for s in result["all_stops"]] == ["JustInside"]


class TestDisplayLimit:
    """`top` trims the list the user scrolls, so the default has to be honoured."""

    def test_default_shows_five_of_eight(self, many_stops_cache: Path, mocker) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920)
        assert len(result["stops"]) == 5, "the UI expects five rows"
        assert len(result["all_stops"]) == 8, "ranking keeps every stop"

    @pytest.mark.parametrize("top", [1, 2, 3, 5, 7])
    def test_top_is_respected(
        self, many_stops_cache: Path, mocker, top: int
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920, top=top)
        assert len(result["stops"]) == top

    def test_top_larger_than_the_data_is_harmless(
        self, many_stops_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920, top=99)
        assert len(result["stops"]) == 8

    def test_the_shown_stops_are_the_closest_ones(
        self, many_stops_cache: Path, mocker
    ) -> None:
        """Trimming must drop the farthest stops, not the nearest."""
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920, top=3)
        assert result["stops"] == result["all_stops"][:3]

    def test_winner_is_present_whatever_the_limit(
        self, many_stops_cache: Path, mocker
    ) -> None:
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0747, 34.7920, top=1)
        assert result["best_stop"]["name_en"] == result["stops"][0]["name_en"]


class TestPerLineClosestStop:
    """Each line is answered with the closest stop that line actually serves."""

    def _result(self, mocker):
        mocker.patch.object(find, "_pick_router", lambda: None)
        return find.find_nearest(32.0700, 34.7900)

    def test_both_lines_are_reported(
        self, cross_served_cache: Path, mocker
    ) -> None:
        """813 is served only by a shared stop, so it must still appear."""
        assert sorted(e["line"] for e in self._result(mocker)["lines"]) == ["811", "813"]

    def test_each_line_gets_its_own_closest_stop(
        self, cross_served_cache: Path, mocker
    ) -> None:
        by_line = {e["line"]: e for e in self._result(mocker)["lines"]}
        assert by_line["811"]["best_stop"]["name_en"] == "Near 811"
        assert by_line["813"]["best_stop"]["name_en"] == "Shared 811 and 813"

    def test_a_shared_stop_does_not_hijack_every_line(
        self, cross_served_cache: Path, mocker
    ) -> None:
        by_line = {e["line"]: e for e in self._result(mocker)["lines"]}
        assert by_line["811"]["best_stop"] is not by_line["813"]["best_stop"], (
            "811 has a closer stop of its own and should not borrow 813's"
        )

    def test_each_line_reports_its_own_distance(
        self, cross_served_cache: Path, mocker
    ) -> None:
        by_line = {e["line"]: e for e in self._result(mocker)["lines"]}
        assert by_line["811"]["best_walk_m"] < by_line["813"]["best_walk_m"], (
            "811's nearest stop is nearer, so its walk must be smaller"
        )

    def test_best_walk_matches_the_named_stop(
        self, cross_served_cache: Path, mocker
    ) -> None:
        """The advertised walk time must be the stop it points at."""
        for entry in self._result(mocker)["lines"]:
            assert entry["best_walk_m"] == entry["best_stop"]["walk_m"]

    def test_a_stop_serving_two_lines_is_counted_for_both(
        self, cross_served_cache: Path, mocker
    ) -> None:
        shared = find.load_stops()
        merged = [s for s in shared if s["name_en"] == "Shared 811 and 813"][0]
        assert sorted(merged["lines"]) == ["811", "813"]


class TestStopNumbers:
    """Stop position along a line, so a rider can find the right stop sign."""

    def test_every_merged_stop_carries_a_number_per_line(
        self, stops_cache: Path
    ) -> None:
        for stop in find.load_stops():
            for line in stop["lines"]:
                assert stop["stop_numbers"].get(line) is not None, (
                    f"{stop['name_en']} has no number on line {line}"
                )

    def test_numbers_are_positive_integers(self, stops_cache: Path) -> None:
        for stop in find.load_stops():
            for number in stop["stop_numbers"].values():
                assert isinstance(number, int) and number >= 1

    def test_a_shared_stop_gets_one_number_per_line(
        self, cross_served_cache: Path
    ) -> None:
        shared = [s for s in find.load_stops() if s["name_en"].startswith("Shared")][0]
        assert sorted(shared["stop_numbers"]) == ["811", "813"]

    def test_line_entry_carries_that_lines_own_number(
        self, cross_served_cache: Path, mocker
    ) -> None:
        """The shared stop is stop 2 on 811 and stop 1 on 813."""
        mocker.patch.object(find, "_pick_router", lambda: None)
        by_line = {
            e["line"]: e
            for e in find.find_nearest(32.0700, 34.7900)["lines"]
        }
        assert by_line["813"]["stop_no"] == 1
        assert by_line["813"]["stop_no"] == by_line["813"]["best_stop"][
            "stop_numbers"
        ]["813"], "the number shown must be the one for this line"

    def test_differing_numbers_stay_distinct_per_line(
        self, cross_served_cache: Path, mocker
    ) -> None:
        """The shared stop is stop 2 on 811 but stop 1 on 813.

        Queried from the north so the shared stop is the closest for both lines,
        which is the only way the two rows can show the same stop carrying two
        different numbers.
        """
        mocker.patch.object(find, "_pick_router", lambda: None)
        shared = [s for s in find.load_stops() if s["name_en"].startswith("Shared")][0]
        assert shared["stop_numbers"] == {"811": 2, "813": 1}

        result = find.find_nearest(32.0800, 34.7900)
        by_line = {e["line"]: e for e in result["lines"]}
        assert by_line["811"]["best_stop"]["name_en"] == "Shared 811 and 813"
        assert by_line["813"]["best_stop"]["name_en"] == "Shared 811 and 813"
        assert by_line["811"]["stop_no"] == 2
        assert by_line["813"]["stop_no"] == 1


    def test_number_follows_the_closest_stop_for_each_line(
        self, cross_served_cache: Path, mocker
    ) -> None:
        """If the closest stop changes, the number must change with it."""
        mocker.patch.object(find, "_pick_router", lambda: None)
        for entry in find.find_nearest(32.0700, 34.7900)["lines"]:
            assert entry["stop_no"] == entry["best_stop"]["stop_numbers"][entry["line"]]

    def test_helper_omits_a_number_it_does_not_have(self) -> None:
        assert find._stop_no_text({"stop_numbers": {}}, "811") == ""
        assert find._stop_no_text({}) == ""

    def test_helper_shows_every_number_when_they_differ(self) -> None:
        stop = {"stop_numbers": {"811": 9, "814": 6}}
        text = find._stop_no_text(stop)
        assert "9" in text and "6" in text, (
            "printing only one number would be wrong for the other line"
        )

    def test_helper_collapses_agreeing_numbers(self) -> None:
        stop = {"stop_numbers": {"811": 2, "813": 2}}
        assert find._stop_no_text(stop) == "stop 2"


class TestStopCodes:
    """The stop's own code, which fixes the physical stop.

    Distinct from the position along a line: the same code is right for every
    line serving the stop, while the position differs per line.
    """

    def test_every_stop_carries_its_code(self, stops_cache: Path) -> None:
        for stop in find.load_stops():
            assert stop["code"] not in (None, ""), f"{stop['name_en']} has no code"

    def test_a_shared_stop_keeps_one_code_for_every_line(
        self, cross_served_cache: Path
    ) -> None:
        """One physical stop, one sign, one code -- whatever lines serve it."""
        shared = [s for s in find.load_stops() if s["name_en"].startswith("Shared")][0]
        assert shared["code"] == "11"
        assert sorted(shared["lines"]) == ["811", "813"]

    def test_code_survives_to_the_wire(
        self, cross_served_cache: Path, mocker
    ) -> None:
        """A rider cannot find the stop without it, so it must reach the payload."""
        mocker.patch.object(find, "_pick_router", lambda: None)
        result = find.find_nearest(32.0800, 34.7900)
        for entry in result["lines"]:
            assert entry["best_stop"]["code"] == "11"

    def test_code_text_helper(self) -> None:
        assert find._stop_code_text({"code": 20726}) == "code 20726"

    def test_code_text_helper_omits_a_missing_code(self) -> None:
        assert find._stop_code_text({}) == ""
        assert find._stop_code_text({"code": None}) == ""
        assert find._stop_code_text({"code": ""}) == ""

    def test_ref_text_pairs_the_number_with_the_code(self) -> None:
        """The example from the field: 813 stop 3 is code 20726."""
        stop = {"stop_numbers": {"813": 3}, "code": 20726}
        assert find._stop_ref_text(stop, "813") == "stop 3, code 20726"

    def test_ref_text_reports_a_code_without_a_number(self) -> None:
        """The park and ride is unnumbered but still has a sign to look for."""
        assert find._stop_ref_text({"stop_numbers": {}, "code": 2465}) == "code 2465"

    def test_ref_text_is_empty_when_nothing_is_known(self) -> None:
        assert find._stop_ref_text({}) == ""

    def test_ref_text_keeps_the_per_line_number(self) -> None:
        """The code is shared; the number is still resolved for the given line."""
        stop = {"stop_numbers": {"811": 2, "813": 1}, "code": 20726}
        assert find._stop_ref_text(stop, "813") == "stop 1, code 20726"
        assert find._stop_ref_text(stop, "811") == "stop 2, code 20726"


class TestWalkMinuteRounding:
    def test_minutes_round_to_nearest_not_truncate(
        self, stops_cache: Path, routed_600m: None
    ) -> None:
        """600 m at 80 m/min is 7.5 min; showing 7 reads as a different distance."""
        stop = find.find_nearest(32.0747, 34.7920)["stops"][0]
        assert stop["walk_m"] == 600
        assert stop["walk_min"] == 8, "7.5 minutes must round up to 8"

    def test_minutes_are_never_negative(
        self, stops_cache: Path, routed_600m: None
    ) -> None:
        for stop in find.find_nearest(32.0747, 34.7920)["stops"]:
            assert stop["walk_min"] >= 0

    def test_minutes_track_metres(
        self, stops_cache: Path, routed_600m: None
    ) -> None:
        for stop in find.find_nearest(32.0747, 34.7920)["stops"]:
            assert stop["walk_min"] == round(stop["walk_m"] / find.WALK_M_PER_MIN)


class TestRouterPick:
    def test_prefers_local_container(self, mocker) -> None:
        """Localhost first: the container is authoritative and free."""
        seen: list[str] = []

        def get(url, timeout=None):
            seen.append(url)
            if "localhost" in url:
                return _FakeSyncResponse(200)
            return _FakeSyncResponse(503)

        mocker.patch.object(find.httpx, "get", get)
        assert find._pick_router() == "http://localhost:8002/route"
        assert seen[0].endswith("/status")

    def test_falls_through_to_public_host(self, mocker) -> None:
        def get(url, timeout=None):
            if "localhost" in url:
                return _FakeSyncResponse(503)
            if "routing.openstreetmap.de" in url:
                return _FakeSyncResponse(200)
            return _FakeSyncResponse(503)

        mocker.patch.object(find.httpx, "get", get)
        assert find._pick_router() == "https://routing.openstreetmap.de/routed-valhalla/route"

    def test_returns_none_when_all_hosts_fail(self, mocker) -> None:
        mocker.patch.object(find.httpx, "get", lambda url, timeout=None: _FakeSyncResponse(503))
        assert find._pick_router() is None

    def test_returns_none_when_all_hosts_raise(self, mocker) -> None:
        def boom(url, timeout=None):
            raise find.httpx.ConnectError("refused")

        mocker.patch.object(find.httpx, "get", boom)
        assert find._pick_router() is None

    def test_success_is_cached_to_avoid_reprobing(self, mocker) -> None:
        calls = {"n": 0}

        def get(url, timeout=None):
            calls["n"] += 1
            return _FakeSyncResponse(200)

        mocker.patch.object(find.httpx, "get", get)
        find._pick_router()
        find._pick_router()
        find._pick_router()
        assert calls["n"] == 1, f"probed {calls['n']} times, should cache the result"


class TestRouterCacheExpiry:
    def test_failure_cache_expires_so_a_restart_is_picked_up(
        self, mocker
    ) -> None:
        """A container that comes back must be noticed without a server restart."""
        healthy = {"v": False}

        def get(url, timeout=None):
            return _FakeSyncResponse(200 if healthy["v"] else 503)

        mocker.patch.object(find.httpx, "get", get)

        assert find._pick_router() is None, "down at first"

        # Inside the failure TTL the negative result stands.
        healthy["v"] = True
        assert find._pick_router() is None, "should still be cached as down"

        # Past the TTL it re-probes and finds the container.
        find._ROUTER_CHECKED_AT -= find.ROUTER_FAIL_TTL_S + 1
        assert find._pick_router() == "http://localhost:8002/route"


class _FakeSyncResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise find.httpx.HTTPStatusError("bad", request=None, response=None)


class _FakeResponse:
    """Stand-in for httpx.Response, enough for the routing call site."""

    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise find.httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self) -> object:
        return self._payload


class _FakeClient:
    """Async client stand-in whose post() delegates to a handler."""

    def __init__(self, handler) -> None:
        self._handler = handler

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, json=None, timeout=None) -> _FakeResponse:
        request = _FakeRequest(json)
        result = self._handler(request)
        if isinstance(result, _FakeResponse):
            return result
        return _FakeResponse(result)


class _FakeRequest:
    """Carries the JSON body the way httpx does, so tests can assert on it."""

    def __init__(self, payload: object) -> None:
        self.content = json.dumps(payload)
