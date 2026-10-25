"""Das Zuhause als echter Ort (Issue #54).

``new_game`` legt für den Heimat-Anker einen echten ``locations``-Eintrag an
(Typ ``home``, Label „Deine Wohnung“). Damit gibt es dort Inventar, Durchsuchen
und Plündern wie an jedem Ort, und der frühere Pseudo-Ort ``HOME_LOC_ID`` für
Hahn/Steckdose entfällt: die Infrastruktur folgt der normalen Orts-Logik
(``decay_world.infra_loc``).

Die Wohnung ist von Anfang an entdeckt (man kennt sein Zuhause), ihr Inventar
entsteht aber wie überall erst beim ersten Durchsuchen (Loot-Tabelle ``home``,
deterministischer Seed). Nachbarn = Gebäude innerhalb ``DECLARE_RADIUS_M``.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from typing import Any

from . import constants

HOME_OSM_ID = "home"
HOME_TYPE = "home"
HOME_LABEL = "Deine Wohnung"
_HOME_FOOTPRINT_M2 = 80.0
_M_PER_DEG_LAT = 111_320.0


def dist_m(lat1, lon1, lat2, lon2) -> float:
    mlon = _M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * mlon, (lat2 - lat1) * _M_PER_DEG_LAT)


def _seed(world_seed: int) -> int:
    """Wie ``osm.loader._generation_seed``: 63-Bit-Hash aus (world_seed, osm_id)."""
    raw = f"{world_seed}|{HOME_OSM_ID}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def home_location(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM locations WHERE osm_id = ?;", (HOME_OSM_ID,)).fetchone()


def ensure_home(conn: sqlite3.Connection, lat: float, lon: float) -> int:
    """Legt die Wohnung an (idempotent) und liefert ihre Location-ID. Läuft in
    der Transaktion des Aufrufers."""
    row = home_location(conn)
    if row is not None:
        return row["id"]
    w = conn.execute("SELECT tick, world_seed FROM world WHERE id = 1;").fetchone()
    cur = conn.execute(
        "INSERT INTO locations (osm_id, type, label, name, lat, lon, footprint_m2, "
        "discovery_status, discovered_at_tick, generation_seed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?);",
        (HOME_OSM_ID, HOME_TYPE, HOME_LABEL, HOME_LABEL, lat, lon, _HOME_FOOTPRINT_M2,
         w["tick"], _seed(w["world_seed"])),
    )
    return cur.lastrowid


def neighbours(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Gebäude (ohne die Wohnung) im Umkreis ``DECLARE_RADIUS_M`` um das Zuhause,
    nächste zuerst (Gleichstand: kleinere ID)."""
    home = home_location(conn)
    if home is None:
        return []
    out = []
    for r in conn.execute(
        "SELECT id, type, label, name, lat, lon FROM locations WHERE id != ?;",
        (home["id"],),
    ).fetchall():
        d = dist_m(home["lat"], home["lon"], r["lat"], r["lon"])
        if d <= constants.DECLARE_RADIUS_M:
            out.append({**dict(r), "dist_home_m": d})
    out.sort(key=lambda x: (x["dist_home_m"], x["id"]))
    return out
