"use strict";

// --- API-Helfer --------------------------------------------------------
async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}
async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail || `${r.status} ${url}`);
  }
  return r.json();
}

// --- Darstellung -------------------------------------------------------
const TYPE_COLORS = {
  house: "#7f8a99",
  building: "#5b6675",
  supermarket: "#6fae5e",
  fuel_station: "#d98a3a",
  hardware: "#b07a4a",
  pharmacy: "#d9534f",
  hospital: "#d973b5",
};
const PLAYER_ID = 1;
const PLAYER_GROUP = 1;

function colorFor(type) {
  return TYPE_COLORS[type] || "#5b6675";
}

// Pfad-Stil (für Polygon-Footprints UND Punkt-Marker). Polygone bleiben dezent,
// damit die OSM-Karte durchscheint, aber per Füllung klickbar.
function styleFor(loc) {
  const c = colorFor(loc.type);
  const poly = loc.footprint_json != null;
  if (loc.discovery_status === "undiscovered") {
    return { color: c, weight: 1, fillColor: c, fillOpacity: poly ? 0.12 : 0.15, radius: 5 };
  }
  if (loc.discovery_status === "depleted") {
    return { color: "#555", weight: 1, fillColor: "#333", fillOpacity: 0.5, radius: 5 };
  }
  return { color: "#fff", weight: 1.5, fillColor: c, fillOpacity: poly ? 0.4 : 0.9, radius: 6 };
}

// --- Zustand -----------------------------------------------------------
let map;
let playerMarker;
let routeLine = null;
let highlightLayer = null;
const markers = new Map(); // id -> L.circleMarker
const locData = new Map(); // id -> location
let selectedId = null;

// Zeitfluss
let speed = 0;          // 0 = Pause; sonst Spielminuten pro Frame
let frameBusy = false;  // verhindert überlappende Tick-Requests
const FRAME_MS = 1000;
let lastPlayer = null;  // zuletzt bekannter Spielerzustand (für Ankunftserkennung)

// Chatfenster ist die Haupt-I/O. role: "player" | "claude" | "system".
function chat(role, text) {
  if (!text) return;
  const box = document.getElementById("chat");
  if (!box) return;
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  box.appendChild(div);
  while (box.children.length > 80) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

// log() bleibt als Alias für System-Zeilen (Ereignisse, Versorgung, Fehler).
function log(msg, severity) {
  chat("system", msg);
}

// Lädt den persistierten Chat-Verlauf und rendert ihn einmalig beim Start.
// "narrator" aus dem chatlog wird auf die CSS-Klasse "claude" gemappt.
let _chatHistoryLoaded = false;
async function loadChatHistory() {
  if (_chatHistoryLoaded) return;
  _chatHistoryLoaded = true;
  try {
    const rows = await getJSON("/chat?character_id=1&limit=40");
    for (const row of rows) {
      const cssRole = row.role === "narrator" ? "claude" : row.role;
      chat(cssRole, row.text);
    }
  } catch (e) { /* Verlauf nicht kritisch – still ignorieren */ }
}

// --- HUD ---------------------------------------------------------------
function fmtDateTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString("de-DE", {
    day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function setBar(fillId, valId, value, thresholds) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  const fill = document.getElementById(fillId);
  fill.style.width = pct + "%";
  let color = "var(--accent)";
  if (value < (thresholds ? thresholds.crit : 0.2)) color = "var(--crit)";
  else if (value < (thresholds ? thresholds.soft : 0.5)) color = "var(--warn)";
  fill.style.background = color;
  document.getElementById(valId).textContent = Math.round(pct) + "%";
}

async function refreshState() {
  const s = await getJSON("/world/state");
  document.getElementById("clock").textContent =
    `${fmtDateTime(s.datetime)} · Phase ${s.phase} · ${s.weather.temp_c}°C`;
  const p = s.player;
  if (p) {
    setBar("hunger-fill", "hunger-val", p.hunger);
    setBar("thirst-fill", "thirst-val", p.thirst);
    setBar("sleep-fill", "sleep-val", p.sleep);
    setBar("sat-fill", "sat-val", p.satisfaction);
    document.getElementById("who").textContent =
      `${p.name}, ${p.age ?? "?"} J. · ${p.height_cm ?? "?"} cm · ${Math.round(p.weight_kg ?? 0)} kg`
      + (p.is_alive ? "" : " · ✝ tot");
    document.getElementById("needs").textContent =
      `Bedarf: ${Math.round(p.daily_kcal)} kcal/Tag · ${p.daily_water_l} L/Tag`;
    if (p.lat != null && p.lon != null) movePlayerMarker(p.lat, p.lon);
    updateRoute(p);
    lastPlayer = p;
  }
  await refreshInventory();
  if (!document.getElementById("roster-list").classList.contains("hidden")) {
    await renderRoster();
  }
}

async function refreshInventory() {
  const inv = await getJSON(`/groups/${PLAYER_ID}/inventory`);
  const ul = document.getElementById("inventory");
  ul.innerHTML = "";
  if (!inv.length) {
    ul.innerHTML = '<li class="empty">leer</li>';
    return;
  }
  for (const it of inv) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${it.item_id}</span><span>${(+it.quantity).toFixed(0)} · q${(+it.quality).toFixed(2)}</span>`;
    ul.appendChild(li);
  }
}

// --- Gruppe / kontrollierte Charaktere ---------------------------------
async function renderRoster() {
  const chars = (await getJSON("/characters")).filter(
    (c) => c.group_id === PLAYER_GROUP
  );
  const ul = document.getElementById("roster-list");
  ul.innerHTML = "";
  document.getElementById("roster-count").textContent = chars.length;
  for (const c of chars) {
    const hunger = Math.round((c.hunger || 0) * 100);
    const perf = Math.round((c.performance || 0) * 100);
    const li = document.createElement("li");
    li.className = "roster-row" + (c.is_alive ? "" : " dead");
    li.innerHTML =
      `<span class="rname">${c.name}</span>` +
      `<span class="rstats">H ${hunger}% · L ${perf}%${c.is_alive ? "" : " ✝"}</span>`;
    li.title = "Auf Karte zentrieren";
    li.onclick = () => {
      if (c.lat != null && c.lon != null) map.panTo([c.lat, c.lon]);
    };
    ul.appendChild(li);
  }
}

function toggleRoster() {
  const ul = document.getElementById("roster-list");
  const nowOpen = ul.classList.toggle("hidden") === false;
  document.getElementById("btn-roster").textContent =
    (nowOpen ? "▾" : "▸") + " Gruppe";
  if (nowOpen) renderRoster();
}

// --- Locations ---------------------------------------------------------
// Viewport-Culling: nur sichtbare Locations laden/zeichnen.
// Marker-Registry (markers Map) hält id -> Layer; idempotent (nie doppelt).
// Beim Verlassen des Viewports werden off-screen Marker entfernt (außer der
// aktuell ausgewählten Location — die bleibt, bis das Panel geschlossen wird).

// Zoom-Schwelle: unter Zoom 13 (Bbox > ~10 km Seite) wird nicht geladen,
// um riesige Queries zu vermeiden.
const VIEWPORT_MIN_ZOOM = 13;

// Zoom-Schwelle für Chunk-Nachladen: erst ab Zoom 14 (Bbox ≤ ~5 km Seite).
// Schützt vor zu großen Bboxen, die das Bbox-Limit auf dem Server überschreiten.
const CHUNK_MIN_ZOOM = 14;

// Puffer in Grad (~200 m), damit Gebäude am Rand nicht schlagartig verschwinden.
const VIEWPORT_PAD = 0.002;

let _viewportDebounceTimer = null;
// Inflight-Guard: kein zweiter /world/ensure-chunks solange einer läuft.
let _chunkLoadInflight = false;

function _addLocationMarker(loc) {
  if (markers.has(loc.id)) return; // bereits gezeichnet
  locData.set(loc.id, loc);
  let m;
  if (loc.footprint_json) {
    try { m = L.polygon(JSON.parse(loc.footprint_json), styleFor(loc)); }
    catch (e) { m = L.circleMarker([loc.lat, loc.lon], styleFor(loc)); }
  } else {
    m = L.circleMarker([loc.lat, loc.lon], styleFor(loc));
  }
  m.on("click", (ev) => { L.DomEvent.stopPropagation(ev); selectLocation(loc.id); });
  m.addTo(map);
  markers.set(loc.id, m);
}

function _removeOffscreenMarkers(bounds) {
  // Etwas größere Bounds für Hysterese, damit Panning nicht ständig recycelt.
  const padded = bounds.pad(0.3);
  for (const [id, m] of markers.entries()) {
    if (id === selectedId) continue; // ausgewählte Location nie entfernen
    const d = locData.get(id);
    if (!d) continue;
    if (!padded.contains([d.lat, d.lon])) {
      map.removeLayer(m);
      markers.delete(id);
    }
  }
}

async function refreshLocationsInView() {
  if (!map) return;
  const zoom = map.getZoom();
  if (zoom < VIEWPORT_MIN_ZOOM) return; // zu weit raus — kein Load

  const b = map.getBounds().pad(VIEWPORT_PAD);
  const url = `/locations?min_lat=${b.getSouth()}&min_lon=${b.getWest()}&max_lat=${b.getNorth()}&max_lon=${b.getEast()}&limit=5000`;
  try {
    const locs = await getJSON(url);
    // Neue hinzufügen
    let added = 0;
    for (const loc of locs) {
      if (!markers.has(loc.id)) {
        _addLocationMarker(loc);
        added++;
      } else {
        // Daten aktualisieren (z. B. discovery_status nach Tick)
        locData.set(loc.id, loc);
      }
    }
    // Off-screen entfernen
    _removeOffscreenMarkers(map.getBounds());
    // Selektion/Highlight wiederherstellen, falls der Marker neu gezeichnet wurde
    if (selectedId !== null && markers.has(selectedId)) {
      const sel = locData.get(selectedId);
      if (sel && !highlightLayer) highlightFootprint(sel);
    }
    if (added > 0) log(`${locs.length} Orte im Viewport (${added} neu).`);
  } catch (e) {
    log("Locations-Ladefehler: " + e.message, "decision");
  }
}

// Ruft POST /world/ensure-chunks für die aktuelle (gepufferte) Bbox auf,
// sofern Zoom ≥ CHUNK_MIN_ZOOM und kein Request bereits läuft (Inflight-Guard).
// Danach wird refreshLocationsInView aufgerufen, damit neue Gebäude erscheinen.
async function ensureChunksInView() {
  if (!map) return;
  const zoom = map.getZoom();
  if (zoom < CHUNK_MIN_ZOOM) return;         // zu weit raus
  if (_chunkLoadInflight) return;             // Inflight-Guard: nur ein Request gleichzeitig

  const b = map.getBounds().pad(VIEWPORT_PAD);
  const bbox = {
    min_lat: b.getSouth(),
    min_lon: b.getWest(),
    max_lat: b.getNorth(),
    max_lon: b.getEast(),
  };

  const statusEl = document.getElementById("chunk-status");
  _chunkLoadInflight = true;
  if (statusEl) statusEl.classList.remove("hidden");

  try {
    const r = await postJSON("/world/ensure-chunks", bbox);
    if (r.loaded_chunks > 0 || r.materialized > 0) {
      // Neue Chunks oder Survivors → Locations neu laden, damit Marker erscheinen
      await refreshLocationsInView();
    }
  } catch (e) {
    // Stille Fehler (z.B. Bbox zu groß bei schnellem Herauszoomen) — kein log-Spam
    if (!e.message.includes("Bbox zu groß")) {
      log("Chunk-Ladefehler: " + e.message, "decision");
    }
  } finally {
    _chunkLoadInflight = false;
    if (statusEl) statusEl.classList.add("hidden");
  }
}

function scheduleViewportRefresh() {
  clearTimeout(_viewportDebounceTimer);
  _viewportDebounceTimer = setTimeout(async () => {
    // Erst Chunks nachladen (nur bei Zoom ≥ 14), dann Locations zeichnen.
    // ensureChunksInView ruft intern refreshLocationsInView auf, wenn neue Chunks kamen.
    // Bei Zoom < 14 (oder wenn Chunks already loaded): direkt refreshLocationsInView.
    if (map && map.getZoom() >= CHUNK_MIN_ZOOM) {
      await ensureChunksInView();
      // ensureChunksInView hat refreshLocationsInView aufgerufen falls nötig.
      // Sicherheitshalber immer neu zeichnen (idempotent — nur neue Marker werden hinzugefügt).
      await refreshLocationsInView();
    } else {
      await refreshLocationsInView();
    }
  }, 300);
}

// Compat-Alias: startGame ruft loadLocations() auf — jetzt = refreshLocationsInView.
async function loadLocations() {
  await refreshLocationsInView();
}

function updateLocation(loc) {
  locData.set(loc.id, loc);
  const m = markers.get(loc.id);
  if (m) m.setStyle(styleFor(loc));
}

// --- Auswahl-Panel -----------------------------------------------------
async function selectLocation(id) {
  selectedId = id;
  const loc = await getJSON(`/locations/${id}`); // voll inkl. footprint_json
  highlightFootprint(loc);
  const panel = document.getElementById("panel");
  panel.classList.remove("hidden");
  const kind = loc.label || loc.type;
  document.getElementById("panel-title").textContent = loc.name || kind;
  const dot = `<span class="legend-dot" style="background:${colorFor(loc.type)}"></span>`;
  const statusDe = { undiscovered: "unerkundet", discovered: "erkundet", depleted: "geplündert" }[loc.discovery_status] || loc.discovery_status;
  document.getElementById("panel-meta").innerHTML =
    `${dot}${kind} · ${statusDe}` +
    (loc.footprint_m2 ? ` · ${Math.round(loc.footprint_m2)} m²` : "");

  const actions = document.getElementById("panel-actions");
  actions.innerHTML = "";
  const inv = document.getElementById("panel-inv");
  inv.innerHTML = "";

  // „Gehe zu" ist immer möglich (setzt die Route; Entdeckung passiert bei Ankunft).
  const go = document.createElement("button");
  go.textContent = "Gehe zu";
  go.onclick = () => doGoto(loc);
  actions.appendChild(go);

  if (loc.discovery_status !== "undiscovered") {
    const items = await getJSON(`/locations/${id}/inventory`);
    renderPanelInv(items);
    if (items.length) {
      const b = document.createElement("button");
      b.textContent = "Plündern";
      b.onclick = () => doLoot(id);
      actions.appendChild(b);
    }
  }
}

let pendingArrival = null;  // Location-id, zu der gerade gelaufen wird

async function doGoto(loc) {
  document.getElementById("panel").classList.add("hidden");
  selectedId = null;
  clearHighlight();
  pendingArrival = loc.id;
  await walkTo(loc.lat, loc.lon);
  setSpeed(4); // automatisch hinlaufen
}

async function arriveAt(id) {
  pendingArrival = null;
  setSpeed(0);
  try {
    const r = await postJSON(`/locations/${id}/arrive`, {});
    const loc = await getJSON(`/locations/${id}`);
    updateLocation(loc);
    await selectLocation(id); // Panel öffnen (Typ/Status; Suche/Aktionen via Chat)
    chat("claude", r.narration || "");
    const input = document.getElementById("cmd");
    input.placeholder = "Was willst du hier tun? (z. B. ich suche …)";
    input.focus();
  } catch (e) { log("Fehler: " + e.message, "decision"); }
}

function renderPanelInv(items) {
  const inv = document.getElementById("panel-inv");
  inv.innerHTML = "";
  if (!items.length) {
    inv.innerHTML = '<li class="empty">nichts mehr hier</li>';
    return;
  }
  for (const it of items) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${it.item_id}</span><span>${(+it.quantity).toFixed(0)} · q${(+it.quality).toFixed(2)}</span>`;
    inv.appendChild(li);
  }
}

async function doDiscover(id) {
  try {
    const r = await postJSON(`/locations/${id}/discover`, {});
    const loc = await getJSON(`/locations/${id}`);
    updateLocation(loc);
    log(`Betreten: ${loc.name || loc.type} (${r.inventory.length} Stapel).`);
    await selectLocation(id);
  } catch (e) { log("Fehler: " + e.message, "decision"); }
}

async function doLoot(id) {
  try {
    const r = await postJSON(`/locations/${id}/loot`, {});
    const loc = await getJSON(`/locations/${id}`);
    updateLocation(loc);
    const n = Object.keys(r.transferred).length;
    log(`Geplündert: ${loc.name || loc.label || loc.type} (${n} Item-Arten) → ${r.status}.`);
    await selectLocation(id);
    await refreshInventory();
  } catch (e) { log("Fehler: " + e.message, "decision"); }
}

// --- Spieler -----------------------------------------------------------
function movePlayerMarker(lat, lon) {
  if (!playerMarker) {
    playerMarker = L.marker([lat, lon], {
      title: "Spieler",
      zIndexOffset: 1000,
      icon: L.divIcon({
        className: "player-icon", html: "🧍",
        iconSize: [40, 40], iconAnchor: [20, 20],
      }),
    }).addTo(map);
  } else {
    playerMarker.setLatLng([lat, lon]);
  }
}

function clearHighlight() {
  if (highlightLayer) { map.removeLayer(highlightLayer); highlightLayer = null; }
}

function highlightFootprint(loc) {
  clearHighlight();
  const style = { color: "#ff6600", weight: 2, fillColor: "#ff8c1a", fillOpacity: 0.45 };
  if (loc.footprint_json) {
    try {
      highlightLayer = L.polygon(JSON.parse(loc.footprint_json), style).addTo(map);
      return;
    } catch (e) { /* fällt auf Marker zurück */ }
  }
  // Kein Umriss (POI/Node): Punkt hervorheben.
  highlightLayer = L.circleMarker([loc.lat, loc.lon],
    { ...style, radius: 12, weight: 3 }).addTo(map);
}

function drawRoute(waypoints) {
  if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
  if (!waypoints || !waypoints.length) return;
  const pts = [];
  if (playerMarker) {
    const ll = playerMarker.getLatLng();
    pts.push([ll.lat, ll.lng]);
  }
  for (const w of waypoints) pts.push([w[0], w[1]]);
  routeLine = L.polyline(pts, {
    color: "#1e90ff", weight: 5, opacity: 0.9,
  }).addTo(map);
}

function updateRoute(player) {
  if (player.path_json) {
    try { drawRoute(JSON.parse(player.path_json)); return; } catch (e) {}
  }
  if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
}

async function walkTo(lat, lon) {
  // Ziel setzen + Route berechnen; der Spieler läuft sie über die Ticks ab.
  try {
    const r = await postJSON(`/characters/${PLAYER_ID}/move`, { lat, lon });
    drawRoute(r.path);
    log(`Unterwegs zum Ziel (${Math.round(r.distance_m)} m).`);
  } catch (e) { log("Fehler: " + e.message, "decision"); }
}

// --- Tick / Aktionen ---------------------------------------------------
function reportInterrupts(list) {
  for (const i of list || []) log(i.message, i.severity);
}
async function refreshDiscoveredMarkers() {
  // Nach Ticks können sich Bestände/Status geändert haben (Verderb) – nur Viewport neu laden.
  await refreshLocationsInView();
}

function setSpeed(s) {
  speed = s;
  for (const b of document.querySelectorAll("#speedbar button")) {
    b.classList.toggle("active", Number(b.dataset.speed) === s);
  }
}

async function tickFrame() {
  if (speed === 0 || frameBusy) return;
  frameBusy = true;
  try {
    const r = await postJSON("/world/tick", { minutes: speed });
    await refreshState();
    const halting = (r.interrupts || []).filter(
      (i) => i.severity === "soft" || i.severity === "decision"
    );
    if (halting.length) {
      setSpeed(0); // bei Ereignis automatisch pausieren (DESIGN.md §4)
      reportInterrupts(halting);
    }
    // Ankunft am Ziel-Gebäude -> Claude-Dialog zum Ort.
    if (pendingArrival != null && lastPlayer && !lastPlayer.path_json) {
      await arriveAt(pendingArrival);
    }
  } catch (e) {
    setSpeed(0);
    log("Fehler: " + e.message, "decision");
  } finally {
    frameBusy = false;
  }
}
async function doFastForward() {
  setSpeed(0);
  try {
    const r = await postJSON("/world/fast-forward", { max_ticks: 5000 });
    log(`Vorgespult bis Tick ${r.tick} (${r.stopped}).`);
    reportInterrupts(r.interrupts);
    await refreshState();
    await refreshDiscoveredMarkers();
  } catch (e) { log("Fehler: " + e.message, "decision"); }
}
// Free-Text-Adjudikation; bei no_heat wird der nächste Input als Override-Begründung gewertet.
let overrideCommand = null;
async function doCommand() {
  const input = document.getElementById("cmd");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  chat("player", text);
  try {
    const r = overrideCommand
      ? await postJSON("/adjudicate/override", { text: overrideCommand, reason: text })
      : await postJSON("/adjudicate", { text });

    // Claudes Antwort als Chat-Blase; Ablehnungs-Hinweis als System-Zeile.
    chat("claude", r.narration || "…");
    if (!r.ok && r.hint) log(r.hint);

    if (r.escalate && r.reason === "no_heat") {
      overrideCommand = overrideCommand || text;
      input.placeholder = "Begründung: womit erzeugst du Hitze?";
    } else if (r.escalate && r.reason === "override_unclear") {
      input.placeholder = "Nenne einen Gegenstand aus deinem Rucksack …";
    } else {
      overrideCommand = null;
      input.placeholder = "Was tust du?";
    }
    if (r.override_learned) {
      log(`Gelernt: ${r.override_learned.key} liefert ab jetzt Hitze.`);
    }
    await refreshState();
    await refreshDiscoveredMarkers();
  } catch (e) {
    log("Fehler: " + e.message, "decision");
  }
}

// --- Onboarding / Intro / Start ---------------------------------------
let _mapReady = false, _hudWired = false, _loopStarted = false;

function buildMap(center) {
  map = L.map("map", { zoomControl: true }).setView(center, 16);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "© OpenStreetMap",
  }).addTo(map);
  map.on("click", (e) => walkTo(e.latlng.lat, e.latlng.lng));
  map.on("moveend", scheduleViewportRefresh);
  map.on("zoomend", scheduleViewportRefresh);
  _mapReady = true;
}

function wireHud() {
  for (const b of document.querySelectorAll("#speedbar button")) {
    b.onclick = () => setSpeed(Number(b.dataset.speed));
  }
  document.getElementById("btn-ff").onclick = doFastForward;
  document.getElementById("btn-newgame").onclick = () => {
    setSpeed(0); // Zeit anhalten, Erstellungs-Screen erneut zeigen
    document.getElementById("onboarding").classList.remove("hidden");
  };
  document.getElementById("btn-roster").onclick = toggleRoster;
  document.getElementById("btn-cmd").onclick = doCommand;
  document.getElementById("cmd").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doCommand();
  });
  document.getElementById("panel-close").onclick = () => {
    document.getElementById("panel").classList.add("hidden");
    selectedId = null;
    clearHighlight();
  };
  _hudWired = true;
}

async function startGame(state) {
  document.getElementById("onboarding").classList.add("hidden");
  document.getElementById("intro").classList.add("hidden");
  document.getElementById("hud").classList.remove("hidden");
  const p = state && state.player;
  const center = (p && p.lat != null) ? [p.lat, p.lon] : (await getJSON("/api/info")).center;
  if (!_mapReady) buildMap(center); else map.setView(center, 16);
  if (!_hudWired) wireHud();
  await loadChatHistory();
  await loadLocations();
  await refreshState();
  if (!_loopStarted) { setInterval(tickFrame, FRAME_MS); _loopStarted = true; }
}

function showIntro(text) {
  document.getElementById("onboarding").classList.add("hidden");
  document.getElementById("intro-text").textContent = text;
  document.getElementById("intro").classList.remove("hidden");
}

async function submitOnboarding() {
  const v = (id) => document.getElementById(id).value.trim();
  const num = (id) => { const x = parseFloat(document.getElementById(id).value); return isNaN(x) ? null : x; };
  const profile = {
    name: v("f-name"), birthdate: v("f-birthdate") || null,
    sex: document.getElementById("f-sex").value,
    height_cm: num("f-height"), weight_kg: num("f-weight"),
    family: v("f-family"), education: v("f-education"),
    profession: v("f-profession"), hobbies: v("f-hobbies"),
    self_description: v("f-desc"), address: v("f-address"),
    lat: num("f-lat"), lon: num("f-lon"),
  };
  const errEl = document.getElementById("onboard-error");
  errEl.classList.add("hidden");
  const btn = document.getElementById("btn-start");
  btn.disabled = true; btn.textContent = "Welt wird geladen …";
  try {
    const r = await postJSON("/game/new", profile);
    showIntro(r.intro);
  } catch (e) {
    errEl.textContent = {
      geocode_failed: "Adresse nicht gefunden — bitte Koordinaten manuell angeben (Abschnitt unten).",
      osm_unavailable: "Kartendaten (OSM) gerade nicht erreichbar — bitte gleich nochmal versuchen.",
    }[e.message] || ("Fehler: " + e.message);
    errEl.classList.remove("hidden");
  } finally {
    btn.disabled = false; btn.textContent = "Spiel starten";
  }
}

async function bootstrap() {
  document.getElementById("btn-start").onclick = submitOnboarding;
  document.getElementById("btn-continue").onclick = async () => {
    const state = await getJSON("/world/state");
    await startGame(state);
  };
  const state = await getJSON("/world/state").catch(() => null);
  if (state && state.player && state.player.home_lat != null) {
    await startGame(state); // laufendes Spiel fortsetzen
  } else {
    document.getElementById("onboarding").classList.remove("hidden");
  }
}

bootstrap().catch((e) => log("Init-Fehler: " + e.message, "decision"));
