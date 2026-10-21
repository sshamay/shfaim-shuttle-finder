"""scripts/fetch_schedule.py: turning the operator's timetable into minutes.

Every test stubs the network boundary. The point is the arithmetic (what "ride"
and "return" mean), the join key, and the promise that a broken fetch leaves a
good cache alone rather than half-overwriting it with guesses.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import scripts.fetch_schedule as fetch_schedule

_BASE = datetime(2026, 10, 22, 9, 55)


def _predict_stop(index: int, code: str, minutes: float) -> dict:
    """One stop of a GetPredictForLine trip, ``minutes`` after the boarding."""
    return {
        "stopIndex": index,
        "stopID": code,
        "departureFromOrigin": _BASE.strftime("%Y-%m-%d %H:%M:%S"),
        "aimedArrivalTime": (_BASE + timedelta(minutes=minutes)).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }


def _trip(*specs: tuple[int, str, float]) -> dict:
    return {"stops": [_predict_stop(*spec) for spec in specs]}


class TestMinutesPerCode:
    def test_median_ride_and_the_matching_return_leg(self) -> None:
        """"Return" is the rest of the run, not a second guess at the ride.

        The route ends at the park and ride, so minutes from a stop to the last
        stop is how long it takes to get back there.
        """
        trips = [
            _trip((1, "2465", 0), (2, "20314", 10), (3, "2463", 30)),
            _trip((1, "2465", 0), (2, "20314", 12), (3, "2463", 34)),
        ]

        got = fetch_schedule.minutes_per_code(trips)

        assert got is not None
        assert got["20314"] == {"ride": 11.0, "return": 21.0}
        assert got["2465"] == {"ride": 0.0, "return": 32.0}
        assert got["2463"] == {"ride": 32.0, "return": 0.0}

    def test_the_join_key_is_the_stop_code(self) -> None:
        """The predict feed's stopID is the *code*, not the route's stopID.

        20314 here is the number printed on the stop sign; the route feed calls
        that field ``code`` and uses a different integer for ``stopID``. Joining
        on the wrong one is why the old get_sched813.py probes printed nothing.
        """
        trips = [_trip((1, "20314", 0), (2, "28567", 5))]

        got = fetch_schedule.minutes_per_code(trips)

        assert got is not None
        assert set(got) == {"20314", "28567"}

    def test_a_trip_that_never_reaches_the_park_and_ride_is_skipped(self) -> None:
        """A trip whose boarding cannot be read carries no usable minutes."""
        trips = [{"stops": [_predict_stop(2, "20314", 10)]}]

        assert fetch_schedule.minutes_per_code(trips) is None

    def test_an_empty_batch_is_reported_as_empty(self) -> None:
        assert fetch_schedule.minutes_per_code([]) is None
        assert fetch_schedule.minutes_per_code([{"stops": []}]) is None

    def test_a_stop_without_an_arrival_time_is_ignored(self) -> None:
        trips = [
            {
                "stops": [
                    _predict_stop(1, "2465", 0),
                    {"stopIndex": 2, "stopID": "20314", "aimedArrivalTime": None},
                    _predict_stop(3, "2463", 30),
                ]
            }
        ]

        got = fetch_schedule.minutes_per_code(trips)

        assert got is not None
        assert set(got) == {"2465", "2463"}


class TestApplySchedule:
    def test_minutes_land_on_the_stops_that_share_their_code(self) -> None:
        payload = {
            "lines": [
                {
                    "line": "812",
                    "stops": [
                        {"code": 2465, "stopID": 52234, "ride_min": 0.0, "return_min": 48.0},
                        {"code": 20314, "stopID": 42858, "ride_min": 18.0, "return_min": 22.0},
                    ],
                }
            ]
        }
        by_line = {
            "812": {
                "2465": {"ride": 0.0, "return": 32.0},
                "20314": {"ride": 12.5, "return": 19.2},
            }
        }

        fetch_schedule.apply_schedule(payload, by_line)

        stops = payload["lines"][0]["stops"]
        assert (stops[0]["ride_min"], stops[0]["return_min"]) == (0.0, 32.0)
        assert (stops[1]["ride_min"], stops[1]["return_min"]) == (12.5, 19.2)

    def test_a_stop_the_timetable_missed_refuses_the_whole_update(self) -> None:
        payload = {
            "lines": [
                {"line": "812", "stops": [{"code": 2465}, {"code": 20314}]},
            ]
        }
        by_line = {"812": {"2465": {"ride": 0.0, "return": 32.0}}}

        with pytest.raises(ValueError, match="no timetable for stops \\[20314\\]"):
            fetch_schedule.apply_schedule(payload, by_line)

    def test_a_line_the_fetch_never_reached_is_left_alone(self) -> None:
        payload = {"lines": [{"line": "815", "stops": [{"code": 2465}]}]}

        fetch_schedule.apply_schedule(payload, {})

        assert payload["lines"][0]["stops"][0] == {"code": 2465}


class TestMain:
    def _stub_client(self, monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def get(self, *args, **kwargs):
                return _Resp()

        monkeypatch.setattr(fetch_schedule.httpx, "Client", _Client)

    def test_a_full_run_stamps_the_cache(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "stops.json"
        path.write_text(
            json.dumps(
                {
                    "fetched_at": "2026-10-05T00:00:00+00:00",
                    "lines": [
                        {
                            "line": "812",
                            "name": "HaBursa",
                            "directions": {"1": {"route_id": 44092}},
                            "stops": [
                                {"code": 2465, "ride_min": 0.0, "return_min": 48.0},
                                {"code": 20314, "ride_min": 18.0, "return_min": 22.0},
                            ],
                        }
                    ],
                }
            )
        )
        monkeypatch.setattr(fetch_schedule.config, "STOPS_CACHE", path)
        self._stub_client(
            monkeypatch,
            {"trips": [_trip((1, "2465", 0), (2, "20314", 12), (3, "2465", 30))]},
        )

        assert fetch_schedule.main() == 0

        saved = json.loads(path.read_text())
        assert saved["schedule_fetched_at"]
        stops = saved["lines"][0]["stops"]
        assert (stops[1]["ride_min"], stops[1]["return_min"]) == (12.0, 18.0)

    def test_a_failed_run_leaves_the_cache_byte_for_byte(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "stops.json"
        original = json.dumps(
            {
                "lines": [
                    {
                        "line": "812",
                        "name": "HaBursa",
                        "directions": {"1": {"route_id": 44092}},
                        "stops": [{"code": 2465, "ride_min": 0.0, "return_min": 48.0}],
                    }
                ]
            }
        )
        path.write_text(original)
        monkeypatch.setattr(fetch_schedule.config, "STOPS_CACHE", path)
        self._stub_client(monkeypatch, {"trips": []})

        assert fetch_schedule.main() == 1
        assert path.read_text() == original

    def test_a_cache_that_was_never_fetched_is_an_error(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fetch_schedule.config, "STOPS_CACHE", tmp_path / "nope.json")

        assert fetch_schedule.main() == 1
