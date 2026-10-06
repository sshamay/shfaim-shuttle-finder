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
VALHALLA_URLS = [
    "http://localhost:8002/route",
    "https://routing.openstreetmap.de/routed-valhalla/route",
    "https://valhalla1.openstreetmap.de/route",
]