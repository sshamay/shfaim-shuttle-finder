"""Geocoder interface, plus Photon and Nominatim implementations."""

from __future__ import annotations

import asyncio
import difflib
import math
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from app import config

PHOTON_URL = "https://photon.komoot.io/api/"
SERPAPI_URL = "https://serpapi.com/search"

# Most specific first: a building beats a road beats a district.
PHOTON_TYPE_RANK = {
    "house": 0,
    "building": 1,
    "house_number": 1,
    "place_of_worship": 2,
    "school": 2,
    "university": 2,
    "hospital": 2,
    "mall": 2,
    "station": 3,
    "commercial": 3,
    "office": 3,
    "amenity": 3,
    "shop": 3,
    "attraction": 3,
    "tourism": 3,
    "bus_station": 4,
    "public_transport": 4,
    "primary": 5,
    "secondary": 5,
    "tertiary": 6,
    "residential": 6,
    "unclassified": 6,
    "road": 6,
    "secondary_road": 6,
    # A query naming a town wants the town, not a venue inside it.
    "town": 0,
    "city": 0,
    "village": 0,
    "borough": 1,
    "suburb": 1,
    "neighbourhood": 1,
    "quarter": 2,
    "hamlet": 2,
}

# Types that are never what someone means when asking "how do I get to X".
NOISE_TYPES = {
    "bicycle_rental",
    "bicycle_parking",
    "charging_station",
    "viewpoint",
    "parking",
    "toilets",
    "bench",
    "tree",
    "street_lamp",
    "address",
    "peak",
    "river",
    "stream",
}

# OSM values that mean "someone lives here", i.e. an answer to an address
# query. A shop with a house number is not one: it shares the number with the
# building it sits in but is not what the caller asked for.
ADDRESS_TYPES = {"house", "building", "house_number", "apartments", "residential"}

# Cities named in the query or in OSM's address fields, and their aliases.
CITY_ALIASES = {
    "tel aviv": "tel aviv",
    "tel-aviv": "tel aviv",
    "telaviv": "tel aviv",
    "תל אביב": "tel aviv",
    "תל-אביב": "tel aviv",
    "tel aviv yafo": "tel aviv",
    "ramat gan": "ramat gan",
    "ramat-gan": "ramat gan",
    "רמת גן": "ramat gan",
    "shefayim": "shefayim",
    "שפיים": "shefayim",
}

# This app only knows shuttle stops in the Shefayim/Tel Aviv metro, so a
# candidate far outside that area is nearly always a different place with the
# same name. Used as an ordering hint only, never as a filter: someone in
# Herzliya still gets an answer, just after the plausible ones.
METRO_CENTER = (32.08, 34.81)  # Tel Aviv, roughly
METRO_RADIUS_M = 25_000
DISTANCE_PENALTY_START_M = 30_000
DISTANCE_PENALTY_PER_KM = 2.0
MAX_DISTANCE_PENALTY = 60

# Past this the candidate is somewhere else entirely, not a long walk. Results
# are never dropped, but the caller is told so the UI can warn.
OUT_OF_AREA_WARN_M = 40_000

# A result rebuilt from the street Photon actually knows outweighs whatever the
# misspelt query stumbled into first: it is the only candidate derived from the
# address that was typed rather than from a number it happened to share.
CORRECTED_BONUS = 5

# How alike a query word and an answer word must be to count as a match. Tuned
# against the pairs that decide real queries: "rotchild"/"rotschild" (0.94) and
# "shefayim"/"shefaram" (0.75) pass, while "yanko"/"janco" (0.60) does not.
TOKEN_MATCH_THRESHOLD = 0.7


class GeocodeError(Exception):
    pass


class Geocoder(ABC):
    @abstractmethod
    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        ...


class PhotonGeocoder(Geocoder):
    """Primary. Better query parsing than Nominatim, handles Hebrew and English."""

    name = "photon"

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        results = [self._to_result(f) for f in await self._fetch(query, limit)]
        corrected = await self._correct_address(query, results, limit)
        if corrected:
            for item in corrected:
                item["_corrected"] = True
            # Corrected first so that a node found by both probes keeps the
            # bonus rather than silently reverting to its unboosted form.
            results = _dedupe(corrected + results)
        return _rerank(results, query, limit)

    async def _fetch(self, query: str, limit: int) -> list[dict[str, Any]]:
        # lang=en, not default: it keeps the query language instead of returning
        # Hebrew-only names, and drops same-name POIs in other towns.
        # lat/lon bias: without it "Bialik 5 Tel Aviv" ranked Ramat Gan streets
        # above the Tel Aviv address; Photon re-ranks around the point given.
        params = {
            "q": query,
            "limit": limit,
            "lang": "en",
            "lat": METRO_CENTER[0],
            "lon": METRO_CENTER[1],
        }
        async with httpx.AsyncClient(timeout=15, headers=_headers()) as c:
            r = await c.get(PHOTON_URL, params=params)
            if r.status_code == 429:
                raise GeocodeError("Photon rate limit hit, try again in a moment")
            r.raise_for_status()
            return r.json().get("features", [])

    async def _correct_address(
        self, query: str, results: list[dict[str, Any]], limit: int
    ) -> list[dict[str, Any]]:
        """Recover an address Photon only half knows.

        A misspelt street plus a house number ("Marsel yanko 10") returns only
        unrelated POIs, while the same query without the number comes back from
        something sitting on the right street and therefore carries its
        canonical spelling. Re-asking with that spelling and the original
        number turns it into the address that was actually typed.

        The probe only fires when nothing in the first response accounts for the
        words that were typed, so well-formed queries still cost one request.
        A Hebrew query cannot be read back at all, so it keeps the older test:
        does a nearby building carry the number. A failure part way through
        keeps whatever we already have.
        """
        parsed = _parse_address(query)
        if parsed is None:
            return []
        number, head, tail = parsed

        if _query_is_latin(query):
            settled = _text_matches(results, query)
        else:
            settled = _resolves(results, number, local_only=True)
        if settled:
            return []

        try:
            street = await self._canonical_street(head, limit)
            if not street:
                return []
            fixed = " ".join(p for p in (street, number, tail) if p)
            return [self._to_result(f) for f in await self._fetch(fixed, limit)]
        except (GeocodeError, httpx.HTTPError):
            return []

    async def _canonical_street(self, head: str, limit: int) -> str:
        """The street name Photon would have used had the query spelled it right."""
        candidates = [self._to_result(f) for f in await self._fetch(head, limit)]
        for pool in (
            [c for c in candidates if not c["out_of_area"] and c["type"] not in NOISE_TYPES],
            [c for c in candidates if not c["out_of_area"]],
            candidates,
        ):
            for candidate in pool:
                if candidate["street"]:
                    return candidate["street"]
        return ""

    @staticmethod
    def _to_result(feature: dict[str, Any]) -> dict[str, Any]:
        coords = feature["geometry"]["coordinates"]
        props = feature.get("properties", {})
        osm_value = props.get("osm_value") or ""
        name = props.get("name") or props.get("street") or ""
        city = props.get("city") or props.get("county") or props.get("district") or ""
        # A bare street name hides that the house number was found: street-only
        # "Katznelson" and "Katznelson 125" are different answers. name also
        # falls back to street, so the two combine into "Katznelson 125".
        housenumber = props.get("housenumber") or ""
        heading = f"{name} {housenumber}".strip() if name and housenumber else name
        parts = [p for p in (heading, city, props.get("state"), props.get("country")) if p]
        return {
            "label": ", ".join(dict.fromkeys(parts)) or props.get("name", "unknown"),
            "street": props.get("street") or "",
            "housenumber": props.get("housenumber") or "",
            "city": city,
            "state": props.get("state") or "",
            "lat": float(coords[1]),
            "lon": float(coords[0]),
            "out_of_area": _meters_from_metro(
                float(coords[1]), float(coords[0])
            )
            > OUT_OF_AREA_WARN_M,
            "type": osm_value or props.get("type") or "",
            "osm_type": props.get("osm_type"),
            "osm_id": props.get("osm_id"),
            "osm_key": props.get("osm_key"),
            "_rank": PHOTON_TYPE_RANK.get(osm_value, 7),
        }


class NominatimGeocoder(Geocoder):
    """Fallback. Handles English street+number well, weak on POIs."""

    name = "nominatim"

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        params = {
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": limit,
            "countrycodes": "il",
        }
        async with httpx.AsyncClient(timeout=15, headers=_headers()) as c:
            r = await c.get(config.NOMINATIM_URL, params=params)
            if r.status_code == 429:
                raise GeocodeError("Nominatim rate limit hit, try again in a moment")
            r.raise_for_status()
            data = r.json()

        if not data:
            raise GeocodeError(f"no match for {query!r}")

        # A malformed upstream payload must not kill the whole chain; let the
        # search fall through to the next geocoder instead.
        if not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
            raise GeocodeError(f"nominatim returned an unexpected payload for {query!r}")

        return [
            {
                "label": d.get("display_name", query),
                "street": (d.get("address") or {}).get("road", ""),
                "housenumber": (d.get("address") or {}).get("house_number", ""),
                "city": (d.get("address") or {}).get("city", ""),
"lat": float(d["lat"]),
            "lon": float(d["lon"]),
            "out_of_area": _meters_from_metro(
                float(d["lat"]), float(d["lon"])
            )
            > OUT_OF_AREA_WARN_M,
            "type": d.get("type", ""),
                "osm_type": d.get("osm_type"),
                "osm_id": d.get("osm_id"),
                "osm_key": None,
            }
            for d in data
        ]


class SerpApiGeocoder(Geocoder):
    """Third fallback. Real Google Maps data, needs SERPAPI_API_KEY.

    A business/POI search rather than an address geocoder, so plain street
    addresses may return nothing. Fires only when photon and nominatim are
    both empty, which keeps it well inside the free monthly quota.
    """

    name = "serpapi"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise GeocodeError("SERPAPI_API_KEY is not set")
        self.api_key = api_key

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        params = {
            "engine": "google_maps",
            "q": query,
            "api_key": self.api_key,
            "hl": "en",
            "gl": "il",
        }
        async with httpx.AsyncClient(timeout=25, headers=_headers()) as c:
            r = await c.get(SERPAPI_URL, params=params)
            if r.status_code == 429:
                raise GeocodeError("SerpAPI quota exhausted for this month")
            if r.status_code == 401:
                raise GeocodeError("SerpAPI rejected the API key")
            r.raise_for_status()
            data = r.json()

        if data.get("error"):
            raise GeocodeError(f"serpapi: {data['error']}")

        # SerpAPI's google_maps engine has changed shape twice: today a search
        # returns one consolidated {"place_results": {title, address,
        # gps_coordinates, type}} object, while older responses carried an
        # array of local_results. Accept both rather than pinning the parse to
        # a format that drifted away from under the test suite.
        featured = data.get("place_results")
        gps = featured.get("gps_coordinates") if isinstance(featured, dict) else None
        results = (
            [self._as_result(featured, query)]
            if gps and gps.get("latitude") is not None
            else []
        )
        if not results:
            for item in (data.get("local_results") or [])[:limit]:
                item_gps = item.get("gps_coordinates") or {}
                if "latitude" not in item_gps:
                    continue
                results.append(self._as_result(item, query))

        if not results:
            raise GeocodeError(f"no match for {query!r}")
        return results

    @staticmethod
    def _as_result(item: dict[str, Any], query: str) -> dict[str, Any]:
        gps = item.get("gps_coordinates") or {}
        types = item.get("type") or item.get("type_ids") or []
        if isinstance(types, list):
            types = types[0] if types else ""
        lat = float(gps["latitude"])
        lon = float(gps["longitude"])
        return {
            "label": item.get("title") or query,
            "street": item.get("address") or "",
            "housenumber": "",
            "city": "",
            "lat": lat,
            "lon": lon,
            "out_of_area": _meters_from_metro(lat, lon) > OUT_OF_AREA_WARN_M,
            "type": types,
            "osm_type": None,
            "osm_id": None,
            "osm_key": None,
            "place_id": item.get("place_id"),
        }


class GoogleGeocoder(Geocoder):
    """Optional, needs GOOGLE_MAPS_API_KEY in the environment."""

    name = "google"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise GeocodeError("GOOGLE_MAPS_API_KEY is not set")
        self.api_key = api_key

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        import urllib.parse

        url = f"https://maps.googleapis.com/maps/api/geocode/json?{urllib.parse.urlencode({
            'address': query, 'language': 'en', 'key': self.api_key,
        })}"
        async with httpx.AsyncClient(timeout=15, headers=_headers()) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()

        if data.get("status") != "OK":
            raise GeocodeError(f"google returned {data.get('status')}")

        results = []
        for item in data.get("results", [])[:limit]:
            loc = item["geometry"]["location"]
            results.append(
                {
                    "label": item.get("formatted_address", query),
                    "street": "",
                    "housenumber": "",
                    "city": "",
                    "lat": float(loc["lat"]),
                    "lon": float(loc["lng"]),
                    "out_of_area": _meters_from_metro(
                        float(loc["lat"]), float(loc["lng"])
                    )
                    > OUT_OF_AREA_WARN_M,
                    "type": item["types"][0] if item.get("types") else "",
                    "osm_type": None,
                    "osm_id": None,
                    "osm_key": None,
                    "place_id": item.get("place_id"),
                }
            )
        if not results:
            raise GeocodeError(f"no match for {query!r}")
        return results


def _headers() -> dict[str, str]:
    return {"User-Agent": config.USER_AGENT, "Accept-Language": "en,he"}


def _wanted_cities(query: str) -> set[str]:
    """Cities named in the query, normalised via CITY_ALIASES."""
    lowered = query.lower()
    return {canon for alias, canon in CITY_ALIASES.items() if alias in lowered}


def _city_of(item: dict[str, Any]) -> set[str]:
    """Cities this candidate belongs to, normalised.

    Only the city field is consulted. The state field is the district
    ("מחוז תל אביב"), which contains "תל אביב" for Bnei Brak and other
    neighbouring towns, so matching on it produces false positives.
    """
    found = set()
    value = (item.get("city") or "").lower()
    for alias, canon in CITY_ALIASES.items():
        if alias in value:
            found.add(canon)
    return found


_HOUSE_NUMBER = re.compile(r"^\d+[א-ת]?$")


def _parse_address(query: str) -> tuple[str, str, str] | None:
    """Split "street number tail" into (number, street words, trailing words).

    Only queries with a standalone number after at least one word count as an
    address, so "Azrieli Center" and "10", the bus line, are left alone.
    Commas are treated as spacing: "Marcel Janco 10, Tel Aviv" is one address
    with a tail, not two queries.
    """
    tokens = query.replace(",", " ").split()
    for i, token in enumerate(tokens):
        if not _HOUSE_NUMBER.match(token):
            continue
        head = " ".join(tokens[:i])
        if not head:
            return None
        return token, head, " ".join(tokens[i + 1 :])
    return None


_WORD_SPLIT = re.compile(r"[^0-9a-z\u0590-\u05ff]+")
_LATIN_LETTER = re.compile(r"[a-zA-Z]")
_HEBREW_LETTER = re.compile(r"[\u0590-\u05ff]")


def _words(text: str | None) -> list[str]:
    """Lowercased alphanumeric words, digits excluded."""
    return [w for w in _WORD_SPLIT.split((text or "").lower()) if w and not w.isdigit()]


def _similar(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= TOKEN_MATCH_THRESHOLD


def _query_is_latin(query: str) -> bool:
    """Can this query be checked against its own answer?

    Photon transliterates street names to Latin even for a Hebrew query, so a
    Hebrew query can never be compared with the streets it comes back with and
    has to rely on the shape of the result instead.
    """
    return bool(_LATIN_LETTER.search(query)) and not _HEBREW_LETTER.search(query)


def _text_matches(results: list[dict[str, Any]], query: str) -> bool:
    """Does one single result account for every word of the query?

    Every word has to fit the same candidate. "Marsel yanko" needs a street
    that reads as both words, which a list of places each sharing one of them
    is not; that distinction is what tells a misspelt street from a correct
    answer, and it has to ignore noise results because those are dropped
    before the caller ever sees them.
    """
    tokens = _words(query)
    if not tokens:
        return True
    for item in results:
        if item.get("type") in NOISE_TYPES:
            continue
        haystack = set(_words(item.get("street")) + _words(item.get("label")))
        if haystack and all(any(_similar(t, h) for h in haystack) for t in tokens):
            return True
    return False


def _resolves(
    results: list[dict[str, Any]], number: str, *, local_only: bool = False
) -> bool:
    """Did anything come back as a building carrying the house number asked for?

    This is the only signal that works across scripts: Photon reports street
    names in Latin even for a Hebrew query, so the query text can never be
    compared against the answer directly.

    local_only rejects the same-number buildings Photon finds overseas, which
    is what a misspelt street turns up: "Marsel yanko 10" matched a house in
    Sofia as readily as the one in Tel Aviv. The chain itself is happy with
    any building, so a search for a real address outside the metro still stops
    at the first geocoder that found it.
    """
    wanted = re.sub(r"\D", "", number)
    if not wanted:
        return False
    for item in results:
        if item.get("type") not in ADDRESS_TYPES:
            continue
        if local_only and item.get("out_of_area"):
            continue
        if re.sub(r"\D", "", item.get("housenumber") or "") == wanted:
            return True
    return False


def _dedupe(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeated OSM nodes, keeping the first sighting."""
    seen: set[tuple[Any, Any]] = set()
    out = []
    for item in results:
        key = (item.get("osm_type"), item.get("osm_id"))
        if key != (None, None):
            if key in seen:
                continue
            seen.add(key)
        out.append(item)
    return out


def _meters_from_metro(lat: float, lon: float) -> float:
    radius = 6_371_000.0
    mlat, mlon = METRO_CENTER
    p1, p2 = math.radians(mlat), math.radians(lat)
    dphi = p2 - p1
    dlambda = math.radians(lon - mlon)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _rerank(
    results: list[dict[str, Any]], query: str, limit: int
) -> list[dict[str, Any]]:
    """Sort by how well a candidate matches the query, not just by OSM type.

    A result in a city the user did not name is almost always the wrong one,
    so a city mismatch outweighs any type advantage.
    """
    wanted = _wanted_cities(query)
    lowered_query = query.lower()
    kept = []

    for item in results:
        if item.get("type") in NOISE_TYPES:
            continue
        rank = item.get("_rank", 7)

        if wanted:
            cities = _city_of(item)
            if wanted & cities:
                rank = max(0, rank - 3)  # right city
            elif cities:
                rank += 40  # named a city this candidate is not in
            else:
                rank += 20  # city unknown, cannot confirm

        if item.get("housenumber") and item.get("type") in ("house", "building"):
            rank -= 2

        if item.pop("_corrected", False):
            rank -= CORRECTED_BONUS

        # Same-name places in other towns can beat a genuine local match on OSM
        # type alone, so push clearly out-of-area candidates down the list.
        # Ranking only: nothing is dropped, so distant addresses still resolve.
        distance = _meters_from_metro(item["lat"], item["lon"])
        excess_km = (distance - DISTANCE_PENALTY_START_M) / 1000.0
        if excess_km > 0:
            rank += int(min(excess_km * DISTANCE_PENALTY_PER_KM, MAX_DISTANCE_PENALTY))

        # A candidate in the town the caller actually typed wins a tie before
        # distance is consulted. Without it "Weizmann 1 Rehovot" hands back
        # Ness Ziona purely because that street happens to be nearer the centre.
        city = (item.get("city") or "").lower()
        unnamed = not city or city not in lowered_query

        kept.append((rank, int(unnamed), distance, item))

    # Ties go to whichever candidate sits nearest the metro the shuttle serves.
    # Latitude used to break them, which is arbitrary, and it let an equally
    # good match in Modi'in outrank the one in Tel Aviv on a lower coordinate.
    kept.sort(
        key=lambda entry: (
            entry[0],
            entry[1],
            entry[2],
            entry[3]["lat"],
            entry[3]["lon"],
        )
    )

    # Move the out-of-area tail behind every local match, so the closest plausible
    # candidate is never pushed off the page by same-name places elsewhere. Each
    # group keeps its own rank order. When nothing is local the full ranked list
    # stands as-is, otherwise every entry would be duplicated.
    in_area = [item for _, _, _, item in kept if not item.get("out_of_area")]
    far = [item for _, _, _, item in kept if item.get("out_of_area")]
    ordered = in_area + far if in_area else [item for _, _, _, item in kept]

    out = []
    # Photon reports the building and each addressable POI inside it as
    # separate nodes, so "Katznelson 125" can arrive twice with identical
    # labels a few metres apart. One row is the answer; two look like noise.
    seen_labels: set[str] = set()
    for item in ordered[:limit]:
        item.pop("_rank", None)
        label = item.get("label") or ""
        if label in seen_labels:
            continue
        seen_labels.add(label)
        out.append(item)

    if not out:
        raise GeocodeError(f"no match for {query!r}")
    return out


_REGISTRY: dict[str, type[Geocoder]] = {
    "photon": PhotonGeocoder,
    "nominatim": NominatimGeocoder,
}

# Order the fallbacks are tried in when the primary finds nothing.
_FALLBACK_ORDER = ["nominatim", "serpapi"]


def _load_dotenv() -> None:
    """Read .env into os.environ without overriding real env vars."""
    env_path = Path(config.BASE_DIR) / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value and key not in os.environ:
            os.environ[key] = value


def get_geocoder(which: str | None = None) -> Geocoder:
    """GEOCODER env var selects the backend; google and serpapi need keys."""
    _load_dotenv()
    which = which or os.getenv("GEOCODER", "photon").lower()
    if which == "google":
        return GoogleGeocoder(os.getenv("GOOGLE_MAPS_API_KEY", ""))
    if which == "serpapi":
        return SerpApiGeocoder(os.getenv("SERPAPI_API_KEY", ""))
    if which not in _REGISTRY:
        raise GeocodeError(
            f"unknown geocoder {which!r}; use photon, nominatim, serpapi or google"
        )
    return _REGISTRY[which]()


def _answers(query: str, results: list[dict[str, Any]]) -> bool:
    """Is this good enough to stop the chain?

    A query with no house number is satisfied by whatever came back. One that
    names a number expects a building carrying that number; until then the
    next geocoder gets its turn. Without this, the first non-empty response
    ends the search and the fallbacks below are dead code.
    """
    parsed = _parse_address(query)
    return parsed is None or _resolves(results, parsed[0])


async def search(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Try each geocoder in turn until one gives a usable answer.

    A fallback that cannot even be constructed (e.g. SerpAPI with no
    SERPAPI_API_KEY) is skipped with an explanatory note rather than being
    fatal, so a missing optional key never stops the primary from being tried.
    """
    primary = get_geocoder()
    order = [primary]
    errors: list[str] = []
    for name in _FALLBACK_ORDER:
        if name == primary.name:
            continue
        try:
            order.append(get_geocoder(name))
        except GeocodeError as exc:
            errors.append(f"{name}: {exc}")

    best = None
    for geocoder in order:
        try:
            results = await geocoder.search(query, limit=limit)
        except GeocodeError as exc:
            errors.append(f"{geocoder.name}: {exc}")
        except httpx.HTTPError as exc:
            errors.append(f"{geocoder.name}: {exc}")
        else:
            if _answers(query, results):
                return results
            # Remembered rather than returned: a later geocoder that also
            # misses the house number must never leave the caller empty-handed
            # when the first one at least found something plausible.
            if best is None:
                best = results

    if best is not None:
        return best
    raise GeocodeError("; ".join(errors) or "geocoding failed")


def search_sync(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    return asyncio.run(search(query, limit=limit))