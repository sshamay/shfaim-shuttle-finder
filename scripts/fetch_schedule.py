"""Stage 2b: write the operator's real timetable into data/stops.json.

Ride and return minutes start life as a 50 km/h guess off each stop's road
distance (see scripts/fetch_lines.py). 20fl publishes the scheduled arrival
time of every stop of every trip through GetPredictForLine, so this script
replaces the guess with the median across the day's trips and stamps the file.
After it has run the app needs nothing but the local cache: /api/nearest reads
data/stops.json and no request goes back to 20fl.

Re-run it by hand - "python scripts/fetch_schedule.py" - whenever the timetable
changes. A run that cannot read a full timetable for every line leaves the file
byte-for-byte alone, so a flaky API can never downgrade real numbers to guesses.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

PREDICT_URL = f"{config.API_ROOT}/GetPredictForLine"


def load_payload(path: Path | None = None) -> dict[str, Any]:
    """The stops cache. The path is resolved here, not in the signature, so a
    caller - or a test - that rewrites config.STOPS_CACHE is actually obeyed."""
    path = path or config.STOPS_CACHE
    if not path.exists():
        raise FileNotFoundError(f"{path} not found, run scripts/fetch_lines.py first")
    return json.loads(path.read_text())


def minutes_per_code(
    trips: list[dict[str, Any]],
) -> dict[str, dict[str, float]] | None:
    """Median minutes from the timetable, keyed by stop code.

    ``ride`` is minutes from boarding at the park and ride to reaching the stop;
    ``return`` is minutes from that stop back to the park and ride, the drop-off
    the route ends at. Returns None when no trip in the batch could be read, so
    the caller can tell "the endpoint answered" from "it answered with nothing".

    The join key is the predict feed's ``stopID`` (a string carrying the stop
    *code*, e.g. "20314"), not the route feed's numeric stopID - conflating the
    two is why the old get_sched813.py probes printed nothing.
    """
    ride: dict[str, list[float]] = {}
    ret: dict[str, list[float]] = {}
    read_any = False

    for trip in trips:
        stops = trip.get("stops") or []
        dep0 = None
        boarding = None
        for stop in stops:
            if stop.get("stopIndex") == 1:
                dep0 = _parse_time(stop.get("departureFromOrigin"))
                if dep0 is None:
                    dep0 = _parse_time(stop.get("aimedArrivalTime"))
                boarding = str(stop.get("stopID") or "").strip()
                break
        if dep0 is None:
            continue

        # The boarding stop is the park and ride, so its ride is zero and its
        # return is the whole run; it has to be in the table all the same or a
        # line whose only data point is a short batch looks broken.
        arrivals: list[tuple[str, float]] = [(boarding, 0.0)] if boarding else []
        for stop in stops:
            if stop.get("stopIndex") == 1:
                continue
            arr = _parse_time(stop.get("aimedArrivalTime"))
            if arr is None:
                continue
            code = str(stop.get("stopID") or "").strip()
            if not code:
                continue
            minutes = (arr - dep0).total_seconds() / 60
            if minutes < 0:
                continue
            arrivals.append((code, minutes))
            read_any = True

        if not read_any or not arrivals:
            continue
        last = max(minutes for _code, minutes in arrivals)
        for code, minutes in arrivals:
            ride.setdefault(code, []).append(minutes)
            ret.setdefault(code, []).append(round(last - minutes, 1))

    if not read_any:
        return None

    out: dict[str, dict[str, float]] = {}
    for code, values in ride.items():
        out[code] = {
            "ride": round(median(values), 1),
            "return": round(median(ret.get(code, [])), 1) if ret.get(code) else 0.0,
        }
    return out


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


def apply_schedule(
    payload: dict[str, Any], by_line: dict[str, dict[str, dict[str, float]]]
) -> None:
    """Write the timetable minutes onto each line's stops, in place.

    Every line in by_line must find every one of its stops, otherwise the
    update is refused - a half-written timetable is worse than the estimate
    it would replace.
    """
    for line in payload.get("lines", []):
        wanted = by_line.get(line.get("line"))
        if wanted is None:
            continue
        missing = [
            s.get("code")
            for s in line.get("stops", [])
            if str(s.get("code")) not in wanted
        ]
        if missing:
            raise ValueError(
                f"line {line.get('line')}: no timetable for stops {missing}"
            )
        for stop in line.get("stops", []):
            minutes = wanted[str(stop.get("code"))]
            stop["ride_min"] = minutes["ride"]
            stop["return_min"] = minutes["return"]


def fetch_timetable(
    client: httpx.Client, route_ids: list[int], date: str
) -> dict[str, dict[str, float]] | None:
    """Minutes for one line, pooled across both of its route directions."""
    pooled: dict[str, dict[str, list[float]]] = {}
    for route_id in route_ids:
        response = client.get(
            PREDICT_URL,
            params={
                "operatorID": config.OPERATOR_ID,
                "lineRef": route_id,
                "date": date,
            },
        )
        response.raise_for_status()
        minutes = minutes_per_code(response.json().get("trips") or [])
        if minutes is None:
            continue
        for code, values in minutes.items():
            slot = pooled.setdefault(code, {"ride": [], "return": []})
            slot["ride"].append(values["ride"])
            slot["return"].append(values["return"])

    if not pooled:
        return None
    return {
        code: {
            "ride": round(median(values["ride"]), 1),
            "return": round(median(values["return"]), 1),
        }
        for code, values in pooled.items()
    }


def collect(
    payload: dict[str, Any], date: str, client: httpx.Client | None = None
) -> dict[str, dict[str, dict[str, float]]]:
    by_line: dict[str, dict[str, dict[str, float]]] = {}
    with client or httpx.Client(timeout=60, headers={"User-Agent": config.USER_AGENT}) as c:
        for line in payload.get("lines", []):
            route_ids = [
                leg["route_id"]
                for leg in (line.get("directions") or {}).values()
                if leg.get("route_id")
            ]
            if not route_ids:
                raise ValueError(f"line {line.get('line')}: no route ids in the cache")
            minutes = fetch_timetable(c, route_ids, date)
            if minutes is None:
                raise ValueError(
                    f"line {line.get('line')}: no scheduled trips returned"
                )
            by_line[line["line"]] = minutes
    return by_line


def save(payload: dict[str, Any], path: Path | None = None) -> None:
    path = path or config.STOPS_CACHE
    payload["schedule_fetched_at"] = datetime.now().astimezone().isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    try:
        payload = load_payload()
        by_line = collect(payload, datetime.now().strftime("%Y-%m-%d"))
        apply_schedule(payload, by_line)
    except (FileNotFoundError, ValueError, httpx.HTTPError) as exc:
        print(f"schedule not updated: {exc}", file=sys.stderr)
        return 1

    save(payload)
    for line in payload["lines"]:
        furthest = max(
            line["stops"], key=lambda s: s.get("ride_min") or 0.0, default=None
        )
        if furthest is None:
            continue
        who = furthest.get("name_en") or furthest.get("name") or ""
        print(
            f"line {line['line']:>3} {line['name']:<16} "
            f"{furthest['ride_min']} min out, {furthest['return_min']} min back"
            + (f" ({who})" if who else "")
        )
    print(f"timetable -> {config.STOPS_CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
