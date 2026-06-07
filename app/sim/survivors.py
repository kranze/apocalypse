"""Globale Überlebenden-Verteilung und lazy NPC-Materialisierung.

Eisernes Prinzip: Nur der Sim-Kern schreibt. Verteilung ist eine rein
deterministische Funktion aus world_seed + Bevölkerungsgitter. Survivor-Zeilen
sind KEIN Ressourcen-Gut: sie berühren weder resource_ledger noch resource_audit.
"""
from __future__ import annotations

import math
import random
import sqlite3
from typing import Sequence

from ..db import get_world_seed
from .identity import generate_identity
from .popgrid import load_grid


# Halbe Zellbreite des Bevölkerungsgitters (~0.25°-Zellen, also ±0.125°).
_HALF_CELL = 0.125


def spawn_survivors(
    conn: sqlite3.Connection,
    total: int = 100_000,
    seed: int | None = None,
) -> int:
    """Verteilt ``total`` Überlebende deterministisch über die Welt.

    Multinomiale Verteilung proportional zur Bevölkerungsdichte des Gitters.
    Innerhalb jeder Zelle wird die Position deterministisch gejittert.

    Idempotent: existieren bereits genau ``total`` Zeilen → nichts tun.
    Sonst: Tabelle leeren und neu erzeugen (sauberer Seed-Wechsel).

    Parameters
    ----------
    conn:
        Offene DB-Verbindung (Sim-Kern-Verbindung).
    total:
        Anzahl zu verteilender Überlebender.
    seed:
        Expliziter Seed; falls None wird ``db.get_world_seed(conn)`` genutzt.

    Returns
    -------
    int
        Anzahl eingefügter Zeilen (== total).
    """
    # Idempotenz-Check
    existing = conn.execute("SELECT COUNT(*) FROM survivors;").fetchone()[0]
    if existing == total:
        return total

    # Gitter laden (prozessweit gecacht)
    grid = load_grid()  # list[tuple[lat, lon, weight]]

    if seed is None:
        seed = get_world_seed(conn)

    rng = random.Random(seed)

    lats = [c[0] for c in grid]
    lons = [c[1] for c in grid]
    weights = [c[2] for c in grid]

    total_weight = sum(weights)

    # --- Multinomiale Verteilung via Alias-/Inversion-Sampling ----------------
    # Für 100k Punkte auf ~163k Zellen ist `random.choices` mit kumulativen
    # Gewichten ausreichend schnell und vollständig deterministisch.
    # Wir ziehen alle Zell-Indizes auf einmal.
    cum_weights: list[float] = []
    running = 0.0
    for w in weights:
        running += w
        cum_weights.append(running)

    # Ziehe total Zell-Indizes (deterministisch)
    cell_indices: list[int] = rng.choices(
        range(len(grid)),
        cum_weights=cum_weights,
        k=total,
    )

    # Jitter innerhalb der Zelle: deterministisch aus separatem RNG-State
    # (RNG läuft deterministisch weiter nach den choices-Calls)
    rows: list[tuple[float, float]] = []
    for cell_idx in cell_indices:
        cell_lat = lats[cell_idx]
        cell_lon = lons[cell_idx]
        jitter_lat = rng.uniform(-_HALF_CELL, _HALF_CELL)
        jitter_lon = rng.uniform(-_HALF_CELL, _HALF_CELL)
        rows.append((cell_lat + jitter_lat, cell_lon + jitter_lon))

    # Tabelle leeren (falls partiell befüllt oder anderer seed)
    conn.execute("DELETE FROM survivors;")

    # Bulk-Insert in einer Transaktion
    conn.executemany(
        "INSERT INTO survivors (lat, lon) VALUES (?, ?);",
        rows,
    )
    conn.commit()
    return total


def materialize_in_bbox(
    conn: sqlite3.Connection,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
) -> list[int]:
    """Materialisiert noch nicht materialisierte Überlebende in der Bbox als NPCs.

    Für jeden Überlebenden mit ``materialized=0`` im angegebenen Rechteck wird
    eine Minimal-NPC-Zeile in ``characters`` angelegt (type='survivor').
    Danach werden ``materialized=1`` und ``character_id`` gesetzt.

    Idempotent: bereits materialisierte Zeilen werden übersprungen.
    Alles in einer Transaktion.

    Returns
    -------
    list[int]
        character_ids der neu angelegten NPCs.
    """
    rows = conn.execute(
        "SELECT id, lat, lon FROM survivors "
        "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND materialized = 0;",
        (min_lat, max_lat, min_lon, max_lon),
    ).fetchall()

    if not rows:
        return []

    # world_seed und start_datetime einmal laden, nicht je Survivor
    world_row = conn.execute(
        "SELECT tick, world_seed, start_datetime FROM world WHERE id = 1;"
    ).fetchone()
    at_tick = world_row["tick"]
    world_seed = world_row["world_seed"]
    start_datetime = world_row["start_datetime"]

    character_ids: list[int] = []

    for row in rows:
        survivor_id = row["id"]
        s_lat = row["lat"]
        s_lon = row["lon"]

        identity = generate_identity(
            survivor_id=survivor_id,
            lat=s_lat,
            lon=s_lon,
            world_seed=world_seed,
            start_datetime=start_datetime,
        )

        # NPC-Zeile mit echter Identitaet aus generate_identity
        conn.execute(
            "INSERT INTO characters "
            "(name, sex, birthdate, profession, type, lat, lon, "
            "hunger, thirst, sleep, injury, exposure, "
            "satisfaction, performance, is_alive, daily_kcal, daily_water_l) "
            "VALUES (?, ?, ?, ?, 'survivor', ?, ?, 1.0, 1.0, 1.0, 1.0, 1.0, "
            "1.0, 1.0, 1, 2000.0, 2.0);",
            (
                identity["name"],
                identity["sex"],
                identity["birthdate"],
                identity["profession"],
                s_lat,
                s_lon,
            ),
        )
        char_id = conn.execute("SELECT last_insert_rowid();").fetchone()[0]
        conn.execute(
            "UPDATE survivors SET materialized = 1, character_id = ? WHERE id = ?;",
            (char_id, survivor_id),
        )
        character_ids.append(char_id)

    conn.commit()
    return character_ids


def count_near(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    radius_m: float,
) -> int:
    """Zählt Überlebende im Radius (Haversine nach Bbox-Vorfilter).

    Parameters
    ----------
    conn:
        DB-Verbindung.
    lat, lon:
        Mittelpunkt in Grad.
    radius_m:
        Suchradius in Metern.

    Returns
    -------
    int
        Anzahl survivors in radius_m um (lat, lon).
    """
    # Grobe Bbox-Vorfilterung (equirektangulär)
    d_lat = radius_m / 111_320.0
    d_lon = radius_m / (111_320.0 * math.cos(math.radians(lat))) if lat != 90.0 else radius_m / 111_320.0

    candidates = conn.execute(
        "SELECT lat, lon FROM survivors "
        "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?;",
        (lat - d_lat, lat + d_lat, lon - d_lon, lon + d_lon),
    ).fetchall()

    count = 0
    for row in candidates:
        if _haversine_m(lat, lon, row["lat"], row["lon"]) <= radius_m:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Interne Hilfsfunktion
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine-Distanz in Metern zwischen zwei Koordinaten."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))
