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

// Aktions-basierte Zeit: Zeit vergeht nur durch Aktionen (POST /actions).
let actionBusy = false;   // eine Aktion läuft -> Aktions-UI gesperrt
let gameOver = false;     // Endzustand (tot / Woche vorbei) angezeigt
const WALK_ANIM_MS = 1200; // Dauer der Lauf-Animation des Spieler-Markers

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

// Welt-Infrastruktur (Strom/Wasser am Standort, Netz, Gerätakku) aus /world/state -> infra.
function renderInfra(infra) {
  if (!infra) return;
  document.getElementById("world-phase").textContent = infra.phase || "";
  setBar("power-fill", "power-val", infra.power);
  setBar("water-fill", "water-val", infra.water);
  setBar("cell-fill", "cell-val", infra.cellular);
  setBar("net-fill", "net-val", infra.quality);
  const devs = infra.devices || [];
  const phone = devs.find((d) => d.item_id === "smartphone") || devs.find((d) => d.item_id === "laptop");
  const pb = devs.find((d) => d.item_id === "powerbank");
  if (phone) setBar("batt-fill", "batt-val", phone.pct / 100);
  else setBar("batt-fill", "batt-val", 0);
  const where = infra.scope === "here" ? "am Ort" : "typisch für die Gegend";
  document.getElementById("infra-note").textContent =
    `Strom/Wasser: ${where}` + (infra.wifi ? " · WLAN da" : "")
    + (pb ? ` · Powerbank ${Math.round(pb.pct)} %` : "")
    + (phone ? "" : " · kein Gerät");
}

async function refreshState() {
  const s = await getJSON("/world/state");
  const dayNo = (s.day ?? 0) + 1;
  document.getElementById("daytime").textContent =
    `Tag ${dayNo} von ${s.days_total ?? 7} · ${s.time_of_day ?? "--:--"}`;
  document.getElementById("clock").textContent = `Wetter: ${s.weather.temp_c}°C`;
  renderInfra(s.infra);
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
  }
  await refreshInventory();
  checkEndState(s);
  if (!document.getElementById("roster-list").classList.contains("hidden")) {
    await renderRoster();
  }
}

async function refreshInventory() {
  const inv = await getJSON(`/groups/${PLAYER_ID}/inventory`);
  try {
    const c = await getJSON(`/groups/${PLAYER_GROUP}/carry`);
    const el = document.getElementById("carry");
    if (el) {
      el.textContent = `${(+c.carried_kg).toFixed(1)} / ${(+c.capacity_kg).toFixed(0)} kg`;
      el.classList.toggle("full", c.carried_kg >= c.capacity_kg - 0.5);
    }
  } catch (e) { /* Tragelast-Anzeige nicht kritisch */ }
  const ul = document.getElementById("inventory");
  ul.innerHTML = "";
  if (!inv.length) {
    ul.innerHTML = '<li class="empty">leer</li>';
    return;
  }
  for (const it of inv) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${it.name || it.item_id}</span><span>${(+it.quantity).toFixed(0)} · q${(+it.quality).toFixed(2)}</span>`;
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
    // „Durchsuchen" nur, wenn der Ort noch nicht durchsucht ist (Aktionsleiste weiß das).
    let canSearch = false;
    try {
      const av = await getJSON(`/actions/available?character_id=${PLAYER_ID}`);
      canSearch = av.actions.some((a) => a.kind === "search" && a.location_id === id);
    } catch (e) { /* ohne Aktionsleiste kein Durchsuchen-Knopf */ }
    if (canSearch) {
      const sb = document.createElement("button");
      sb.textContent = "Durchsuchen";
      sb.onclick = () => doAction({ kind: "search", location_id: id });
      actions.appendChild(sb);
    }
    if (items.length) {
      const b = document.createElement("button");
      b.textContent = "Plündern";
      b.onclick = () => doAction({ kind: "loot", location_id: id });
      actions.appendChild(b);
    }
  }
}

async function doGoto(loc) {
  document.getElementById("panel").classList.add("hidden");
  selectedId = null;
  clearHighlight();
  await doAction({ kind: "walk", location_id: loc.id });
}

async function arriveAt(id) {
  try {
    const r = await postJSON(`/locations/${id}/arrive`, {});
    const loc = await getJSON(`/locations/${id}`);
    updateLocation(loc);
    await selectLocation(id); // Panel öffnen (Typ/Status; Suche/Aktionen via Chat)
    chat("claude", r.narration || "");
    const input = document.getElementById("cmd");
    input.placeholder = "Was willst du hier tun? (z. B. ich suche …)";
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
    li.innerHTML = `<span>${it.name || it.item_id}</span><span>${(+it.quantity).toFixed(0)} · q${(+it.quality).toFixed(2)}</span>`;
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

function clearRoute() {
  if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
}

// Spieler-Marker clientseitig entlang des Pfads bewegen (reine Optik;
// die echte Position kommt danach aus dem Serverzustand).
function animateAlongPath(path) {
  return new Promise((resolve) => {
    if (!path || path.length < 2 || !playerMarker) { resolve(); return; }
    const seg = [0]; // kumulierte Länge (Grad-Distanz reicht für Interpolation)
    for (let i = 1; i < path.length; i++) {
      seg.push(seg[i - 1] + Math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]));
    }
    const total = seg[seg.length - 1];
    if (total === 0) { resolve(); return; }
    const t0 = performance.now();
    function step(now) {
      const f = Math.min(1, (now - t0) / WALK_ANIM_MS);
      const d = f * total;
      let i = 1;
      while (i < seg.length - 1 && seg[i] < d) i++;
      const span = seg[i] - seg[i - 1] || 1;
      const u = Math.max(0, Math.min(1, (d - seg[i - 1]) / span));
      playerMarker.setLatLng([
        path[i - 1][0] + (path[i][0] - path[i - 1][0]) * u,
        path[i - 1][1] + (path[i][1] - path[i - 1][1]) * u,
      ]);
      if (f < 1) requestAnimationFrame(step); else resolve();
    }
    requestAnimationFrame(step);
  });
}

// --- Tick / Aktionen ---------------------------------------------------
function reportInterrupts(list) {
  for (const i of list || []) log(i.message, i.severity);
}
async function refreshDiscoveredMarkers() {
  // Nach Aktionen können sich Bestände/Status geändert haben – nur Viewport neu laden.
  await refreshLocationsInView();
}

// Aktions-UI sperren/entsperren, solange eine Aktion läuft.
function setActionBusy(busy) {
  actionBusy = busy;
  const sel = "#actionbar button, #actionbar select, #panel-actions button, #btn-cmd, #cmd";
  for (const el of document.querySelectorAll(sel)) el.disabled = busy;
}

// Aktionsleiste aus GET /actions/available füllen.
async function loadActionBar() {
  const bar = document.getElementById("actionbar");
  if (!bar) return;
  if (gameOver) { bar.innerHTML = ""; return; }
  try {
    const av = await getJSON(`/actions/available?character_id=${PLAYER_ID}`);
    bar.innerHTML = "";
    const chooser = document.getElementById("dl-chooser");
    let keepChooser = false;
    for (const a of av.actions || []) {
      if (a.kind === "download") {
        // Download: Knopf öffnet die Paketauswahl mit Größe, Dauer, Akkuverbrauch.
        const db = document.createElement("button");
        db.textContent = a.label;
        db.disabled = actionBusy;
        db.onclick = () => { chooser.classList.toggle("hidden"); renderChooser(a); };
        bar.appendChild(db);
        if (!chooser.classList.contains("hidden")) { renderChooser(a); keepChooser = true; }
        continue;
      }
      const b = document.createElement("button");
      b.textContent = a.label;
      if (a.minutes != null) {
        const c = document.createElement("span");
        c.className = "cost";
        c.textContent = ` · ${a.minutes} min`;
        b.appendChild(c);
      }
      b.disabled = actionBusy;
      b.onclick = () => {
        const { label, minutes, options, ...payload } = a; // kind + Parameter zurücksenden
        doAction(payload); // ohne item_id wählt der Sim-Kern das beste Item
      };
      if (a.options && a.options.length > 1) {
        // Essen/Zubereiten: Hauptknopf = bestes Item, kleines Menü = konkretes Item.
        const wrap = document.createElement("span");
        wrap.className = "split";
        const menu = document.createElement("select");
        menu.className = "pick";
        menu.title = "Bestimmtes Item wählen";
        menu.disabled = actionBusy;
        const ph = document.createElement("option");
        ph.value = ""; ph.textContent = "▾";
        menu.appendChild(ph);
        for (const o of a.options) {
          const opt = document.createElement("option");
          opt.value = o.item_id;
          opt.textContent = `${o.name} (${(+o.quantity).toFixed(0)})`;
          menu.appendChild(opt);
        }
        menu.onchange = () => {
          if (!menu.value) return;
          doAction({ kind: a.kind, item_id: menu.value });
        };
        wrap.appendChild(b);
        wrap.appendChild(menu);
        bar.appendChild(wrap);
      } else {
        bar.appendChild(b);
      }
    }
    if (!keepChooser) { chooser.classList.add("hidden"); chooser.innerHTML = ""; }
  } catch (e) { /* Leiste nicht kritisch */ }
}

function fmtMinutes(m) {
  if (m >= 10 ** 8) return "nie";
  const h = Math.floor(m / 60), r = m % 60;
  return h ? `${h} Std. ${r} min` : `${r} min`;
}

// Paketauswahl für Wissens-Downloads (Teilfortschritt wird angezeigt).
function renderChooser(a) {
  const el = document.getElementById("dl-chooser");
  el.innerHTML = "";
  const head = document.createElement("div");
  head.className = "dl-head";
  head.textContent = `Verbindung ${Math.round((a.quality ?? 0) * 100)} % · Schätzung bei aktueller Qualität`;
  el.appendChild(head);
  for (const o of a.options || []) {
    const row = document.createElement("div");
    row.className = "dl-row";
    const t = document.createElement("span");
    t.className = "dl-title";
    t.textContent = o.title;
    const m = document.createElement("span");
    m.className = "dl-meta";
    m.textContent = `${o.size_mb} MB · ${fmtMinutes(o.minutes_total)} · ${o.battery_pct} % Akku`
      + (o.progress > 0 ? ` · ${Math.round(o.progress * 100)} % da` : "");
    const btn = document.createElement("button");
    btn.textContent = o.progress > 0 ? "Fortsetzen" : "Laden";
    btn.disabled = actionBusy;
    btn.onclick = () => doAction({ kind: "download", package_id: o.package_id });
    row.append(t, m, btn);
    el.appendChild(row);
    if (o.progress > 0) {
      const pr = document.createElement("div");
      pr.className = "dl-progress";
      const sp = document.createElement("span");
      sp.style.width = Math.round(o.progress * 100) + "%";
      pr.appendChild(sp);
      el.appendChild(pr);
    }
  }
}

// Einziger Weg, Zeit vergehen zu lassen: eine Aktion an den Sim-Kern senden.
async function doAction(payload) {
  if (actionBusy || gameOver) return null;
  setActionBusy(true);
  let r = null;
  try {
    r = await postJSON("/actions", { character_id: PLAYER_ID, ...payload });
    await handleActionResult(r);
  } catch (e) {
    log("Fehler: " + e.message, "decision");
  } finally {
    setActionBusy(false);
  }
  await loadActionBar();
  return r;
}

async function handleActionResult(r) {
  if (r.narration) chat("claude", r.narration);
  if (r.hint) log(r.hint);
  if (!r.ok) {
    if (!r.hint && !r.narration) log(`Nicht möglich (${r.reason || "unbekannt"}).`);
    return;
  }
  if (r.minutes_spent > 0) log(`(+${r.minutes_spent} min)`);
  reportInterrupts(r.interrupts);

  // Laufen: Marker entlang des Pfads animieren, dann auf Serverposition setzen.
  if (r.path && r.path.length > 1) {
    drawRoute(r.path);
    await animateAlongPath(r.path);
    clearRoute();
  }
  if (r.path && r.stopped === "interrupt") log("Der Weg wurde unterbrochen.");
  if (r.path && r.stopped === "max") log("Du bist lange gelaufen und legst eine Pause ein.");
  if (r.player && r.player.lat != null) movePlayerMarker(r.player.lat, r.player.lon);

  await refreshState(); // HUD, Inventar, Endzustand
  await refreshDiscoveredMarkers();
  if (r.stopped === "dead") checkEndState({ player: { is_alive: false }, day: r.day });
  if (r.arrived_location_id != null) await arriveAt(r.arrived_location_id);
  else if (selectedId !== null) await selectLocation(selectedId);
}

// --- Endzustände (Platzhalter für M5) ----------------------------------
function showEnd(title, text) {
  gameOver = true;
  document.getElementById("end-title").textContent = title;
  document.getElementById("end-text").textContent = text;
  document.getElementById("endscreen").classList.remove("hidden");
}

function checkEndState(s) {
  if (gameOver || !s) return;
  const dayNo = (s.day ?? 0) + 1;
  if (s.player && s.player.is_alive === false) {
    showEnd("Du hast nicht überlebt", `Du hast Tag ${dayNo} erreicht.`);
  } else if (s.days_total != null && s.day >= s.days_total) {
    showEnd("Die erste Woche ist vorbei",
      "Die Entscheidung, wie es weitergeht, folgt in einer späteren Version.");
  }
}

function newGameScreen() {
  document.getElementById("endscreen").classList.add("hidden");
  gameOver = false;
  document.getElementById("onboarding").classList.remove("hidden");
}

// Free-Text-Adjudikation; bei no_heat wird der nächste Input als Override-Begründung gewertet.
let overrideCommand = null;
async function doCommand() {
  if (actionBusy || gameOver) return;
  const input = document.getElementById("cmd");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  chat("player", text);
  const payload = overrideCommand
    ? { kind: "freetext", text: overrideCommand, override_text: overrideCommand, override_reason: text }
    : { kind: "freetext", text };
  const r = await doAction(payload);
  if (!r) return;
  // escalate/reason/override_learned: Top-Level oder in result
  const res = r.result || {};
  const escalate = r.escalate ?? res.escalate;
  const reason = r.reason ?? res.reason;
  if (escalate && reason === "no_heat") {
    overrideCommand = overrideCommand || text;
    input.placeholder = "Begründung: womit erzeugst du Hitze?";
  } else if (escalate && reason === "override_unclear") {
    input.placeholder = "Nenne einen Gegenstand aus deinem Rucksack …";
  } else {
    overrideCommand = null;
    input.placeholder = "Was tust du?";
  }
  const learned = r.override_learned || res.override_learned;
  if (learned) log(`Gelernt: ${learned.key} liefert ab jetzt Hitze.`);
  if (!gameOver) input.focus();
}

// --- Onboarding / Intro / Start ---------------------------------------
let _mapReady = false, _hudWired = false;

function buildMap(center) {
  map = L.map("map", { zoomControl: true }).setView(center, 16);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "© OpenStreetMap",
  }).addTo(map);
  map.on("click", (e) => doAction({ kind: "walk", lat: e.latlng.lat, lon: e.latlng.lng }));
  map.on("moveend", scheduleViewportRefresh);
  map.on("zoomend", scheduleViewportRefresh);
  _mapReady = true;
}

function wireHud() {
  document.getElementById("btn-newgame").onclick = newGameScreen;
  document.getElementById("btn-end-newgame").onclick = newGameScreen;
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
  await loadActionBar();
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
  btn.disabled = true; btn.textContent = "Spiel wird gestartet …";
  try {
    const r = await postJSON("/game/new", profile);
    // Erfolg: Spiel sofort anzeigen, Karte auf home zentrieren.
    const center = r.home;
    const startZoom = Math.max(VIEWPORT_MIN_ZOOM, CHUNK_MIN_ZOOM); // >= 14 damit Chunk-Load triggert
    document.getElementById("onboarding").classList.add("hidden");
    document.getElementById("intro").classList.add("hidden");
    document.getElementById("hud").classList.remove("hidden");
    if (!_mapReady) buildMap(center); else map.setView(center, startZoom);
    if (_mapReady && map) map.setView(center, startZoom);
    if (!_hudWired) wireHud();
    // Intro in den Chat schreiben (direkt, kein loadChatHistory nötig — frisch).
    _chatHistoryLoaded = true; // verhindert doppeltes Laden
    chat("claude", r.intro);
    gameOver = false;
    document.getElementById("endscreen").classList.add("hidden");
    await refreshState();
    await loadActionBar();
    // Heimat-Chunks sofort im Hintergrund laden (kein Warten auf moveend).
    ensureChunksInView().then(() => refreshLocationsInView()).catch(() => {});
  } catch (e) {
    // Fehler: Meldung anzeigen, Button reaktivieren — kein Frozen-State.
    errEl.textContent = {
      geocode_failed: "Adresse nicht gefunden — bitte Koordinaten manuell angeben (Abschnitt unten).",
    }[e.message] || ("Fehler: " + e.message);
    errEl.classList.remove("hidden");
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
