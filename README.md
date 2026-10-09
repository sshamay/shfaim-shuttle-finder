# Shfaim Shuttle Finder

Find the walking-nearest Egged Meitar Shefayim shuttle stop from any address in
Tel Aviv, and which of lines 811–815 to take.

English UI, Leaflet + OpenStreetMap, local Valhalla for real pedestrian routing.
No API key needed for the core flow.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium      # for stops refresh and the e2e test
```

Two more pieces, both optional to start but one is needed for accurate times:

```bash
./scripts/setup_routing.sh    # local Valhalla, ~115MB + a few minutes, once
./scripts/serve.sh            # http://127.0.0.1:8000
```

Refresh the stop list from 20fl (cached 20 hours):

```bash
.venv/bin/python scripts/fetch_lines.py --force
```

## Usage

Type an address, or click the map to drop a pin. The winner is the nearest stop;
the table below shows the closest stop on each line, so you can pick a line
you're comfortable with rather than only the physically closest one.

Command line, same logic:

```bash
.venv/bin/python scripts/find.py "Azrieli Center Tel Aviv"
```

## Deploy

**Render (simplest, free tier).** The repo includes `render.yaml`, a Render
Blueprint. On Render: *New + → Blueprint → connect this repo*. It installs
dependencies, *tries* to refresh stop data with `python scripts/fetch_lines.py
--api-only` (no browser needed; if the 20fl API blocks cloud networks the build
falls back to the committed snapshot in `data/stops.json`), and serves via
`uvicorn` on `$PORT`. The only manual step is telling Render the
`SERPAPI_API_KEY` (optional).

**Any Docker host (Railway, Fly.io, or a $5 VPS).** The repo includes a hardened
`Dockerfile` (non-root, no CDN dependency). On boot the entrypoint fetches the
stops cache from the live API if it is stale (20h TTL), then serves on port
`8000`:

```bash
docker build -t shfaim .
docker run -p 8000:8000 -e SERPAPI_API_KEY=... shfaim
```

Environment variables (`SERPAPI_API_KEY` lives in `.env` locally, set it in the
host's env on a server):

| Variable | Meaning | Default |
| --- | --- | --- |
| `GEOCODER` | `photon`, `nominatim`, `serpapi`, or `google` | `photon` |
| `SERPAPI_API_KEY` | 3rd geocoder fallback (POI/search); optional | *unset* |
| `GOOGLE_MAPS_API_KEY` | optional alternate backend | *unset* |
| `VALHALLA_URL` | your own Valhalla `/route` URL, probed first (see caveat) | *unset* |

Two deployment caveats:

- **Walking times.** Without a reachable Valhalla server the site works but
  shows straight-line estimates marked `~` with a warning banner. The app
  probes its routers with a real, tiny walk request (10s timeout): a local
  container first (`scripts/setup_routing.sh`), then two public
  `openstreetmap.de` demos. Those demos are reliable from nearby networks but
  can time out from US cloud regions; if the banner shows on a hosted deploy,
  run the container on a VPS and set `VALHALLA_URL=http://YOUR_VPS:8002/route`
  — it is probed first, so exact walk times return.
- **Map tiles** come from OpenStreetMap's public CDN and need no key. Photon
  geocoding likewise needs nothing; `SERPAPI_API_KEY` only upgrades the fallback.
- **Data freshness.** `data/stops.json` is committed as a baseline so deploys
  never fail on network. Each build *tries* the live 20fl API and uses it when
  reachable; otherwise the committed snapshot serves. To refresh the snapshot
  locally: `.venv/bin/python scripts/fetch_lines.py --api-only` then push the
  new `data/stops.json`.

## How it works

1. `app/geocode.py` resolves the address to coordinates. Photon first,
   Nominatim as fallback, then SerpAPI. Candidate ranking prefers the query's own
   city and penalises same-name places far outside the metro, so "Rothschild"
   puts Tel Aviv and Bat Yam above Jerusalem, London and Wisconsin. Ties are
   broken by whether the candidate sits in a city the caller actually typed and
   only then by distance to the shuttle, never by which one is further south —
   latitude used to decide them, which is how "Azrieli Center" resolved to the
   branch in Modi'in.

   Two things keep that chain honest. A misspelt street with a house number
   ("Marsel yanko 10") returns only unrelated POIs, while the same query without
   the number comes back from something sitting on the right street, so Photon
   re-asks using that street's real spelling before giving up. It re-asks
   whenever no single result accounts for every word that was typed, since a
   house carrying the right number on the wrong street is otherwise taken for an
   answer, and the rebuilt result is then ranked above the guess it replaced.
   And the chain moves on when a result is not an address at all, rather than
   stopping at the first non-empty response.
2. `scripts/find.py` asks Valhalla for a **pedestrian** route to all 33 stops
   concurrently, then ranks by distance. Stops more than a 30-minute walk away
   are dropped from every result set, so the closest stop is always genuinely
   walkable and a far-away street never surfaces a nonsense suggestion.
3. `app/web.py` serves that over JSON; `static/` is the Leaflet UI.

Walk times are address-to-stop only. Door-to-door time depends on when the
shuttle is due, which this does not cover.

## Things worth knowing

**Routing backends, in order.** Walk times are exact when a pedestrian router
is reachable: a local Valhalla container (`scripts/setup_routing.sh`) is tried
first, then BRouter as the primary cloud backend, then OpenRouteService as the
keyed fallback (free key, ~2,000 directions/day, no credit card). BRouter is a
self-hosted instance (`render.yaml`'s `shfaim-brouter` web service) whose tile
files come from brouter.de — keyless, no per-IP quota, and its GeoJSON answer
carries the walk line to the map. The app learns its URL automatically via
`BROUTER_PUBLIC_URL` (a copy of that service's `RENDER_EXTERNAL_URL`); a
deployment without a self-hosted instance falls back to `https://brouter.de/brouter`
as a strict last resort behind OpenRouteService. OpenRouteService matters when
BRouter is down, because public `openstreetmap.de` hosts refuse routes from US
data centers — the old public Valhalla demo was dropped for answering
`GET /status` from there while refusing `POST /route`. Set its key as
`OPENROUTESERVICE_API_KEY` in Render's dashboard; `render.yaml` keeps it out of
sync so the value stays secret. Every request to a Valhalla host carries
`User-Agent` and `X-Client-Id: shfaim-shuttle-finder`, as the FOSSGIS
public-demo README asks. When BRouter or OpenRouteService answers, its GeoJSON
path rides along to the browser, which draws the walk to the winning stop on
the map; estimates and Valhalla return no drawable path, so those searches show
no line.

**If every router is down, results degrade quietly.** The app falls back to
straight-line estimates, marked `~`, with a banner. Those are optimistically
low, typically by 20%. Start `scripts/setup_routing.sh` for the local
container; a router that comes back is noticed within ~20s, no restart needed.

**Address-to-stop only, no timetable.** This finds the stop, not the bus.

**Stop numbers are per line.** We show a stop's position along a line, as a
rider counts stops from the origin. The 20fl feed counts the Shefayim park and
ride as stop 1; for lines that start there we subtract 1 so that in-town stops
match their signs, and the park and ride itself is unnumbered. The same physical
stop can have a different number on different lines (e.g. Shefayim park and ride
disembark is 8/8/7/5/8 on 811/812/813/814/815). Each line's row shows that
line's own number, and where numbers differ the nearby-stops list shows all of
them (`6 / 8 / 9`). This matches on-stop signage; GTFS/20fl's `index` differs by
+1 on those lines.

**Two numbers identify a stop, and they mean different things.** Every result
shows a stop's **code** alongside its position along a line. The position is per
line and shifts with the origin, so it tells you how many stops to count; the
code belongs to the physical stop itself and is the same on every line serving
it, and it is the number 20fl prints next to the stop name. Both are shown
because each answers a different question, and showing only the position risks
counting to the right number at the wrong stop. The worked example: line 813
stop 3 is Menahem Begin Road/HaMelacha, code `20726`.

**Ambiguous queries ask instead of guessing.** Multiple matches show a picker.
Out-of-area candidates are labelled "far away" and never auto-selected. A lone
local street address is treated as decided even when the query also surfaces a
POI on the same number or same-name streets abroad, so "Marsel yanko 10" lands
straight on the map instead of making the user choose the Marcel Janco house
from a list.

## Files

| Path | Role |
| --- | --- |
| `app/config.py` | Endpoints, line names, cache paths |
| `app/geocode.py` | Geocoder adapters and candidate ranking |
| `app/web.py` | FastAPI app, JSON endpoints |
| `scripts/fetch_lines.py` | Scrapes lines/stops into `data/stops.json` |
| `scripts/find.py` | Walking-distance ranking, shared by CLI and API |
| `scripts/setup_routing.sh` | Builds the local Valhalla container |
| `scripts/serve.sh` | Runs the app with reload enabled |
| `static/` | Leaflet UI |
| `data/stops.json` | Cached stops, regenerated by `fetch_lines.py` |
| `tests/` | Unit and in-process API tests, all offline |

## Tests

```bash
.venv/bin/pytest              # everything
.venv/bin/pytest tests/unit   # pure logic only
.venv/bin/pytest -k walking   # by name
```

210 tests, about a second, no network. Two layers plus a browser run:

| Layer | What it covers |
| --- | --- |
| `tests/unit/test_geocode.py` | Geocoder fallback chain, address correction, tie-breaks, ranking, out-of-area flagging |
| `tests/unit/test_walking_distance.py` | Walking math, router selection, ranking, display limits |
| `tests/integration/test_api.py` | HTTP endpoints and the real `data/stops.json`, via `TestClient` |
| `tests/e2e/test_happy_path.py` | Playwright drives the real server: type an address, resolve straight to the map |

**Design decisions.** The project kept its existing `app/` and `scripts/` layout
rather than being restructured into `src/`; tests live in the conventional
`tests/{unit,integration,e2e}/` layout alongside it. The e2e tier drives the
real server and UI through headless Playwright: the app runs on a free
localhost port in a thread, with geocoding, routing, and the stops cache
stubbed deterministically, and the browser aborts every non-local request — so
the happy path uses no network any more than the unit tests do. Leaflet is
vendored under `static/vendor/leaflet/` (BSD-2-Clause) so the UI, and thus the
e2e test, does not depend on a CDN. Tests are deterministic: every external
call (Photon, Nominatim, SerpAPI, Valhalla, 20fl) is mocked at the HTTP client
boundary.

**Offline is enforced, not just intended.** An autouse fixture in
`tests/conftest.py` rejects any socket opened to a non-localhost host. This
caught a test that was silently loading the real `.env` and calling SerpAPI with
a live key.

**Tests run against real data too.** `TestRealStopData` uses the actual
`data/stops.json`, because the synthetic fixture drifted from the real schema
once and broke `/api/lines` in tests only. That class is skipped if the cache
is absent; everything else uses synthetic routes in `tmp_path`. It also pins the
worked example end to end — 813 stop 3 is code `20726` — which is what catches
the sign-number shift breaking against the real feed rather than against a
fixture that would have drifted with it.

**The table columns are pinned from both ends.** Headers live in `index.html`
and the cells are built in `app.js`, so the header count is checked against the
number of cells `tr.append()` receives. Without that second check, a row missing
a cell shifts every column to its right with nothing failing.

**Live tests.** `--run-live` together with `RUN_LIVE=1` lifts the network block
for tests marked `@pytest.mark.live`. None are written yet.

**Mutation-checked.** Deliberate bugs were injected to confirm the suite fails
when it should. Twelve of fourteen were caught, including `costing: pedestrian` →
`auto`, the 1.2 detour factor, truncated `top`, unrounded walk minutes, a
per-line aggregation bug, and dropping the stop code from the payload. The two
survivors were confirmed equivalent mutants, i.e. code that cannot change
behaviour:

- `sorted(per_line.values(), ...)` in `find_nearest` — `per_line` is built while
  walking an already-ascending `ranked`, so the sort is a no-op.
- The `in_area`/`far` reorder in `_rerank` — anything past the 40 km warn radius
  takes a distance penalty of ≥22 (rank ≥20) while any in-area candidate ranks
  ≤7, so local candidates already come first. It is kept as a guard against
  future retuning of `DISTANCE_PENALTY_*`, not because a test depends on it.

## Optional: third-party geocoders

Photon handles most addresses. For tricky ones you can set `SERPAPI_API_KEY`
in `.env` (third fallback, 250 queries/month, good for named places, weak on
raw addresses) or `GOOGLE_MAPS_API_KEY` (stronger, bills per request). Neither
is required. Both are reached only after the earlier geocoders failed to pin
the address down, which is rare: a query that has no house number stops at
Photon, and one that does is settled by Photon's own correction first.

`.env` is gitignored. Treat the API keys as secrets: a key committed or pasted
in a screenshot should be rotated.
