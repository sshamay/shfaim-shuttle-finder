"""Stage 5: local web app. FastAPI backend, Leaflet frontend."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app import config, geocode  # noqa: E402
from scripts import find  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Shfaim Shuttle Finder")


def _stop_payload(
    stop: dict[str, Any], *, include_geometry: bool = True
) -> dict[str, Any]:
    """Trim a stop for the wire and keep the Hebrew name for the tooltip."""
    numbers = stop.get("stop_numbers") or {}
    return {
        "lat": stop["lat"],
        "lon": stop["lon"],
        "name_en": stop.get("name_en") or stop.get("name") or "",
        "name_he": stop.get("name") or "",
        "lines": stop.get("lines", []),
        # The stop's own code, e.g. 20726: the number 20fl prints next to the
        # stop name. Unlike the position along a line it belongs to the physical
        # stop, so the same value is right for every line serving it.
        "code": stop.get("code"),
        # Position along each line serving this stop, keyed by line.
        "stop_numbers": numbers,
        # Set only when every line agrees, so the UI never shows one number that
        # is wrong for some of the lines printed next to it.
        "stop_no": next(iter(set(numbers.values()))) if len(set(numbers.values())) == 1 else None,
        "walk_min": stop.get("walk_min"),
        "walk_m": stop.get("walk_m"),
        "ride_min": stop.get("ride_min"),
        "return_min": stop.get("return_min"),
        "is_estimate": stop.get("is_estimate", False),
        "is_park_and_ride": stop.get("is_park_and_ride", False),
        # The routed walk path as [lon, lat] pairs, only when the backend
        # returned one (ORS); estimates and Valhalla send null and the map
        # draws no line. The winner and nearby-stop payloads carry it; the
        # per-line copies do not need it, so the wire does not repeat it.
        "geometry": stop.get("geometry") if include_geometry else None,
    }


@app.exception_handler(geocode.GeocodeError)
async def geocode_error_handler(_request, exc: geocode.GeocodeError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/api/lines")
async def api_lines() -> dict[str, Any]:
    import json

    if not config.STOPS_CACHE.exists():
        raise HTTPException(503, "stops not fetched yet, run scripts/fetch_lines.py")
    payload = json.loads(config.STOPS_CACHE.read_text())
    return {
        "fetched_at": payload.get("fetched_at"),
        "lines": [
            {"line": l["line"], "name": l["name"], "stop_count": l["stop_count"]}
            for l in payload["lines"]
        ],
    }


@app.get("/api/search")
async def api_search(q: str) -> dict[str, Any]:
    if len(q.strip()) < 2:
        raise HTTPException(400, "type at least two characters")
    return {"candidates": await geocode.search(q, limit=6)}


@app.get("/api/nearest")
async def api_nearest(lat: float, lon: float) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(find.find_nearest, lat, lon, 5)
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc

    return {
        "origin": result["origin"],
        "best_stop": _stop_payload(result["best_stop"]) if result["best_stop"] else None,
        "stops": [_stop_payload(s) for s in result["stops"]],
        "lines": [
            {
                "line": e["line"],
                "name": e["name"],
                "best_walk_min": e["best_stop"]["walk_min"],
                "best_walk_m": e["best_walk_m"],
                "best_ride_min": e["best_stop"].get("ride_min"),
                "best_return_min": e["best_stop"].get("return_min"),
                "is_estimate": e["best_stop"]["is_estimate"],
                # This line's own number for its closest stop, which can differ
                # from the same stop's number on another line.
                "stop_no": e["stop_no"],
                # Line rows only display text; the drawn walk comes from the
                # best_stop/stops payloads above, so this copy omits it.
                "stop": _stop_payload(e["best_stop"], include_geometry=False),
            }
            for e in result["lines"]
        ],
        "routing_available": not all(s["is_estimate"] for s in result["stops"]),
    }


@app.get("/api/routing-status")
async def api_routing_status() -> dict[str, Any]:
    """Probe every routing backend live and say which one would win.

    Diagnostic for cloud deploys, where the only clue about a dead router is
    otherwise the "~" estimate banner. Reports latencies, status codes and
    error bodies, but never the API key itself.
    """
    return await asyncio.to_thread(find.routing_status)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"}
    )


# StaticFiles sends far-future cache headers by default, so an edited app.js or
# app.css keeps running in the browser after the file on disk has changed.
# That silently served stale logic during development. StaticFiles takes no
# headers argument, so wrap it.
class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")