"""Geschlossenes, validiertes Effekt-Vokabular des Adjudikators.

Der Spieler adressiert diese Ops nie direkt — der Adjudikator (bzw. das LLM)
komponiert sie aus einer offenen Absicht. Jeder Effekt hat einen Validator
(read-only Prüfung gegen die DB) und einen Applier (Ausführung über bestehende,
ledger-geprüfte Sim-Funktionen). Der Adjudikator validiert ALLE Effekte zuerst
und wendet sie nur an, wenn jeder besteht (atomar genug).

Eisernes Prinzip: Effekte sind Vorschläge; hier wird hart gegen DB/Ressourcen/
Vorbedingungen geprüft. Halluzinierte Ziele/Mengen werden abgelehnt.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from ..llm import get_backend

from . import (
    capabilities,
    constants,
    generation,
    kb,
    looting,
    movement,
    requirements,
    resources,
    tick,
)
from .events import HALTING

OPS = (
    "move_to", "discover", "transfer", "consume_food", "prepare", "search",
    "advance_time", "transform", "establish_capability", "narrate",
)

_SEARCH_REACH_M = 40.0  # man durchsucht den Ort, an dem man steht

_M_PER_DEG_LAT = 111_320.0
_LOCATION_TYPES = {
    "house", "building", "supermarket", "fuel_station", "hardware",
    "pharmacy", "hospital",
}
_REACH_M = 400.0  # Nähe für discover/transfer


# --- Geo / Ziel-Auflösung ----------------------------------------------
def _dist(lat1, lon1, lat2, lon2) -> float:
    mlon = _M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * mlon, (lat2 - lat1) * _M_PER_DEG_LAT)


def resolve_location(conn, player, target, *, reach=None):
    """Löst eine Zielbeschreibung (Typ oder Name) deterministisch gegen die DB
    auf. None, wenn nichts (in Reichweite) passt — Anti-Halluzination."""
    if player is None or player["lat"] is None:
        return None
    lat, lon = player["lat"], player["lon"]
    loc_type, name = None, None
    if target:
        t = str(target).lower()
        if t in _LOCATION_TYPES:
            loc_type = t
        else:
            name = target
    q = "SELECT id, type, name, lat, lon, discovery_status FROM locations WHERE 1=1"
    params: list[Any] = []
    if loc_type:
        q += " AND type = ?"
        params.append(loc_type)
    if name:
        q += " AND lower(name) LIKE ?"
        params.append(f"%{str(name).lower()}%")
    best, best_d = None, float("inf")
    for r in conn.execute(q, params).fetchall():
        d = _dist(lat, lon, r["lat"], r["lon"])
        if d < best_d and (reach is None or d <= reach):
            best, best_d = r, d
    return best


# --- Inventar-Helfer ----------------------------------------------------
def _owned(conn, group_id, item_id) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(quantity),0.0) AS q FROM group_inventory "
        "WHERE group_id = ? AND item_id = ?;",
        (group_id, item_id),
    ).fetchone()["q"] or 0.0


def _produce(conn, group_id, item_id, qty, tick_now):
    conn.execute(
        "INSERT INTO group_inventory (group_id, item_id, quantity, quality, "
        "acquired_tick) VALUES (?, ?, ?, 1.0, ?) "
        "ON CONFLICT(group_id, item_id, quality) DO UPDATE SET "
        "quantity = quantity + excluded.quantity;",
        (group_id, item_id, qty, tick_now),
    )


def _group_of(conn, character_id) -> int | None:
    row = conn.execute(
        "SELECT group_id FROM characters WHERE id = ?;", (character_id,)
    ).fetchone()
    return row["group_id"] if row else None


def _fail(reason: str) -> dict:
    return {"ok": False, "reason": reason}


# --- Validatoren (read-only) -------------------------------------------
def _v_move(conn, char, eff, ctx):
    return (True, None) if resolve_location(conn, ctx["player"], eff.get("target")) \
        else (False, "no_target")


def _v_near(conn, char, eff, ctx):
    return (True, None) if resolve_location(conn, ctx["player"], eff.get("target"), reach=_REACH_M) \
        else (False, "no_target")


def _v_consume_food(conn, char, eff, ctx):
    gid = _group_of(conn, char)
    row = conn.execute(
        "SELECT 1 FROM group_inventory gi JOIN item_catalog ic ON ic.id = gi.item_id "
        "WHERE gi.group_id = ? AND ic.category = 'food' AND ic.needs_preparation = 0 "
        "AND gi.quantity > 0 LIMIT 1;",
        (gid,),
    ).fetchone()
    return (True, None) if row else (False, "no_food")


def _v_prepare(conn, char, eff, ctx):
    gid = _group_of(conn, char)
    row = conn.execute(
        "SELECT ic.requires_water_l FROM group_inventory gi "
        "JOIN item_catalog ic ON ic.id = gi.item_id WHERE gi.group_id = ? "
        "AND ic.needs_preparation = 1 AND ic.prepared_into IS NOT NULL "
        "AND gi.quantity >= 1 LIMIT 1;",
        (gid,),
    ).fetchone()
    if row is None:
        return (False, "nothing_to_prepare")
    if _owned(conn, gid, "water_1l") < (row["requires_water_l"] or 0.0):
        return (False, "no_water")
    if requirements.satisfy(conn, gid, "heat") is None:
        return (False, "no_heat")
    return (True, None)


def _v_advance_time(conn, char, eff, ctx):
    return (True, None)


def _v_transform(conn, char, eff, ctx):
    gid = _group_of(conn, char)
    for c in eff.get("consume", []):
        if _owned(conn, gid, c["item"]) < float(c.get("qty", 1)):
            return (False, f"missing:{c['item']}")
    for req in eff.get("requires", []):
        if requirements.satisfy(conn, gid, req) is None:
            return (False, f"missing:{req}")
    if not eff.get("produce"):
        return (False, "nothing_produced")
    return (True, None)


def _v_establish(conn, char, eff, ctx):
    gid = _group_of(conn, char)
    ctype = eff.get("ctype")
    recipe = kb.lookup(conn, f"capability_recipe:{ctype}", ctype)
    if recipe is None or not isinstance(recipe["value"], dict):
        return (False, "no_recipe")  # z.B. "Mobilfunknetz" -> kein Rezept
    for req in recipe["value"].get("requires", []):
        if requirements.satisfy(conn, gid, req) is None:
            return (False, f"missing:{req}")
    return (True, None)


def _v_narrate(conn, char, eff, ctx):
    return (True, None)


# --- Applier ------------------------------------------------------------
def _a_move(conn, char, eff, ctx):
    loc = resolve_location(conn, ctx["player"], eff.get("target"))
    r = movement.set_destination(conn, char, loc["lat"], loc["lon"])
    r["target_name"] = loc["name"] or loc["type"]
    return r


def _a_discover(conn, char, eff, ctx):
    loc = resolve_location(conn, ctx["player"], eff.get("target"), reach=_REACH_M)
    r = generation.discover(conn, loc["id"])
    r["target_name"] = loc["name"] or loc["type"]
    return r


def _a_transfer(conn, char, eff, ctx):
    loc = resolve_location(conn, ctx["player"], eff.get("target"), reach=_REACH_M)
    gid = _group_of(conn, char)
    r = looting.loot(conn, loc["id"], gid, eff.get("items"))
    r["target_name"] = loc["name"] or loc["type"]
    return r


def _a_consume_food(conn, char, eff, ctx):
    return resources.eat(conn, char, eff.get("item"))


def _a_prepare(conn, char, eff, ctx):
    return resources.prepare(conn, char, eff.get("item"))


def _a_advance_time(conn, char, eff, ctx):
    """Zeit verstreichen lassen: geklemmt auf FREETEXT_MAX_MINUTES, in
    TICK_MINUTES-Schritten (Rest als letzter Teilschritt). Bricht bei Tod oder
    einem haltenden Interrupt ab. Ohne Minutenangabe: ein einzelner Tick."""
    try:
        requested = int(eff.get("minutes", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    if requested <= 0:
        r = tick.advance_tick(conn)
        return {"ok": True, "tick": r["tick"], "minutes": constants.TICK_MINUTES,
                "interrupts": r["interrupts"]}
    minutes = min(requested, constants.FREETEXT_MAX_MINUTES)
    spent, interrupts, now = 0, [], None
    while spent < minutes:
        step = min(constants.TICK_MINUTES, minutes - spent)
        r = tick.advance_tick(conn, step)
        spent += step
        now = r["tick"]
        interrupts += r["interrupts"]
        alive = conn.execute(
            "SELECT is_alive FROM characters WHERE id = ?;", (char,)
        ).fetchone()
        if alive is None or not alive["is_alive"]:
            break
        if any(i["severity"] in HALTING for i in r["interrupts"]):
            break
    return {"ok": True, "tick": now, "minutes": spent, "clamped": minutes != requested,
            "interrupts": interrupts}


def _a_transform(conn, char, eff, ctx):
    gid = _group_of(conn, char)
    now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
    with conn:
        for c in eff.get("consume", []):
            resources._consume_from_group(conn, gid, c["item"], float(c.get("qty", 1)))
            ledger_add(conn, c["item"], -float(c.get("qty", 1)))
        for req in eff.get("requires", []):
            src = requirements.satisfy(conn, gid, req)
            if src and src["consume"] > 0:
                resources._consume_from_group(conn, gid, src["item"], src["consume"])
                ledger_add(conn, src["item"], -src["consume"])
        for p in eff.get("produce", []):
            _produce(conn, gid, p["item"], float(p.get("qty", 1)), now)
            ledger_add(conn, p["item"], float(p.get("qty", 1)))
    return {"ok": True, "produced": eff.get("produce")}


def _a_establish(conn, char, eff, ctx):
    gid = _group_of(conn, char)
    ctype = eff.get("ctype")
    recipe = kb.lookup(conn, f"capability_recipe:{ctype}", ctype)["value"]
    loc = resolve_location(conn, ctx["player"], eff.get("target"), reach=_REACH_M)
    now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
    params = dict(eff.get("params") or {})
    if "range_km" in recipe:
        params.setdefault("range_km", recipe["range_km"])
    with conn:
        for req in recipe.get("requires", []):
            src = requirements.satisfy(conn, gid, req)
            if src and src["consume"] > 0:
                resources._consume_from_group(conn, gid, src["item"], src["consume"])
                ledger_add(conn, src["item"], -src["consume"])
        cap_id = capabilities.create(
            conn, ctype, gid,
            location_id=(loc["id"] if loc else None),
            params=params, upkeep=recipe.get("upkeep"), tick=now,
        )
    return {"ok": True, "capability_id": cap_id, "ctype": ctype, "params": params}


def _a_narrate(conn, char, eff, ctx):
    return {"ok": True}


def ledger_add(conn, item_id, delta):
    from . import ledger
    ledger.add(conn, item_id, delta)


# --- Suche: Abgleich gegen das generierte Inventar ------------------------
def _discovered_here(conn, player):
    """Nächste ENTDECKTE Location, an der der Spieler steht (≤ Reichweite)."""
    if not player or player.get("lat") is None:
        return None
    lat, lon = player["lat"], player["lon"]
    best, best_d = None, _SEARCH_REACH_M
    for r in conn.execute(
        "SELECT id, type, label, name, lat, lon FROM locations "
        "WHERE discovery_status != 'undiscovered';"
    ).fetchall():
        d = _dist(lat, lon, r["lat"], r["lon"])
        if d <= best_d:
            best, best_d = r, d
    return best


def _v_search(conn, char, eff, ctx):
    if not eff.get("query"):
        return (False, "no_query")
    if _discovered_here(conn, ctx["player"]) is None:
        return (False, "no_location_here")
    return (True, None)


def _a_search(conn, char, eff, ctx):
    """Freitext-Suche: löst dieselbe deterministische Generierung aus wie die
    Klickaktion „Durchsuchen" und gleicht die Anfrage NUR gegen das vorhandene
    Inventar des Ortes ab. Das LLM wählt aus der Kandidatenliste und erzählt;
    es kann weder Items noch Mengen anlegen (Eisernes Prinzip)."""
    loc = _discovered_here(conn, ctx["player"])
    query = str(eff.get("query", "")).strip()
    generation.ensure_generated(conn, loc["id"])
    cands = [
        dict(r) for r in conn.execute(
            "SELECT li.item_id, ic.name, ic.category, SUM(li.quantity) AS quantity "
            "FROM location_inventory li JOIN item_catalog ic ON ic.id = li.item_id "
            "WHERE li.location_id = ? AND li.quantity > 0 "
            "GROUP BY li.item_id ORDER BY li.item_id;",
            (loc["id"],),
        ).fetchall()
    ]
    where = loc["name"] or loc["label"] or loc["type"]
    if not cands:
        return {"ok": True, "found": False, "items": [], "location_id": loc["id"],
                "narration": f"In {where} ist nichts Brauchbares (mehr) zu finden."}

    res = get_backend().match_items(
        query, {"type": loc["type"], "label": loc["label"], "name": loc["name"]},
        cands, ctx.get("profile"),
    )
    by_id = {c["item_id"]: c for c in cands}
    ids: list[str] = []
    for i in res.get("item_ids") or []:  # nur Kandidaten zählen; Rest wird verworfen
        if isinstance(i, str) and i in by_id and i not in ids:
            ids.append(i)
    narration = (res.get("narration") or "").strip()
    if not ids:
        return {"ok": True, "found": False, "items": [], "location_id": loc["id"],
                "narration": narration or "Danach suchst du hier vergeblich."}
    items = [{"item": i, "name": by_id[i]["name"], "quantity": by_id[i]["quantity"]}
             for i in ids]
    listing = ", ".join(f"{it['quantity']:g}x {it['name']}" for it in items)
    return {"ok": True, "found": True, "items": items, "location_id": loc["id"],
            "narration": (narration + " " if narration else "") + f"Vor Ort liegt: {listing}."}


_VALIDATORS = {
    "move_to": _v_move, "discover": _v_near, "transfer": _v_near,
    "consume_food": _v_consume_food, "prepare": _v_prepare, "search": _v_search,
    "advance_time": _v_advance_time, "transform": _v_transform,
    "establish_capability": _v_establish, "narrate": _v_narrate,
}
_APPLIERS = {
    "move_to": _a_move, "discover": _a_discover, "transfer": _a_transfer,
    "consume_food": _a_consume_food, "prepare": _a_prepare, "search": _a_search,
    "advance_time": _a_advance_time, "transform": _a_transform,
    "establish_capability": _a_establish, "narrate": _a_narrate,
}


# --- öffentliche Schnittstelle -----------------------------------------
def validate_all(conn, character_id, effects, ctx) -> tuple[bool, str | None]:
    """Prüft alle Effekte (read-only). Liefert (ok, reason)."""
    for eff in effects:
        op = eff.get("op")
        if op not in _VALIDATORS:
            return (False, f"unknown_op:{op}")
        ok, reason = _VALIDATORS[op](conn, character_id, eff, ctx)
        if not ok:
            return (False, reason)
    return (True, None)


def apply_all(conn, character_id, effects, ctx) -> list[dict]:
    """Wendet alle Effekte der Reihe nach an (nach erfolgreicher Validierung)."""
    results = []
    for eff in effects:
        res = _APPLIERS[eff["op"]](conn, character_id, eff, ctx)
        results.append({"op": eff["op"], "result": res})
    return results
