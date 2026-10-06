"""Stage 1: discover the shuttle lines and stops via Playwright.

Opens the live-lines page in a real browser and captures the network traffic the
site itself makes, then stores the Shefayim shuttle stops in data/stops.json.
Falls back to calling the API directly if the page changes.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402


def cache_is_fresh(path: Path = config.STOPS_CACHE) -> bool:
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        path.stat().st_mtime, timezone.utc
    )
    return age < timedelta(hours=config.CACHE_TTL_HOURS)


async def scrape_with_playwright() -> dict[str, Any]:
    """Drive the site in a browser and capture routes + stops off the wire."""
    from playwright.async_api import async_playwright

    routes: list[dict[str, Any]] = []
    stops_by_route: dict[str, list[dict[str, Any]]] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(locale="he-IL")

        async def handle(response: Any) -> None:
            url = response.url
            is_routes = "/gtfs/getRoutes/" in url
            if not is_routes and "getRouteStopsForLines" not in url:
                return
            try:
                body = await response.json()
            except Exception:
                return
            if is_routes:
                routes.extend(body.get("lines", []))
            else:
                stops_by_route.update(body.get("lines", {}))

        page.on("response", lambda r: asyncio.ensure_future(handle(r)))

        await page.goto(config.SITE_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(8000)
        await browser.close()

    if not routes:
        raise RuntimeError("no routes captured from the site")
    return {"routes": routes, "stops_by_route": stops_by_route}


async def fetch_via_api() -> dict[str, Any]:
    """Direct API path, same endpoints the site uses."""
    today = datetime.now().strftime("%Y-%m-%d")
    headers = {"Accept": "application/json", "User-Agent": config.USER_AGENT}

    async with httpx.AsyncClient(timeout=30, headers=headers) as c:
        r = await c.get(
            f"{config.API_ROOT}/gtfs/getRoutes/",
            params={"operatorID": config.OPERATOR_ID, "date": today},
        )
        r.raise_for_status()
        routes = r.json().get("lines", [])

        line_list = [
            {
                "lineRef": rt["routeID"],
                "rte": rt["RTE"],
                "operatorID": config.OPERATOR_ID,
            }
            for rt in routes
            if str(rt.get("line", "")).strip() in config.SHEFAYIM_LINES
        ]
        if not line_list:
            raise RuntimeError("no Shefayim lines returned by the API")

        r = await c.post(
            f"{config.API_ROOT}/gtfs/getRouteStopsForLines",
            params={"date": today},
            json={"lineList": line_list},
        )
        r.raise_for_status()
        stops_by_route = r.json().get("lines", {})

    return {"routes": routes, "stops_by_route": stops_by_route}


SPEED_KMH = 50.0
SPEED_MPM = SPEED_KMH * 1000 / 60.0


def _ride_min_from_dist(dist_m: float | None) -> float | None:
    if dist_m is None:
        return None
    try:
        mins = float(dist_m) / SPEED_MPM
    except (TypeError, ValueError):
        return None
    return round(mins, 1)

    for record in by_line.values():
        merged: dict[tuple, dict[str, Any]] = {}
        for leg in record["directions"].values():
            for stop in leg["stops"]:
                key = (stop["name"], round(stop["lat"], 5), round(stop["lon"], 5))
                merged.setdefault(key, stop)
        record["stops"] = sorted(merged.values(), key=lambda s: s["name"] or "")
        record["stop_count"] = len(record["stops"])

    return [by_line[k] for k in sorted(by_line)]



def build_line_records(
    routes: list[dict[str, Any]], stops_by_route: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Keep Shefayim lines, merging both directions into one stop set per line."""
    by_line: dict[str, dict[str, Any]] = {}

    for route in routes:
        line_no = str(route.get("line", "")).strip()
        if line_no not in config.SHEFAYIM_LINES:
            continue

        stops = [
            {
                "stop_id": s.get("stopID"),
                "code": s.get("code"),
                "name": s.get("he") or s.get("name"),
                "name_en": s.get("en"),
                "lat": float(s["lat"]),
                "lon": float(s["lon"]),
                "index": s.get("index"),
                "is_park_and_ride": s.get("stopType") == 1,
                "dist_from_orig_m": s.get("distFromOrig"),
                "ride_min": _ride_min_from_dist(s.get("distFromOrig")),
            }
            for s in stops_by_route.get(str(route.get("routeID")), [])
            if s.get("lat") and s.get("lon")
        ]
        if not stops:
            continue

        record = by_line.setdefault(
            line_no,
            {"line": line_no, "name": config.SHEFAYIM_LINES[line_no], "directions": {}},
        )
        direction = str(route.get("direction", ""))
        record["directions"][direction] = {
            "route_id": route.get("routeID"),
            "source": route.get("sourceHE") or route.get("source"),
            "dest": route.get("destHE") or route.get("dest"),
            "stops": stops,
        }

    for record in by_line.values():
        merged: dict[tuple, dict[str, Any]] = {}
        for leg in record["directions"].values():
            for stop in leg["stops"]:
                key = (stop["name"], round(stop["lat"], 5), round(stop["lon"], 5))
                merged.setdefault(key, stop)
        record["stops"] = sorted(merged.values(), key=lambda s: s["name"] or "")
        record["stop_count"] = len(record["stops"])

    return [by_line[k] for k in sorted(by_line)]


def save(records: list[dict[str, Any]]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": config.SITE_URL,
        "lines": records,
    }
    config.STOPS_CACHE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    force = "--force" in sys.argv
    api_only = "--api-only" in sys.argv

    if not force and cache_is_fresh():
        print(f"cache is fresh: {config.STOPS_CACHE}")
        return 0

    if api_only:
        # Skipping the browser: cloud builds (Render, Docker) have no Playwright
        # Chromium, and the direct API is cheaper and just as fresh. Falls over
        # to the same build/merge/save path below.
        raw = asyncio.run(fetch_via_api())
        print(f"api captured {len(raw['routes'])} routes")
    else:
        try:
            raw = asyncio.run(scrape_with_playwright())
            print(f"playwright captured {len(raw['routes'])} routes")
        except Exception as exc:  # noqa: BLE001
            print(f"playwright failed ({exc}); falling back to the API")
            raw = asyncio.run(fetch_via_api())

    records = build_line_records(raw["routes"], raw["stops_by_route"])
    if not records:
        print("no Shefayim lines found in the response", file=sys.stderr)
        return 1

    save(records)
    for rec in records:
        print(f"line {rec['line']:>3} {rec['name']:<16} {rec['stop_count']} stops")
    print(f"total {sum(r['stop_count'] for r in records)} stops -> {config.STOPS_CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())