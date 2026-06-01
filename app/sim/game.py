"""Spielstart: aus dem Onboarding-Profil eine neue Welt aufsetzen.

Geocodet die Wohnadresse (oder nimmt manuelle Koordinaten), lädt dort das
Viertel + Straßennetz, setzt die Welt zurück und erschafft den Spieler-Charakter
aus dem Profil (Bedarf aus Mifflin-St-Jeor). Der Spieler erwacht in seiner
Wohnung — die wird sofort entdeckt, damit die Auto-Versorgung greifen kann.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from .. import config
from ..osm import geocode, loader, overpass, roads
from . import biology, generation

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

    # 2) OSM-Daten ZUERST holen (Netz) — Fail-Early, bevor der Spielstand
    #    angefasst wird. So zerstört ein Netzfehler keine bestehende Welt.
    road_radius = config.RADIUS_M + 200
    try:
        overpass.fetch(lat, lon, config.RADIUS_M)
        roads.fetch_roads(lat, lon, road_radius)
    except Exception:
        return {"ok": False, "reason": "osm_unavailable"}

    # 3) Spielstand zurücksetzen (frische Welt am neuen Ort).
    for stmt in (
        "DELETE FROM location_inventory;", "DELETE FROM group_inventory;",
        "DELETE FROM resource_ledger;", "DELETE FROM events;",
        "DELETE FROM resource_audit;", "DELETE FROM capabilities;",
        "DELETE FROM locations;", "UPDATE world SET tick = 0 WHERE id = 1;",
    ):
        conn.execute(stmt)
    conn.commit()

    # 4) Viertel + Straßennetz laden (jetzt aus dem Cache, kein Netz mehr nötig).
    loader.load_area(lat, lon)
    roads.get_graph(lat, lon, radius_m=road_radius, force=True)

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

    # 5) Zuhause (nächstgelegenes Gebäude) entdecken -> Vorrat verfügbar.
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
