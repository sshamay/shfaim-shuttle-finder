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


def _probe_url(base: str) -> str:
    """The cheap health endpoint Valhalla keeps beside its /route handler."""
    return base.rsplit("/", 1)[0] + "/status"


def _status_answers(url: str) -> bool:
    """Does a probe endpoint answer 200 within the probe timeout?"""
    try:
        httpx.get(url, timeout=config.VALHALLA_PROBE_TIMEOUT_S).raise_for_status()
        return True
    except Exception:  # noqa: BLE001,S110
        return False


# ORS has no /status, so a minimal directions call is its probe. The pair is a
# short walk in central Tel Aviv, cheap in both time and quota.
_ORS_PROBE = (34.7880, 32.0710, 34.7890, 32.0720)  # lon1, lat1, lon2, lat2


def _ors_answers(base: str) -> bool:
    """Probe OpenRouteService with a directions call, which also validates the key."""
    lon1, lat1, lon2, lat2 = _ORS_PROBE
    try:
        httpx.get(
            f"{base}/{lon1},{lat1};{lon2},{lat2}",
            params={"overview": "false"},
            headers={"Authorization": config.ors_api_key()},
            timeout=config.VALHALLA_PROBE_TIMEOUT_S,
        ).raise_for_status()
        return True
    except Exception:  # noqa: BLE001,S110
        return False


def _probe_outcome(
    kind: str,
    route_url: str,
    probe_url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One live probe with its latency and failure reason, for diagnostics."""
    started = time.monotonic()
    try:
        kwargs: dict[str, Any] = {"timeout": config.VALHALLA_PROBE_TIMEOUT_S}
        if headers:
            kwargs["headers"] = headers
        if params:
            kwargs["params"] = params
        response = httpx.get(probe_url, **kwargs)
        latency_ms = round((time.monotonic() - started) * 1000)
        ok = 200 <= response.status_code < 300
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
        _probe_outcome("valhalla", base, _probe_url(base))
        for base in config.VALHALLA_URLS
    ]
    lon1, lat1, lon2, lat2 = _ORS_PROBE
    ors_probe = f"{config.OPENROUTESERVICE_URL}/{lon1},{lat1};{lon2},{lat2}"
    if key:
        candidates.append(
            _probe_outcome(
                "ors",
                config.OPENROUTESERVICE_URL,
                ors_probe,
                headers={"Authorization": key},
                params={"overview": "false"},
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

    return {
        "key_configured": bool(key),
        "key_length": len(key),
        # Same order as _pick_router: first Valhalla host that answers, else
        # ORS only when every Valhalla host failed.
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
    (probed on its ``/status`` endpoint), then OpenRouteService when an
    ``OPENROUTESERVICE_API_KEY`` is configured (probed with a minimal
    directions request). The first that answers is used for the whole request.

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
        if _status_answers(_probe_url(base)):
            found = base
            break
    if found is None and config.ors_api_key():
        if _ors_answers(config.OPENROUTESERVICE_URL):
            found = config.OPENROUTESERVICE_URL

    _ROUTER_CACHE = found
    _ROUTER_CHECKED_AT = now
    return found


def _is_ors(base: str) -> bool:
    """OpenRouteService answers on a different wire protocol than Valhalla.

    Valhalla POSTs a JSON costings payload and reports kilometres; ORS GETs
    coordinates in the URL and reports metres. The picked backend's URL tells
    the caller which shape to speak, so tests can stub a router with a bare
    string and the routing layer still dispatches correctly.
    """
    return base.startswith(config.OPENROUTESERVICE_URL)


async def _valhalla_walk_m(
    client: httpx.AsyncClient,
    router: str,
    origin: tuple[float, float],
    stop: dict[str, Any],
) -> float:
    """Pedestrian metres from a Valhalla POST /route (kilometres on the wire)."""
    payload = {
        "locations": [
            {"lat": origin[0], "lon": origin[1]},
            {"lat": stop["lat"], "lon": stop["lon"]},
        ],
        "costing": "pedestrian",
    }
    r = await client.post(router, json=payload, timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise ValueError("valhalla reported an error")
    meters = data["trip"]["summary"]["length"] * 1000.0
    if not meters:
        raise ValueError("valhalla returned a zero-length route")
    return float(meters)


async def _ors_walk_m(
    client: httpx.AsyncClient,
    origin: tuple[float, float],
    stop: dict[str, Any],
) -> float:
    """Pedestrian metres from an OpenRouteService directions request."""
    lon1, lat1 = origin[1], origin[0]
    lon2, lat2 = stop["lon"], stop["lat"]
    r = await client.get(
        f"{config.OPENROUTESERVICE_URL}/{lon1},{lat1};{lon2},{lat2}",
        params={"overview": "false"},
        headers={"Authorization": config.ors_api_key()},
        timeout=25,
    )
    r.raise_for_status()
    data = r.json()
    routes = data.get("routes") or []
    meters = routes[0]["summary"]["distance"]
    if not meters:
        raise ValueError("ors returned a zero-length route")
    return float(meters)


async def walking_distances(
    origin: tuple[float, float], stops: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pedestrian distance from the origin to each stop.

    Requests run concurrently, capped so a shared public server is not
    overwhelmed. Any stop whose route fails falls back to a straight-line
    estimate, flagged so the UI can say so.
    """
    semaphore = asyncio.Semaphore(6)
    router = await asyncio.to_thread(_pick_router)

    async def one(client: httpx.AsyncClient, stop: dict[str, Any]) -> dict[str, Any]:
        straight = haversine_m(origin[0], origin[1], stop["lat"], stop["lon"])
        fallback = {
            "stop": stop,
            "walk_m": straight * ESTIMATE_DETOUR_FACTOR,
            "is_estimate": True,
        }

        # With no router at all, go straight to the estimate. Probing dead
        # public hosts once per stop added tens of seconds per request.
        if router is None:
            return fallback

        async with semaphore:
            try:
                if _is_ors(router):
                    meters = await _ors_walk_m(client, origin, stop)
                else:
                    meters = await _valhalla_walk_m(client, router, origin, stop)
                if meters > 0:
                    return {"stop": stop, "walk_m": meters, "is_estimate": False}
            except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError):
                pass
            return fallback

    async with httpx.AsyncClient() as client:
        return list(await asyncio.gather(*(one(client, s) for s in stops)))


def find_nearest(lat: float, lon: float, top: int = 5) -> dict[str, Any]:
    """Walk to every stop and rank. `lat`/`lon` come from geocoding or a map click."""
    stops = load_stops()
    results = asyncio.run(walking_distances((lat, lon), stops))

    ranked = []
    for item in results:
        minutes = item["walk_m"] / WALK_M_PER_MIN
        if minutes > MAX_WALK_MIN:
            continue
        stop = item["stop"]
        stop["walk_m"] = round(item["walk_m"])
        stop["walk_min"] = round(minutes)
        stop["is_estimate"] = item["is_estimate"]
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