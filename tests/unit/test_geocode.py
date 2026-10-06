"""Geocoder candidate ranking.

Every case here maps to something that actually went wrong during
development: Photon returning a Modi'in EV charger for "Azrieli Center", Hebrew
labels for an English UI, and "Rothschild" resolving to London and Wisconsin.
"""

from __future__ import annotations

import httpx
import pytest

from app import geocode
from tests.conftest import photon_feature, photon_response

TEL_AVIV = "Tel Aviv"
MODIIN = "Modiin-Maccabim-Reut"
LONDON_CITY = "London"


async def _search(monkeypatch: pytest.MonkeyPatch, query: str, *features: dict, limit: int = 5) -> list[dict]:
    """Run a Photon search against canned features."""
    calls: list[dict] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return photon_response(*features)

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url, params=None, **kwargs):
            calls.append(dict(params or {}))
            return _Resp()

    monkeypatch.setattr(geocode.httpx, "AsyncClient", _Client)
    results = await geocode.PhotonGeocoder().search(query, limit=limit)
    return results


def _sync(monkeypatch: pytest.MonkeyPatch, query: str, *features: dict, limit: int = 5) -> list[dict]:
    import asyncio

    return asyncio.run(_search(monkeypatch, query, *features, limit=limit))


async def _canned_search(
    monkeypatch: pytest.MonkeyPatch, query: str, by_query: dict[str, list[dict]], limit: int = 5
) -> tuple[list[dict], list[str]]:
    """Photon search answering each distinct request from its own fixture.

    Returns the results alongside the queries actually sent, so a test can
    prove both what was asked and how many times the network was touched.
    """
    sent: list[str] = []

    class _Resp:
        status_code = 200

        def __init__(self, features: list[dict]) -> None:
            self._features = features

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return photon_response(*self._features)

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url, params=None, **kwargs):
            q = (params or {}).get("q", "")
            sent.append(q)
            return _Resp(by_query.get(q, []))

    monkeypatch.setattr(geocode.httpx, "AsyncClient", _Client)
    results = await geocode.PhotonGeocoder().search(query, limit=limit)
    return results, sent


class TestPhotonLanguage:
    def test_requests_english_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """lang=default returned Hebrew-only names for an English UI."""
        calls: list[dict] = []

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return photon_response()

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return None

            async def get(self, url, params=None, **k):
                calls.append(dict(params or {}))
                return _Resp()

        monkeypatch.setattr(geocode.httpx, "AsyncClient", _Client)
        # Empty response raises, but the request params were still recorded.
        with pytest.raises(geocode.GeocodeError):
            import asyncio

            asyncio.run(geocode.PhotonGeocoder().search("Azrieli Center"))

        assert calls[0]["lang"] == "en", "expected English result names"

    def test_azrieli_resolves_to_tel_aviv_not_modiin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real failure: an EV charger in Modi'in matched on the name."""
        results = _sync(
            monkeypatch,
            "Azrieli Center",
            photon_feature(
                name="Modi'in Azrieli Supercharger",
                city=MODIIN,
                osm_value="charging_station",
                lat=31.8997,
                lon=35.0089,
            ),
            photon_feature(
                name="Azrieli Center",
                city=TEL_AVIV,
                osm_value="mall",
                lat=32.0747,
                lon=34.7920,
            ),
        )
        assert results[0]["city"] == TEL_AVIV
        assert "Modi'in" not in results[0]["label"]


class TestCityPreference:
    def test_query_city_outranks_a_better_typed_result_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bus stop beats a house type-wise, but only if it is the right town."""
        results = _sync(
            monkeypatch,
            "Rothschild Tel Aviv",
            photon_feature(
                name="Rothschild", city=TEL_AVIV, osm_value="station",
                lat=32.0267, lon=34.7445,
            ),
            photon_feature(
                name="Rothschild", city=TEL_AVIV, osm_value="house",
                housenumber="22", lat=32.0628, lon=34.7717,
            ),
        )
        assert results[0]["type"] == "house"

    def test_city_mismatch_is_penalised_heavily(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results = _sync(
            monkeypatch,
            "Rothschild Tel Aviv",
            photon_feature(name="Rothschild", city=TEL_AVIV, osm_value="house", lat=32.06, lon=34.77),
            photon_feature(
                name="Rothschild", city="Jerusalem", osm_value="house",
                lat=31.7683, lon=35.2137,
            ),
        )
        assert results[0]["city"] == TEL_AVIV

    def test_state_field_alone_does_not_count_as_the_city(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The district field reads "Tel Aviv" for Bnei Brak too, which caused
        false positives before the city-only rule went in."""
        results = _sync(
            monkeypatch,
            "Kaplan Tel Aviv",
            photon_feature(
                name="Eliezer Kaplan", city="Bnei Brak", osm_value="primary",
                state="מחוז תל אביב", lat=32.0847, lon=34.8338,
            ),
            photon_feature(
                name="Eliezer Kaplan", city=TEL_AVIV, osm_value="primary",
                state="מחוז תל אביב", lat=32.0800, lon=34.7800,
            ),
        )
        assert results[0]["city"] == TEL_AVIV

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("Ramat Gan", "ramat gan"),
            ("רמת גן", "ramat gan"),
            ("Tel Aviv", "tel aviv"),
            ("tel-aviv", "tel aviv"),
            ("תל אביב", "tel aviv"),
        ],
    )
    def test_city_aliases_normalise(self, query: str, expected: str) -> None:
        assert expected in geocode._wanted_cities(query)


class TestNoiseFiltering:
    @pytest.mark.parametrize(
        "osm_value",
        ["bicycle_parking", "charging_station", "toilets", "parking", "bench", "tree"],
    )
    def test_noise_types_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch, osm_value: str
    ) -> None:
        with pytest.raises(geocode.GeocodeError):
            _sync(
                monkeypatch,
                "Somewhere",
                photon_feature(name="A", city=TEL_AVIV, osm_value=osm_value),
            )

    def test_a_noise_result_does_not_hide_a_good_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = _sync(
            monkeypatch,
            "Azrieli Center",
            photon_feature(name="Bench", city=TEL_AVIV, osm_value="bench"),
            photon_feature(name="Azrieli Center", city=TEL_AVIV, osm_value="mall", lat=32.0747, lon=34.792),
        )
        assert len(results) == 1
        assert results[0]["label"].startswith("Azrieli")


class TestOutOfArea:
    def test_far_candidates_are_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results = _sync(
            monkeypatch,
            "Rothschild",
            photon_feature(name="Rothschild", city=LONDON_CITY, osm_value="house", lat=51.5, lon=-0.1),
        )
        assert results[0]["out_of_area"] is True

    def test_tel_aviv_is_not_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results = _sync(
            monkeypatch,
            "Azrieli Center",
            photon_feature(name="Azrieli Center", city=TEL_AVIV, osm_value="mall", lat=32.0747, lon=34.792),
        )
        assert results[0]["out_of_area"] is False

    def test_in_area_results_are_kept_ahead_of_far_ones(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """London matched "Rothschild" better on type than Tel Aviv did."""
        results = _sync(
            monkeypatch,
            "Rothschild",
            photon_feature(name="Rothschild", city=LONDON_CITY, osm_value="house", lat=51.5, lon=-0.1),
            photon_feature(name="Rothschild Island", city="Indonesia", osm_value="island", lat=-0.5, lon=106.0),
            photon_feature(name="Rothschild 22", city=TEL_AVIV, osm_value="house", lat=32.0628, lon=34.7717),
        )
        assert results[0]["city"] == TEL_AVIV
        assert results[0]["out_of_area"] is False

    def test_far_results_are_never_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Flagged, not filtered: a distant address still deserves an answer."""
        results = _sync(
            monkeypatch,
            "Rothschild",
            photon_feature(name="Rothschild", city=LONDON_CITY, osm_value="house", lat=51.5, lon=-0.1),
        )
        assert len(results) == 1
        assert results[0]["out_of_area"] is True

    def test_a_far_only_result_is_still_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = _sync(
            monkeypatch,
            "Eilat",
            photon_feature(name="Eilat", city="Eilat", osm_value="town", lat=29.558, lon=34.948),
        )
        assert len(results) == 1


class TestMetroGeometry:
    def test_distance_measurement_is_symmetric(self) -> None:
        a = geocode._meters_from_metro(32.07, 34.79)
        b = geocode._meters_from_metro(32.09, 34.81)
        assert a >= 0 and b >= 0

    def test_center_is_zero_distance(self) -> None:
        assert geocode._meters_from_metro(*geocode.METRO_CENTER) < 1.0

    def test_jerusalem_is_outside_the_radius(self) -> None:
        assert geocode._meters_from_metro(31.7683, 35.2137) > geocode.OUT_OF_AREA_WARN_M

    def test_ramat_gan_is_inside_the_radius(self) -> None:
        assert geocode._meters_from_metro(32.0792, 34.8218) < geocode.METRO_RADIUS_M

    def test_penalty_is_bounded(self) -> None:
        """An uncapped penalty would bury a legitimate far address entirely."""
        assert geocode.MAX_DISTANCE_PENALTY <= 100


class TestTypeRanking:
    def test_town_wins_over_a_venue_inside_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Searching "Ramat Gan" returned a theatre before the town itself."""
        results = _sync(
            monkeypatch,
            "Ramat Gan",
            photon_feature(name="HaShpagel", city="Ramat Gan", osm_value="theatre", lat=32.07, lon=34.82),
            photon_feature(name="Ramat Gan", city="Ramat Gan", osm_value="town", lat=32.0792, lon=34.8218),
        )
        assert results[0]["type"] == "town"

    def test_house_with_number_beats_a_bare_road(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = _sync(
            monkeypatch,
            "Begin 100 Tel Aviv",
            photon_feature(name="Begin Road", city=TEL_AVIV, osm_value="secondary", lat=32.069, lon=34.788),
            photon_feature(
                name="Begin Road", city=TEL_AVIV, osm_value="house",
                housenumber="100", lat=32.0695, lon=34.7882,
            ),
        )
        assert results[0]["housenumber"] == "100"

    def test_limit_is_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results = _sync(
            monkeypatch,
            "Rothschild Tel Aviv",
            *[photon_feature(name=f"Rothschild {i}", city=TEL_AVIV, osm_value="house", lat=32.06 + i * 0.001, lon=34.77) for i in range(8)],
            limit=3,
        )
        assert len(results) == 3


class TestResultShape:
    def test_internal_rank_key_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Leaking _rank would put an internal key in the API response."""
        results = _sync(
            monkeypatch,
            "Azrieli Center",
            photon_feature(name="Azrieli Center", city=TEL_AVIV, osm_value="mall"),
        )
        assert "_rank" not in results[0]

    def test_the_correction_marker_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_corrected orders results internally; it is not part of the payload."""
        import asyncio

        results, _ = asyncio.run(
            _canned_search(monkeypatch, "Marsel yanko 10", TestAddressCorrection._scenario())
        )
        assert all("_corrected" not in r for r in results)

    def test_coordinates_are_floats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results = _sync(
            monkeypatch,
            "Azrieli Center",
            photon_feature(name="Azrieli Center", city=TEL_AVIV, osm_value="mall"),
        )
        assert isinstance(results[0]["lat"], float)
        assert isinstance(results[0]["lon"], float)

    def test_label_is_non_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results = _sync(
            monkeypatch,
            "Azrieli Center",
            photon_feature(name="Azrieli Center", city=TEL_AVIV, osm_value="mall"),
        )
        assert results[0]["label"].strip()

    def test_every_result_carries_out_of_area_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = _sync(
            monkeypatch,
            "Rothschild",
            photon_feature(name="A", city=TEL_AVIV, osm_value="house", lat=32.06, lon=34.77),
            photon_feature(name="B", city=LONDON_CITY, osm_value="house", lat=51.5, lon=-0.1),
        )
        assert all("out_of_area" in r for r in results)


class TestEmptyResults:
    def test_no_features_raises_rather_than_returning_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The UI treats an empty list as 'nothing found', which needs a reason."""
        with pytest.raises(geocode.GeocodeError):
            _sync(monkeypatch, "Nonexistent Place Name")

    def test_all_noise_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(geocode.GeocodeError):
            _sync(
                monkeypatch,
                "Bench",
                photon_feature(name="Bench", city=TEL_AVIV, osm_value="bench"),
            )


class TestFallbackChain:
    def test_photon_rate_limit_is_reported_clearly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Resp:
            status_code = 429

            def raise_for_status(self):
                return None

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return None

            async def get(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(geocode.httpx, "AsyncClient", _Client)
        with pytest.raises(geocode.GeocodeError, match="rate limit"):
            import asyncio

            asyncio.run(geocode.PhotonGeocoder().search("Anything"))

    def test_all_geocoders_failing_mentions_each_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        async def boom(self, query, *, limit=5):
            raise geocode.GeocodeError(f"{self.name}: down")

        for cls in (
            geocode.PhotonGeocoder,
            geocode.NominatimGeocoder,
            geocode.SerpApiGeocoder,
            geocode.GoogleGeocoder,
        ):
            monkeypatch.setattr(cls, "search", boom)

        with pytest.raises(geocode.GeocodeError) as exc:
            asyncio.run(geocode.search("anything"))

        message = str(exc.value)
        assert "photon" in message
        assert "nominatim" in message

    def test_http_errors_are_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return None

            async def get(self, *a, **k):
                raise httpx.ConnectError("dns fail")

        monkeypatch.setattr(geocode.httpx, "AsyncClient", _Client)
        with pytest.raises(geocode.GeocodeError):
            asyncio.run(geocode.search("anything"))


def _house(
    number: str = "", *, label: str = "Dizengoff", out_of_area: bool = False
) -> dict:
    """One candidate that ``_answers`` will recognise as a house for the chain.

    Shaped like a Photon answer; only ``housenumber``, ``type`` and
    ``out_of_area`` matter to ``_resolves``.
    """
    return {
        "label": label,
        "street": label,
        "housenumber": number,
        "city": "Tel Aviv" if not out_of_area else "Wyoming",
        "lat": 32.0,
        "lon": 34.7,
        "out_of_area": out_of_area,
        "type": "house",
        "osm_type": "W",
        "osm_id": 1,
        "osm_key": "building",
    }


def _no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no .env and no env vars: the chain must cope on its own."""
    monkeypatch.setattr(geocode, "_load_dotenv", lambda: None)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("GEOCODER", raising=False)


class TestFallbackChainAdvances:
    """The chain really moves: primary -> nominatim -> serpapi.

    The pre-existing fallback tests assert error messages; these assert the
    order in which backends are actually consulted and whose answer wins, so
    a fallback that never gets its turn is caught rather than invisible.
    """

    def test_primary_answer_stops_the_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_keys(monkeypatch)
        import asyncio

        called: list[str] = []

        async def photon(self, query, *, limit=5):
            called.append("photon")
            return [_house("34")]

        async def nominatim(self, query, *, limit=5):
            called.append("nominatim")
            return [_house("34")]

        async def serpapi(self, query, *, limit=5):
            called.append("serpapi")
            return [_house("34")]

        for cls, fn in (
            (geocode.PhotonGeocoder, photon),
            (geocode.NominatimGeocoder, nominatim),
            (geocode.SerpApiGeocoder, serpapi),
        ):
            monkeypatch.setattr(cls, "search", fn)

        results = asyncio.run(geocode.search("Dizengoff 34"))
        assert called == ["photon"]
        assert results[0]["housenumber"] == "34"

    def test_missing_serpapi_key_never_blocks_photon(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: search() used to raise before trying anything.

        The fallback list is built up front, and constructing SerpApiGeocoder
        without a key raised GeocodeError then and there -- so the very first
        geocoder, photon, never got a turn unless .env happened to configure a
        key. The suite's 200+ tests passed only because a real key exists in
        .env on the dev machine.
        """
        _no_keys(monkeypatch)
        import asyncio

        called: list[str] = []

        async def photon(self, query, *, limit=5):
            called.append("photon")
            return [_house("34")]

        monkeypatch.setattr(geocode.PhotonGeocoder, "search", photon)

        results = asyncio.run(geocode.search("Dizengoff 34"))
        assert called == ["photon"]
        assert results[0]["housenumber"] == "34"

    def test_unresolved_primary_defers_to_nominatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_keys(monkeypatch)
        import asyncio

        called: list[str] = []

        async def photon(self, query, *, limit=5):
            called.append("photon")
            return [_house("")]  # the street, but not the number asked for

        async def nominatim(self, query, *, limit=5):
            called.append("nominatim")
            return [_house("34")]

        async def serpapi(self, query, *, limit=5):
            called.append("serpapi")
            return [_house("34")]

        for cls, fn in (
            (geocode.PhotonGeocoder, photon),
            (geocode.NominatimGeocoder, nominatim),
            (geocode.SerpApiGeocoder, serpapi),
        ):
            monkeypatch.setattr(cls, "search", fn)

        results = asyncio.run(geocode.search("Dizengoff 34"))
        assert called == ["photon", "nominatim"]
        assert results[0]["housenumber"] == "34"

    def test_failing_primary_falls_back_to_nominatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_keys(monkeypatch)
        import asyncio

        called: list[str] = []

        async def photon(self, query, *, limit=5):
            called.append("photon")
            raise geocode.GeocodeError("photon: rate limit")

        async def nominatim(self, query, *, limit=5):
            called.append("nominatim")
            return [_house("34")]

        monkeypatch.setattr(geocode.PhotonGeocoder, "search", photon)
        monkeypatch.setattr(geocode.NominatimGeocoder, "search", nominatim)

        results = asyncio.run(geocode.search("Dizengoff 34"))
        assert called == ["photon", "nominatim"]
        assert results[0]["housenumber"] == "34"

    def test_chain_reaches_serpapi_last_of_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_keys(monkeypatch)
        import asyncio

        # SerpAPI needs a key to be constructible; give it one so it can be
        # tried after photon and nominatim both return nothing for the number.
        monkeypatch.setenv("SERPAPI_API_KEY", "secret-key-value")
        called: list[str] = []

        async def photon(self, query, *, limit=5):
            called.append("photon")
            return [_house("")]

        async def nominatim(self, query, *, limit=5):
            called.append("nominatim")
            return [_house("")]

        async def serpapi(self, query, *, limit=5):
            called.append("serpapi")
            return [_house("34")]

        for cls, fn in (
            (geocode.PhotonGeocoder, photon),
            (geocode.NominatimGeocoder, nominatim),
            (geocode.SerpApiGeocoder, serpapi),
        ):
            monkeypatch.setattr(cls, "search", fn)

        results = asyncio.run(geocode.search("Dizengoff 34"))
        assert called == ["photon", "nominatim", "serpapi"]
        assert results[0]["housenumber"] == "34"

    def test_best_partial_result_survives_when_the_rest_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plausible-but-incomplete answer is returned over nothing at all."""
        _no_keys(monkeypatch)
        monkeypatch.setenv("SERPAPI_API_KEY", "secret-key-value")
        import asyncio

        partial = [_house("")]

        async def photon(self, query, *, limit=5):
            return partial

        for cls in (geocode.NominatimGeocoder, geocode.SerpApiGeocoder):
            async def boom(self, query, *, limit=5):
                raise geocode.GeocodeError(f"{self.name}: down")

            monkeypatch.setattr(cls, "search", boom)

        monkeypatch.setattr(geocode.PhotonGeocoder, "search", photon)

        results = asyncio.run(geocode.search("Dizengoff 34"))
        assert results is partial

    def test_every_failure_is_named_for_people_to_fix(self, monkeypatch) -> None:
        """The raised message lists every backend, including a skipped one."""
        _no_keys(monkeypatch)
        import asyncio

        async def photon(self, query, *, limit=5):
            raise geocode.GeocodeError("photon: down")

        async def nominatim(self, query, *, limit=5):
            raise geocode.GeocodeError("nominatim: down")

        for cls, fn in (
            (geocode.PhotonGeocoder, photon),
            (geocode.NominatimGeocoder, nominatim),
        ):
            monkeypatch.setattr(cls, "search", fn)

        with pytest.raises(geocode.GeocodeError) as exc:
            asyncio.run(geocode.search("Dizengoff 34"))

        message = str(exc.value)
        assert "photon" in message
        assert "nominatim" in message
        assert "serpapi" in message  # constructed, failed, and must not hide
        assert "SERPAPI_API_KEY is not set" in message


class TestGetGeocoder:
    def test_unknown_name_rejects_with_the_valid_options(self) -> None:
        with pytest.raises(geocode.GeocodeError, match="photon"):
            geocode.get_geocoder("bogus")

    def test_default_is_photon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GEOCODER", raising=False)
        assert geocode.get_geocoder().name == "photon"

    def test_env_var_selects_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEOCODER", "nominatim")
        assert geocode.get_geocoder().name == "nominatim"

    def test_serpapi_without_key_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(geocode, "_load_dotenv", lambda: None)
        monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
        with pytest.raises(geocode.GeocodeError, match="SERPAPI_API_KEY"):
            geocode.get_geocoder("serpapi")


class TestSecretHygiene:
    def test_api_key_is_never_returned_to_callers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key in a geocode result would ship to the browser."""
        import asyncio

        monkeypatch.setenv("SERPAPI_API_KEY", "secret-key-value")

        async def fake(self, query, *, limit=5):
            return [{
                "label": "Some Place", "lat": 32.07, "lon": 34.79,
                "type": "cafe", "out_of_area": False,
            }]

        monkeypatch.setattr(geocode.SerpApiGeocoder, "search", fake)
        results = asyncio.run(geocode.SerpApiGeocoder("secret-key-value").search("x"))

        assert results
        for result in results:
            assert "secret-key-value" not in str(result)

    def test_serpapi_rejects_a_missing_key(self) -> None:
        with pytest.raises(geocode.GeocodeError):
            geocode.SerpApiGeocoder("")


def _serpapi_client(payload: dict) -> type:
    """A httpx.AsyncClient stub whose GET returns a fixed SerpAPI payload."""

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return payload

    class _Client:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *e: object) -> None:
            return None

        async def get(self, *a: object, **k: object) -> _Resp:
            return _Resp()

    return _Client


_MODERN_PLACE = {
    "place_results": {
        "title": "Azrieli Center",
        "address": "Menakhem Begin Rd, Tel Aviv-Jaffa",
        "gps_coordinates": {"latitude": 32.0740769, "longitude": 34.7922028},
        "type": ["Building"],
        "type_ids": ["compound_building"],
        "place_id": "ChIJk_oKbZlLHRURHIGMQNqds_c",
    }
}


class TestSerpApiParse:
    """Parsing real SerpAPI payloads, which used to be untested.

    The suite previously patched SerpApiGeocoder.search wholesale, so the
    response-format shits (local_results list -> featured place_results object)
    went unnoticed: the backend called SerpAPI fine and then concluded there
    was "no match" even though the answer was in the response.
    """

    def test_modern_place_results_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        monkeypatch.setattr(geocode.httpx, "AsyncClient", _serpapi_client(_MODERN_PLACE))
        results = asyncio.run(geocode.SerpApiGeocoder("secret-key-value").search("Azrieli"))

        assert len(results) == 1
        r = results[0]
        assert r["label"] == "Azrieli Center"
        assert r["street"] == "Menakhem Begin Rd, Tel Aviv-Jaffa"
        assert (r["lat"], r["lon"]) == (32.0740769, 34.7922028)
        assert r["type"] == "Building"
        assert r["out_of_area"] is False
        assert r["place_id"] == "ChIJk_oKbZlLHRURHIGMQNqds_c"

    def test_legacy_local_results_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        payload = {
            "local_results": [
                {
                    "title": "Azrieli Center",
                    "address": "Menakhem Begin Rd",
                    "gps_coordinates": {"latitude": 32.0740769, "longitude": 34.7922028},
                    "type": "shopping_mall",
                    "place_id": "id-1",
                },
                {
                    "title": "Elsewhere",
                    "address": "Far Away",
                    "gps_coordinates": {"latitude": 51.5, "longitude": -0.1},
                    "type": None,
                    "place_id": "id-2",
                },
            ]
        }
        monkeypatch.setattr(geocode.httpx, "AsyncClient", _serpapi_client(payload))
        results = asyncio.run(geocode.SerpApiGeocoder("secret-key-value").search("Azrieli"))

        assert len(results) == 2
        assert results[0]["out_of_area"] is False
        assert results[1]["out_of_area"] is True

    def test_featured_place_without_coordinates_uses_local_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        payload = {
            "place_results": {"title": "Azrieli Center", "address": "x", "type": str},
            "local_results": [
                {
                    "title": "Azrieli Center",
                    "address": "Menakhem Begin Rd",
                    "gps_coordinates": {"latitude": 32.0740769, "longitude": 34.7922028},
                    "type_ids": ["compound_building"],
                    "place_id": "id-1",
                }
            ],
        }
        monkeypatch.setattr(geocode.httpx, "AsyncClient", _serpapi_client(payload))
        results = asyncio.run(geocode.SerpApiGeocoder("secret-key-value").search("Azrieli"))

        assert len(results) == 1
        assert results[0]["type"] == "compound_building"

    def test_api_error_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        payload = {"error": "some quota message"}
        monkeypatch.setattr(geocode.httpx, "AsyncClient", _serpapi_client(payload))
        with pytest.raises(geocode.GeocodeError, match="quota message"):
            asyncio.run(geocode.SerpApiGeocoder("secret-key-value").search("Azrieli"))


def _answer(
    *,
    label: str,
    city: str = TEL_AVIV,
    street: str = "",
    housenumber: str = "",
    osm_value: str = "house",
    lat: float = 32.07,
    lon: float = 34.79,
) -> dict:
    """A result-shaped dict, as a Geocoder.search returns it."""
    return {
        "label": label,
        "street": street,
        "housenumber": housenumber,
        "city": city,
        "state": "",
        "lat": lat,
        "lon": lon,
        "out_of_area": geocode._meters_from_metro(lat, lon) > geocode.OUT_OF_AREA_WARN_M,
        "type": osm_value,
        "osm_type": None,
        "osm_id": None,
        "osm_key": None,
    }


class TestAddressCorrection:
    """The reported bug: "Marsel yanko 10" resolving to a Holon butcher.

    Photon's first answer has the house number right and everything else wrong,
    and because the chain stopped at the first non-empty response the fallbacks
    never ran either. The street is still recoverable by asking for it without
    the number, which is what these cases replay.
    """

    @staticmethod
    def _scenario() -> dict[str, list[dict]]:
        return {
            # What Photon returns for the address as typed: unrelated POIs that
            # happen to share the house number, none of them a building.
            "Marsel yanko 10": [
                photon_feature(
                    name="Marsel Brothers", city="Holon", osm_value="butcher",
                    housenumber="10", lat=32.0222, lon=34.7751, osm_id=11,
                ),
                photon_feature(
                    street="Yanko Sakazov Blvd.", city="Sofia", osm_value="apartments",
                    housenumber="10", lat=42.6965, lon=23.3367, osm_id=12,
                ),
            ],
            # Drop the number and the canonical spelling surfaces, carried by
            # whatever sits on that street.
            "Marsel yanko": [
                photon_feature(
                    name="7 Marcel Yanko St.", street="Marcel Janco", city=TEL_AVIV,
                    osm_value="bicycle_rental", housenumber="7",
                    lat=32.1176, lon=34.8246, osm_id=13,
                ),
            ],
            # Re-asked with the real spelling and the original number.
            "Marcel Janco 10": [
                photon_feature(
                    street="Marcel Janco", city=TEL_AVIV, osm_value="house",
                    housenumber="10", lat=32.117517, lon=34.824959, osm_id=14,
                ),
            ],
        }

    def test_a_misspelt_address_is_corrected_through_its_street_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        results, sent = asyncio.run(
            _canned_search(monkeypatch, "Marsel yanko 10", self._scenario())
        )

        assert sent == ["Marsel yanko 10", "Marsel yanko", "Marcel Janco 10"]
        assert results[0]["street"] == "Marcel Janco"
        assert results[0]["housenumber"] == "10"
        assert results[0]["city"] == TEL_AVIV
        assert results[0]["out_of_area"] is False

    def test_the_butchers_result_is_still_available_but_not_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Correction adds an answer, it never throws away what was found."""
        import asyncio

        results, _ = asyncio.run(
            _canned_search(monkeypatch, "Marsel yanko 10", self._scenario())
        )
        cities = [r["city"] for r in results]
        assert cities[0] == TEL_AVIV
        assert "Holon" in cities

    @pytest.mark.parametrize(
        ("query", "sent_count"),
        [
            ("Dizengoff 34", 1),
            ("Azrieli Center", 1),
            ("מרסל ינקו 10", 1),
            ("Rothschild Tel Aviv", 1),
        ],
    )
    def test_a_query_that_already_works_costs_one_request(
        self, monkeypatch: pytest.MonkeyPatch, query: str, sent_count: int
    ) -> None:
        import asyncio

        by_query = {
            query: [
                photon_feature(
                    street="Dizengoff" if "Dizengoff" in query else "Marcel Janco",
                    name="Azrieli Center" if "Azrieli" in query else "",
                    city=TEL_AVIV, osm_value="house",
                    housenumber="34" if "Dizengoff" in query else "10",
                    lat=32.0747, lon=34.7920,
                )
            ]
        }
        if query == "Rothschild Tel Aviv":
            by_query[query] = [
                photon_feature(name="Rothschild", city=TEL_AVIV, osm_value="station", lat=32.07, lon=34.79)
            ]

        _, sent = asyncio.run(_canned_search(monkeypatch, query, by_query))
        assert len(sent) == sent_count, f"expected one Photon request, got {sent}"

    def test_a_failed_correction_keeps_the_original_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rate limit mid-recovery must not turn a mediocre answer into none."""
        import asyncio

        scenario = self._scenario()
        scenario["Marsel yanko"] = []

        results, sent = asyncio.run(
            _canned_search(monkeypatch, "Marsel yanko 10", scenario)
        )

        assert len(sent) == 2
        assert results
        assert results[0]["city"] == "Holon"


class TestAddressParsing:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("Marsel yanko 10", ("10", "Marsel yanko", "")),
            ("Marcel Janco 10, Tel Aviv", ("10", "Marcel Janco", "Tel Aviv")),
            ("Rothschild 1 Tel Aviv", ("1", "Rothschild", "Tel Aviv")),
            ("Begin 100 Tel Aviv", ("100", "Begin", "Tel Aviv")),
        ],
    )
    def test_address_queries_split(self, query: str, expected: tuple[str, str, str]) -> None:
        assert geocode._parse_address(query) == expected

    @pytest.mark.parametrize(
        "query",
        ["Azrieli Center", "10", "Dizengoff", "Habima Square", ""],
    )
    def test_non_addresses_are_left_alone(self, query: str) -> None:
        assert geocode._parse_address(query) is None

    def test_a_result_must_be_a_building_to_count_as_resolved(self) -> None:
        """A shop at number 10 is not an answer to "number 10"."""
        shop = {"type": "butcher", "housenumber": "10"}
        house = {"type": "house", "housenumber": "10"}
        assert geocode._resolves([shop], "10") is False
        assert geocode._resolves([house], "10") is True

    def test_house_numbers_compare_without_suffixes(self) -> None:
        """Israeli addresses carry letter suffixes: 10 is 10א too."""
        assert geocode._resolves([{"type": "house", "housenumber": "10א"}], "10") is True


class TestChainAdvances:
    def test_an_unresolved_primary_does_not_end_the_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallbacks were documented but unreachable while Photon was verbose."""
        import asyncio

        async def photon(self, query, *, limit=5):
            return [
                _answer(
                    label="Marsel Brothers, Holon", city="Holon", street="Weizman Square",
                    housenumber="10", osm_value="butcher", lat=32.0222, lon=34.7751,
                )
            ]

        async def nominatim(self, query, *, limit=5):
            return [
                _answer(
                    label="10 Marcel Janco St, Tel Aviv", street="Marcel Janco",
                    housenumber="10", lat=32.1175, lon=34.825,
                )
            ]

        monkeypatch.setattr(geocode.PhotonGeocoder, "search", photon)
        monkeypatch.setattr(geocode.NominatimGeocoder, "search", nominatim)
        monkeypatch.setattr(geocode.SerpApiGeocoder, "search", photon)

        results = asyncio.run(geocode.search("Marsel yanko 10"))
        assert results[0]["street"] == "Marcel Janco"

    def test_nothing_resolving_never_leaves_the_caller_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Advancing the chain must not turn a mediocre answer into an error."""
        import asyncio

        async def photon(self, query, *, limit=5):
            return [
                _answer(
                    label="Marsel Brothers, Holon", city="Holon", street="Weizman Square",
                    housenumber="10", osm_value="butcher", lat=32.0222, lon=34.7751,
                )
            ]

        async def down(self, query, *, limit=5):
            raise geocode.GeocodeError("no match")

        monkeypatch.setattr(geocode.PhotonGeocoder, "search", photon)
        monkeypatch.setattr(geocode.NominatimGeocoder, "search", down)
        monkeypatch.setattr(geocode.SerpApiGeocoder, "search", down)

        results = asyncio.run(geocode.search("Marsel yanko 10"))
        assert results[0]["city"] == "Holon"

    def test_a_query_without_a_number_still_stops_at_the_primary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        async def photon(self, query, *, limit=5):
            return [_answer(label="Azrieli Center, Tel Aviv", osm_value="mall")]

        async def never(self, query, *, limit=5):
            raise AssertionError("the primary already answered, no fallback needed")

        monkeypatch.setattr(geocode.PhotonGeocoder, "search", photon)
        monkeypatch.setattr(geocode.NominatimGeocoder, "search", never)
        monkeypatch.setattr(geocode.SerpApiGeocoder, "search", never)

        results = asyncio.run(geocode.search("Azrieli Center"))
        assert results[0]["label"].startswith("Azrieli")


NEAR_MALL = (32.0747, 34.7920)  # Azrieli Center, ~0.6 km from the metro
FAR_MALL = (31.8998, 34.7761)  # the same mall in Modi'in, ~20 km away


class TestTieBreak:
    """Ordering candidates that rank equally.

    Latitude used to break every tie, which is not a rule so much as an
    accident of the sort: the Modi'in branch of Azrieli wins it by sitting
    further south than the Tel Aviv one, so the shop picked the wrong mall.
    """

    @staticmethod
    def _azrieli() -> list[dict]:
        return [
            photon_feature(
                name="Azrieli Modi'in Mall",
                city=MODIIN,
                osm_value="mall",
                housenumber="16",
                lat=FAR_MALL[0],
                lon=FAR_MALL[1],
                osm_id=21,
            ),
            photon_feature(
                name="Azrieli Center",
                city=TEL_AVIV,
                osm_value="mall",
                lat=NEAR_MALL[0],
                lon=NEAR_MALL[1],
                osm_id=22,
            ),
        ]

    def test_a_tie_goes_to_the_candidate_nearer_the_shuttle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = _sync(monkeypatch, "Azrieli Center", *self._azrieli())

        assert results[0]["city"] == TEL_AVIV
        assert results[1]["city"] == MODIIN

    def test_the_city_the_caller_named_beats_the_nearer_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Distance is only a tie-break, and the city names win it first."""
        results = _sync(
            monkeypatch,
            "Weizmann 1 Rehovot",
            photon_feature(
                street="Chaim Weizmann", city="Rehovot", housenumber="1",
                lat=31.8991, lon=34.7796, osm_id=23,
            ),
            photon_feature(
                street="Weizmann", city="Ness Ziona", housenumber="1",
                lat=31.9248, lon=34.7774, osm_id=24,
            ),
        )

        assert geocode._meters_from_metro(31.9248, 34.7774) < geocode._meters_from_metro(
            31.8991, 34.7796
        )
        assert results[0]["city"] == "Rehovot"


class TestTextGate:
    """Deciding whether the first response answers the street that was typed.

    "Marsel Yanko 10, Tel Aviv" came back with a house at number 10 that had
    nothing to do with the street. A local building carrying the number used to
    count as resolved, so the correction never ran and the address stayed
    wherever Photon guessed.
    """

    @staticmethod
    def _scenario() -> dict[str, list[dict]]:
        return {
            # Right number, wrong street, one of them local: the response that
            # used to be mistaken for an answer.
            "Marsel Yanko 10, Tel Aviv": [
                photon_feature(
                    street="Yaakov Reifman", city=TEL_AVIV, housenumber="10",
                    lat=32.0700, lon=34.7900, osm_id=31,
                ),
                photon_feature(
                    street="HaAm", city="Be'er Ya'akov", housenumber="10",
                    lat=31.9200, lon=34.7800, osm_id=32,
                ),
            ],
            "Marsel Yanko": [
                photon_feature(
                    name="7 Marcel Yanko St.", street="Marcel Janco", city=TEL_AVIV,
                    osm_value="bicycle_rental", housenumber="7",
                    lat=32.1176, lon=34.8246, osm_id=33,
                ),
            ],
            "Marcel Janco 10 Tel Aviv": [
                photon_feature(
                    street="Marcel Janco", city=TEL_AVIV, osm_value="house",
                    housenumber="10", lat=32.117517, lon=34.824959, osm_id=34,
                ),
            ],
        }

    def test_a_local_building_with_the_right_number_is_not_enough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole bug: `_resolves` said yes, so nothing further was tried."""
        import asyncio

        results, sent = asyncio.run(
            _canned_search(monkeypatch, "Marsel Yanko 10, Tel Aviv", self._scenario())
        )

        assert sent == ["Marsel Yanko 10, Tel Aviv", "Marsel Yanko", "Marcel Janco 10 Tel Aviv"]
        assert results[0]["street"] == "Marcel Janco"
        assert results[0]["housenumber"] == "10"

        # The nearer house is still in the list, just behind the rebuilt one.
        assert [r["street"] for r in results][1] == "Yaakov Reifman"

    def test_the_number_alone_still_resolves_for_the_chain(self) -> None:
        """Only the trigger changed; the chain gate keeps its shape."""
        house = {"type": "house", "housenumber": "10", "out_of_area": False}
        assert geocode._resolves([house], "10", local_only=True) is True

        no_street = [_answer(label="Yaakov Reifman, Tel Aviv", street="Yaakov Reifman", housenumber="10")]
        assert geocode._text_matches(no_street, "Marsel Yanko 10, Tel Aviv") is False

    def test_an_answer_that_already_reads_right_costs_one_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        by_query = {
            "Marcel Janco 10 Tel Aviv": [
                photon_feature(
                    street="Marcel Janco", city=TEL_AVIV, housenumber="10",
                    lat=32.117517, lon=34.824959, osm_id=35,
                )
            ]
        }
        _, sent = asyncio.run(
            _canned_search(monkeypatch, "Marcel Janco 10 Tel Aviv", by_query)
        )

        assert sent == ["Marcel Janco 10 Tel Aviv"]

    def test_every_query_word_has_to_fit_the_same_result(self) -> None:
        """A list of places sharing one word each is not a match."""
        dizengoff = _answer(label="Dizengoff, Holon", street="Dizengoff", city="Holon", housenumber="34")
        herzl = _answer(label="Herzl, Tel Aviv", street="Herzl", city=TEL_AVIV)
        together = _answer(label="Dizengoff, Tel Aviv", street="Dizengoff", city=TEL_AVIV)

        assert geocode._text_matches([dizengoff, herzl], "Dizengoff 34 Tel Aviv") is False
        assert geocode._text_matches([dizengoff, herzl, together], "Dizengoff 34 Tel Aviv") is True

    def test_a_noise_result_does_not_explain_the_query(self) -> None:
        """Those are dropped before anyone sees them, so they cannot answer."""
        station = _answer(
            label="Dizengoff 34, Tel Aviv", street="Dizengoff", city=TEL_AVIV,
            osm_value="bicycle_rental",
        )
        house = _answer(label="Herzl, Holon", street="Herzl", city="Holon", housenumber="34")

        assert geocode._text_matches([station, house], "Dizengoff 34 Tel Aviv") is False

    @pytest.mark.parametrize("query", ["Marsel Yanko 10, Tel Aviv", "Dizengoff 34"])
    def test_latin_queries_are_checked_against_their_answer(self, query: str) -> None:
        assert geocode._query_is_latin(query) is True

    @pytest.mark.parametrize("query", ["מרסל ינקו 10", "תל אביב 10"])
    def test_a_hebrew_query_still_relies_on_the_number(self, query: str) -> None:
        assert geocode._query_is_latin(query) is False

    def test_a_hebrew_query_still_stops_at_a_local_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Photon answers in Latin, so a Hebrew query has nothing to compare."""
        import asyncio

        by_query = {
            "מרסל ינקו 10": [
                photon_feature(
                    street="Yaakov Reifman", city=TEL_AVIV, housenumber="10",
                    lat=32.0700, lon=34.7900, osm_id=36,
                )
            ]
        }
        _, sent = asyncio.run(_canned_search(monkeypatch, "מרסל ינקו 10", by_query))

        assert sent == ["מרסל ינקו 10"]


class TestCorrectedBonus:
    def test_a_rebuilt_answer_outranks_a_nearer_house_with_the_same_number(self) -> None:
        """Without it the bonus lives only in the sort, which is too late to matter.

        Both candidates are houses at number 10 in Tel Aviv, so rank, city and
        distance all agree: the nearer one wins until the rebuilt street is
        marked as corrected.
        """
        near = _answer(
            label="Yaakov Reifman, Tel Aviv", street="Yaakov Reifman",
            housenumber="10", lat=32.0700, lon=34.7900,
        )
        rebuilt = _answer(
            label="Marcel Janco, Tel Aviv", street="Marcel Janco",
            housenumber="10", lat=32.117517, lon=34.824959,
        )
        query = "Marsel Yanko 10, Tel Aviv"

        plain = geocode._rerank([dict(near), dict(rebuilt)], query, 5)
        boosted = geocode._rerank(
            [dict(near), dict(rebuilt, _corrected=True)], query, 5
        )

        assert plain[0]["street"] == "Yaakov Reifman"
        assert boosted[0]["street"] == "Marcel Janco"
        assert [r["street"] for r in boosted] == ["Marcel Janco", "Yaakov Reifman"]
