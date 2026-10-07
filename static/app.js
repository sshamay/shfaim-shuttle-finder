const map = L.map("map", { zoomControl: false }).setView([32.08, 34.79], 13);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap",
}).addTo(map);
L.control.zoom({ position: "bottomright" }).addTo(map);

const markerLayer = L.layerGroup().addTo(map);
let originMarker = null;
let stopMarkers = [];
let walkLine = null;

// The routed walk from the origin to a stop, drawn only when the backend
// returned real path geometry (OpenRouteService). Estimates and local
// Valhalla send none, so those searches simply show no line. Geometry comes
// as GeoJSON [lon, lat] pairs; Leaflet wants [lat, lon].
function drawWalkLine(geometry) {
  if (walkLine) {
    walkLine.remove();
    walkLine = null;
  }
  if (!geometry || geometry.length < 2) return;
  walkLine = L.polyline(
    geometry.map(([lon, lat]) => [lat, lon]),
    { color: "#4a8cff", weight: 5, opacity: 0.85, className: "walk-route" }
  ).addTo(map);
}

const qEl = document.getElementById("q");
const goEl = document.getElementById("go");
const listEl = document.getElementById("candidates");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const resultControlsEl = document.querySelector(".result-controls");
const collapseBtnEl = document.getElementById("collapseBtn");
const resultPillEl = document.getElementById("resultPill");

// The results panel can be folded up so the map gets the screen back. The
// toggle lives OUTSIDE the panel (in .result-controls) so it survives the
// collapse and is always the explicit way back; on phones a floating pill is
// a second, thumb-friendly restore point.
function setCollapsed(collapsed) {
  resultEl.hidden = collapsed;
  resultPillEl.hidden = !collapsed;
  collapseBtnEl.innerHTML = collapsed ? "Show results &#9650;" : "Hide results &#9660;";
  map.invalidateSize();
}
collapseBtnEl.addEventListener("click", () => setCollapsed(!resultEl.hidden));
resultPillEl.addEventListener("click", () => {
  setCollapsed(false);
  resultEl.scrollIntoView({ block: "nearest" });
});

// Photon's OSM values for "someone lives here", mirroring ADDRESS_TYPES in
// app/geocode.py. Used to tell a firm address answer apart from a POI that
// merely shares its house number.
const ADDRESS_TYPES = ["house", "building", "house_number", "apartments", "residential"];

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = kind ? "status " + kind : "status";
  statusEl.hidden = !text;
}

function walkLabel(min) {
  if (min == null) return "";
  return min <= 1 ? "1 min walk" : `${min} min walk`;
}

function estMark(s) {
  return s.is_estimate ? '<span class="est" title="routing server unavailable, straight-line estimate">~</span> ' : "";
}

function heName(el, text) {
  if (!text) return;
  const span = document.createElement("span");
  span.className = "he";
  span.textContent = text;
  el.appendChild(span);
}

// Guards against a slow request overwriting a newer one, which otherwise
// shows results for the previous address.
let nearestSeq = 0;

async function loadNearest(lat, lon) {
  const seq = ++nearestSeq;
  setStatus("Working out walking distances...");
  resultEl.hidden = true;
  try {
    const res = await fetch(`/api/nearest?lat=${lat}&lon=${lon}`);
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const data = await res.json();
    if (seq !== nearestSeq) return;
    render(data);
    // render may have set a routing warning; only clear when it did not.
    if (data.routing_available) setStatus("");
  } catch (err) {
    if (seq !== nearestSeq) return;
    setStatus(err.message, "err");
  }
}

function stopNoLabel(stop, line) {
  const numbers = stop.stop_numbers || {};
  const n = line ? numbers[line] : stop.stop_no;
  return n ? `<span class="stop-no">stop ${n}</span>` : "";
}

function stopNumbersFor(stop) {
  const values = Object.values(stop.stop_numbers || {});
  if (!values.length) return "";
  const unique = [...new Set(values)].sort((a, b) => a - b);
  return unique.length === 1
    ? stopNoLabel(stop)
    : `<span class="stop-no" title="Number differs per line">${unique.join(" / ")}</span>`;
}

function codeLabel(stop) {
  return stop.code != null && stop.code !== ""
    ? `<span class="stop-code" title="Stop code, the same on every line serving it">${stop.code}</span>`
    : "";
}

// One row shape for both result tables:
// Line | Route | Closest stop | No. | Code | Walk | Ride | Return.
// Built in a single place so the two tables cannot drift apart again.
function resultRow(
  line, route, stop, walkMin, isEstimate, noHtml, rideMin, returnMin
) {
  const tr = document.createElement("tr");
  const tdLine = document.createElement("td");
  tdLine.innerHTML = `<span class="line-no">${line}</span>`;
  const tdRoute = document.createElement("td");
  tdRoute.textContent = route;
  const tdStop = document.createElement("td");
  tdStop.textContent = stop.name_en;
  heName(tdStop, stop.name_he);
  const tdNo = document.createElement("td");
  tdNo.className = "no";
  tdNo.innerHTML = noHtml;
  const tdCode = document.createElement("td");
  tdCode.className = "code";
  tdCode.innerHTML = codeLabel(stop);
  const tdWalk = document.createElement("td");
  tdWalk.className = "walk";
  tdWalk.innerHTML = `${isEstimate ? estMark({ is_estimate: true }) : ""}${walkLabel(walkMin)}`;
  const tdRide = document.createElement("td");
  tdRide.className = "ride";
  tdRide.textContent = rideMin != null ? `${Math.round(rideMin)} min` : "-";
  const tdReturn = document.createElement("td");
  tdReturn.className = "return";
  tdReturn.textContent = returnMin != null ? `${Math.round(returnMin)} min` : "-";
  // data-label feeds the mobile card layout's little column labels.
  tdLine.setAttribute("data-label", "Line");
  tdRoute.setAttribute("data-label", "Route");
  tdStop.setAttribute("data-label", "Closest stop");
  tdNo.setAttribute("data-label", "No.");
  tdCode.setAttribute("data-label", "Code");
  tdWalk.setAttribute("data-label", "Walk");
  tdRide.setAttribute("data-label", "Ride");
  tdReturn.setAttribute("data-label", "Return");
  tr.append(tdLine, tdRoute, tdStop, tdNo, tdCode, tdWalk, tdRide, tdReturn);
  return tr;
}

function render(data) {
  const best = data.best_stop;
  markerLayer.clearLayers();
  stopMarkers = [];
  listEl.innerHTML = "";
  listEl.hidden = true;

  if (!best) {
    drawWalkLine(null);
    resultEl.hidden = true;
    resultControlsEl.hidden = true;
    resultPillEl.hidden = true;
    setStatus("No stops found.", "err");
    return;
  }

  const lineList = best.lines.join(", ");
  const rideMin = best.ride_min != null ? `Ride ${Math.round(best.ride_min)} min` : "";
  const returnMin = best.return_min != null ? `Return ${Math.round(best.return_min)} min` : "";
  const rideHtml = [rideMin, returnMin].filter(Boolean).join(" &middot; ");
  const winner = document.getElementById("winner");
  winner.innerHTML = `
    <div class="badge">${best.lines[0]}</div>
    <div class="meta">
      <div class="headline">Line ${lineList} &mdash; ${best.name_en}</div>
      <div class="walk">${stopNumbersFor(best)} ${codeLabel(best)} ${estMark(best)}${walkLabel(best.walk_min)} &middot; ${best.walk_m} m</div>
      <div class="ride">${rideHtml}</div>
    </div>`;
  heName(winner.querySelector(".meta"), best.name_he);
  resultEl.hidden = false;
  // A fresh search always comes back expanded with a current one-liner ready
  // for the collapse pill and a reset toggle.
  resultControlsEl.hidden = false;
  collapseBtnEl.innerHTML = "Hide results &#9660;";
  resultPillEl.hidden = true;
  resultPillEl.innerHTML =
    `${best.lines[0]} &middot; ${best.name_en} &middot; ${walkLabel(best.walk_min)}`;

  if (!data.routing_available) {
    setStatus(
      "Routing server is not running, so walk times are straight-line estimates " +
      "and are likely too low. Run scripts/setup_routing.sh for accurate times.",
      "warn"
    );
  }

  const linesTbody = document.getElementById("lines");
  linesTbody.innerHTML = "";
  const routeName = {};
  data.lines.forEach((entry) => {
    routeName[entry.line] = entry.name;
  });
  // The winner card already shows the best stop and every line it serves, so
  // those lines' rows in "Other lines" would be duplicates. A line whose
  // closest stop is that same physical stop (same coordinates) is skipped.
  data.lines.forEach((entry) => {
    if (entry.stop.lat === best.lat && entry.stop.lon === best.lon) return;
    linesTbody.append(
      resultRow(
        entry.line,
        entry.name,
        entry.stop,
        entry.best_walk_min,
        entry.is_estimate,
        stopNoLabel(entry.stop, entry.line),
        entry.best_ride_min,
        entry.best_return_min
      )
    );
  });
  // No alternative lines left when the winner is the only stop in range:
  // empty tables under a header would just look broken.
  const otherLinesTitle = document.getElementById("otherLinesTitle");
  const otherLinesTable = linesTbody.closest("table");
  const hasOtherLines = linesTbody.children.length > 0;
  otherLinesTitle.hidden = !hasOtherLines;
  otherLinesTable.hidden = !hasOtherLines;

  const stopsTbody = document.getElementById("stops");
  stopsTbody.innerHTML = "";
  // A nearby stop can serve several lines, and its number differs between them,
  // so it gets one row per line rather than a single row listing them.
  // The best stop is skipped: it already leads the winner card, so repeating
  // it here adds no information.
  const firstRowForStop = new Map();  // data.stops index -> first <tr> for it
  data.stops.forEach((stop, i) => {
    if (i === 0) return;
    stop.lines.forEach((line) => {
      const tr = resultRow(
        line,
        routeName[line] || "",
        stop,
        stop.walk_min,
        stop.is_estimate,
        stopNoLabel(stop, line),
        stop.ride_min,
        stop.return_min
      );
      if (!firstRowForStop.has(i)) firstRowForStop.set(i, tr);
      stopsTbody.append(tr);
    });
  });

  data.stops.forEach((stop, i) => {
    const m = L.marker([stop.lat, stop.lon], {
      icon: L.divIcon({
        className: "",
        html: `<div class="pin${i === 0 ? " first" : ""}"><span>${stop.lines[0]}</span></div>`,
        iconSize: [26, 26],
        iconAnchor: [13, 26],
      }),
      title: `${stop.name_en} · ${stop.lines.join(", ")}${
        stop.stop_no ? ` · stop ${stop.stop_no}` : ""
      }${stop.code != null && stop.code !== "" ? ` · code ${stop.code}` : ""}`,
    });
    m.on("click", () => {
      // A pin click focuses that stop's walk on the map, then points at its
      // row (or the winner card when it is the best stop).
      drawWalkLine(stop.geometry);
      const target = firstRowForStop.get(i) || document.getElementById("winner");
      target?.scrollIntoView({ block: "nearest" });
    });
    m.addTo(markerLayer);
    stopMarkers.push(m);
  });

  drawWalkLine(best.geometry);

  const bounds = L.latLngBounds([
    [data.origin.lat, data.origin.lon],
    ...data.stops.map((s) => [s.lat, s.lon]),
  ]);
  map.fitBounds(bounds.pad(0.2));
  if (originMarker) originMarker.remove();
  originMarker = L.circleMarker([data.origin.lat, data.origin.lon], {
    radius: 8,
    color: "#fff",
    weight: 2,
    fillColor: "#4a8cff",
    fillOpacity: 1,
  }).addTo(map);
}

function selectCandidate(candidate, { updateBox = false } = {}) {
  if (updateBox) qEl.value = candidate.label;
  listEl.hidden = true;
  map.setView([candidate.lat, candidate.lon], 17);
  loadNearest(candidate.lat, candidate.lon);
}

async function searchAddress() {
  const query = qEl.value.trim();
  if (query.length < 2) return;
  setStatus("Searching...");
  goEl.disabled = true;
  listEl.innerHTML = "";
  listEl.hidden = true;
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const { candidates } = await res.json();
    if (!candidates.length) {
      setStatus(`Nothing found for "${query}". Try a different spelling or click the map.`, "err");
      resultEl.hidden = true;
      return;
    }
    // Auto-select only when the match is unambiguous and local: either a lone
    // candidate, or a single street address. "Marsel yanko 10" returns the
    // corrected Marcel Janco house together with a Holon butcher on the same
    // number and same-name streets abroad; only the house is the address, so it
    // is safe to skip the list and go straight to the map.
    const local = candidates.filter((c) => !c.out_of_area);
    const loneAddress = local.filter((c) => c.housenumber && ADDRESS_TYPES.includes(c.type));
    if (candidates.length === 1 && local.length === 1) {
      selectCandidate(candidates[0]);
      return;
    }
    if (loneAddress.length === 1) {
      selectCandidate(loneAddress[0]);
      return;
    }
    showCandidates(candidates);
    setStatus(
      candidates.length === 1
        ? "Found, but it looks far from Tel Aviv. Check it before continuing."
        : `${local.length} local ${local.length === 1 ? "match" : "matches"}` +
          (candidates.length - local.length
            ? `, ${candidates.length - local.length} elsewhere — pick the right one:`
            : " — pick the right one:"),
      candidates.length === 1 ? "warn" : ""
    );
  } catch (err) {
    setStatus(err.message, "err");
  } finally {
    goEl.disabled = false;
  }
}

function showCandidates(candidates) {
  listEl.innerHTML = "";
  candidates.forEach((c) => {
    const li = document.createElement("li");
    if (c.out_of_area) {
      // Same street name, different town or country. Still selectable, but
      // never silently auto-selected as if it were the local match.
      const warn = document.createElement("strong");
      warn.textContent = "far away — ";
      warn.className = "est";
      li.append(warn, document.createTextNode(c.label));
      li.className = "far";
    } else {
      li.textContent = c.label;
    }
    li.addEventListener("click", () => selectCandidate(c, { updateBox: true }));
    listEl.append(li);
  });
  listEl.hidden = false;
}

goEl.addEventListener("click", searchAddress);
qEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") searchAddress();
});

map.on("click", (e) => {
  qEl.value = `${e.latlng.lat.toFixed(6)}, ${e.latlng.lng.toFixed(6)}`;
  listEl.hidden = true;
  map.setView(e.latlng, 17);
  loadNearest(e.latlng.lat, e.latlng.lng);
});