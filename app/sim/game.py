"""Spielstart: aus dem Onboarding-Profil eine neue Welt aufsetzen.

Geocodet die Wohnadresse (oder nimmt manuelle Koordinaten), setzt die Welt
zurück und erschafft den Spieler-Charakter aus dem Profil (Bedarf aus
Mifflin-St-Jeor). Die Umgebung (Chunks, Gebäude, Straßennetz) wird NICHT
synchron geladen — das übernimmt POST /world/ensure-chunks im Hintergrund,
ausgelöst vom Frontend nach dem Spielstart.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from ..osm import geocode
from . import biology, chatlog, survivors

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
    """Setzt eine neue Welt aus dem Profil auf. Liefert {ok, intro, home}.

    Kein synchroner OSM-Load: Chunks/Gebäude/Straßen kommen via
    POST /world/ensure-chunks im Hintergrund (Phase 4/6).
    """
    # 1) Startkoordinaten: manuell vorgegeben oder geocodet.
    #    Geocode-Fehler bricht VOR dem Reset ab (kein State-Verlust).
    lat, lon = profile.get("lat"), profile.get("lon")
    if lat is None or lon is None:
        coords = geocode.geocode(profile.get("address", ""))
        if coords is None:
            return {"ok": False, "reason": "geocode_failed"}
        lat, lon = coords

    # 2) Spielstand zurücksetzen (frische Welt am neuen Ort).
    for stmt in (
        "DELETE FROM location_inventory;", "DELETE FROM group_inventory;",
        "DELETE FROM resource_ledger;", "DELETE FROM events;",
        "DELETE FROM resource_audit;", "DELETE FROM capabilities;",
        "DELETE FROM locations;", "DELETE FROM survivors;",
        "DELETE FROM survivor_groups;",
        "DELETE FROM world_chunks;",
        "UPDATE world SET tick = 0, survivor_sim_day = 0 WHERE id = 1;",
    ):
        conn.execute(stmt)
    conn.commit()

    # 3) Globale Überlebenden-Verteilung aufsetzen (schnell, kein Netz).
    survivors.spawn_survivors(conn)

    # 4) Spieler aus Profil erschaffen (id 1).
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

    # 5) Chat-Log zurücksetzen und Intro als erste Narrator-Zeile schreiben.
    chatlog.clear(conn, 1)
    chatlog.append(conn, 1, "narrator", INTRO)
    conn.commit()

    # Heimat-Gebäude wird sichtbar, sobald der Heimat-Chunk im Hintergrund geladen wird.
    return {
        "ok": True,
        "intro": INTRO,
        "home": [lat, lon],
    }
