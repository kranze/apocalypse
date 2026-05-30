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

function styleFor(loc) {
  const c = colorFor(loc.type);
  if (loc.discovery_status === "undiscovered") {
    return { radius: 5, color: c, weight: 1, fillColor: c, fillOpacity: 0.12 };
  }
  if (loc.discovery_status === "depleted") {
    return { radius: 5, color: "#444", weight: 1, fillColor: "#222", fillOpacity: 0.6 };
  }
  return { radius: 6, color: "#fff", weight: 1.5, fillColor: c, fillOpacity: 0.9 };
}

// --- Zustand -----------------------------------------------------------
let map;
let playerMarker;
let routeLine = null;
const markers = new Map(); // id -> L.circleMarker
const locData = new Map(); // id -> location
let selectedId = null;

// Zeitfluss
let speed = 0;          // 0 = Pause; sonst Spielminuten pro Frame
let frameBusy = false;  // verhindert überlappende Tick-Requests
const FRAME_MS = 1000;

function log(msg, severity) {
  const ul = document.getElementById("log");
  const li = document.createElement("li");
  li.textContent = msg;
  if (severity) li.className = severity;
  ul.prepend(li);
  while (ul.children.length > 40) ul.removeChild(ul.lastChild);
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
    setBar("perf-fill", "perf-val", p.performance);
    if (p.lat != null && p.lon != null) movePlayerMarker(p.lat, p.lon);
    updateRoute(p);
    document.getElementById("btn-eat").disabled = !p.is_alive;
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
async function loadLocations() {
  const locs = await getJSON("/locations");
  for (const loc of locs) {
    locData.set(loc.id, loc);
    const m = L.circleMarker([loc.lat, loc.lon], styleFor(loc));
    m.on("click", (e) => { L.DomEvent.stopPropagation(e); selectLocation(loc.id); });
    m.addTo(map);
    markers.set(loc.id, m);
  }
  log(`${locs.length} Orte geladen.`);
}

function updateLocation(loc) {
  locData.set(loc.id, loc);
  const m = markers.get(loc.id);
  if (m) m.setStyle(styleFor(loc));
}

// --- Auswahl-Panel -----------------------------------------------------
async function selectLocation(id) {
  selectedId = id;
  const loc = locData.get(id);
  const panel = document.getElementById("panel");
  panel.classList.remove("hidden");
  document.getElementById("panel-title").textContent = loc.name || loc.type;
  const dot = `<span class="legend-dot" style="background:${colorFor(loc.type)}"></span>`;
  document.getElementById("panel-meta").innerHTML =
    `${dot}${loc.type} · ${loc.discovery_status}` +
    (loc.footprint_m2 ? ` · ${Math.round(loc.footprint_m2)} m²` : "");

  const actions = document.getElementById("panel-actions");
  actions.innerHTML = "";
  const inv = document.getElementById("panel-inv");
  inv.innerHTML = "";

  if (loc.discovery_status === "undiscovered") {
    const b = document.createElement("button");
    b.textContent = "Betreten";
    b.onclick = () => doDiscover(id);
    actions.appendChild(b);
  } else {
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
    log(`Geplündert: ${loc.name || loc.type} (${n} Item-Arten) → ${r.status}.`);
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
  // Nach Ticks können sich Bestände/Status geändert haben (Verderb) – Status neu laden.
  const locs = await getJSON("/locations");
  for (const loc of locs) updateLocation(loc);
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
async function doEat() {
  try {
    const r = await postJSON(`/characters/${PLAYER_ID}/eat`, {});
    log(`Gegessen: ${r.item} (+${r.kcal} kcal).`);
    await refreshState();
  } catch (e) {
    log(e.message === "no_food" ? "Nichts (Essbares) im Rucksack." : "Fehler: " + e.message, "soft");
  }
}
async function doPrepare() {
  try {
    const r = await postJSON(`/characters/${PLAYER_ID}/prepare`, {});
    log(`Zubereitet: ${r.prepared} (aus ${r.from}, ${r.water_used} L Wasser).`);
    await refreshInventory();
  } catch (e) {
    const msg = {
      no_heat: "Keine Hitzequelle (Feuerholz fehlt).",
      no_water: "Nicht genug Wasser.",
      nothing_to_prepare: "Nichts zuzubereiten.",
    }[e.message] || ("Fehler: " + e.message);
    log(msg, "soft");
  }
}

// --- Init --------------------------------------------------------------
async function init() {
  const info = await getJSON("/api/info");
  map = L.map("map", { zoomControl: true }).setView(info.center, 16);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap",
  }).addTo(map);

  map.on("click", (e) => walkTo(e.latlng.lat, e.latlng.lng));

  for (const b of document.querySelectorAll("#speedbar button")) {
    b.onclick = () => setSpeed(Number(b.dataset.speed));
  }
  document.getElementById("btn-ff").onclick = doFastForward;
  document.getElementById("btn-eat").onclick = doEat;
  document.getElementById("btn-prep").onclick = doPrepare;
  document.getElementById("btn-roster").onclick = toggleRoster;
  document.getElementById("panel-close").onclick = () => {
    document.getElementById("panel").classList.add("hidden");
    selectedId = null;
  };

  await loadLocations();
  await refreshState();
  setInterval(tickFrame, FRAME_MS); // kontinuierlicher Zeitfluss (gesteuert über speed)
}

init().catch((e) => log("Init-Fehler: " + e.message, "decision"));
