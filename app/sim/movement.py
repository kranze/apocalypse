"""Bewegung: Ziel setzen (Routing) und im Tick entlang der Route laufen.

Teil der Welt-Phase (Phase 1) des Ticks. Die Position ist Sim-State; sie ändert
sich nur hier, deterministisch über die verstrichene Spielzeit. Bewegung liefert
die gelaufene Distanz zurück, damit die Biologie die Aktivitäts-Kalorien
verbuchen kann (Körper- + Rucksackgewicht).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

import math

from ..osm import roads
from .. import config
from . import constants
from .events import emit_event


def _ensure_corridor_roads(
    start_lat: float, start_lon: float,
    goal_lat: float, goal_lon: float,
    cap: int = 8,
) -> None:
    """Lädt Korridor-Chunks entlang der Luftlinie Start→Ziel in den additiven Graph.

    Wählt gleichmäßig verteilte Punkte entlang der Linie (max ``cap`` Chunks)
    und ruft ``ensure_roads_for_chunk`` für jeden auf. Fehler werden ignoriert.
    """
    try:
        # Chunk-Koordinaten für Start und Ziel
        cx_start = math.floor(start_lat / config.CHUNK_DEG)
        cy_start = math.floor(start_lon / config.CHUNK_DEG)
        cx_goal = math.floor(goal_lat / config.CHUNK_DEG)
        cy_goal = math.floor(goal_lon / config.CHUNK_DEG)

        # Anzahl Schritte begrenzen
        steps = max(abs(cx_goal - cx_start), abs(cy_goal - cy_start), 1)
        steps = min(steps, cap - 1)  # max cap Chunks

        seen: set[tuple[int, int]] = set()
        for i in range(steps + 1):
            t = i / steps if steps > 0 else 0.0
            lat_i = start_lat + (goal_lat - start_lat) * t
            lon_i = start_lon + (goal_lon - start_lon) * t
            cx = math.floor(lat_i / config.CHUNK_DEG)
            cy = math.floor(lon_i / config.CHUNK_DEG)
            if (cx, cy) not in seen:
                seen.add((cx, cy))
                roads.ensure_roads_for_chunk(cx, cy)
    except Exception:
        pass  # Korridor-Load-Fehler ist nicht kritisch


def carried_weight(conn: sqlite3.Connection, group_id: int) -> float:
    """Gesamtgewicht des Gruppen-Inventars in kg (für Aktivitäts-Energie)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(gi.quantity * ic.weight_kg), 0.0) AS w "
        "FROM group_inventory gi JOIN item_catalog ic ON ic.id = gi.item_id "
        "WHERE gi.group_id = ?;",
        (group_id,),
    ).fetchone()
    return row["w"] or 0.0


def set_destination(
    conn: sqlite3.Connection, character_id: int, lat: float, lon: float
) -> dict[str, Any]:
    """Berechnet die Fußroute von der aktuellen Position zum Ziel und speichert
    die verbleibenden Wegpunkte. Atomar."""
    with conn:
        char = conn.execute(
            "SELECT id, lat, lon, home_lat, home_lon FROM characters "
            "WHERE id = ? AND is_alive = 1;",
            (character_id,),
        ).fetchone()
        if char is None:
            return {"ok": False, "reason": "no_such_living_character"}
        if char["lat"] is None or char["lon"] is None:
            return {"ok": False, "reason": "no_position"}

        # Korridor-Chunks entlang der Luftlinie Start→Ziel vorladen (begrenzt auf cap=8).
        # Fehler werden ignoriert (Fallback auf Luftlinie bleibt).
        _ensure_corridor_roads(
            char["lat"], char["lon"], lat, lon, cap=8
        )

        # Graph: anchor_lat/lon des Spielers (rückwärtskompatibel, monkeypatching in Tests).
        anchor_lat = char["home_lat"] if char["home_lat"] is not None else char["lat"]
        anchor_lon = char["home_lon"] if char["home_lon"] is not None else char["lon"]
        graph = roads.get_graph(anchor_lat, anchor_lon)
        start = graph.nearest_node(char["lat"], char["lon"])
        goal = graph.nearest_node(lat, lon)
        if start is None or goal is None:
            # Kein Straßennetz -> Luftlinie als Fallback.
            waypoints = [[lat, lon]]
            distance = roads._dist_m((char["lat"], char["lon"]), (lat, lon))
        else:
            coords, dist = graph.shortest_path(start, goal)
            if not coords:
                waypoints = [[lat, lon]]
                distance = roads._dist_m((char["lat"], char["lon"]), (lat, lon))
            else:
                # Graph-Pfad + exaktes Klickziel als letzte Wegmarke.
                waypoints = [[la, lo] for la, lo in coords] + [[lat, lon]]
                distance = dist

        conn.execute(
            "UPDATE characters SET dest_lat = ?, dest_lon = ?, path_json = ? "
            "WHERE id = ?;",
            (lat, lon, json.dumps(waypoints), character_id),
        )
    return {"ok": True, "path": waypoints, "distance_m": round(distance, 1)}


def advance_movement(conn: sqlite3.Connection, minutes: int, now_tick: int) -> dict:
    """Lässt alle laufenden Charaktere um WALK_SPEED * minutes entlang ihres
    Pfads laufen. Liefert {char_id: gelaufene_distanz_m, "_interrupts": [...]}."""
    budget0 = constants.WALK_SPEED_M_PER_MIN * minutes
    distances: dict[int, float] = {}
    interrupts: list[dict[str, Any]] = []

    rows = conn.execute(
        "SELECT id, name, lat, lon, path_json FROM characters "
        "WHERE is_alive = 1 AND path_json IS NOT NULL;"
    ).fetchall()

    for row in rows:
        path = json.loads(row["path_json"])
        cur = (row["lat"], row["lon"])
        budget = budget0
        traveled = 0.0

        while path and budget > 1e-9:
            target = (path[0][0], path[0][1])
            d = roads._dist_m(cur, target)
            if d <= budget:
                cur = target
                budget -= d
                traveled += d
                path.pop(0)
            else:
                frac = budget / d
                cur = (
                    cur[0] + (target[0] - cur[0]) * frac,
                    cur[1] + (target[1] - cur[1]) * frac,
                )
                traveled += budget
                budget = 0.0

        arrived = not path
        conn.execute(
            "UPDATE characters SET lat = ?, lon = ?, path_json = ?, "
            "dest_lat = CASE WHEN ? THEN NULL ELSE dest_lat END, "
            "dest_lon = CASE WHEN ? THEN NULL ELSE dest_lon END WHERE id = ?;",
            (
                cur[0], cur[1],
                None if arrived else json.dumps(path),
                arrived, arrived,
                row["id"],
            ),
        )
        distances[row["id"]] = traveled
        if arrived:
            interrupts.append(
                emit_event(
                    conn, now_tick, "world",
                    f"{row['name']} hat das Ziel erreicht.",
                    severity="soft", subject_type="character", subject_id=row["id"],
                )
            )

    distances["_interrupts"] = interrupts  # type: ignore[assignment]
    return distances
