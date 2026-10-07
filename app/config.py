import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STOPS_CACHE = DATA_DIR / "stops.json"

SITE_URL = "https://20fl.co.il/real-time-lines"
API_ROOT = "https://isr.20fl.co.il/TripsInfoProvider"

OPERATOR_ID = 39

SHEFAYIM_LINES = {
    "811": "HaKiriya",
    "812": "HaBursa",
    "813": "Bagin-Hamasger",
    "814": "Yigal Allon",
    "815": "Kiryat Atidim",
}

CACHE_TTL_HOURS = 20

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "shfaim-shuttle-finder/1.0 (personal project)"

# FOSSGIS asks apps that request their public Valhalla demo
# (valhalla1.openstreetmap.de) to identify themselves with an X-Client-Id
# header alongside the usual User-Agent - see the note in valhalla's README.
CLIENT_ID = "shfaim-shuttle-finder"

# Routing backends, tried in order: a local Valhalla container
# (scripts/setup_routing.sh), a public Valhalla instance, then OpenRouteService
# when OPENROUTESERVICE_API_KEY is set. VALHALLA_URL points at the first
# candidate when set, so a deployment that cannot reach the public demos can
# use a server of its own.
DEFAULT_VALHALLA_URLS = [
    "http://localhost:8002/route",
    "https://valhalla1.openstreetmap.de/route",
]

# OpenRouteService is the cloud-friendly fallback: reachable from US data
# centers that time out against openstreetmap.de, with a free tier (~2,000
# directions/day) that covers a shuttle finder. Its foot-walking profile
# returns real pedestrian distances, unlike the OSRM public demo which answers
# every query as a drive.
OPENROUTESERVICE_URL = "https://api.openrouteservice.org/v2/directions/foot-walking"

_valhalla_env = os.environ.get("VALHALLA_URL", "").strip()
if _valhalla_env:
    VALHALLA_URLS = [_valhalla_env, *DEFAULT_VALHALLA_URLS]
else:
    VALHALLA_URLS = list(DEFAULT_VALHALLA_URLS)

# How long to wait for a router's /status probe. Public demos answered in a
# second from nearby networks but the app also runs in US data centers, where
# the round trip to openstreetmap.de can exceed the old 3s and wrongly mark
# every router down (all walk times become "~" estimates).
VALHALLA_PROBE_TIMEOUT_S = 10.0


def _load_dotenv() -> None:
    """Read .env into os.environ without overriding real env vars."""
    env_path = BASE_DIR / ".env"
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


def ors_api_key() -> str:
    """The OpenRouteService API key, or "" when none is configured.

    Read lazily so local development can keep it in .env and tests can pin it
    either way; Render sets the value from the dashboard (render.yaml keeps it
    out of sync).
    """
    _load_dotenv()
    return os.environ.get("OPENROUTESERVICE_API_KEY", "").strip()
