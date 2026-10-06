#!/usr/bin/env bash
# Build a local Valhalla routing server for pedestrian walking distances.
#
# One-time setup: downloads the Israel OSM extract (~115 MB) and builds the
# routing graph, which takes a few minutes. After this the container is reused.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$DIR/valhalla_data"
PBF="$DATA/israel.osm.pbf"
IMAGE="ghcr.io/gis-ops/docker-valhalla/valhalla:latest"
CONTAINER="shfaim-valhalla"
# Geofabrik renamed this extract to israel-and-palestine; the old israel-*
# name redirects to their homepage and downloads an HTML error page.
PBF_URL="https://download.geofabrik.de/asia/israel-and-palestine-latest.osm.pbf"

if docker ps --filter "name=^${CONTAINER}$" --filter status=running \
  | grep -q "$CONTAINER"; then
  echo "container already running on http://localhost:8002"
  exit 0
fi

mkdir -p "$DATA"

if [ ! -s "$PBF" ] || ! file "$PBF" | grep -q "Protocolbuffer"; then
  echo "downloading Israel OSM extract..."
  curl -fsSL -o "$PBF" "$PBF_URL"
fi
file "$PBF" | grep -q "Protocolbuffer" || {
  echo "download failed, not an OSM PBF file" >&2
  exit 1
}

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" -p 8002:8002 \
  -v "$DATA:/custom_files" "$IMAGE" >/dev/null

echo "building routing graph, this takes a few minutes..."
for _ in $(seq 1 40); do
  if curl -sf -m 5 http://localhost:8002/status >/dev/null 2>&1; then
    echo "ready: http://localhost:8002"
    exit 0
  fi
  sleep 15
done

echo "container did not become ready, check: docker logs $CONTAINER" >&2
exit 1