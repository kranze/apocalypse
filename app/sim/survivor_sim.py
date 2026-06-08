"""Globaler Tages-Tick für lebende Survivors: Gravitation/Geruch + Gruppenbildung.

Eisernes Prinzip: deterministisch aus (world_seed, survivor_id, day).
Nur Sim-Kern schreibt. Kein O(n²): Dichtefeld als numpy-Array, Spatial-Grid
für Gruppen. Survivors sind kein Ressourcen-Gut: kein resource_ledger-Eintrag.

Phase früh (day < T_FLEE): Bewegung Dichte-Gradient hinauf (Urbanisierung).
Phase spät (smell > SMELL_THRESHOLD): Bewegung Dichte-Gradient hinunter (Flucht).
Gruppenbildung: Survivors/Gruppen innerhalb MEET_DIST_KM verschmelzen.

step_day(conn, day) ist die öffentliche API; wird von tick.advance_tick() aufgerufen.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

import numpy as np

from ..db import get_world_seed
from .constants import (
    SURVIVOR_AGE_THRESHOLDS,
    SURVIVOR_ADULT_MAX_AGE,
    SURVIVOR_ADULT_MIN_AGE,
    SURVIVOR_BASE_SURVIVE_PER_DAY,
    SURVIVOR_BUCKET_DEG,
    SURVIVOR_CHILD_MAX_AGE,
    SURVIVOR_GRAVITY_WEIGHT,
    SURVIVOR_GROUP_BOOST_BASE,
    SURVIVOR_GROUP_BOOST_CAP,
    SURVIVOR_GROUP_BOOST_PER_ADULT,
    SURVIVOR_MAX_STEP_KM,
    SURVIVOR_MEET_DIST_KM,
    SURVIVOR_NOISE_WEIGHT,
    SURVIVOR_RAMP_DAYS,
    SURVIVOR_SMELL_THRESHOLD,
    SURVIVOR_SOCIAL_WEIGHT,
    SURVIVOR_T_FLEE,
)
from .popgrid import load_grid

# ---------------------------------------------------------------------------
# Modul-Cache: Dichtefeld wird einmal pro Prozess gebaut.
# ---------------------------------------------------------------------------
_density_field: dict[str, Any] | None = None


def _build_density_field() -> dict[str, Any]:
    """Baut ein 2-D-numpy-Array aus dem Pop-Gitter (0,25°-Zellen → 1°-Zellen).

    Gibt ein Dict zurück mit:
      grid:     2-D float32-Array [lat_idx, lon_idx]
      lat_min:  kleinste Gitter-Latitude  (Zellzentrum)
      lon_min:  kleinste Gitter-Longitude (Zellzentrum)
      cell_deg: Zellgröße in Grad (1° für das echte Pop-Gitter)
    """
    global _density_field
    if _density_field is not None:
        return _density_field

    raw = load_grid()  # list[(lat, lon, weight)]
    if not raw:
        # Leerer Fallback (nur im Test)
        _density_field = {
            "grid": np.zeros((1, 1), dtype=np.float32),
            "lat_min": 0.0,
            "lon_min": 0.0,
            "cell_deg": 1.0,
        }
        return _density_field

    lats = np.array([r[0] for r in raw], dtype=np.float32)
    lons = np.array([r[1] for r in raw], dtype=np.float32)
    weights = np.array([r[2] for r in raw], dtype=np.float32)

    # Zellgröße aus dem Gitter ableiten (Median-Differenz sortierter Werte)
    u_lats = np.unique(lats)
    u_lons = np.unique(lons)
    cell_deg: float = float(np.median(np.diff(u_lats))) if len(u_lats) > 1 else 1.0

    lat_min = float(u_lats.min())
    lon_min = float(u_lons.min())

    lat_n = int(round((float(u_lats.max()) - lat_min) / cell_deg)) + 1
    lon_n = int(round((float(u_lons.max()) - lon_min) / cell_deg)) + 1

    grid = np.zeros((lat_n, lon_n), dtype=np.float32)

    lat_idx = np.round((lats - lat_min) / cell_deg).astype(np.int32)
    lon_idx = np.round((lons - lon_min) / cell_deg).astype(np.int32)

    # Clamp sicherheitshalber
    lat_idx = np.clip(lat_idx, 0, lat_n - 1)
    lon_idx = np.clip(lon_idx, 0, lon_n - 1)

    grid[lat_idx, lon_idx] = weights

    _density_field = {
        "grid": grid,
        "lat_min": lat_min,
        "lon_min": lon_min,
        "cell_deg": cell_deg,
    }
    return _density_field


def _density_at(field: dict[str, Any], lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Dichte D für N Punkte (N-Vektor). Nearest-Neighbor-Lookup."""
    grid: np.ndarray = field["grid"]
    lat_min: float = field["lat_min"]
    lon_min: float = field["lon_min"]
    cell_deg: float = field["cell_deg"]
    lat_n, lon_n = grid.shape

    li = np.round((lats - lat_min) / cell_deg).astype(np.int32)
    oi = np.round((lons - lon_min) / cell_deg).astype(np.int32)
    li = np.clip(li, 0, lat_n - 1)
    oi = np.clip(oi, 0, lon_n - 1)
    return grid[li, oi]


def _gradient_at(
    field: dict[str, Any], lats: np.ndarray, lons: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Dichte-Gradient (dlat, dlon) für N Punkte via finite Differenzen.

    Gibt normalisierte Einheitsvektoren zurück (0 bei flachem Gradient).
    """
    grid: np.ndarray = field["grid"]
    lat_min: float = field["lat_min"]
    lon_min: float = field["lon_min"]
    cell_deg: float = field["cell_deg"]
    lat_n, lon_n = grid.shape

    li = np.round((lats - lat_min) / cell_deg).astype(np.int32)
    oi = np.round((lons - lon_min) / cell_deg).astype(np.int32)
    li = np.clip(li, 0, lat_n - 1)
    oi = np.clip(oi, 0, lon_n - 1)

    # Nachbarzellen mit Clamp
    li_p = np.clip(li + 1, 0, lat_n - 1)
    li_m = np.clip(li - 1, 0, lat_n - 1)
    oi_p = np.clip(oi + 1, 0, lon_n - 1)
    oi_m = np.clip(oi - 1, 0, lon_n - 1)

    dlat = (grid[li_p, oi].astype(np.float64) - grid[li_m, oi].astype(np.float64)) / 2.0
    dlon = (grid[li, oi_p].astype(np.float64) - grid[li, oi_m].astype(np.float64)) / 2.0

    # Normieren: Einheitsvektor (0-Vektor bleibt 0)
    mag = np.sqrt(dlat ** 2 + dlon ** 2)
    nonzero = mag > 0.0
    dlat[nonzero] /= mag[nonzero]
    dlon[nonzero] /= mag[nonzero]
    return dlat, dlon


# ---------------------------------------------------------------------------
# O(n) Gruppenbildung via Union-Find über Gitterzellen
# ---------------------------------------------------------------------------
# Approximation: Jeder lebende Survivor wird einer feinen Gitterzelle mit
# Kantenlänge ≈ MEET_DIST_KM zugeordnet.  Der Zellschlüssel ist
#   (round(lat / cell_deg_lat), round(lon / cell_deg_lon))
# wobei cell_deg_lat = meet_km / 111.32 und
#       cell_deg_lon = meet_km / (111.32 * cos(ref_lat)).
# Danach wird Union-Find NUR über die belegten ZELLEN durchgeführt:
# jede belegte Zelle wird mit ihren 8 Nachbarzellen geuniert, sofern belegt.
# Komplexität: O(n) für Zuweisung + O(k·α) für Union-Find (k = belegte Zellen).
# Border-Fälle (zwei Survivors nahe Zellengrenze, aber in verschiedenen Zellen)
# werden durch die 8-Nachbar-Union korrekt erfasst, sofern sie innerhalb einer
# Zellenbreite liegen — was bei meet_km-großen Zellen der Fall ist.

def _merge_groups(
    ids: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    group_ids: np.ndarray,
    meet_km: float,
    bucket_deg: float,  # ungenutzt (Kompatibilität), Zellgröße wird aus meet_km berechnet
    conn: sqlite3.Connection,
    current_tick: int,
) -> np.ndarray:
    """O(n)-Gruppierung via Union-Find über Gitterzellen.

    Jede belegte Gitterzelle (Kantenlänge ≈ meet_km) wird mit ihren 8
    Nachbarzellen geuniert.  Alle Survivors einer zusammenhängenden Komponente
    erhalten dieselbe group_id (kleinste Survivor-id der Komponente als Anker).
    Neue Gruppen werden in `survivor_groups` angelegt; bestehende bleiben.
    Gibt aktualisiertes `group_ids`-Array zurück.
    """
    n = len(ids)
    lats_list = lats.tolist()
    lons_list = lons.tolist()
    ids_list = ids.tolist()
    gids_list = group_ids.tolist()

    # Gitterzellgröße in Grad
    # Lat-Richtung: 1° ≈ 111.32 km (konstant)
    cell_deg_lat = meet_km / 111.32
    # Lon-Richtung: 1° ≈ 111.32 * cos(lat) km; wir nehmen einen globalen
    # Referenz-Breitengrad (Mittel der Population) für eine gute Näherung.
    # Einzelne Survivors nahe den Polen könnten leicht falsch zugeordnet werden,
    # aber das ist für realistische Populationen (< ±80°) vernachlässigbar.
    ref_lat = float(lats.mean()) if n > 0 else 0.0
    cos_ref = max(math.cos(math.radians(ref_lat)), 0.01)  # min. cos für hohe Breiten
    cell_deg_lon = meet_km / (111.32 * cos_ref)

    # Schritt 1: Survivor → Gitterzelle zuordnen
    # Zellschlüssel = (gerundeter lat-Index, gerundeter lon-Index)
    survivor_cell: list[tuple[int, int]] = []
    for i in range(n):
        ci = int(round(lats_list[i] / cell_deg_lat))
        cj = int(round(lons_list[i] / cell_deg_lon))
        survivor_cell.append((ci, cj))

    # Schritt 2: Belegte Zellen ermitteln und durchnummerieren
    occupied_cells: dict[tuple[int, int], int] = {}  # Zelle → Zellen-Index
    for cell in survivor_cell:
        if cell not in occupied_cells:
            occupied_cells[cell] = len(occupied_cells)

    n_cells = len(occupied_cells)

    # Schritt 3: Union-Find über Zellen (nicht über Survivors)
    cell_parent: list[int] = list(range(n_cells))
    cell_rank: list[int] = [0] * n_cells

    def _find(x: int) -> int:
        root = x
        while cell_parent[root] != root:
            root = cell_parent[root]
        # Pfadkompression
        while cell_parent[x] != root:
            cell_parent[x], x = root, cell_parent[x]
        return root

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra == rb:
            return
        # Union by rank
        if cell_rank[ra] < cell_rank[rb]:
            ra, rb = rb, ra
        cell_parent[rb] = ra
        if cell_rank[ra] == cell_rank[rb]:
            cell_rank[ra] += 1

    # Jede belegte Zelle mit ihren 8 Nachbarzellen unieren
    neighbor_offsets = [
        (-1, -1), (-1, 0), (-1, 1),
        ( 0, -1),           ( 0, 1),
        ( 1, -1), ( 1, 0), ( 1, 1),
    ]
    for (ci, cj), idx in occupied_cells.items():
        for dci, dcj in neighbor_offsets:
            neighbor = (ci + dci, cj + dcj)
            if neighbor in occupied_cells:
                _union(idx, occupied_cells[neighbor])

    # Schritt 4: Survivor → Zellen-Wurzel → Komponente
    # Pro Komponente (Zellen-Root): kleinste Survivor-id als Anker für
    # deterministischen Gruppen-Anker.
    root_to_min_id: dict[int, int] = {}   # Zellen-Root → kleinste Survivor-id (index)
    root_members_idx: dict[int, list[int]] = {}  # Zellen-Root → [Survivor-Indizes]

    cell_roots = {cell: _find(idx) for cell, idx in occupied_cells.items()}

    for i in range(n):
        cell = survivor_cell[i]
        root = cell_roots[cell]
        root_members_idx.setdefault(root, []).append(i)
        sid = ids_list[i]
        if root not in root_to_min_id or sid < root_to_min_id[root]:
            root_to_min_id[root] = i  # speichern als Array-Index des Survivors

    # Schritt 5: DB-group_id je Komponente bestimmen
    # - Wenn mind. 2 Survivors in Komponente:
    #     - Vorhandene group_ids recyceln (kleinste); sonst neue Gruppe anlegen.
    # - Einzelne Survivor: group_id = 0 (kein Gruppen-Eintrag).
    #
    # Optimierung: Alle neuen Gruppen in einem einzigen executemany-Aufruf
    # anlegen statt n×(INSERT + SELECT last_insert_rowid()). Dazu:
    # 1. Aktuellen MAX(id) aus survivor_groups holen.
    # 2. Neue Gruppen nummerieren (max_id + 1, max_id + 2, ...).
    # 3. Alle auf einmal via executemany einfügen (mit expliziten IDs).
    max_gid_row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM survivor_groups;"
    ).fetchone()[0]
    next_gid = int(max_gid_row) + 1

    # Zwei-Pass: erst recyceln/sammeln, dann Batch-Insert.
    root_to_db_group: dict[int, int] = {}
    new_group_rows: list[tuple] = []  # (id, created_tick, clat, clon)

    for root, members in root_members_idx.items():
        if len(members) < 2:
            root_to_db_group[root] = 0
            continue
        existing = [gids_list[i] for i in members if gids_list[i] > 0]
        if existing:
            root_to_db_group[root] = min(existing)
        else:
            # Neue Gruppe: Zentroid aus Mitgliedern
            nm = len(members)
            clat = sum(lats_list[i] for i in members) / nm
            clon = sum(lons_list[i] for i in members) / nm
            root_to_db_group[root] = next_gid
            new_group_rows.append((next_gid, current_tick, clat, clon))
            next_gid += 1

    if new_group_rows:
        conn.executemany(
            "INSERT INTO survivor_groups (id, created_tick, lat, lon) VALUES (?, ?, ?, ?);",
            new_group_rows,
        )

    # Schritt 6: Neue group_ids als Array zurückgeben
    new_gids_list = [0] * n
    for i in range(n):
        cell = survivor_cell[i]
        root = cell_roots[cell]
        new_gids_list[i] = root_to_db_group[root]

    return np.array(new_gids_list, dtype=np.int64)


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------

def step_day(conn: sqlite3.Connection, day: int) -> None:
    """Führt einen Simulations-Tag für alle lebenden Survivors durch.

    Lädt alle lebenden Survivors, berechnet neue Positionen (Gravitation oder
    Flucht je nach Phase + Geruchsschwelle) und Gruppen, schreibt per
    Bulk-executemany zurück. Alles in einer Transaktion (wird von advance_tick
    bereits in einer with conn:-Transaktion aufgerufen — SQLite nested OK).

    Parameters
    ----------
    conn:
        Offene DB-Verbindung des Sim-Kerns.
    day:
        Spieltag (ganzzahlig, 0-basiert).
    """
    # Alle lebenden Survivors laden (inkl. birth_tick für Sterbe-Modell)
    rows = conn.execute(
        "SELECT id, lat, lon, group_id, birth_tick FROM survivors WHERE alive = 1;"
    ).fetchall()

    if not rows:
        # Bug 2-Fix: survivor_sim_day auch bei leerer Population fortschreiben,
        # damit kein Tag doppelt verarbeitet wird.
        conn.execute("UPDATE world SET survivor_sim_day = ? WHERE id = 1;", (day,))
        return

    world_seed = get_world_seed(conn)
    current_tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]

    n = len(rows)
    ids = np.array([r["id"] for r in rows], dtype=np.int64)
    lats = np.array([r["lat"] for r in rows], dtype=np.float64)
    lons = np.array([r["lon"] for r in rows], dtype=np.float64)
    group_ids = np.array([r["group_id"] if r["group_id"] is not None else 0 for r in rows], dtype=np.int64)

    # --- Schritt 1: Gruppenbildung auf AKTUELLEN Positionen ---------------
    # Survivors innerhalb MEET_DIST_KM verschmelzen, bevor sie sich bewegen.
    # So wandern Gruppe-Mitglieder als Einheit (gemeinsamer Zentroid).
    new_group_ids = _merge_groups(
        ids, lats, lons, group_ids,
        SURVIVOR_MEET_DIST_KM, SURVIVOR_BUCKET_DEG,
        conn, current_tick,
    )

    # --- Schritt 2: Bewegung (vektorisiert, Gruppe als Einheit) -----------
    field = _build_density_field()

    # Für Gruppen: Bewegungsrichtung aus dem ZENTROID der Gruppe berechnen.
    # Einzelne Survivors (group_id=0) bewegen sich aus ihrer eigenen Position.
    # Vektorisiert via bincount (O(n), kein Python-Loop über Gruppen).
    in_group_mask = new_group_ids > 0
    rep_lats = lats.copy()
    rep_lons = lons.copy()

    if np.any(in_group_mask):
        # Remapping: group_ids → dichte 0-basierte Indizes
        gids_in = new_group_ids[in_group_mask]
        unique_groups, inv_g = np.unique(gids_in, return_inverse=True)
        n_groups = len(unique_groups)
        counts_g = np.bincount(inv_g, minlength=n_groups).astype(np.float64)
        # Gruppen-Zentroide via bincount (sicher: counts_g > 0 da jeder Eintrag belegt)
        sum_lat = np.bincount(inv_g, weights=lats[in_group_mask], minlength=n_groups)
        sum_lon = np.bincount(inv_g, weights=lons[in_group_mask], minlength=n_groups)
        mean_lat_g = sum_lat / counts_g
        mean_lon_g = sum_lon / counts_g
        # rep-Arrays mit Gruppen-Zentroid befüllen
        rep_lats[in_group_mask] = mean_lat_g[inv_g]
        rep_lons[in_group_mask] = mean_lon_g[inv_g]
    else:
        unique_groups = np.empty(0, dtype=np.int64)

    dlat_grad, dlon_grad = _gradient_at(field, rep_lats, rep_lons)
    d_local = _density_at(field, rep_lats, rep_lons).astype(np.float64)

    # --- Phase bestimmen -------------------------------------------------
    ramp = min(day / max(SURVIVOR_RAMP_DAYS, 1), 1.0)
    smell = d_local * ramp

    # Modus je Survivor: 1 = Gravitation (hinauf), -1 = Flucht (hinunter)
    if day < SURVIVOR_T_FLEE:
        mode = np.ones(n, dtype=np.float64)   # alle Gravitation
    else:
        mode = np.where(smell > SURVIVOR_SMELL_THRESHOLD, -1.0, 1.0)

    # --- Soziale Anziehung (Richtung zum Gesamtzentroid) -----------------
    center_lat = lats.mean()
    center_lon = lons.mean()
    soc_dlat = center_lat - rep_lats
    soc_dlon = center_lon - rep_lons
    soc_mag = np.sqrt(soc_dlat ** 2 + soc_dlon ** 2)
    nonzero = soc_mag > 0.0
    soc_dlat[nonzero] /= soc_mag[nonzero]
    soc_dlon[nonzero] /= soc_mag[nonzero]

    # --- Deterministischer Rausch ----------------------------------------
    # Rausch-Salt: je Gruppe die kleinste Survivor-id (deterministisch).
    # Vektorisiert via np.minimum.at (O(n), kein Python-Loop über Gruppen).
    rng_seed_ids = ids.copy()
    if np.any(in_group_mask):
        # group_min_id[g] = min(ids) für Gruppe g (0-basierter Index)
        group_min_id = np.full(n_groups, np.iinfo(np.int64).max, dtype=np.int64)
        np.minimum.at(group_min_id, inv_g, ids[in_group_mask])
        rng_seed_ids[in_group_mask] = group_min_id[inv_g]

    # Deterministischer Rausch: O(1) RNG-Aufrufe statt O(n).
    # Strategie: Einen einzelnen RNG mit (world_seed, day, salt=4) seeden und
    # 2*n Werte generieren. Die Abbildung salt_id → Rausch-Wert ist über den
    # sortierten Rang der unique salts definiert — reihenfolgeunabhängig und
    # stabil bei gleicher (world_seed, day)-Kombination.
    # Determinismus: gleiche (world_seed, day) + gleiche IDs → identischer Rausch.
    # Salt=4 trennt Bewegungs-RNG vom Sterbe-RNG (Salt=0).
    unique_salts, inv_salt = np.unique(rng_seed_ids, return_inverse=True)
    n_salts = len(unique_salts)
    rng_noise = np.random.default_rng(
        np.random.SeedSequence([int(world_seed), int(day), 4])
    )
    # n_salts Paare: [noise_lat_0, noise_lon_0, noise_lat_1, noise_lon_1, ...]
    raw_noise = rng_noise.uniform(-1.0, 1.0, size=n_salts * 2)
    noise_lat = raw_noise[inv_salt * 2]
    noise_lon = raw_noise[inv_salt * 2 + 1]

    # --- Bewegungsrichtung zusammensetzen --------------------------------
    dir_lat = (
        SURVIVOR_GRAVITY_WEIGHT * mode * dlat_grad
        + SURVIVOR_SOCIAL_WEIGHT * soc_dlat
        + SURVIVOR_NOISE_WEIGHT * noise_lat
    )
    dir_lon = (
        SURVIVOR_GRAVITY_WEIGHT * mode * dlon_grad
        + SURVIVOR_SOCIAL_WEIGHT * soc_dlon
        + SURVIVOR_NOISE_WEIGHT * noise_lon
    )

    # Normieren
    dir_mag = np.sqrt(dir_lat ** 2 + dir_lon ** 2)
    nonzero = dir_mag > 0.0
    dir_lat[nonzero] /= dir_mag[nonzero]
    dir_lon[nonzero] /= dir_mag[nonzero]

    # --- Schrittweite begrenzen (MAX_STEP_KM) ----------------------------
    km_per_deg_lat = 111.32
    cos_lat = np.cos(np.radians(rep_lats))
    km_per_deg_lon = 111.32 * cos_lat

    step_lat_deg = dir_lat * (SURVIVOR_MAX_STEP_KM / km_per_deg_lat)
    step_lon_deg = dir_lon * (SURVIVOR_MAX_STEP_KM / np.maximum(km_per_deg_lon, 1e-6))

    # Neue Positionen: Individuen bewegen sich von ihren EIGENEN Koordinaten,
    # aber mit der Richtung ihres Gruppen-Repräsentanten (Zentroid).
    new_lats = np.clip(lats + step_lat_deg, -90.0, 90.0)
    new_lons = lons + step_lon_deg
    new_lons = ((new_lons + 180.0) % 360.0) - 180.0

    # Gruppen-Zentroide in survivor_groups aktualisieren (nach Bewegung)
    # Vektorisiert: executemany statt per-Gruppe execute
    if len(unique_groups) > 0:
        sum_nlat = np.bincount(inv_g, weights=new_lats[in_group_mask], minlength=n_groups)
        sum_nlon = np.bincount(inv_g, weights=new_lons[in_group_mask], minlength=n_groups)
        mean_nlat_g = sum_nlat / counts_g
        mean_nlon_g = sum_nlon / counts_g
        group_update_rows = [
            (float(mean_nlat_g[k]), float(mean_nlon_g[k]), int(unique_groups[k]))
            for k in range(n_groups)
        ]
        conn.executemany(
            "UPDATE survivor_groups SET lat = ?, lon = ? WHERE id = ?;",
            group_update_rows,
        )

    # --- Bulk-Update: Positionen + group_id schreiben --------------------
    update_rows = [
        (
            float(new_lats[i]),
            float(new_lons[i]),
            int(new_group_ids[i]) if new_group_ids[i] > 0 else None,
            int(ids[i]),
        )
        for i in range(n)
    ]
    conn.executemany(
        "UPDATE survivors SET lat = ?, lon = ?, group_id = ? WHERE id = ?;",
        update_rows,
    )

    # --- Schritt 3: Sterbe-Modell (vektorisiert) --------------------------
    # birth_tick ist bereits in `rows` geladen (SELECT enthält birth_tick).
    # Defensiv: NULL birth_tick wird als Erwachsener (30 Jahre = -15_768_000 min)
    # behandelt, NICHT als Säugling (Alter 0). Nach spawn_survivors(force=True)
    # sollte birth_tick nie NULL sein – dies ist nur ein Sicherheitsnetz.
    _FALLBACK_BIRTH_TICK = -(30 * 525_600)  # 30 Jahre vor Kollaps
    birth_ticks_arr = np.array(
        [r["birth_tick"] if r["birth_tick"] is not None else _FALLBACK_BIRTH_TICK for r in rows],
        dtype=np.int64,
    )

    # Alter in Jahren
    age_years = (current_tick - birth_ticks_arr) / 525_600.0

    # Altersklassen-Index per np.searchsorted (vollständig vektorisiert)
    thresholds = np.array(SURVIVOR_AGE_THRESHOLDS, dtype=np.float64)
    age_class = np.searchsorted(thresholds, age_years, side="right") - 1
    age_class = np.clip(age_class, 0, len(SURVIVOR_AGE_THRESHOLDS) - 1)

    # Basis-p_survive je Altersklasse
    base_p_arr = np.array(SURVIVOR_BASE_SURVIVE_PER_DAY, dtype=np.float64)
    p_base = base_p_arr[age_class]

    # group_ids: direkt aus new_group_ids (gleiche Reihenfolge wie ids/rows)
    # Boolesches Masken-Array: ist Survivor Erwachsener (13–64)?
    is_adult = (age_years >= SURVIVOR_ADULT_MIN_AGE) & (age_years < SURVIVOR_ADULT_MAX_AGE)
    is_child = age_years < SURVIVOR_CHILD_MAX_AGE

    # Anzahl Erwachsener je group_id — vollständig vektorisiert.
    # boost-Vektor startet bei 1.0 (= kein Boost = Allein-Wahrscheinlichkeit).
    boost = np.ones(n, dtype=np.float64)
    in_group = new_group_ids > 0
    if np.any(in_group):
        # n_adults je group_id via bincount
        group_ids_in = new_group_ids[in_group]
        adults_in = is_adult[in_group].astype(np.float64)
        unique_gids, inv_idx = np.unique(group_ids_in, return_inverse=True)
        n_adults_arr = np.bincount(inv_idx, weights=adults_in)
        # n_adults je Survivor in Gruppe (shape = n_in_group,)
        n_adults_per_survivor = n_adults_arr[inv_idx]

        # Boost-Formel vektorisiert:
        # n_adults > 0             → clamp(BOOST_BASE + BOOST_PER*(n-1), 1, CAP)
        # n_adults == 0, !is_child → BOOST_BASE
        # n_adults == 0, is_child  → 1.0 (kein Boost)
        boost_vals = np.where(
            n_adults_per_survivor > 0,
            np.clip(
                SURVIVOR_GROUP_BOOST_BASE
                + SURVIVOR_GROUP_BOOST_PER_ADULT * (n_adults_per_survivor - 1),
                1.0,
                SURVIVOR_GROUP_BOOST_CAP,
            ),
            np.where(
                ~is_child[in_group],
                np.float64(SURVIVOR_GROUP_BOOST_BASE),
                1.0,
            ),
        )
        boost[in_group] = boost_vals

    # Endgültige p_survive: p_survive_boosted = 1 - (1 - p_base) / boost
    die_prob = 1.0 - p_base
    die_prob_boosted = die_prob / boost
    p_survive_final = np.clip(1.0 - die_prob_boosted, 0.0, 1.0)

    # Bug 1-Fix: Echte, uniforme, deterministische Ziehung via np.random.default_rng.
    # SeedSequence([world_seed, day]) garantiert: gleicher Seed+Tag → identische Werte.
    # ids sind bereits aufsteigend sortiert (SELECT ... ORDER BY id; implizit durch DB-Rowid),
    # aber wir sortieren explizit nach id für garantierte stabile Reihenfolge.
    sort_order = np.argsort(ids, kind="stable")
    ids_sorted = ids[sort_order]
    rng_death = np.random.default_rng(
        np.random.SeedSequence([int(world_seed), int(day)])
    )
    rand_vals_sorted = rng_death.random(n)
    # Zurück auf die ursprüngliche Reihenfolge mappen
    inv_order = np.empty(n, dtype=np.int64)
    inv_order[sort_order] = np.arange(n, dtype=np.int64)
    rand_vals = rand_vals_sorted[inv_order]

    # Stirbt wenn rand_val > p_survive_final
    dies_mask = rand_vals > p_survive_final
    dead_ids = ids[dies_mask]

    if len(dead_ids) > 0:
        # Tote: alive=0 setzen, group_id NULLen (Bulk via executemany)
        dead_list = [(int(did),) for did in dead_ids]
        conn.executemany(
            "UPDATE survivors SET alive = 0, group_id = NULL WHERE id = ?;",
            dead_list,
        )

        # Leere / Einzel-Gruppen auflösen: In-Memory-Berechnung (kein DB-Query).
        # Wir wissen für jede beteiligte Gruppe, wie viele Survivor nach dem Tod
        # noch leben — direkt aus den numpy-Arrays.
        affected_mask = dies_mask & in_group_mask
        if np.any(affected_mask):
            affected_gids = np.unique(new_group_ids[affected_mask])
            # survivors_alive_after[i] = 1 wenn überlebt hat
            alive_after = ~dies_mask  # bool-Maske: überlebt?

            # Für jede betroffene Gruppe: Anzahl lebender Mitglieder zählen (in-memory)
            # Vektorisiert: Für jede gid die Maske aufbauen + sum
            # Da affected_gids typischerweise klein (Hunderte bis wenige Tausende),
            # ist dies ausreichend schnell ohne per-gid DB-Query.
            groups_to_delete: list[int] = []
            survivors_to_ungroup: list[tuple] = []  # (gid,) für UPDATE

            for gid in affected_gids.tolist():
                group_mask = new_group_ids == gid
                n_alive = int((group_mask & alive_after).sum())
                if n_alive == 0:
                    groups_to_delete.append(gid)
                elif n_alive == 1:
                    survivors_to_ungroup.append((int(gid),))
                    groups_to_delete.append(gid)

            if survivors_to_ungroup:
                conn.executemany(
                    "UPDATE survivors SET group_id = NULL WHERE group_id = ? AND alive = 1;",
                    survivors_to_ungroup,
                )
            if groups_to_delete:
                conn.executemany(
                    "DELETE FROM survivor_groups WHERE id = ?;",
                    [(int(gid),) for gid in groups_to_delete],
                )

    # survivor_sim_day aktualisieren
    conn.execute("UPDATE world SET survivor_sim_day = ? WHERE id = 1;", (day,))


def population_stats(conn: sqlite3.Connection) -> dict:
    """Gibt Kurzstatistik der Survivor-Population zurück.

    Returns
    -------
    dict with keys:
        alive:   int  — Anzahl lebender Survivors
        dead:    int  — Anzahl toter Survivors
        total:   int  — Gesamt (alive + dead)
        groups:  int  — Anzahl aktiver Gruppen (survivor_groups-Zeilen)
        grouped: int  — Anzahl lebender Survivors in einer Gruppe (group_id NOT NULL)
        alone:   int  — Anzahl lebender Survivors ohne Gruppe
    """
    row = conn.execute(
        "SELECT "
        "  COUNT(*) AS total,"
        "  SUM(CASE WHEN alive = 1 THEN 1 ELSE 0 END) AS alive,"
        "  SUM(CASE WHEN alive = 0 THEN 1 ELSE 0 END) AS dead,"
        "  SUM(CASE WHEN alive = 1 AND group_id IS NOT NULL THEN 1 ELSE 0 END) AS grouped "
        "FROM survivors;"
    ).fetchone()
    total = int(row["total"] or 0)
    alive = int(row["alive"] or 0)
    dead = int(row["dead"] or 0)
    grouped = int(row["grouped"] or 0)
    groups = conn.execute("SELECT COUNT(*) FROM survivor_groups;").fetchone()[0]
    return {
        "alive": alive,
        "dead": dead,
        "total": total,
        "groups": int(groups),
        "grouped": grouped,
        "alone": alive - grouped,
    }
