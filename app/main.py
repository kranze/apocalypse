"""FastAPI-App für Wasteland (Schritt 1).

Read-Endpoints für Locations + ein Lade-Endpoint, der den OSM-Loader anstößt.
Basis für den späteren Renderer (DESIGN.md §3 Weltsicht).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db
from .osm import loader
from .sim import (
    adjudicator, chatlog, constants, game, generation, kb, looting, movement, resources, tick,
)
from .sim import survivor_sim, survivors as survivors_mod

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


class AdjudicateRequest(BaseModel):
    text: str
    character_id: int = 1


class OverrideRequest(BaseModel):
    text: str
    reason: str
    character_id: int = 1


class NewGameRequest(BaseModel):
    name: str | None = None
    birthdate: str | None = None      # ISO YYYY-MM-DD
    sex: str | None = None            # m|f|x
    height_cm: float | None = None
    weight_kg: float | None = None
    family: str | None = None
    education: str | None = None
    profession: str | None = None
    hobbies: str | None = None
    self_description: str | None = None
    address: str | None = None
    lat: float | None = None          # manueller Fallback
    lon: float | None = None


class LootRequest(BaseModel):
    group_id: int = 1
    items: dict[str, float] | None = None  # None = alles nehmen


_CHARACTER_COLS = (
    "id, name, type, group_id, age, lat, lon, hunger, thirst, sleep, injury, "
    "exposure, satisfaction, performance, is_alive, daily_kcal, daily_water_l, "
    "birthdate, sex, height_cm, weight_kg, profession, education, family, hobbies, "
    "self_description, home_lat, home_lon, dest_lat, dest_lon, path_json"
)


_LOCATION_COLS = (
    "id, osm_id, type, label, name, lat, lon, footprint_m2, footprint_json, "
    "discovery_status"
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


_CATEGORY_LABELS = {
    "food": "Lebensmittel", "water": "Wasser", "tool": "Werkzeug",
    "fuel": "Brennstoff", "medical": "Medizin", "material": "Material", "misc": "Kram",
}


@app.post("/locations/{location_id}/arrive")
def location_arrive(location_id: int) -> dict:
    """Ankunft an einem Ort: entdecken + kurze Claude-Geschichte zum Ort."""
    from .llm import get_backend

    conn = db.get_connection()
    try:
        loc = conn.execute(
            "SELECT id, type, label, name FROM locations WHERE id = ?;", (location_id,)
        ).fetchone()
        if loc is None:
            raise HTTPException(status_code=404, detail="no_such_location")
        disc = generation.discover(conn, location_id)  # markiert nur (kein Auto-Loot)
        prof = conn.execute(
            "SELECT profession, hobbies, self_description FROM characters WHERE id = 1;"
        ).fetchone()
        narration = get_backend().narrate_location(
            {"type": loc["type"], "label": loc["label"], "name": loc["name"]},
            dict(prof) if prof else None,
        )
        chatlog.append(conn, 1, "narrator", narration)
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True, "status": disc.get("status"), "narration": narration,
        "inventory": disc.get("inventory", []),
    }


@app.get("/chat")
def get_chat(character_id: int = Query(1), limit: int = Query(40)) -> list[dict]:
    """Gibt den Chat-Verlauf eines Characters zurück (chronologisch aufsteigend)."""
    conn = db.get_connection()
    try:
        return chatlog.recent(conn, character_id, limit)
    finally:
        conn.close()


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
            f"SELECT {_CHARACTER_COLS} FROM characters WHERE type = 'player' ORDER BY id;"
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


@app.post("/game/new")
def game_new(req: NewGameRequest) -> dict:
    """Neues Spiel aus dem Onboarding-Profil (Geocoding -> Viertel -> Spieler)."""
    conn = db.get_connection()
    try:
        result = game.new_game(conn, req.model_dump())
    finally:
        conn.close()
    if not result["ok"]:
        raise HTTPException(status_code=422, detail=result["reason"])
    return result


@app.get("/game/intro")
def game_intro() -> dict:
    return {"intro": game.INTRO}


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


@app.post("/adjudicate")
def adjudicate(req: AdjudicateRequest) -> dict:
    """Free-Text-Intention bewerten und (bei Erfolg) über den Sim-Kern ausführen."""
    conn = db.get_connection()
    try:
        return adjudicator.adjudicate(conn, req.character_id, req.text)
    finally:
        conn.close()


@app.post("/adjudicate/override")
def adjudicate_override(req: OverrideRequest) -> dict:
    """Spieler-Override: Begründung reichert die KB an, dann erneuter Versuch."""
    conn = db.get_connection()
    try:
        return adjudicator.override(conn, req.character_id, req.text, req.reason)
    finally:
        conn.close()


class SimulateDaysRequest(BaseModel):
    days: int = 1


@app.get("/survivors")
def list_survivors(
    materialized: int | None = Query(None),
    all: int | None = Query(None),
) -> dict:
    """Gibt Überlebende als Entitäten zurück: Einzelpersonen + Gruppen-Zentroide.

    Format: {"count": N, "points": [[lat, lon, size], ...]}
    size = 1 für Einzelpersonen, >= 2 für Gruppen (Anzahl Mitglieder).
    Standardmäßig nur lebende (alive=1). ?all=1 inkl. Toter.
    Optional: ?materialized=1 filtert auf materialisierte Überlebende.
    """
    conn = db.get_connection()
    try:
        alive_cond = "" if all == 1 else "alive = 1"
        mat_cond = f"materialized = {1 if materialized == 1 else 0}" if materialized is not None else ""

        extra = " AND ".join(c for c in [alive_cond, mat_cond] if c)
        base_where = f"WHERE {extra}" if extra else ""

        # Einzelpersonen (group_id IS NULL)
        solo_where = base_where + (" AND " if base_where else "WHERE ") + "group_id IS NULL"
        solo_rows = conn.execute(
            f"SELECT lat, lon FROM survivors {solo_where};"
        ).fetchall()
        points = [[row["lat"], row["lon"], 1] for row in solo_rows]

        # Gruppen (group_id NOT NULL) -> Zentroid + Größe
        group_where = base_where + (" AND " if base_where else "WHERE ") + "group_id IS NOT NULL"
        group_rows = conn.execute(
            f"""
            SELECT AVG(lat) AS lat, AVG(lon) AS lon, COUNT(*) AS size
            FROM survivors
            {group_where}
            GROUP BY group_id;
            """
        ).fetchall()
        for row in group_rows:
            points.append([row["lat"], row["lon"], row["size"]])
    finally:
        conn.close()
    return {"count": len(points), "points": points}


@app.get("/survivors/stats")
def survivors_stats() -> dict:
    """Populationsstatistik: alive, dead, groups, grouped, alone + aktueller Tag."""
    conn = db.get_connection()
    try:
        stats = survivor_sim.population_stats(conn)
        row = conn.execute(
            "SELECT survivor_sim_day FROM world WHERE id = 1;"
        ).fetchone()
        day = int(row["survivor_sim_day"]) if row is not None else 0
    finally:
        conn.close()
    return {"day": day, **stats}


# DEBUG: Spawn-Endpunkt – ruft Sim-Kern-Funktion auf (eisernes Prinzip gewahrt)
@app.post("/debug/spawn-survivors")
def debug_spawn_survivors() -> dict:
    """[DEBUG] Verteilt 100.000 Überlebende deterministisch über die Welt.

    Ruft spawn_survivors() aus dem Sim-Kern auf – kein direkter DB-Zugriff.
    Setzt survivor_sim_day auf 0 zurück (Reset-Semantik).
    """
    conn = db.get_connection()
    try:
        count = survivors_mod.spawn_survivors(conn, force=True)
        conn.execute("UPDATE world SET survivor_sim_day = 0 WHERE id = 1;")
        conn.commit()
    finally:
        conn.close()
    return {"count": count}


@app.post("/debug/simulate-days")
def debug_simulate_days(
    req: SimulateDaysRequest | None = None,
    days: int = Query(1),
) -> dict:
    """[DEBUG] Simuliert N Tage Survivor-Bewegung/-Sterben (max 30 pro Aufruf).

    Ruft survivor_sim.step_day() für jeden neuen Tag auf und schreibt
    world.tick + world.survivor_sim_day konsistent fort.
    Verändert den gemeinsamen Weltzustand – Debug-Werkzeug.
    """
    n_days = (req.days if req is not None else days)
    n_days = max(1, min(30, n_days))  # clamp 1..30

    conn = db.get_connection()
    try:
        world = conn.execute(
            "SELECT tick, survivor_sim_day FROM world WHERE id = 1;"
        ).fetchone()
        last_sim_day: int = world["survivor_sim_day"] if world["survivor_sim_day"] is not None else 0

        # Pro Tag committen (kurze Locks), damit parallele Reads nicht blockieren.
        for i in range(n_days):
            next_day = last_sim_day + 1 + i
            new_tick = next_day * constants.MINUTES_PER_DAY
            with conn:
                conn.execute("UPDATE world SET tick = ? WHERE id = 1;", (new_tick,))
                # step_day setzt survivor_sim_day selbst am Ende
                survivor_sim.step_day(conn, next_day)

        stats = survivor_sim.population_stats(conn)
        row = conn.execute(
            "SELECT survivor_sim_day FROM world WHERE id = 1;"
        ).fetchone()
        day = int(row["survivor_sim_day"]) if row is not None else 0
    finally:
        conn.close()
    return {"days_simulated": n_days, "day": day, **stats}


@app.get("/worldmap", response_class=FileResponse)
def worldmap_page() -> FileResponse:
    """Liefert die Debug-Weltkarte für Überlebende."""
    path = WEB_DIR / "worldmap.html"
    return FileResponse(str(path))


@app.get("/kb")
def kb_topic(topic: str = Query("provides:heat")) -> list[dict]:
    """Knowledge-Base-Fakten eines Topics (Anzeige)."""
    conn = db.get_connection()
    try:
        return kb.list_topic(conn, topic)
    finally:
        conn.close()


@app.get("/api/info")
def api_info() -> dict:
    from .llm import get_backend

    return {
        "app": "Wasteland",
        "tick_minutes": constants.TICK_MINUTES,
        "center": [config.CENTER_LAT, config.CENTER_LON],
        "radius_m": config.RADIUS_M,
        "llm_backend": get_backend().name,  # "stub" oder "claude"
    }


# Statisches Frontend zuletzt mounten, damit alle API-Routen Vorrang haben.
# html=True liefert index.html unter "/".
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
