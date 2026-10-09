"""Stage 3: rank shuttle stops by real walking distance from the user's address."""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, geocode  # noqa: E402

WALK_SPEED_KMH = 4.8
WALK_M_PER_MIN = WALK_SPEED_KMH * 1000 / 60

# Applied to the straight-line fallback, since a street grid forces detours and
# straight-line distance otherwise understates how far someone must walk.
ESTIMATE_DETOUR_FACTOR = 1.2

# Stops farther away than this are dropped from every result set: the site is a
# "nearest stop" tool, and a 45-minute walk is not a candidate the UI should
# list. Checked on the raw minutes so a boundary of 30.5 min does not sneak in
# through a round() rule; the ranking decision uses raw minutes too.
MAX_WALK_MIN = 30.0

# The longest straight-line hop that can still fit inside the walk cutoff: a
# real path is never shorter than the crow-flies distance, so a stop beyond
# this can be discarded without asking any router. This is what keeps a sweep
# of the whole dataset down to the few stops an origin could actually reach.
ROUTE_PREFILTER_M = MAX_WALK_MIN * WALK_M_PER_MIN

# A community server that starts answering 403 "retry later" is left alone
# until this cooldown passes: hammering it only extends the block, and a
# straight-line estimate serves the stop better in the meantime.
BROUTER_COOLDOWN_S = 60.0
_BROUTER_DOWN_UNTIL = 0.0


def _sign_number_shift(stops: list[dict[str, Any]]) -> int:
    """How far 20fl's ``index`` runs ahead of the number painted on the stop sign.

    The feed counts the Shefayim park and ride as stop 1 on every line, but the
    signs only number the in-town stops, so every sign is one lower than the feed
    (e.g. Menahem Begin Road/HaMelacha is index 4 on 813 but signed 3).

    Only subtracts when stop 1 really is the park and ride. A line that starts in
    town already numbers its first stop 1, and shifting it would invent an
    off-by-one that is not there.
    """
    first = next((s for s in stops if s.get("index") == 1), None)
    return 1 if first is not None and first.get("is_park_and_ride") else 0


def load_stops() -> list[dict[str, Any]]:
    """Every unique stop across every Shefayim line, tagged with its lines.

    Each stop also carries ``stop_numbers``: the position of that stop along each
    line serving it, keyed by line. It has to be per line rather than a single
    value, because the same physical stop is not always in the same position on
    every line it serves -- the Shefayim park and ride is the last stop on 811 but
    only the sixth on 814. The numbers agree across a line's two directions, so
    one number per line is well defined.

    Numbers are the ones painted on the stop signs, not the feed's raw ``index``;
    see ``_sign_number_shift``. The park and ride itself is unnumbered, since it is
    where the shuttle is boarded rather than one of the numbered stops.
    """
    if not config.STOPS_CACHE.exists():
        raise FileNotFoundError(
            f"{config.STOPS_CACHE} not found, run scripts/fetch_lines.py first"
        )
    payload = json.loads(config.STOPS_CACHE.read_text())

    merged: dict[tuple, dict[str, Any]] = {}
    for line in payload["lines"]:
        shift = _sign_number_shift(line["stops"])
        for stop in line["stops"]:
            key = (stop["name"], round(stop["lat"], 5), round(stop["lon"], 5))
            entry = merged.get(key)
            if entry is None:
                entry = {**stop, "lines": [], "stop_numbers": {}}
                merged[key] = entry
            if line["line"] not in entry["lines"]:
                entry["lines"].append(line["line"])
            number = stop.get("index")
            if number is not None:
                number = int(number)
                if shift:
                    if number == 1:
                        continue
                    number -= shift
                entry["stop_numbers"][line["line"]] = number

    for stop in merged.values():
        stop["ride_min"] = stop.get("ride_min")
        stop["return_min"] = stop.get("return_min")
        for line in stop["lines"]:
            if "ride_min" in stop:
                continue
        # compute from dist if present? not stored; skip

    return list(merged.values())


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


_ROUTER_CACHE: str | None = None
_ROUTER_CHECKED_AT = 0.0

# Negative results expire quickly so a routing container that comes back is
# picked up without a restart. Positive results are held longer, since a
# working local server stays working.
ROUTER_OK_TTL_S = 300.0
ROUTER_FAIL_TTL_S = 20.0


def _valhalla_headers() -> dict[str, str]:
    """Identify this client to the FOSSGIS public demo, as its README asks.

    Their note in valhalla's README: apps hitting the public demo server
    should send an identifying X-Client-Id header. The local container in
    dev gets the same headers - harmless, and one code path instead of two.
    """
    return {"User-Agent": config.USER_AGENT, "X-Client-Id": config.CLIENT_ID}


# Every backend is probed with a real, minimal directions request over this
# short walk in central Tel Aviv - cheap in both time and quota. A status
# endpoint alone proves nothing for Valhalla: valhalla1 answers GET /status
# from Oregon while POST /route is refused, and picking it on that answer
# alone pins every stop to straight-line estimates for the whole cache TTL.
_PROBE_WALK = (34.7880, 32.0710, 34.7890, 32.0720)  # lon1, lat1, lon2, lat2


def _valhalla_probe_payload() -> dict[str, Any]:
    """The tiniest payload that makes Valhalla do real routing work."""
    lon1, lat1, lon2, lat2 = _PROBE_WALK
    return {
        "locations": [{"lat": lat1, "lon": lon1}, {"lat": lat2, "lon": lon2}],
        "costing": "pedestrian",
    }


def _valhalla_has_route(body: object) -> bool:
    """A 200 must carry a trip: an interposed portal or proxy page would
    otherwise count as healthy, win the pick, and then fail every real
    routing call for the whole cache TTL."""
    return isinstance(body, dict) and "trip" in body


def _brouter_has_route(body: object) -> bool:
    """The BRouter probe body must be GeoJSON with an actual path in it."""
    if not isinstance(body, dict):
        return False
    features = body.get("features")
    if not isinstance(features, list) or not features:
        return False
    geometry = (features[0] or {}).get("geometry") or {}
    return bool(geometry.get("coordinates"))


def _ors_directions_url(
    base: str, lon1: float, lat1: float, lon2: float, lat2: float
) -> str:
    """The simple GET the current ORS API documents: start/end query params.

    The older ``/{lon},{lat};{lon},{lat}`` path form answers 405 now - the
    deploy diagnostic caught exactly that on Render, where no-key probes from
    a laptop never get past the 401 auth gate to see it.
    """
    return f"{base}?start={lon1},{lat1}&end={lon2},{lat2}"


def _brouter_directions_url(
    base: str, lon1: float, lat1: float, lon2: float, lat2: float
) -> str:
    """BRouter's documented GET: pipe-separated lonlat pairs, GeoJSON out.

    ``trekking`` is the profile this public server has installed for
    non-motorised travel (``hiking`` and ``fast`` answer 500), and the app
    converts the returned metres to minutes itself, so the profile only has
    to keep the route on walkable ways.
    """
    return (
        f"{base}?lonlats={lon1},{lat1}|{lon2},{lat2}"
        "&profile=trekking&alternativeidx=0&format=geojson"
    )


def _ors_answers(base: str) -> bool:
    """Probe OpenRouteService with a directions call, which also validates the key."""
    lon1, lat1, lon2, lat2 = _PROBE_WALK
    try:
        httpx.get(
            _ors_directions_url(base, lon1, lat1, lon2, lat2),
            headers={"Authorization": config.ors_api_key()},
            timeout=config.VALHALLA_PROBE_TIMEOUT_S,
        ).raise_for_status()
        return True
    except Exception:  # noqa: BLE001,S110
        return False


def _brouter_cooling_down() -> bool:
    """True while a rate limit is still being respected."""
    return time.monotonic() < _BROUTER_DOWN_UNTIL


def _brouter_note_throttled(retry_after: str | None = None) -> None:
    """Remember a rate limit so the next stops skip BRouter instead of feeding it.

    ``Retry-After`` is honoured when it is a number of seconds; the default
    cooldown covers servers that only answer "Please, retry later!".
    """
    global _BROUTER_DOWN_UNTIL
    delay = BROUTER_COOLDOWN_S
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            pass
    _BROUTER_DOWN_UNTIL = max(_BROUTER_DOWN_UNTIL, time.monotonic() + delay)


def _brouter_outcome(base: str) -> dict[str, Any]:
    """Probe BRouter with a real tiny walk over GET, demanding GeoJSON back.

    Last in the pick order, so this community server only ever sees probe
    traffic once every other backend has already failed. While it is cooling
    down after a rate limit the probe is skipped entirely - there is no point
    spending quota to learn what the last answer already said.
    """
    lon1, lat1, lon2, lat2 = _PROBE_WALK
    probe_url = _brouter_directions_url(base, lon1, lat1, lon2, lat2)
    if _brouter_cooling_down():
        return {
            "kind": "brouter",
            "route_url": base,
            "probe_url": probe_url,
            "ok": False,
            "http_status": 403,
            "latency_ms": 0,
            "skipped": True,
            "error": "cooling down after a rate limit",
        }
    outcome = _probe_outcome(
        "brouter",
        base,
        probe_url,
        headers={"User-Agent": config.USER_AGENT},
        body_check=_brouter_has_route,
    )
    if outcome["http_status"] in (403, 429):
        _brouter_note_throttled()
    return outcome


def _probe_outcome(
    kind: str,
    route_url: str,
    probe_url: str,
    headers: dict[str, str] | None = None,
    post_json: dict[str, Any] | None = None,
    body_check: Any = None,
) -> dict[str, Any]:
    """One live probe with its latency and failure reason.

    ``_pick_router`` and the ``/api/routing-status`` diagnostic both call
    this, so the reported verdict and the router actually used can never
    drift apart. With ``post_json`` the probe is a real route POST -
    Valhalla's answer to that, not its status page, is what being pickable
    means - and ``body_check`` additionally parses a 200's body, since a
    captive portal answering 200 with HTML is not a working router.
    """
    started = time.monotonic()
    try:
        kwargs: dict[str, Any] = {"timeout": config.VALHALLA_PROBE_TIMEOUT_S}
        if headers:
            kwargs["headers"] = headers
        if post_json is not None:
            response = httpx.post(probe_url, json=post_json, **kwargs)
        else:
            response = httpx.get(probe_url, **kwargs)
        latency_ms = round((time.monotonic() - started) * 1000)
        ok = 200 <= response.status_code < 300
        if ok and body_check is not None:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                ok = False
            else:
                ok = bool(body_check(body))
        return {
            "kind": kind,
            "route_url": route_url,
            "probe_url": probe_url,
            "ok": ok,
            "http_status": response.status_code,
            "latency_ms": latency_ms,
            "skipped": False,
            # The error body never echoes the Authorization header, so a
            # 401/403 from ORS is safe to show while the key stays hidden.
            "error": None if ok else (getattr(response, "text", "") or "")[:200],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "kind": kind,
            "route_url": route_url,
            "probe_url": probe_url,
            "ok": False,
            "http_status": None,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "skipped": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def routing_status() -> dict[str, Any]:
    """Live health report over every routing backend, for debugging deploys.

    Probes each candidate exactly the way ``_pick_router`` would, but without
    touching the shared verdict cache, and reports which backend would win.
    The API key itself is never included - only whether one is configured -
    so the endpoint is safe to leave public.
    """
    key = config.ors_api_key()
    candidates = [
        _probe_outcome(
            "valhalla",
            base,
            base,
            headers=_valhalla_headers(),
            post_json=_valhalla_probe_payload(),
            body_check=_valhalla_has_route,
        )
        for base in config.VALHALLA_URLS
    ]
    lon1, lat1, lon2, lat2 = _PROBE_WALK
    ors_probe = _ors_directions_url(
        config.OPENROUTESERVICE_URL, lon1, lat1, lon2, lat2
    )
    if key:
        candidates.append(
            _probe_outcome(
                "ors",
                config.OPENROUTESERVICE_URL,
                ors_probe,
                headers={"Authorization": key},
            )
        )
    else:
        candidates.append(
            {
                "kind": "ors",
                "route_url": config.OPENROUTESERVICE_URL,
                "probe_url": ors_probe,
                "ok": False,
                "http_status": None,
                "latency_ms": 0,
                "skipped": True,
                "error": "OPENROUTESERVICE_API_KEY is not set",
            }
        )
    candidates.append(_brouter_outcome(config.BROUTER_URL))

    return {
        "key_configured": bool(key),
        "key_length": len(key),
        # Same order as _pick_router: first Valhalla host that answers, else
        # ORS when its key is set and it answers, else BRouter.
        "would_pick": next((c["route_url"] for c in candidates if c["ok"]), None),
        "cached": {
            "router": _ROUTER_CACHE,
            "checked_age_s": round(time.monotonic() - _ROUTER_CHECKED_AT, 1)
            if _ROUTER_CHECKED_AT
            else None,
            "ttl_s": ROUTER_OK_TTL_S if _ROUTER_CACHE else ROUTER_FAIL_TTL_S,
        },
        "candidates": candidates,
    }


def _pick_router() -> str | None:
    """First routing backend that answers, cached with a TTL.

    Candidates run in order: each Valhalla host in ``config.VALHALLA_URLS``
    (probed with a tiny real ``POST /route`` - its ``/status`` answers while
    routes are refused, which would pin every stop to estimates), then
    OpenRouteService when an ``OPENROUTESERVICE_API_KEY`` is configured
    (probed with a minimal directions request), then the keyless BRouter
    community server (probed with the same tiny walk, GeoJSON body
    required). The first that answers is used for the whole request.

    Probing costs a few seconds and was previously repeated for every stop,
    which turned a routing outage into a multi-minute request. Caching the
    failure forever, though, means a container that is restarted stays
    invisible until the server is restarted too.
    """
    global _ROUTER_CACHE, _ROUTER_CHECKED_AT

    now = time.monotonic()
    ttl = ROUTER_OK_TTL_S if _ROUTER_CACHE else ROUTER_FAIL_TTL_S
    if _ROUTER_CHECKED_AT and now - _ROUTER_CHECKED_AT < ttl:
        return _ROUTER_CACHE

    found: str | None = None
    for base in config.VALHALLA_URLS:
        outcome = _probe_outcome(
            "valhalla",
            base,
            base,
            headers=_valhalla_headers(),
            post_json=_valhalla_probe_payload(),
            body_check=_valhalla_has_route,
        )
        if outcome["ok"]:
            found = base
            break
    if found is None and config.ors_api_key():
        if _ors_answers(config.OPENROUTESERVICE_URL):
            found = config.OPENROUTESERVICE_URL
    if found is None and _brouter_outcome(config.BROUTER_URL)["ok"]:
        found = config.BROUTER_URL

    _ROUTER_CACHE = found
    _ROUTER_CHECKED_AT = now
    return found


def _backend(base: str) -> str:
    """Which wire protocol the picked URL speaks.

    Valhalla POSTs a JSON costings payload and reports kilometres; ORS and
    BRouter GET query params and answer GeoJSON with metres. The picked
    backend's URL tells the caller which shape to speak, so tests can stub
    a router with a bare string and the routing layer still dispatches
    correctly.
    """
    if base.startswith(config.OPENROUTESERVICE_URL):
        return "ors"
    if base.startswith(config.BROUTER_URL):
        return "brouter"
    return "valhalla"


def _decode_polyline6(shape: str) -> list[list[float]]:
    """Google's encoded polyline at 1e-6 precision, into [lon, lat] pairs.

    Valhalla answers routes with this encoding under ``trip.legs[].shape``.
    Points are rounded to 5 decimals (~1 m) - finer precision is noise on
    the wire, and it is what the ORS geometry already uses.
    """
    coords: list[list[float]] = []
    index = lat = lon = 0
    while index < len(shape):
        for axis in range(2):
            result = shift = 0
            while True:
                if index >= len(shape):
                    raise ValueError("truncated polyline")
                char = ord(shape[index]) - 63
                index += 1
                result |= (char & 0x1F) << shift
                shift += 5
                if char < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if axis == 0:
                lat += delta
            else:
                lon += delta
        coords.append([round(lon / 1e6, 5), round(lat / 1e6, 5)])
    return coords


async def _valhalla_walk_m(
    client: httpx.AsyncClient,
    router: str,
    origin: tuple[float, float],
    stop: dict[str, Any],
) -> tuple[float, list[list[float]] | None]:
    """Pedestrian metres plus path geometry from a Valhalla POST /route.

    Kilometres on the wire; the drawable path comes back as an encoded
    polyline6 per leg, decoded here into the same [lon, lat] shape ORS
    sends. A shape that will not decode costs only the line, never the
    distance - the walk time is still exact.
    """
    payload = {
        "locations": [
            {"lat": origin[0], "lon": origin[1]},
            {"lat": stop["lat"], "lon": stop["lon"]},
        ],
        "costing": "pedestrian",
    }
    r = await client.post(router, json=payload, timeout=25, headers=_valhalla_headers())
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise ValueError("valhalla reported an error")
    meters = data["trip"]["summary"]["length"] * 1000.0
    if not meters:
        raise ValueError("valhalla returned a zero-length route")

    points: list[list[float]] = []
    try:
        for leg in data["trip"].get("legs") or []:
            shape = leg.get("shape")
            if shape:
                points.extend(_decode_polyline6(shape))
    except (ValueError, IndexError, TypeError, AttributeError):
        points = []
    deduped = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
    geometry = deduped if len(deduped) >= 2 else None
    return float(meters), geometry


async def _ors_walk_m(
    client: httpx.AsyncClient,
    origin: tuple[float, float],
    stop: dict[str, Any],
) -> tuple[float, list[list[float]] | None]:
    """Pedestrian metres plus path geometry from an OpenRouteService request.

    The documented simple GET answers GeoJSON: the metre count sits under
    features[0].properties.summary.distance and the drawn path under
    features[0].geometry.coordinates as [lon, lat] pairs. The geometry is
    rounded to 5 decimals (~1 m) - finer precision is noise on the wire.
    """
    lon1, lat1 = origin[1], origin[0]
    lon2, lat2 = stop["lon"], stop["lat"]
    r = await client.get(
        _ors_directions_url(config.OPENROUTESERVICE_URL, lon1, lat1, lon2, lat2),
        headers={"Authorization": config.ors_api_key()},
        timeout=25,
    )
    r.raise_for_status()
    data = r.json()
    features = data.get("features") or []
    meters = features[0]["properties"]["summary"]["distance"]
    if not meters:
        raise ValueError("ors returned a zero-length route")
    coords = (features[0].get("geometry") or {}).get("coordinates") or None
    geometry = (
        [[round(float(lon), 5), round(float(lat), 5)] for lon, lat in coords]
        if coords
        else None
    )
    return float(meters), geometry


async def _brouter_walk_m(
    client: httpx.AsyncClient,
    origin: tuple[float, float],
    stop: dict[str, Any],
) -> tuple[float, list[list[float]] | None]:
    """Pedestrian metres plus path geometry from BRouter's GeoJSON answer.

    The metre count rides in ``properties.track-length`` as a string and the
    drawn path under ``geometry.coordinates`` as full-precision [lon, lat]
    pairs, rounded to 5 decimals exactly like the ORS geometry.
    """
    lon1, lat1 = origin[1], origin[0]
    lon2, lat2 = stop["lon"], stop["lat"]
    if _brouter_cooling_down():
        raise ValueError("brouter is cooling down after a rate limit")
    r = await client.get(
        _brouter_directions_url(config.BROUTER_URL, lon1, lat1, lon2, lat2),
        headers={"User-Agent": config.USER_AGENT},
        timeout=25,
    )
    if r.status_code in (403, 429):
        _brouter_note_throttled(r.headers.get("Retry-After"))
    r.raise_for_status()
    data = r.json()
    features = data.get("features") or []
    meters = float(features[0]["properties"]["track-length"])
    if not meters:
        raise ValueError("brouter returned a zero-length route")
    coords = (features[0].get("geometry") or {}).get("coordinates") or None
    geometry = (
        [[round(float(lon), 5), round(float(lat), 5)] for lon, lat in coords]
        if coords
        else None
    )
    return meters, geometry


async def walking_distances(
    origin: tuple[float, float], stops: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pedestrian distance from the origin to each stop.

    Requests run concurrently, capped so a shared public server is not
    overwhelmed. BRouter is throttled harder still - one request at a time -
    because its per-IP limit is what a burst of stop lookups would trip. Any
    stop whose route fails falls back to a straight-line estimate, flagged so
    the UI can say so.
    """
    semaphore = asyncio.Semaphore(6)
    brouter_gate = asyncio.Semaphore(1)
    router = await asyncio.to_thread(_pick_router)
    backend = _backend(router) if router else None

    async def one(client: httpx.AsyncClient, stop: dict[str, Any]) -> dict[str, Any]:
        straight = haversine_m(origin[0], origin[1], stop["lat"], stop["lon"])
        fallback = {
            "stop": stop,
            "walk_m": straight * ESTIMATE_DETOUR_FACTOR,
            "is_estimate": True,
            # Estimates carry no path, so the map draws no line for them;
            # every routed backend answers with drawable geometry instead.
            "geometry": None,
        }

        # With no router at all, go straight to the estimate. Probing dead
        # public hosts once per stop added tens of seconds per request.
        if router is None:
            return fallback

        async with semaphore:
            try:
                if backend == "ors":
                    meters, geometry = await _ors_walk_m(client, origin, stop)
                elif backend == "brouter":
                    # Serialised: the community server counts requests per IP,
                    # so a burst of five stops is what earns the 403.
                    async with brouter_gate:
                        meters, geometry = await _brouter_walk_m(client, origin, stop)
                else:
                    meters, geometry = await _valhalla_walk_m(client, router, origin, stop)
                if meters > 0:
                    return {
                        "stop": stop,
                        "walk_m": meters,
                        "is_estimate": False,
                        "geometry": geometry,
                    }
            except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError):
                pass
            return fallback

    async with httpx.AsyncClient() as client:
        return list(await asyncio.gather(*(one(client, s) for s in stops)))


def find_nearest(lat: float, lon: float, top: int = 5) -> dict[str, Any]:
    """Walk to every reachable stop and rank. `lat`/`lon` come from geocoding or a map click."""
    stops = load_stops()
    # Only stops a walk could still reach are routed. The rest cannot qualify
    # under the cutoff no matter which streets a router picks, and sweeping the
    # whole dataset is exactly what tripped BRouter's per-IP limit.
    reachable = [
        s
        for s in stops
        if haversine_m(lat, lon, s["lat"], s["lon"]) <= ROUTE_PREFILTER_M
    ]
    results = asyncio.run(walking_distances((lat, lon), reachable))

    ranked = []
    for item in results:
        minutes = item["walk_m"] / WALK_M_PER_MIN
        if minutes > MAX_WALK_MIN:
            continue
        stop = item["stop"]
        stop["walk_m"] = round(item["walk_m"])
        stop["walk_min"] = round(minutes)
        stop["is_estimate"] = item["is_estimate"]
        stop["geometry"] = item.get("geometry")
        ranked.append(stop)

    ranked.sort(key=lambda s: s["walk_m"])

    per_line: dict[str, dict[str, Any]] = {}
    for stop in ranked:
        for line_no in stop["lines"]:
            entry = per_line.setdefault(
                line_no,
                {"line": line_no, "name": config.SHEFAYIM_LINES.get(line_no, ""),
                 "best_stop": stop, "best_walk_m": stop["walk_m"],
                 "best_ride_min": stop.get("ride_min"),
                 "best_return_min": stop.get("return_min"),
                 "stop_no": stop.get("stop_numbers", {}).get(line_no)},
            )
            if stop["walk_m"] < entry["best_walk_m"]:
                entry["best_stop"] = stop
                entry["best_walk_m"] = stop["walk_m"]
                entry["best_ride_min"] = stop.get("ride_min")
                entry["best_return_min"] = stop.get("return_min")
                entry["stop_no"] = stop.get("stop_numbers", {}).get(line_no)

    return {
        "origin": {"lat": lat, "lon": lon},
        "best_stop": ranked[0] if ranked else None,
        "stops": ranked[:top],
        "lines": sorted(per_line.values(), key=lambda x: x["best_walk_m"]),
        "all_stops": ranked,
    }


def find_by_address(query: str, top: int = 5) -> dict[str, Any]:
    candidates = geocode.search_sync(query)
    place = candidates[0]
    result = find_nearest(place["lat"], place["lon"], top=top)
    result["place"] = place
    result["candidates"] = candidates
    return result


def _stop_no_text(stop: dict[str, Any], line: str | None = None) -> str:
    """Stop position along a line, e.g. 'stop 3'. Empty when unknown.

    With no line given, a stop whose number differs between the lines serving it
    shows every number, since printing just one would be wrong for the others.
    """
    numbers = stop.get("stop_numbers") or {}
    if line is not None:
        number = numbers.get(line)
        return f"stop {number}" if number else ""
    unique = sorted(set(numbers.values()))
    if not unique:
        return ""
    if len(unique) == 1:
        return f"stop {unique[0]}"
    return "/".join(f"stop {n}" for n in unique)


def _stop_code_text(stop: dict[str, Any]) -> str:
    """The stop's own sign code, e.g. 'code 20726'. Empty when unknown."""
    code = stop.get("code")
    return f"code {code}" if code else ""


def _stop_ref_text(stop: dict[str, Any], line: str | None = None) -> str:
    """How a rider identifies the stop sign, e.g. 'stop 3, code 20726'.

    Both halves are printed: the code identifies the physical stop and is the
    same on every line serving it, while the position along a line is what a
    rider counts stops from the origin. Empty when neither is known.
    """
    parts = [p for p in (_stop_no_text(stop, line), _stop_code_text(stop)) if p]
    return ", ".join(parts)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: find.py <address>", file=sys.stderr)
        return 2

    query = " ".join(sys.argv[1:])
    try:
        result = find_by_address(query)
    except (geocode.GeocodeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    place = result["place"]
    print(f"place: {place['label']}")
    print(f"       {place['lat']:.6f}, {place['lon']:.6f}\n")

    best = result["best_stop"]
    if best:
        est = "~" if best["is_estimate"] else " "
        number = _stop_ref_text(best)
        print(
            f"closest stop: {best['name_en'] or best['name']} "
            f"[{', '.join(best['lines'])}] {est}{best['walk_min']} min, {best['walk_m']} m"
            + (f"  ({number})" if number else "")
        )

    print("\ntop stops:")
    for stop in result["stops"]:
        est = "~" if stop["is_estimate"] else " "
        number = _stop_ref_text(stop)
        print(
            f"  {est}{stop['walk_min']:>3} min  "
            f"{stop['name_en'] or stop['name']} [{', '.join(stop['lines'])}]"
            + (f"  ({number})" if number else "")
        )

    print("\nbest stop per line:")
    for entry in result["lines"]:
        stop = entry["best_stop"]
        est = "~" if stop["is_estimate"] else " "
        number = _stop_ref_text(stop, entry["line"])
        print(
            f"  line {entry['line']} {entry['name']:<16} {est}{stop['walk_min']:>3} min  "
            f"{stop['name_en'] or stop['name']}" + (f"  ({number})" if number else "")
        )
    if any(s["is_estimate"] for s in result["all_stops"]):
        print("\n~ = straight-line estimate, routing server unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())