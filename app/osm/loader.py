"""OSM-Loader: holt ein Viertel, parst es und füllt die ``locations``-Tabelle.

Idempotent: Re-Import desselben Gebiets erzeugt keine Duplikate (Upsert auf
``osm_id``) und setzt bereits entdeckte Orte nicht zurück.

Schreibt ausschließlich OSM-Stammdaten (Typ, Footprint, Position) — **kein**
Inventar. Inventar entsteht erst bei Entdeckung (Lazy Generation, späterer
Schritt). Damit bleibt das eiserne 3-Schichten-Prinzip gewahrt.
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from .. import config, db
from . import overpass, parser, roads as _roads


def _generation_seed(world_seed: int, osm_id: str) -> int:
    """Deterministischer Seed pro Ort aus (world_seed, osm_id).

    osm_id ist der stabile Anker (anders als die autoincrement-PK), daher
    bleibt der Seed über Re-Imports konstant. 63-Bit, passt in SQLite INTEGER.
    """
    raw = f"{world_seed}|{osm_id}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


_UPSERT = """
INSERT INTO locations (osm_id, type, label, name, lat, lon, footprint_m2,
                       footprint_json, generation_seed, discovery_status)
VALUES (:osm_id, :type, :label, :name, :lat, :lon, :footprint_m2,
        :footprint_json, :generation_seed, 'undiscovered')
ON CONFLICT(osm_id) DO UPDATE SET
    type           = excluded.type,
    label          = excluded.label,
    name           = excluded.name,
    lat            = excluded.lat,
    lon            = excluded.lon,
    footprint_m2   = excluded.footprint_m2,
    footprint_json = excluded.footprint_json
;
"""


def _upsert_records(
    conn: sqlite3.Connection, records: list[dict[str, Any]], world_seed: int
) -> int:
    """Upserted eine Liste von Location-Records in die DB; gibt Anzahl zurück."""
    for rec in records:
        rec = {**rec, "generation_seed": _generation_seed(world_seed, rec["osm_id"])}
        conn.execute(_UPSERT, rec)
    return len(records)


def load_bbox_combined(
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    conn: sqlite3.Connection,
    *,
    force_refresh: bool = False,
) -> int:
    """Lädt Gebäude UND Straßen in EINEM Overpass-Request für eine Bbox.

    Nutzt overpass.fetch_bbox_combined (ein Round-Trip statt zwei/drei).
    - parser.parse extrahiert nur Gebäude (ignoriert highway-Ways per eigenem Filter).
    - roads._graph.merge_ways extrahiert nur Straßen (ignoriert building-Ways per
      eigenem highway-Pflicht-Filter in merge_ways).
    Beide Filter wirken auf dieselbe Rohantwort; kein Doppel-Fetch nötig.

    Gibt die Anzahl der geupserteten Locations zurück.
    Die Connection muss vom Aufrufer verwaltet werden (commit/rollback).
    """
    data = overpass.fetch_bbox_combined(
        min_lat, min_lon, max_lat, max_lon, force=force_refresh
    )
    records = parser.parse(data)
    world_seed = db.get_world_seed(conn)
    count = _upsert_records(conn, records, world_seed)
    # Straßen-Teil in den additiven prozessweiten Graph mergen (Prozess-State,
    # kein DB-Schreiben — Eisernes Prinzip gewahrt). merge_ways ist idempotent.
    _roads._graph.merge_ways(data)
    return count


def load_bbox(
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    conn: sqlite3.Connection,
    *,
    force_refresh: bool = False,
) -> int:
    """Lädt Gebäude UND Straßen in EINEM Overpass-Request für eine Bbox.

    Delegiert intern an load_bbox_combined (ein Round-Trip statt vormals zwei).
    Bleibt als öffentlicher Einstiegspunkt erhalten (Rückwärtskompatibilität,
    Monkeypatch-Anker in Tests).

    Rand-Overlap zwischen benachbarten Chunks wird durch den Upsert auf
    ``osm_id`` dedupliziert — kein Doppeleintrag möglich.

    Gibt die Anzahl der geupserteten (neuen + aktualisierten) Locations zurück.
    Die Connection muss vom Aufrufer verwaltet werden (commit/rollback).
    """
    return load_bbox_combined(
        min_lat, min_lon, max_lat, max_lon, conn, force_refresh=force_refresh
    )


def load_area(
    lat: float | None = None,
    lon: float | None = None,
    radius_m: int | None = None,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Lädt das Viertel um (lat, lon) mit Radius und upsertet alle Locations.

    Defaults stammen aus der Config. Liefert eine Zusammenfassung
    ``{loaded, by_type}``.
    """
    lat = config.CENTER_LAT if lat is None else lat
    lon = config.CENTER_LON if lon is None else lon
    radius_m = config.RADIUS_M if radius_m is None else radius_m

    db.init_db()
    data = overpass.fetch(lat, lon, radius_m, force=force_refresh)
    records = parser.parse(data)

    conn = db.get_connection()
    try:
        world_seed = db.get_world_seed(conn)
        _upsert_records(conn, records, world_seed)
        conn.commit()
        by_type = _count_by_type(conn)
    finally:
        conn.close()

    return {"loaded": len(records), "by_type": by_type}


def _count_by_type(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT type, COUNT(*) AS n FROM locations GROUP BY type ORDER BY n DESC;"
    ).fetchall()
    return {row["type"]: row["n"] for row in rows}


def main() -> None:
    result = load_area()
    print(f"Geladen: {result['loaded']} Locations")
    print(f"Gebiet:  {config.CENTER_LAT}, {config.CENTER_LON}  (r={config.RADIUS_M} m)")
    print("Nach Typ:")
    for loc_type, n in result["by_type"].items():
        print(f"  {loc_type:<14} {n}")


if __name__ == "__main__":
    main()
