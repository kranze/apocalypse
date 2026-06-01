"""Automatische Bedürfnis-Versorgung — kein Micromanagement.

Läuft im Tick (Phase 3, nach dem Bedürfnis-Zerfall, vor Zufriedenheit/Performance).
Deckt Durst und Hunger selbsttätig aus dem Rucksack UND dem aktuellen Ort
(entdecktes Gebäude in Reichweite); kocht automatisch, wenn rohe Zutat + Wasser +
Hitze vorhanden sind, sonst nur Fertiges. Schlaf wird durch automatisches Ruhen
gedeckt, wenn der Charakter untätig und müde ist.

Läuft INNERHALB der Tick-Transaktion → keine eigenen Transaktionen; nutzt die
Low-Level-Helfer direkt und bucht jede Mengenänderung ins Ledger (Bilanz bleibt
drift-frei).
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from . import constants, kb, ledger
from .events import emit_event

WATER = "water_1l"
_MAX_STEPS = 30  # Sicherheitsnetz gegen Endlosschleifen


def _dist(lat1, lon1, lat2, lon2):
    mlon = 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * mlon, (lat2 - lat1) * 111_320.0)


def _current_location(conn, lat, lon) -> int | None:
    if lat is None:
        return None
    best, best_d = None, constants.PROVISION_SOURCE_RADIUS_M
    for r in conn.execute(
        "SELECT id, lat, lon FROM locations WHERE discovery_status != 'undiscovered';"
    ).fetchall():
        d = _dist(lat, lon, r["lat"], r["lon"])
        if d <= best_d:
            best, best_d = r["id"], d
    return best


def _consume_from_location(conn, location_id, item_id, amount) -> float:
    remaining = amount
    for r in conn.execute(
        "SELECT id, quantity FROM location_inventory WHERE location_id = ? "
        "AND item_id = ? ORDER BY quality DESC, id;",
        (location_id, item_id),
    ).fetchall():
        if remaining <= 1e-9:
            break
        take = min(r["quantity"], remaining)
        left = r["quantity"] - take
        if left <= 1e-9:
            conn.execute("DELETE FROM location_inventory WHERE id = ?;", (r["id"],))
        else:
            conn.execute("UPDATE location_inventory SET quantity = ? WHERE id = ?;", (left, r["id"]))
        remaining -= take
    return amount - remaining


def _available(conn, group_id, loc_id, item_id) -> float:
    g = conn.execute(
        "SELECT COALESCE(SUM(quantity),0.0) q FROM group_inventory WHERE group_id=? AND item_id=?;",
        (group_id, item_id),
    ).fetchone()["q"] or 0.0
    l = 0.0
    if loc_id is not None:
        l = conn.execute(
            "SELECT COALESCE(SUM(quantity),0.0) q FROM location_inventory WHERE location_id=? AND item_id=?;",
            (loc_id, item_id),
        ).fetchone()["q"] or 0.0
    return g + l


def _consume_group(conn, group_id, item_id, amount) -> float:
    remaining = amount
    for r in conn.execute(
        "SELECT id, quantity FROM group_inventory WHERE group_id=? AND item_id=? "
        "ORDER BY quality DESC, id;",
        (group_id, item_id),
    ).fetchall():
        if remaining <= 1e-9:
            break
        take = min(r["quantity"], remaining)
        left = r["quantity"] - take
        if left <= 1e-9:
            conn.execute("DELETE FROM group_inventory WHERE id=?;", (r["id"],))
        else:
            conn.execute("UPDATE group_inventory SET quantity=? WHERE id=?;", (left, r["id"]))
        remaining -= take
    return amount - remaining


def _consume(conn, group_id, loc_id, item_id, qty) -> float:
    """Verbraucht bis zu qty: erst Rucksack, dann aktueller Ort. Ledger-gebucht."""
    c = _consume_group(conn, group_id, item_id, qty)
    if c < qty - 1e-9 and loc_id is not None:
        c += _consume_from_location(conn, loc_id, item_id, qty - c)
    if c > 1e-9:
        ledger.add(conn, item_id, -c)
    return c


def _best_ready_food(conn, group_id, loc_id):
    """Bestes sofort essbares Item (verderbliches zuerst, dann kcal) aus
    Rucksack + Ort. Liefert (item_id, kcal_per_unit) oder None."""
    src = ("SELECT gi.item_id AS item_id, ic.kcal_per_unit AS kcal, "
           "ic.decay_halflife_min AS hl, ic.needs_preparation AS np, ic.category AS cat "
           "FROM group_inventory gi JOIN item_catalog ic ON ic.id=gi.item_id "
           "WHERE gi.group_id=? AND gi.quantity>0")
    params = [group_id]
    if loc_id is not None:
        src += (" UNION ALL SELECT li.item_id, ic.kcal_per_unit, ic.decay_halflife_min, "
                "ic.needs_preparation, ic.category FROM location_inventory li "
                "JOIN item_catalog ic ON ic.id=li.item_id WHERE li.location_id=? AND li.quantity>0")
        params.append(loc_id)
    row = conn.execute(
        f"SELECT item_id, kcal FROM ({src}) WHERE cat='food' AND np=0 "
        "ORDER BY (hl IS NULL), hl ASC, kcal DESC LIMIT 1;",
        params,
    ).fetchone()
    return (row["item_id"], row["kcal"] or 0.0) if row else None


def _try_cook(conn, group_id, loc_id, now_tick) -> bool:
    """Kocht ein rohes Item, wenn Zutat + Wasser + Hitze (Rucksack+Ort) da sind."""
    src = ("SELECT gi.item_id AS item_id, ic.requires_water_l AS w, ic.prepared_into AS into_ "
           "FROM group_inventory gi JOIN item_catalog ic ON ic.id=gi.item_id "
           "WHERE gi.group_id=? AND gi.quantity>0 AND ic.needs_preparation=1 AND ic.prepared_into IS NOT NULL")
    params = [group_id]
    if loc_id is not None:
        src += (" UNION ALL SELECT li.item_id, ic.requires_water_l, ic.prepared_into "
                "FROM location_inventory li JOIN item_catalog ic ON ic.id=li.item_id "
                "WHERE li.location_id=? AND li.quantity>0 AND ic.needs_preparation=1 AND ic.prepared_into IS NOT NULL")
        params.append(loc_id)
    raw = conn.execute(f"SELECT item_id, w, into_ FROM ({src}) LIMIT 1;", params).fetchone()
    if raw is None:
        return False
    need_water = raw["w"] or 0.0
    if _available(conn, group_id, loc_id, WATER) < need_water:
        return False
    # Hitzequelle aus KB (provides:heat), vorhanden in Rucksack+Ort?
    heat_item, heat_amt = None, 0.0
    for fact in kb.list_topic(conn, "provides:heat"):
        amt = float((fact["value"] or {}).get("consume", 1)) if isinstance(fact["value"], dict) else 1.0
        if _available(conn, group_id, loc_id, fact["key"]) >= max(amt, 1.0):
            heat_item, heat_amt = fact["key"], amt
            break
    if heat_item is None:
        return False
    # Verbrauchen + Mahlzeit erzeugen
    _consume(conn, group_id, loc_id, raw["item_id"], 1.0)
    if need_water > 0:
        _consume(conn, group_id, loc_id, WATER, need_water)
    if heat_amt > 0:
        _consume(conn, group_id, loc_id, heat_item, heat_amt)
    conn.execute(
        "INSERT INTO group_inventory (group_id,item_id,quantity,quality,acquired_tick) "
        "VALUES (?,?,1.0,1.0,?) ON CONFLICT(group_id,item_id,quality) DO UPDATE SET "
        "quantity = quantity + 1.0;",
        (group_id, raw["into_"], now_tick),
    )
    ledger.add(conn, raw["into_"], 1.0)
    return True


def auto_provision(conn: sqlite3.Connection, minutes: int, now_tick: int) -> list[dict[str, Any]]:
    """Deckt Durst/Hunger automatisch aus Rucksack+Ort; lässt Müde ruhen."""
    events: list[dict[str, Any]] = []
    for char in conn.execute(
        "SELECT id, name, group_id, lat, lon, hunger, thirst, sleep, path_json, "
        "daily_kcal, daily_water_l FROM characters WHERE is_alive = 1;"
    ).fetchall():
        gid = char["group_id"]
        loc = _current_location(conn, char["lat"], char["lon"])

        # --- Durst: trinken ---
        thirst, drank = char["thirst"], 0.0
        steps = 0
        while thirst < constants.PROVISION_TARGET and steps < _MAX_STEPS:
            steps += 1
            if _available(conn, gid, loc, WATER) < 1.0 or _consume(conn, gid, loc, WATER, 1.0) < 1.0:
                break
            thirst = min(1.0, thirst + 1.0 / (char["daily_water_l"] or 2.5))
            drank += 1.0
        if drank:
            conn.execute("UPDATE characters SET thirst=? WHERE id=?;", (round(thirst, 6), char["id"]))
            events.append(emit_event(conn, now_tick, "need",
                          f"{char['name']} trinkt {drank:g} L Wasser.",
                          subject_type="character", subject_id=char["id"]))

        # --- Hunger: essen (auto-kochen falls nötig) ---
        hunger, ate_kcal = char["hunger"], 0.0
        steps = 0
        while hunger < constants.PROVISION_TARGET and steps < _MAX_STEPS:
            steps += 1
            food = _best_ready_food(conn, gid, loc)
            if food is None:
                if not _try_cook(conn, gid, loc, now_tick):
                    break
                food = _best_ready_food(conn, gid, loc)
                if food is None:
                    break
            item_id, kcal = food
            if _consume(conn, gid, loc, item_id, 1.0) < 1.0:
                break
            hunger = min(1.0, hunger + kcal / (char["daily_kcal"] or 2500.0))
            ate_kcal += kcal
        if ate_kcal:
            conn.execute("UPDATE characters SET hunger=? WHERE id=?;", (round(hunger, 6), char["id"]))
            events.append(emit_event(conn, now_tick, "need",
                          f"{char['name']} isst (+{ate_kcal:.0f} kcal).",
                          subject_type="character", subject_id=char["id"]))

        # --- Schlaf: ruhen, wenn untätig und müde ---
        if char["sleep"] < constants.SLEEP_REST_BELOW and char["path_json"] is None:
            rest = minutes / constants.MINUTES_PER_DAY * constants.SLEEP_RECOVERY_PER_DAY
            new_sleep = min(1.0, char["sleep"] + rest)
            conn.execute("UPDATE characters SET sleep=? WHERE id=?;", (round(new_sleep, 6), char["id"]))
            events.append(emit_event(conn, now_tick, "need", f"{char['name']} ruht sich aus.",
                          subject_type="character", subject_id=char["id"]))
    return events

