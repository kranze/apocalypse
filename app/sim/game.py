"""Spielstart: aus dem Onboarding-Profil eine neue Welt aufsetzen.

Geocodet die Wohnadresse (oder nimmt manuelle Koordinaten), lädt den
Heimat-Chunk + Nachbarn (HOME_PRELOAD_RADIUS_M) und das Straßennetz für
diesen kleinen Bereich, setzt die Welt zurück und erschafft den
Spieler-Charakter aus dem Profil (Bedarf aus Mifflin-St-Jeor). Der Spieler
erwacht in seiner Wohnung — die wird sofort entdeckt, damit die
Auto-Versorgung greifen kann.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Any

from .. import config
from ..osm import geocode, overpass, roads
from . import biology, chatlog, chunks, generation, survivors

INTRO = (
    "Du wachst auf. Es ist still — vollkommen still.\n"
    "Die anderen liegen da, als wären sie nur eingeschlafen. Friedlich. "
    "Keiner wird wieder aufwachen.\n"
    "Ob irgendwo sonst noch jemand lebt? Du weißt es nicht.\n"
    "Du bist allein. Das ist alles, was du sicher weißt."
)


def _age_from(birthdate: str | None, start_datetime: str) -> int | None:
    if not birthdate:
        return None
    try:
        b = datetime.fromisoformat(birthdate)
        now = datetime.fromisoformat(start_datetime)
    except ValueError:
        return None
    years = now.year - b.year - ((now.month, now.day) < (b.month, b.day))
    return max(0, years)


def new_game(conn: sqlite3.Connection, profile: dict[str, Any]) -> dict[str, Any]:
    """Setzt eine neue Welt aus dem Profil auf. Liefert {ok, intro, home, ...}."""
    # 1) Startkoordinaten: manuell vorgegeben oder geocodet.
    lat, lon = profile.get("lat"), profile.get("lon")
    if lat is None or lon is None:
        coords = geocode.geocode(profile.get("address", ""))
        if coords is None:
            return {"ok": False, "reason": "geocode_failed"}
        lat, lon = coords

    # 2) Vorlade-Bbox für Heimat-Chunk + Nachbarn berechnen.
    #    Grad-Umrechnung: N-S ist konstant, O-W skaliert mit cos(lat).
    preload_m = config.HOME_PRELOAD_RADIUS_M
    lat_deg = preload_m / 111_320.0
    lon_deg = preload_m / (111_320.0 * math.cos(math.radians(lat)))
    preload_bbox = (
        lat - lat_deg,  # min_lat
        lon - lon_deg,  # min_lon
        lat + lat_deg,  # max_lat
        lon + lon_deg,  # max_lon
    )

    # 3) Fail-Early: OSM-Daten zuerst holen, BEVOR der Spielstand angefasst wird.
    #    Strategie: Straßennetz (fetch_roads) als Netz-Probe nutzen; danach
    #    chunks.ensure_chunks_in_bbox wird nach dem Reset aufgerufen — da
    #    loader.load_bbox intern cached, wirft ein erneuter Fehler keinen Reset.
    #    Gebäude: ensure_chunks_in_bbox kann intern fehlschlagen; wir starten
    #    einen Probe-Fetch des Heimat-Chunks (lädt ggf. cache-miss via Overpass)
    #    BEVOR wir resetten, damit ein Netzfehler die Welt nicht zerstört.
    road_radius = preload_m + 200
    try:
        # Straßen-Fetch (nutzt fetch_query / Disk-Cache).
        roads.fetch_roads(lat, lon, road_radius)
        # Heimat-Chunk-Probe: overpass.fetch_bbox cached auf Disk und macht
        # keinen DB-Zugriff. Dieser Aufruf befüllt den Disk-Cache des Heimat-
        # Chunks, sodass der Post-Reset-Aufruf (chunks.ensure_chunks_in_bbox)
        # ihn ohne Netz-Request aus dem Cache nimmt. Ein Netzfehler hier bricht
        # ab BEVOR der Spielstand angefasst wird — Fail-Early garantiert.
        cx, cy = chunks.chunk_key(lat, lon)
        home_bbox = chunks.chunk_bbox(cx, cy)
        overpass.fetch_bbox(*home_bbox)
    except Exception:
        return {"ok": False, "reason": "osm_unavailable"}

    # 4) Spielstand zurücksetzen (frische Welt am neuen Ort).
    for stmt in (
        "DELETE FROM location_inventory;", "DELETE FROM group_inventory;",
        "DELETE FROM resource_ledger;", "DELETE FROM events;",
        "DELETE FROM resource_audit;", "DELETE FROM capabilities;",
        "DELETE FROM locations;", "DELETE FROM survivors;",
        "DELETE FROM world_chunks;",
        "UPDATE world SET tick = 0 WHERE id = 1;",
    ):
        conn.execute(stmt)
    conn.commit()

    # 5) Globale Überlebenden-Verteilung aufsetzen (muss VOR dem Chunk-Load
    #    stehen, damit ensure_bbox_bulk direkt materialisieren kann).
    survivors.spawn_survivors(conn)

    # 6) Heimat-Chunk + Nachbarn laden (idempotent; Heimat-Chunk ist bereits
    #    im overpass-Cache aus dem Probe-Fetch oben → kein Netzaufruf mehr).
    #    ensure_bbox_bulk materialisiert die Survivors der Preload-Bbox direkt
    #    mit (Locations vorhanden → Wohnhaus-Snap funktioniert).
    #    Fehler bei Nachbar-Chunks sind tolerierbar; kritisch ist nur der Heimat-Chunk.
    chunks.ensure_bbox_bulk(conn, *preload_bbox)

    # Heimat-Chunk-Check: mindestens der eigene Chunk muss geladen sein.
    home_cx, home_cy = chunks.chunk_key(lat, lon)
    home_chunk_row = conn.execute(
        "SELECT status FROM world_chunks WHERE cx = ? AND cy = ?;",
        (home_cx, home_cy),
    ).fetchone()
    if home_chunk_row is None or home_chunk_row["status"] != "loaded":
        return {"ok": False, "reason": "osm_unavailable"}

    # 7) Straßennetz für den Vorlade-Bereich aufbauen (aus dem Cache).
    roads.get_graph(lat, lon, radius_m=road_radius, force=True)

    # 8) Spieler aus Profil erschaffen (id 1).
    start_dt = conn.execute("SELECT start_datetime FROM world WHERE id=1;").fetchone()["start_datetime"]
    age = _age_from(profile.get("birthdate"), start_dt)
    weight = profile.get("weight_kg") or 75.0
    height = profile.get("height_cm") or 175.0
    daily_kcal, daily_water_l = biology.compute_targets(profile.get("sex"), weight, height, age)
    conn.execute(
        "UPDATE characters SET name=?, birthdate=?, sex=?, height_cm=?, weight_kg=?, "
        "age=?, profession=?, education=?, family=?, hobbies=?, self_description=?, "
        "home_lat=?, home_lon=?, lat=?, lon=?, daily_kcal=?, daily_water_l=?, "
        "hunger=1.0, thirst=1.0, sleep=1.0, satisfaction=1.0, performance=1.0, "
        "is_alive=1, dest_lat=NULL, dest_lon=NULL, path_json=NULL WHERE id=1;",
        (
            profile.get("name") or "Überlebende:r", profile.get("birthdate"),
            profile.get("sex"), height, weight, age, profile.get("profession"),
            profile.get("education"), profile.get("family"), profile.get("hobbies"),
            profile.get("self_description"), lat, lon, lat, lon, daily_kcal, daily_water_l,
        ),
    )
    conn.commit()

    # 9) Chat-Log zurücksetzen und Intro als erste Narrator-Zeile schreiben.
    chatlog.clear(conn, 1)
    chatlog.append(conn, 1, "narrator", INTRO)
    conn.commit()

    # 10) Zuhause (nächstgelegenes Gebäude) entdecken -> Vorrat verfügbar.
    home = conn.execute(
        "SELECT id, (ABS(lat-?)+ABS(lon-?)) AS d FROM locations ORDER BY d LIMIT 1;",
        (lat, lon),
    ).fetchone()
    if home is not None:
        generation.discover(conn, home["id"])

    return {
        "ok": True,
        "intro": INTRO,
        "home": [lat, lon],
        "home_location_id": home["id"] if home else None,
    }
