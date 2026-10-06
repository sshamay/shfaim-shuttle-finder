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

OSRM_FOOT_URL = "https://router.project-osrm.org/route/v1/foot"

# OSRM's public demo server ignores the profile parameter and answers every
# query with driving distances, so walking routes come from Valhalla instead,
# which supports pedestrian costing properly.
#
# Preference order: a local Valhalla container, then the public instances.
# Run the container with scripts/setup_routing.sh for the local one.
# Set VALHALLA_URL to a routing server you control (e.g. a VPS running that
# container); it is probed first, ahead of the public demos, so deployments
# that cannot reach openstreetmap.de get exact walk times.
DEFAULT_VALHALLA_URLS = [
    "http://localhost:8002/route",
    "https://routing.openstreetmap.de/routed-valhalla/route",
    "https://valhalla1.openstreetmap.de/route",
]

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