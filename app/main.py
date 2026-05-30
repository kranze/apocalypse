"""FastAPI-App für Wasteland (Schritt 1).

Read-Endpoints für Locations + ein Lade-Endpoint, der den OSM-Loader anstößt.
Basis für den späteren Renderer (DESIGN.md §3 Weltsicht).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db
from .osm import loader
from .sim import constants, generation, looting, movement, resources, tick

WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Wasteland", version="0.1.0", lifespan=lifespan)


class LoadOsmRequest(BaseModel):
    lat: float | None = None
    lon: float | None = None
    radius_m: int | None = None
    force_refresh: bool = False


class TickRequest(BaseModel):
    minutes: int | None = None


class FastForwardRequest(BaseModel):
    max_ticks: int = 100_000
    until_tick: int | None = None
    minutes: int | None = None


class EatRequest(BaseModel):
    item_id: str | None = None


class MoveRequest(BaseModel):
    lat: float
    lon: float


class LootRequest(BaseModel):
    group_id: int = 1
    items: dict[str, float] | None = None  # None = alles nehmen


_CHARACTER_COLS = (
    "id, name, type, group_id, lat, lon, hunger, thirst, sleep, injury, "
    "exposure, performance, is_alive, daily_kcal, dest_lat, dest_lon, path_json"
)


_LOCATION_COLS = (
    "id, osm_id, type, name, lat, lon, footprint_m2, discovery_status"
)


@app.post("/world/load-osm")
def load_osm(req: LoadOsmRequest) -> dict:
    """Lädt ein Viertel von OSM in die DB (Default-Gebiet aus der Config)."""
    return loader.load_area(
        req.lat, req.lon, req.radius_m, force_refresh=req.force_refresh
    )


@app.get("/locations")
def list_locations(
    min_lat: float | None = Query(None),
    min_lon: float | None = Query(None),
    max_lat: float | None = Query(None),
    max_lon: float | None = Query(None),
) -> list[dict]:
    """Listet Locations, optional auf eine Bounding-Box gefiltert."""
    where = []
    params: list[float] = []
    bbox = (min_lat, min_lon, max_lat, max_lon)
    if all(v is not None for v in bbox):
        where = ["lat BETWEEN ? AND ?", "lon BETWEEN ? AND ?"]
        params = [min_lat, max_lat, min_lon, max_lon]

    sql = f"SELECT {_LOCATION_COLS} FROM locations"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id;"

    conn = db.get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@app.get("/locations/{location_id}")
def get_location(location_id: int) -> dict:
    conn = db.get_connection()
    try:
        row = conn.execute(
            f"SELECT {_LOCATION_COLS} FROM locations WHERE id = ?;",
            (location_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Location not found")
    return dict(row)


@app.post("/locations/{location_id}/discover")
def discover_location(location_id: int) -> dict:
    """Betritt eine Location: generiert (lazy) ihr Inventar, einmalig."""
    conn = db.get_connection()
    try:
        result = generation.discover(conn, location_id)
    finally:
        conn.close()
    if not result["ok"]:
        raise HTTPException(status_code=404, detail=result["reason"])
    return result


@app.post("/locations/{location_id}/loot")
def loot_location(location_id: int, req: LootRequest) -> dict:
    """Plündert eine Location (Transfer in die Gruppe). Auto-Discover inklusive."""
    conn = db.get_connection()
    try:
        result = looting.loot(conn, location_id, req.group_id, req.items)
    finally:
        conn.close()
    if not result["ok"]:
        raise HTTPException(status_code=404, detail=result["reason"])
    return result


@app.get("/locations/{location_id}/inventory")
def location_inventory(location_id: int) -> list[dict]:
    conn = db.get_connection()
    try:
        return generation.current_inventory(conn, location_id)
    finally:
        conn.close()


@app.get("/groups/{group_id}/inventory")
def group_inventory(group_id: int) -> list[dict]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT item_id, quantity, quality, acquired_tick FROM group_inventory "
            "WHERE group_id = ? ORDER BY item_id, quality DESC;",
            (group_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@app.post("/world/tick")
def world_tick(req: TickRequest) -> dict:
    """Rückt die Welt um einen Tick (Default-Schrittweite aus der Config)."""
    minutes = req.minutes if req.minutes is not None else constants.TICK_MINUTES
    conn = db.get_connection()
    try:
        return tick.advance_tick(conn, minutes)
    finally:
        conn.close()


@app.post("/world/fast-forward")
def world_fast_forward(req: FastForwardRequest) -> dict:
    """Spult Ticks vor, bis ein Interrupt / Ziel-Tick / Aussterben eintritt."""
    minutes = req.minutes if req.minutes is not None else constants.TICK_MINUTES
    conn = db.get_connection()
    try:
        return tick.fast_forward(
            conn, max_ticks=req.max_ticks, until_tick=req.until_tick, minutes=minutes
        )
    finally:
        conn.close()


@app.get("/characters")
def list_characters() -> list[dict]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            f"SELECT {_CHARACTER_COLS} FROM characters ORDER BY id;"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@app.post("/characters/{character_id}/eat")
def character_eat(character_id: int, req: EatRequest) -> dict:
    conn = db.get_connection()
    try:
        result = resources.eat(conn, character_id, req.item_id)
    finally:
        conn.close()
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["reason"])
    return result


@app.post("/characters/{character_id}/prepare")
def character_prepare(character_id: int, req: EatRequest) -> dict:
    """Bereitet ein rohes Lebensmittel zu (Hitze + Wasser -> Mahlzeit)."""
    conn = db.get_connection()
    try:
        result = resources.prepare(conn, character_id, req.item_id)
    finally:
        conn.close()
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["reason"])
    return result


@app.post("/characters/{character_id}/move")
def character_move(character_id: int, req: MoveRequest) -> dict:
    """Setzt ein Ziel und berechnet die Fußroute (Weltsicht: 'laufen').
    Die Bewegung selbst läuft über die Ticks ab."""
    conn = db.get_connection()
    try:
        result = movement.set_destination(conn, character_id, req.lat, req.lon)
    finally:
        conn.close()
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["reason"])
    return result


@app.get("/world/state")
def world_state() -> dict:
    """Kompakter HUD-Zustand: Zeit (abgeleitet aus tick), Phase, Wetter, Spieler."""
    conn = db.get_connection()
    try:
        w = conn.execute(
            "SELECT tick, start_datetime, phase, weather_temp_c, weather_state "
            "FROM world WHERE id = 1;"
        ).fetchone()
        player = conn.execute(
            f"SELECT {_CHARACTER_COLS} FROM characters WHERE id = 1;"
        ).fetchone()
    finally:
        conn.close()
    dt = datetime.fromisoformat(w["start_datetime"]) + timedelta(minutes=w["tick"])
    return {
        "tick": w["tick"],
        "datetime": dt.isoformat(),
        "phase": w["phase"],
        "tick_minutes": constants.TICK_MINUTES,
        "weather": {"temp_c": w["weather_temp_c"], "state": w["weather_state"]},
        "player": dict(player) if player else None,
    }


@app.get("/api/info")
def api_info() -> dict:
    return {
        "app": "Wasteland",
        "tick_minutes": constants.TICK_MINUTES,
        "center": [config.CENTER_LAT, config.CENTER_LON],
        "radius_m": config.RADIUS_M,
    }


# Statisches Frontend zuletzt mounten, damit alle API-Routen Vorrang haben.
# html=True liefert index.html unter "/".
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
