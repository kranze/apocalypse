"""Chunk-System: deterministische Welt-Kacheln für lazy OSM-Laden.

Jede Kachel (cx, cy) entspricht einem CHUNK_DEG x CHUNK_DEG Grad-Rechteck.
Bei CHUNK_DEG=0.01 sind das ca. 1,1 km N-S und ~0,7 km O-W (bei 50°N).

Chunk-Koordinaten:
    cx = floor(lat / CHUNK_DEG)   (Ganzzahl, kann negativ sein)
    cy = floor(lon / CHUNK_DEG)

Das ist eine reine Funktion ohne Seiteneffekte — deterministisch und stabil.

Eisernes Prinzip: Nur dieser Modul (Sim-Kern) schreibt in world_chunks.
                  overpass/loader schreiben nur locations.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from .. import config
from ..osm import loader


# ---------------------------------------------------------------------------
# Reine Hilfsfunktionen (deterministisch, keine Seiteneffekte)
# ---------------------------------------------------------------------------

def chunk_key(lat: float, lon: float) -> tuple[int, int]:
    """Gibt den deterministischen Chunk-Schlüssel (cx, cy) für eine Position zurück.

    cx = floor(lat / CHUNK_DEG), cy = floor(lon / CHUNK_DEG).
    Reine Funktion: kein Zustand, kein IO, immer dasselbe Ergebnis.
    """
    cx = math.floor(lat / config.CHUNK_DEG)
    cy = math.floor(lon / config.CHUNK_DEG)
    return cx, cy


def chunk_bbox(cx: int, cy: int) -> tuple[float, float, float, float]:
    """Gibt die Bounding-Box eines Chunks zurück: (min_lat, min_lon, max_lat, max_lon)."""
    min_lat = cx * config.CHUNK_DEG
    min_lon = cy * config.CHUNK_DEG
    max_lat = min_lat + config.CHUNK_DEG
    max_lon = min_lon + config.CHUNK_DEG
    return min_lat, min_lon, max_lat, max_lon


def chunks_in_bbox(
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
) -> list[tuple[int, int]]:
    """Gibt alle Chunk-Schlüssel zurück, die die angegebene Bbox abdecken.

    Schließt alle Chunks ein, die mit der Bbox überlappen (auch Randchunks).
    """
    cx_min = math.floor(min_lat / config.CHUNK_DEG)
    cy_min = math.floor(min_lon / config.CHUNK_DEG)
    cx_max = math.floor(max_lat / config.CHUNK_DEG)
    cy_max = math.floor(max_lon / config.CHUNK_DEG)

    result = []
    for cx in range(cx_min, cx_max + 1):
        for cy in range(cy_min, cy_max + 1):
            result.append((cx, cy))
    return result


# ---------------------------------------------------------------------------
# Sim-Kern: Laden und Persistenz (schreibt world_chunks + locations via loader)
# ---------------------------------------------------------------------------

def ensure_chunk_loaded(
    conn: sqlite3.Connection,
    cx: int,
    cy: int,
) -> dict[str, Any]:
    """Stellt sicher, dass der Chunk (cx, cy) geladen ist.

    Idempotent: Ist der Chunk bereits mit status='loaded' in world_chunks,
    wird kein Overpass-Request ausgelöst und keine DB-Schreiboperation
    durchgeführt. Erst beim ersten Aufruf (oder wenn die Zeile fehlt) wird
    die Bbox über overpass.fetch_bbox geholt, geparst und in locations
    geupserted.

    Bei einem Fetch-/Load-Fehler (z.B. Overpass 504): kein status='loaded'
    geschrieben; optionaler status='error'-Eintrag für spätere Retry-Sichtbarkeit.
    Rückgabe: dict mit ok (bool), loaded_now (bool), building_count (int), cx, cy.
    WIRFT KEINE Exception.
    """
    # Status-Check vor dem Fetch — Idempotenz-Gate.
    row = conn.execute(
        "SELECT status, building_count FROM world_chunks WHERE cx = ? AND cy = ?;",
        (cx, cy),
    ).fetchone()

    if row is not None and row["status"] == "loaded":
        # Bereits geladen — kein weiterer Netz-Request.
        return {
            "ok": True,
            "loaded_now": False,
            "cx": cx,
            "cy": cy,
            "building_count": row["building_count"],
        }

    # Chunk noch nicht geladen: Bbox holen, parsen, upserten.
    min_lat, min_lon, max_lat, max_lon = chunk_bbox(cx, cy)
    try:
        count = loader.load_bbox(min_lat, min_lon, max_lat, max_lon, conn)
    except Exception as exc:
        # Fetch fehlgeschlagen: Chunk bleibt ungeladen (kein 'loaded'-Status).
        # status='error' vermerken, damit spätere Retries den Zustand sehen.
        tick_row = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()
        current_tick = int(tick_row["tick"]) if tick_row is not None else 0
        conn.execute(
            """
            INSERT INTO world_chunks (cx, cy, status, loaded_at_tick, building_count)
            VALUES (?, ?, 'error', ?, 0)
            ON CONFLICT(cx, cy) DO UPDATE SET
                status          = 'error',
                loaded_at_tick  = excluded.loaded_at_tick
            ;
            """,
            (cx, cy, current_tick),
        )
        conn.commit()
        return {
            "ok": False,
            "loaded_now": False,
            "cx": cx,
            "cy": cy,
            "building_count": 0,
            "reason": str(exc),
        }

    # Aktuellen Tick aus der Welt-Tabelle lesen (NULL-sicher).
    tick_row = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()
    current_tick = int(tick_row["tick"]) if tick_row is not None else 0

    # world_chunks-Zeile schreiben (Upsert: Insert oder Update bei Konflikt).
    conn.execute(
        """
        INSERT INTO world_chunks (cx, cy, status, loaded_at_tick, building_count)
        VALUES (?, ?, 'loaded', ?, ?)
        ON CONFLICT(cx, cy) DO UPDATE SET
            status          = 'loaded',
            loaded_at_tick  = excluded.loaded_at_tick,
            building_count  = excluded.building_count
        ;
        """,
        (cx, cy, current_tick, count),
    )
    conn.commit()

    return {
        "ok": True,
        "loaded_now": True,
        "cx": cx,
        "cy": cy,
        "building_count": count,
    }


def ensure_bbox_bulk(
    conn: sqlite3.Connection,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
) -> dict[str, Any]:
    """Lädt den gesamten Bereich in EINER Overpass-Bbox-Query (statt N Einzelabfragen).

    Strategie:
    - Alle Chunks in der Bbox ermitteln.
    - Sind ALLE bereits status='loaded' → kein Netz, sofort {mode:'noop'}.
    - Sonst: EIN loader.load_bbox(gesamte Bbox) → alle Locations upserted;
      danach ALLE abgedeckten Chunks als status='loaded' markieren.
    - Bei Bulk-Fetch-Fehler: KEINE Chunks als loaded markieren;
      Fallback auf ensure_chunk_loaded pro Chunk (toleriert Einzelfehler).
    Rückgabe: {loaded_chunks, failed_chunks, new_locations, mode}.
    WIRFT KEINE Exception.
    """
    cells = chunks_in_bbox(min_lat, min_lon, max_lat, max_lon)

    if not cells:
        return {"loaded_chunks": 0, "failed_chunks": 0, "new_locations": 0, "mode": "noop"}

    # Noop-Gate: alle Chunks bereits geladen?
    placeholders = ",".join("(?,?)" for _ in cells)
    params_flat = [v for cx, cy in cells for v in (cx, cy)]
    loaded_rows = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM world_chunks
        WHERE (cx, cy) IN (VALUES {placeholders}) AND status = 'loaded';
        """,
        params_flat,
    ).fetchone()

    if loaded_rows is not None and loaded_rows["cnt"] == len(cells):
        return {"loaded_chunks": 0, "failed_chunks": 0, "new_locations": 0, "mode": "noop"}

    # Bulk-Fetch: eine Overpass-Query für den gesamten Bereich.
    try:
        new_locations = loader.load_bbox(min_lat, min_lon, max_lat, max_lon, conn)
    except Exception:
        # Bulk fehlgeschlagen → Fallback: Chunk-für-Chunk versuchen.
        loaded = 0
        failed = 0
        for cx, cy in cells:
            result = ensure_chunk_loaded(conn, cx, cy)
            if result.get("ok"):
                loaded += 1
            else:
                failed += 1
        return {
            "loaded_chunks": loaded,
            "failed_chunks": failed,
            "new_locations": 0,
            "mode": "fallback",
        }

    # Bulk erfolgreich: alle Chunks als 'loaded' markieren.
    tick_row = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()
    current_tick = int(tick_row["tick"]) if tick_row is not None else 0

    conn.executemany(
        """
        INSERT INTO world_chunks (cx, cy, status, loaded_at_tick, building_count)
        VALUES (?, ?, 'loaded', ?, 0)
        ON CONFLICT(cx, cy) DO UPDATE SET
            status         = 'loaded',
            loaded_at_tick = excluded.loaded_at_tick
        ;
        """,
        [(cx, cy, current_tick) for cx, cy in cells],
    )
    conn.commit()

    return {
        "loaded_chunks": len(cells),
        "failed_chunks": 0,
        "new_locations": new_locations,
        "mode": "bulk",
    }


def ensure_chunks_in_bbox(
    conn: sqlite3.Connection,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
) -> dict[str, Any]:
    """Lädt alle Chunks, die die angegebene Bbox abdecken (lazy).

    Bereits geladene Chunks werden übersprungen (idempotent).
    Ein fehlgeschlagener Chunk bricht die Operation NICHT ab — es wird
    weitergelaufen und der Fehler im Summary gezählt.
    Rückgabe: dict mit loaded_chunks, skipped_chunks, failed_chunks,
              total_chunks, new_locations.
    WIRFT KEINE Exception.
    """
    keys = chunks_in_bbox(min_lat, min_lon, max_lat, max_lon)
    loaded = 0
    skipped = 0
    failed = 0
    new_locations = 0

    for cx, cy in keys:
        result = ensure_chunk_loaded(conn, cx, cy)
        if not result.get("ok", True):
            failed += 1
        elif result["loaded_now"]:
            loaded += 1
            new_locations += result["building_count"]
        else:
            skipped += 1

    return {
        "loaded_chunks": loaded,
        "skipped_chunks": skipped,
        "failed_chunks": failed,
        "total_chunks": len(keys),
        "new_locations": new_locations,
    }
