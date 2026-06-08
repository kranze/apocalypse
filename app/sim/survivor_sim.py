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
# Spatial-Grid für Gruppenbildung
# ---------------------------------------------------------------------------

def _spatial_buckets(
    lats: np.ndarray, lons: np.ndarray, bucket_deg: float
) -> dict[tuple[int, int], list[int]]:
    """Verteilt N Punkte in Zellen-Hash-Buckets."""
    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(len(lats)):
        key = (int(math.floor(lats[i] / bucket_deg)), int(math.floor(lons[i] / bucket_deg)))
        buckets.setdefault(key, []).append(i)
    return buckets


def _haversine_km_vec(
    lat1: np.ndarray, lon1: np.ndarray, lat2: float, lon2: float
) -> np.ndarray:
    """Haversine-Distanz in km von N Punkten zu einem Punkt."""
    R = 6371.0
    phi1 = np.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlam = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * math.cos(phi2) * np.sin(dlam / 2) ** 2
    return R * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _merge_groups(
    ids: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    group_ids: np.ndarray,
    meet_km: float,
    bucket_deg: float,
    conn: sqlite3.Connection,
    current_tick: int,
) -> np.ndarray:
    """Gruppiert nahegelegene Survivors via Spatial-Grid (kein O(n²)).

    Survivors innerhalb `meet_km` erhalten dieselbe `group_id`.
    Neue Gruppen werden in `survivor_groups` angelegt; Zentroide aktualisiert.
    Gibt aktualisiertes `group_ids`-Array zurück.
    """
    n = len(ids)

    # Union-Find mit plain Python-Listen (kein numpy-Overhead pro Zugriff)
    parent: list[int] = list(range(n))

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        # Pfadkompression
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Bucket-Hash für Spatial-Grid
    buckets: dict[tuple[int, int], list[int]] = {}
    lats_list = lats.tolist()
    lons_list = lons.tolist()
    inv_bucket = 1.0 / bucket_deg
    for i in range(n):
        key = (int(lats_list[i] * inv_bucket), int(lons_list[i] * inv_bucket))
        buckets.setdefault(key, []).append(i)

    # Bbox-Schwelle in Grad
    lat_thresh = meet_km / 111.0
    lon_thresh = meet_km / 55.5  # konservativ (bei ~50° Breite ~78 km/°)

    # Haversine-Konstanten
    R = 6371.0
    meet_km_sq = meet_km * meet_km  # für schnellen Vergleich (Approximation)
    deg2rad = math.pi / 180.0

    neighbor_offsets = [(0, 0), (0, 1), (1, 0), (1, -1), (1, 1)]
    for (bx, by), indices in buckets.items():
        for dbx, dby in neighbor_offsets:
            other_key = (bx + dbx, by + dby)
            other_indices = buckets.get(other_key)
            if other_indices is None:
                continue
            for i in indices:
                lat_i = lats_list[i]
                lon_i = lons_list[i]
                for j in other_indices:
                    if i >= j:
                        continue
                    dlat = abs(lat_i - lats_list[j])
                    if dlat > lat_thresh:
                        continue
                    dlon = abs(lon_i - lons_list[j])
                    if dlon > lon_thresh:
                        continue
                    # Haversine (inline für Speed)
                    phi1 = lat_i * deg2rad
                    dphi = (lats_list[j] - lat_i) * deg2rad
                    dlam = (lons_list[j] - lon_i) * deg2rad
                    a = (math.sin(dphi * 0.5) ** 2
                         + math.cos(phi1) * math.cos(lats_list[j] * deg2rad)
                         * math.sin(dlam * 0.5) ** 2)
                    if R * 2 * math.asin(math.sqrt(a)) <= meet_km:
                        union(i, j)

    # Roots als plain Python-Liste bestimmen
    roots = [find(i) for i in range(n)]

    # group_ids als Python-Liste für schnellen Zugriff
    gids_list = group_ids.tolist()

    # Für jeden Root: DB-group_id ermitteln oder neue anlegen
    root_members: dict[int, list[int]] = {}
    for i, r in enumerate(roots):
        root_members.setdefault(r, []).append(i)

    root_to_db_group: dict[int, int] = {}
    for r, members in root_members.items():
        # Vorhandene group_ids unter Mitgliedern (>0)
        existing = [gids_list[i] for i in members if gids_list[i] > 0]
        if existing:
            root_to_db_group[r] = min(existing)
        elif len(members) > 1:
            # Neue Gruppe anlegen
            clat = sum(lats_list[i] for i in members) / len(members)
            clon = sum(lons_list[i] for i in members) / len(members)
            conn.execute(
                "INSERT INTO survivor_groups (created_tick, lat, lon) VALUES (?, ?, ?);",
                (current_tick, clat, clon),
            )
            root_to_db_group[r] = conn.execute("SELECT last_insert_rowid();").fetchone()[0]
        else:
            root_to_db_group[r] = 0

    new_gids = np.array([root_to_db_group[r] for r in roots], dtype=np.int64)
    # Hinweis: Zentroide werden in step_day nach der Bewegung aktualisiert,
    # damit sie die finalen Positionen widerspiegeln.
    return new_gids


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
    # Wir bauen Repräsentanten-Arrays: für Gruppen-Mitglieder nutzen wir den
    # Gruppen-Zentroid als Ausgangspunkt für Gradient/Rausch.
    rep_lats = lats.copy()
    rep_lons = lons.copy()

    unique_groups = np.unique(new_group_ids[new_group_ids > 0])
    for gid in unique_groups:
        mask = new_group_ids == gid
        clat = float(lats[mask].mean())
        clon = float(lons[mask].mean())
        rep_lats[mask] = clat
        rep_lons[mask] = clon

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
    # Seed je Survivor: (world_seed XOR survivor_id * 2654435761) XOR day.
    # Gruppe: Rausch aus dem Survivor mit der kleinsten ID in der Gruppe
    # (deterministisch, verhindert Gruppen-Auseinanderdriften).
    # Deterministischer Rausch: je Gruppe den kleinsten survivor-id als Salt,
    # damit Gruppenmitglieder dieselbe Richtung haben.
    # Bug-1-Fix (Bewegung): np.random.default_rng mit SeedSequence statt LCG —
    # liefert echte Gleichverteilung, bleibt voll deterministisch.
    rng_seed_ids = ids.copy()
    for gid in unique_groups:
        mask = new_group_ids == gid
        min_id = int(ids[mask].min())
        rng_seed_ids[mask] = min_id

    # Eindeutige Einzel-Seeds je Survivor/Gruppe: (world_seed, day, salt=4, seed_id)
    # salt=4 trennt Bewegungs-RNG vom Sterbe-RNG (salt=0).
    noise_lat = np.empty(n, dtype=np.float64)
    noise_lon = np.empty(n, dtype=np.float64)
    seen_seed_ids: dict[int, tuple[float, float]] = {}
    for i in range(n):
        sid = int(rng_seed_ids[i])
        if sid in seen_seed_ids:
            noise_lat[i], noise_lon[i] = seen_seed_ids[sid]
        else:
            rng_mv = np.random.default_rng(
                np.random.SeedSequence([int(world_seed), int(day), 4, sid])
            )
            nl = float(rng_mv.uniform(-1.0, 1.0))
            no = float(rng_mv.uniform(-1.0, 1.0))
            seen_seed_ids[sid] = (nl, no)
            noise_lat[i] = nl
            noise_lon[i] = no

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
    for gid in unique_groups:
        mask = new_group_ids == gid
        conn.execute(
            "UPDATE survivor_groups SET lat = ?, lon = ? WHERE id = ?;",
            (float(new_lats[mask].mean()), float(new_lons[mask].mean()), int(gid)),
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
    birth_ticks_arr = np.array(
        [r["birth_tick"] if r["birth_tick"] is not None else 0 for r in rows],
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
        # Tote: alive=0 setzen, group_id NULLen (Bulk)
        dead_list = [(int(did),) for did in dead_ids]
        conn.executemany(
            "UPDATE survivors SET alive = 0, group_id = NULL WHERE id = ?;",
            dead_list,
        )

        # Leere / Einzel-Gruppen auflösen: betroffene group_ids sammeln
        affected_gids: set[int] = set(
            int(new_group_ids[i]) for i in np.where(dies_mask & in_group)[0]
        )
        for gid in affected_gids:
            still_alive = conn.execute(
                "SELECT COUNT(*) FROM survivors WHERE group_id = ? AND alive = 1;",
                (gid,),
            ).fetchone()[0]
            if still_alive == 0:
                conn.execute("DELETE FROM survivor_groups WHERE id = ?;", (gid,))
            elif still_alive == 1:
                # Einzelperson → Gruppe auflösen
                conn.execute(
                    "UPDATE survivors SET group_id = NULL "
                    "WHERE group_id = ? AND alive = 1;",
                    (gid,),
                )
                conn.execute("DELETE FROM survivor_groups WHERE id = ?;", (gid,))

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
