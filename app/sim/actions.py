"""Aktionsbasierte Zeit (Slice M1: Allein überleben).

Jede Spieleraktion kostet Spielminuten. ``perform`` prüft die Machbarkeit,
wendet den Effekt über die bestehenden, atomaren Sim-Kern-Funktionen an
(``looting.loot``, ``resources.eat`` ...) und rückt danach die Welt um genau
die Aktionsdauer vor — in ``TICK_MINUTES``-Schritten, Rest als letzter
Teilschritt. Die Debug-Endpoints ``/world/tick`` und ``/world/fast-forward``
bleiben daneben bestehen.

Eisernes Prinzip: Dieses Modul schreibt NIE Ressourcen oder Charakter-Werte
inline. Zustandsänderungen laufen ausschließlich über Sim-Funktionen; der
Freitext geht durch den Adjudikator (LLM schlägt nur vor). Abgelehnte Aktionen
verändern die Welt nicht (``minutes_spent = 0``).
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable

from . import (
    adjudicator,
    constants,
    devices,
    effects,
    generation,
    heat,
    looting,
    movement,
    online,
    provision,
    resources,
    tick,
)
from .events import DECISION, HALTING, emit_event

# Spalten des Spieler-Objekts — identisch zu GET /world/state -> player.
CHARACTER_COLS = (
    "id, name, type, group_id, age, lat, lon, hunger, thirst, sleep, injury, "
    "exposure, satisfaction, performance, is_alive, daily_kcal, daily_water_l, "
    "birthdate, sex, height_cm, weight_kg, profession, education, family, hobbies, "
    "self_description, home_lat, home_lon, dest_lat, dest_lon, path_json"
)

_NOT_HUNGRY_ABOVE = 0.99


# --- Zeit-Helfer --------------------------------------------------------
def calendar_day(start_datetime: str, tick: int) -> int:
    """0-basierter Kalendertag: Datum von start+tick minus Datum von start
    (nach Mitternacht beginnt der nächste Tag, nicht erst nach 24 h)."""
    start = datetime.fromisoformat(start_datetime)
    return (start + timedelta(minutes=tick)).date().toordinal() - start.date().toordinal()


def world_clock(conn: sqlite3.Connection) -> dict[str, Any]:
    """Tick, 0-basierter Tag und Uhrzeit ("HH:MM") der Spielwelt."""
    w = conn.execute("SELECT tick, start_datetime FROM world WHERE id = 1;").fetchone()
    start = datetime.fromisoformat(w["start_datetime"])
    dt = start + timedelta(minutes=w["tick"])
    return {
        "tick": w["tick"],
        "day": calendar_day(w["start_datetime"], w["tick"]),
        "datetime": dt,
        "time_of_day": dt.strftime("%H:%M"),
    }


def minutes_until_morning(conn: sqlite3.Connection) -> int:
    """Minuten bis zur nächsten WAKE_HOUR:00 Spielzeit (mind. 1)."""
    now = world_clock(conn)["datetime"]
    target = now.replace(hour=constants.WAKE_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1, math.ceil((target - now).total_seconds() / 60.0))


_MORNING_MIN_MINUTES = 10


def _tick_now(conn) -> int:
    return conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]


def _player_state(conn, character_id) -> dict | None:
    row = conn.execute(
        f"SELECT {CHARACTER_COLS} FROM characters WHERE id = ?;", (character_id,)
    ).fetchone()
    return dict(row) if row else None


def _alive(conn, character_id) -> bool:
    row = conn.execute(
        "SELECT is_alive FROM characters WHERE id = ?;", (character_id,)
    ).fetchone()
    return bool(row and row["is_alive"])


def _public_interrupts(events: list[dict]) -> list[dict]:
    """Nur bemerkenswerte (nicht-info) Events, auf severity+message reduziert."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for e in events:
        key = (e["severity"], e["message"])
        if e["severity"] in HALTING and key not in seen:
            seen.add(key)
            out.append({"severity": key[0], "message": key[1]})
    return out


def _is_tiredness(event: dict) -> bool:
    msg = event.get("message", "")
    return event.get("category") == "need" and ("müde" in msg or "erschöpft" in msg)


def _is_halting(event: dict) -> bool:
    return event["severity"] in HALTING


def _is_soft_need_warning(event: dict) -> bool:
    """Erste weiche Bedürfnis-Warnung (müde/hungrig/durstig), nicht kritisch."""
    msg = event.get("message", "")
    return (
        event.get("category") == "need" and event.get("severity") == "soft"
        and "kritisch" not in msg and "völlig" not in msg
    )


def _ignore_while_sleeping(event: dict) -> bool:
    return _is_tiredness(event) or _is_soft_need_warning(event)


def _advance(conn, cid: int, minutes: int, resting: bool = False) -> dict[str, Any]:
    """tick.advance_tick, aber Bedürfnis-Events anderer Charaktere (NPCs)
    erreichen den Spieler nicht als Interrupts (sie bleiben in der events-Tabelle)."""
    r = tick.advance_tick(conn, minutes, resting={cid} if resting else None)
    r["interrupts"] = [
        e for e in r["interrupts"]
        if not (
            e.get("category") == "need" and e.get("subject_type") == "character"
            and e.get("subject_id") not in (None, cid)
        )
    ]
    return r


# --- Welt vorrücken -----------------------------------------------------
def advance_world(
    conn: sqlite3.Connection,
    character_id: int,
    minutes: int,
    *,
    stop: str = "run",
    resting: bool = False,
    until: Callable[[sqlite3.Connection], str | None] | None = None,
    ignore: Callable[[dict], bool] | None = None,
) -> dict[str, Any]:
    """Rückt die Welt um ``minutes`` vor (TICK_MINUTES-Schritte, Rest zuletzt).

    ``stop="run"``: läuft komplett durch (nur Tod bricht ab), Interrupts werden
    gesammelt. ``stop="halt"``: bricht zusätzlich bei einem haltenden Interrupt
    ab (außer ``ignore(event)`` ist wahr). ``until(conn)`` ist eine optionale
    Stopp-Bedingung nach jedem Schritt und liefert den ``stopped``-Code.

    Rückgabe: ``{"spent", "stopped", "events"}`` mit stopped in
    done | interrupt | dead | <until-Code>.
    """
    spent = 0
    events: list[dict] = []
    stopped = "done"
    while spent < minutes:
        step = min(constants.TICK_MINUTES, minutes - spent)
        r = _advance(conn, character_id, step, resting)
        spent += step
        events += r["interrupts"]
        if not _alive(conn, character_id):
            stopped = "dead"
            break
        if until is not None:
            code = until(conn)
            if code:
                stopped = code
                break
        if stop == "halt" and any(
            _is_halting(e) and not (ignore and ignore(e)) for e in r["interrupts"]
        ):
            stopped = "interrupt"
            break
    return {"spent": spent, "stopped": stopped, "events": events}


# --- Antwort-Bau --------------------------------------------------------
def _response(
    conn, kind, character_id, *, ok=True, reason=None, minutes_spent=0, stopped=None,
    events=None, narration=None, hint=None, path=None, arrived_location_id=None,
    result=None,
) -> dict[str, Any]:
    clock = world_clock(conn)
    return {
        "ok": ok, "kind": kind, "reason": reason,
        "minutes_spent": minutes_spent, "tick": clock["tick"], "day": clock["day"],
        "stopped": stopped,
        "interrupts": _public_interrupts(events or []),
        "narration": narration, "hint": hint,
        "path": path, "arrived_location_id": arrived_location_id,
        "result": result or {},
        "player": _player_state(conn, character_id),
    }


def _reject(conn, kind, character_id, reason, hint, *, narration=None, result=None):
    return _response(
        conn, kind, character_id, ok=False, reason=reason, hint=hint,
        narration=narration, result=result,
    )


_SIM_HINTS = {
    "no_food": "Du hast nichts Essbares dabei.",
    "nothing_to_prepare": "Du hast nichts zuzubereiten.",
    "no_water": "Dafür fehlt dir Wasser.",
    "no_heat": "Dafür fehlt dir eine Hitzequelle.",
    "no_such_location": "Diesen Ort gibt es nicht.",
    "no_position": "Dein Standort ist unbekannt.",
    "no_such_living_character": "Kein lebender Charakter.",
}


# --- Erzähltext (deterministisch, aus den Ergebnisdaten; kein LLM) --------
def _item_names(conn) -> dict[str, str]:
    return dict(conn.execute("SELECT id, name FROM item_catalog;").fetchall())


def _listing(names: dict[str, str], items: dict[str, float]) -> str:
    return ", ".join(f"{q:g}x {names.get(i, i)}" for i, q in items.items())


def _duration(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} Std. {m} min"
    return f"{h} Std." if h else f"{m} min"


def _place_name(conn, location_id) -> str | None:
    if location_id is None:
        return None
    r = conn.execute(
        "SELECT name, label, type FROM locations WHERE id = ?;", (location_id,)
    ).fetchone()
    return (r["name"] or r["label"] or r["type"]) if r else None


def _narrate_loot(conn, result: dict) -> str:
    names = _item_names(conn)
    took = result.get("transferred") or {}
    left = result.get("left") or {}
    text = f"Du packst {_listing(names, took)} ein." if took else "Du nimmst nichts mit."
    if left:
        text += f" Mehr kannst du nicht tragen, {_listing(names, left)} lässt du zurück."
    else:
        text += " Hier ist nichts mehr übrig."
    return text


def _narrate_consume(conn, kind: str, result: dict) -> str:
    names = _item_names(conn)
    if kind == "eat":
        return (f"Du isst {names.get(result.get('item'), result.get('item'))} "
                f"(+{result.get('kcal', 0):.0f} kcal).")
    return (f"Du bereitest {names.get(result.get('from'), result.get('from'))} zu "
            f"und hast nun {names.get(result.get('prepared'), result.get('prepared'))}.")


def _narrate_wait(minutes: int, stopped: str) -> str:
    text = f"Du wartest {_duration(minutes)}."
    if stopped == "interrupt":
        text += " Etwas reißt dich aus der Ruhe."
    return text


def _narrate_sleep(minutes: int, stopped: str, after: float) -> str:
    text = f"Du schläfst {_duration(minutes)}."
    if stopped == "interrupt":
        return text + " Du wirst unsanft geweckt."
    if after >= constants.SLEEP_TARGET:
        return text + " Du fühlst dich ausgeruht."
    return text + " Richtig erholt bist du noch nicht."


def _narrate_walk(conn, minutes: int, stopped: str, distance_m, arrived) -> str:
    where = _place_name(conn, arrived)
    if stopped == "arrived":
        if not minutes:
            return f"Du bist bereits bei {where}." if where else "Du bist bereits am Ziel."
        tail = f" und erreichst {where}." if where else " und bist am Ziel."
        return f"Du gehst {distance_m:.0f} m ({_duration(minutes)})" + tail
    if stopped == "interrupt":
        return f"Du gehst {_duration(minutes)}, dann hältst du an."
    if stopped == "max":
        return f"Du gehst {_duration(minutes)} und legst eine Pause ein."
    return f"Du gehst {_duration(minutes)}."


# --- Orts-Helfer --------------------------------------------------------
def _location_here(conn, character_id, reach: float):
    """Nächste Location in Reichweite des Spielers (oder None)."""
    char = conn.execute(
        "SELECT lat, lon FROM characters WHERE id = ?;", (character_id,)
    ).fetchone()
    return effects.resolve_location(conn, char, None, reach=reach)


def _scaled_minutes(footprint_m2: float | None, lo_min: int, hi_min: int) -> int:
    lo, hi = constants.LOOT_FOOTPRINT_MIN_M2, constants.LOOT_FOOTPRINT_MAX_M2
    f = 0.0 if footprint_m2 is None else (footprint_m2 - lo) / (hi - lo)
    f = max(0.0, min(1.0, f))
    return int(round(lo_min + (hi_min - lo_min) * f))


def _loot_minutes(footprint_m2: float | None) -> int:
    return _scaled_minutes(
        footprint_m2, constants.LOOT_MIN_MINUTES, constants.LOOT_MAX_MINUTES
    )


def _search_minutes(footprint_m2: float | None) -> int:
    return _scaled_minutes(
        footprint_m2, constants.SEARCH_MIN_MINUTES, constants.SEARCH_MAX_MINUTES
    )


def _has_inventory(conn, location_id) -> bool:
    return conn.execute(
        "SELECT 1 FROM location_inventory WHERE location_id = ? AND quantity > 0 LIMIT 1;",
        (location_id,),
    ).fetchone() is not None


# --- Gehen --------------------------------------------------------------
def _walk_route(conn, character_id, distance_m: float) -> dict[str, Any]:
    """Läuft die bereits gesetzte Route ab: Ankunft, haltender Interrupt, Tod
    oder WALK_MAX_MINUTES. Die erwartete Gehzeit bestimmt die Schrittgröße,
    damit ``minutes_spent`` der tatsächlichen Gehzeit entspricht."""
    expected = math.ceil(distance_m / constants.WALK_SPEED_M_PER_MIN)
    spent, events, stopped = 0, [], "max"
    while spent < constants.WALK_MAX_MINUTES:
        step = constants.TICK_MINUTES
        if spent < expected:
            step = min(step, expected - spent)
        step = min(step, constants.WALK_MAX_MINUTES - spent)
        r = _advance(conn, character_id, step)
        spent += step
        events += r["interrupts"]
        if not _alive(conn, character_id):
            stopped = "dead"
            break
        row = conn.execute(
            "SELECT path_json FROM characters WHERE id = ?;", (character_id,)
        ).fetchone()
        if row["path_json"] is None:
            stopped = "arrived"
            break
        if any(_is_halting(e) for e in r["interrupts"]):
            stopped = "interrupt"
            break
    return {"spent": spent, "stopped": stopped, "events": events}


def _arrive(conn, character_id, location_id: int | None) -> int | None:
    """Bei Ankunft: Ort entdecken (markiert nur). Ohne bekannte ID den Ort
    unter den Füßen suchen. Gibt die Location-ID oder None zurück."""
    if location_id is None:
        loc = _location_here(conn, character_id, constants.ARRIVE_REACH_M)
        location_id = loc["id"] if loc else None
    if location_id is not None:
        generation.discover(conn, location_id)
    return location_id


def _do_walk(conn, cid, action) -> dict[str, Any]:
    char = conn.execute(
        "SELECT lat, lon FROM characters WHERE id = ?;", (cid,)
    ).fetchone()
    location_id = action.get("location_id")
    if location_id is not None:
        loc = conn.execute(
            "SELECT id, lat, lon FROM locations WHERE id = ?;", (location_id,)
        ).fetchone()
        if loc is None:
            return _reject(conn, "walk", cid, "no_such_location", _SIM_HINTS["no_such_location"])
        lat, lon = loc["lat"], loc["lon"]
    elif action.get("lat") is not None and action.get("lon") is not None:
        lat, lon = float(action["lat"]), float(action["lon"])
    else:
        return _reject(conn, "walk", cid, "no_target", "Wohin? Es fehlt ein Ziel.")
    if char["lat"] is None:
        return _reject(conn, "walk", cid, "no_position", _SIM_HINTS["no_position"])

    # Schon da: nichts laufen, nur ankommen.
    if effects._dist(char["lat"], char["lon"], lat, lon) <= constants.ARRIVE_REACH_M:
        arrived = _arrive(conn, cid, location_id)
        return _response(
            conn, "walk", cid, stopped="arrived", path=[],
            arrived_location_id=arrived, result={"distance_m": 0.0},
            narration=_narrate_walk(conn, 0, "arrived", 0.0, arrived),
        )

    dest = movement.set_destination(conn, cid, lat, lon)
    if not dest.get("ok"):
        reason = dest.get("reason", "no_route")
        return _reject(conn, "walk", cid, reason, _SIM_HINTS.get(reason, "Dorthin kommst du nicht."))

    walked = _walk_route(conn, cid, dest["distance_m"])
    arrived = None
    if walked["stopped"] == "arrived":
        arrived = _arrive(conn, cid, location_id)
    return _response(
        conn, "walk", cid, minutes_spent=walked["spent"], stopped=walked["stopped"],
        events=walked["events"], path=dest["path"], arrived_location_id=arrived,
        result={"distance_m": dest["distance_m"]},
        narration=_narrate_walk(
            conn, walked["spent"], walked["stopped"], dest["distance_m"], arrived
        ),
    )


# --- Plündern / Essen / Zubereiten --------------------------------------
def _do_loot(conn, cid, action) -> dict[str, Any]:
    location_id = action.get("location_id")
    if location_id is None:
        here = _location_here(conn, cid, constants.LOOT_REACH_M)
        location_id = here["id"] if here else None
    loc = conn.execute(
        "SELECT id, lat, lon, footprint_m2 FROM locations WHERE id = ?;", (location_id,)
    ).fetchone() if location_id is not None else None
    if loc is None:
        return _reject(conn, "loot", cid, "not_here", "Hier gibt es nichts zu plündern.")
    char = conn.execute("SELECT lat, lon FROM characters WHERE id = ?;", (cid,)).fetchone()
    if char["lat"] is None or effects._dist(
        char["lat"], char["lon"], loc["lat"], loc["lon"]
    ) > constants.LOOT_REACH_M:
        return _reject(conn, "loot", cid, "not_here", "Du bist zu weit weg. Geh erst hin.")
    if not _has_inventory(conn, loc["id"]):
        if not generation.is_searched(conn, loc["id"]):
            return _reject(conn, "loot", cid, "not_searched",
                           "Durchsuche den Ort erst, um etwas zu finden.")
        return _reject(conn, "loot", cid, "empty", "Hier gibt es nichts mehr zu plündern.")

    movement.clear_destination(conn, cid)
    result = looting.loot_limited(conn, loc["id"], _group_id(conn, cid))
    if not result.get("ok"):
        reason = result.get("reason", "loot_failed")
        if reason == "too_heavy":
            names = _item_names(conn)
            return _reject(
                conn, "loot", cid, reason,
                "Du kannst nichts mehr tragen. Lass erst etwas zurück.",
                narration=f"Dein Rucksack ist voll, {_listing(names, result['left'])} bleibt liegen.",
                result=result,
            )
        return _reject(conn, "loot", cid, reason, _SIM_HINTS.get(reason, "Das klappt nicht."))
    narration = _narrate_loot(conn, result)
    adv = advance_world(conn, cid, _loot_minutes(loc["footprint_m2"]))
    return _response(
        conn, "loot", cid, minutes_spent=adv["spent"], stopped=adv["stopped"],
        events=adv["events"], result=result, narration=narration,
    )


def _do_search(conn, cid, action) -> dict[str, Any]:
    """Durchsuchen: erzeugt das Inventar des Ortes einmalig deterministisch
    (Loot-Tabelle + generation_seed), kostet Zeit nach Footprint. Danach kann
    geplündert werden. Die Aktion erfindet nichts — nur der Sim-Kern generiert."""
    location_id = action.get("location_id")
    if location_id is None:
        here = _location_here(conn, cid, constants.LOOT_REACH_M)
        location_id = here["id"] if here else None
    loc = conn.execute(
        "SELECT id, name, type, label, lat, lon, footprint_m2 FROM locations WHERE id = ?;",
        (location_id,),
    ).fetchone() if location_id is not None else None
    if loc is None:
        return _reject(conn, "search", cid, "not_here", "Hier gibt es nichts zu durchsuchen.")
    char = conn.execute("SELECT lat, lon FROM characters WHERE id = ?;", (cid,)).fetchone()
    if char["lat"] is None or effects._dist(
        char["lat"], char["lon"], loc["lat"], loc["lon"]
    ) > constants.LOOT_REACH_M:
        return _reject(conn, "search", cid, "not_here", "Du bist zu weit weg. Geh erst hin.")
    if generation.is_searched(conn, loc["id"]):
        return _reject(conn, "search", cid, "already_searched",
                       "Hier hast du schon alles durchsucht.")

    movement.clear_destination(conn, cid)
    gen = generation.ensure_generated(conn, loc["id"])
    if not gen.get("ok"):
        reason = gen.get("reason", "search_failed")
        return _reject(conn, "search", cid, reason, _SIM_HINTS.get(reason, "Das klappt nicht."))
    names = dict(conn.execute("SELECT id, name FROM item_catalog;").fetchall())
    found = [
        {"item_id": r["item_id"], "name": names.get(r["item_id"], r["item_id"]),
         "quantity": r["quantity"]}
        for r in gen["items"]
    ]
    where = loc["name"] or loc["label"] or loc["type"]
    if found:
        listing = ", ".join(f"{f['quantity']:g}x {f['name']}" for f in found)
        narration = f"Du durchsuchst {where} und findest: {listing}."
    else:
        narration = f"Du durchsuchst {where} gründlich, aber es ist nichts Brauchbares mehr da."
    adv = advance_world(conn, cid, _search_minutes(loc["footprint_m2"]))
    return _response(
        conn, "search", cid, minutes_spent=adv["spent"], stopped=adv["stopped"],
        events=adv["events"], narration=narration,
        result={"location_id": loc["id"], "found": found, "empty": not found},
    )


def _group_id(conn, cid) -> int:
    return conn.execute(
        "SELECT group_id FROM characters WHERE id = ?;", (cid,)
    ).fetchone()["group_id"]


def _do_consume(conn, cid, action, kind: str) -> dict[str, Any]:
    item_id = action.get("item_id")
    if kind == "eat":
        hunger = conn.execute(
            "SELECT hunger FROM characters WHERE id = ?;", (cid,)
        ).fetchone()["hunger"]
        if hunger >= _NOT_HUNGRY_ABOVE:
            return _reject(conn, kind, cid, "not_hungry", "Du bist satt.")
        fn, minutes = resources.eat, constants.EAT_MINUTES
        if item_id is None:  # bestes verfügbares Essen (wie die Auto-Versorgung)
            best = provision._best_ready_food(conn, _group_id(conn, cid), None)
            item_id = best[0] if best else None
    else:
        fn, minutes = resources.prepare, constants.PREPARE_MINUTES
    result = fn(conn, cid, item_id)
    if not result.get("ok"):
        reason = result.get("reason", "failed")
        return _reject(conn, kind, cid, reason, _SIM_HINTS.get(reason, "Das geht gerade nicht."))
    movement.clear_destination(conn, cid)
    narration = _narrate_consume(conn, kind, result)
    adv = advance_world(conn, cid, minutes)
    return _response(
        conn, kind, cid, minutes_spent=adv["spent"], stopped=adv["stopped"],
        events=adv["events"], result=result, narration=narration,
    )


# --- Warten / Schlafen --------------------------------------------------
def _do_wait(conn, cid, action) -> dict[str, Any]:
    try:
        minutes = int(action.get("minutes") or 60)
    except (TypeError, ValueError):
        return _reject(conn, "wait", cid, "bad_minutes", "Ungültige Wartezeit.")
    minutes = max(constants.WAIT_MIN_MINUTES, min(constants.WAIT_MAX_MINUTES, minutes))
    movement.clear_destination(conn, cid)
    adv = advance_world(conn, cid, minutes, stop="halt")
    return _response(
        conn, "wait", cid, minutes_spent=adv["spent"], stopped=adv["stopped"],
        events=adv["events"], result={"requested_minutes": minutes},
        narration=_narrate_wait(adv["spent"], adv["stopped"]),
    )


def _sleep_value(conn, cid) -> float:
    return conn.execute("SELECT sleep FROM characters WHERE id = ?;", (cid,)).fetchone()["sleep"]


def _do_sleep(conn, cid, action) -> dict[str, Any]:
    if _sleep_value(conn, cid) >= constants.SLEEP_TARGET:
        return _reject(conn, "sleep", cid, "not_tired", "Du bist nicht müde.")
    movement.clear_destination(conn, cid)
    start = _sleep_value(conn, cid)
    adv = advance_world(
        conn, cid, constants.SLEEP_MAX_MINUTES, stop="halt", resting=True,
        until=lambda c: "done" if _sleep_value(c, cid) >= constants.SLEEP_TARGET else None,
        ignore=_ignore_while_sleeping,
    )
    stopped = adv["stopped"]
    if stopped == "done" and adv["spent"] >= constants.SLEEP_MAX_MINUTES \
            and _sleep_value(conn, cid) < constants.SLEEP_TARGET:
        stopped = "max"
    return _response(
        conn, "sleep", cid, minutes_spent=adv["spent"], stopped=stopped,
        events=adv["events"],
        result={"sleep_before": round(start, 4), "sleep_after": round(_sleep_value(conn, cid), 4)},
        narration=_narrate_sleep(adv["spent"], stopped, _sleep_value(conn, cid)),
    )


# --- Geräte & Online (Slice M2 Teil B, Issue #46) -------------------------
_ONLINE_HINTS = {
    "no_device": "Du hast kein Smartphone oder Laptop dabei.",
    "no_battery": "Dein Akku ist leer. Lade ihn erst auf.",
    "no_network": "Kein Netz. Hier kommst du nicht ins Internet.",
}


def _online_gate(conn, cid, kind):
    """Gemeinsame Vorprüfung der Online-Aktionen: Gerät, Akku, Netz.
    Liefert (None, ctx) bei Erfolg, sonst (Ablehnung, None)."""
    gid = _group_id(conn, cid)
    if not devices.has_online_device(conn, gid):
        return _reject(conn, kind, cid, "no_device", _ONLINE_HINTS["no_device"]), None
    if devices.best_online_device(conn, gid) is None:
        return _reject(conn, kind, cid, "no_battery", _ONLINE_HINTS["no_battery"]), None
    here = _location_here(conn, cid, constants.LOOT_REACH_M)
    net = devices.network_quality(conn, here["id"] if here else None)
    if not net["online"]:
        return _reject(conn, kind, cid, "no_network", _ONLINE_HINTS["no_network"]), None
    return None, {"gid": gid, "net": net, "loc": here}


def _finish_online(conn, gid, adv):
    """Akku für die tatsächlich online verbrachte Zeit abbuchen (danach Leerlauf)."""
    devices.drain_online(conn, gid, adv["spent"])
    devices.sync(conn, gid)


def _do_read_news(conn, cid, action) -> dict[str, Any]:
    rej, ctx = _online_gate(conn, cid, "read_news")
    if rej:
        return rej
    day = world_clock(conn)["day"]
    frag = online.pick_news(conn, day)
    if frag is None:
        return _reject(conn, "read_news", cid, "nothing_new",
                       "Es gibt nichts Neues zu lesen.")
    minutes = online.news_minutes(conn, day)
    if devices.online_minutes_available(conn, ctx["gid"]) < minutes:
        return _reject(conn, "read_news", cid, "no_battery", _ONLINE_HINTS["no_battery"])
    movement.clear_destination(conn, cid)
    adv = advance_world(conn, cid, minutes)
    online.mark_news_read(conn, frag["id"], _tick_now(conn))
    _finish_online(conn, ctx["gid"], adv)
    return _response(
        conn, "read_news", cid, minutes_spent=adv["spent"], stopped=adv["stopped"],
        events=adv["events"],
        result={"fragment": dict(frag), "quality": ctx["net"]["quality"]},
        narration=f"Du liest ({frag['source']}): {frag['title']}. {frag['text']}",
    )


def _do_search_survivors(conn, cid, action) -> dict[str, Any]:
    rej, ctx = _online_gate(conn, cid, "search_survivors")
    if rej:
        return rej
    if online.contact_count(conn) >= constants.SEARCH_SURVIVORS_MAX_CONTACTS:
        return _reject(conn, "search_survivors", cid, "no_more_contacts",
                       "Es melden sich keine weiteren Überlebenden.")
    minutes = constants.SEARCH_SURVIVORS_MINUTES
    if devices.online_minutes_available(conn, ctx["gid"]) < minutes:
        return _reject(conn, "search_survivors", cid, "no_battery",
                       _ONLINE_HINTS["no_battery"])
    movement.clear_destination(conn, cid)
    adv = advance_world(conn, cid, minutes)
    _finish_online(conn, ctx["gid"], adv)
    rec = online.record_search(conn, ctx["net"]["quality"], _tick_now(conn))
    events = list(adv["events"])
    if rec["reply"]:
        # Haken für #48: die Antwort steht in online_contacts (handled = 0).
        with conn:
            events.append(emit_event(
                conn, _tick_now(conn), "contact", "Jemand hat geantwortet…",
                severity=DECISION, subject_type="character", subject_id=cid,
                payload={"text": rec["reply"]}))
        narration = (f"Du suchst nach anderen Überlebenden. Jemand hat geantwortet. "
                     f"{rec['reply']}")
    else:
        narration = "Du rufst nach anderen Überlebenden. Keine Antwort."
    return _response(
        conn, "search_survivors", cid, minutes_spent=adv["spent"], stopped=adv["stopped"],
        events=events, narration=narration,
        result={"tries": rec["tries"], "reply": rec["reply"]},
    )


def _do_download(conn, cid, action) -> dict[str, Any]:
    pid = action.get("package_id")
    pkg = online.package(pid) if pid else None
    if pkg is None:
        return _reject(conn, "download", cid, "no_such_package", "Dieses Paket gibt es nicht.")
    if online.progress(conn, pid)["done"]:
        return _reject(conn, "download", cid, "already_downloaded",
                       "Dieses Paket hast du schon heruntergeladen.")
    rej, ctx = _online_gate(conn, cid, "download")
    if rej:
        return rej
    prog = online.progress(conn, pid)
    bw = online.bandwidth_mb_per_min(ctx["net"]["quality"])
    minutes = min(
        online.minutes_needed(prog["remaining_mb"], ctx["net"]["quality"]),
        constants.DOWNLOAD_CHUNK_MINUTES,
        devices.online_minutes_available(conn, ctx["gid"]),
    )
    if minutes < 1:
        return _reject(conn, "download", cid, "no_battery", _ONLINE_HINTS["no_battery"])
    movement.clear_destination(conn, cid)
    adv = advance_world(conn, cid, minutes, stop="halt")
    mb = min(prog["remaining_mb"], adv["spent"] * bw)
    res = online.add_progress(conn, pid, mb, _tick_now(conn))
    _finish_online(conn, ctx["gid"], adv)
    stopped = adv["stopped"]
    if res["done"]:
        narration = f"Download abgeschlossen: {pkg['title']}. Das Wissen ist jetzt gespeichert."
    else:
        if stopped == "done":
            stopped = "max"
        narration = (f"Du lädst {pkg['title']} herunter: {res['downloaded_mb']:.0f} von "
                     f"{pkg['size_mb']} MB. Der Download ist noch nicht fertig.")
    return _response(
        conn, "download", cid, minutes_spent=adv["spent"], stopped=stopped,
        events=adv["events"], narration=narration,
        result={"package_id": pid, "downloaded_mb": round(res["downloaded_mb"], 2),
                "size_mb": pkg["size_mb"], "done": res["done"],
                "quality": ctx["net"]["quality"]},
    )


def _do_charge(conn, cid, action) -> dict[str, Any]:
    gid = _group_id(conn, cid)
    if not devices.sync(conn, gid):
        return _reject(conn, "charge", cid, "no_device", _ONLINE_HINTS["no_device"])
    here = _location_here(conn, cid, constants.LOOT_REACH_M)
    source = action.get("source")
    plan = None
    if source in (None, "grid"):
        plan = devices.grid_charge_plan(conn, gid, here["id"] if here else None)
    if plan is None and source in (None, "powerbank"):
        plan = devices.powerbank_charge_plan(conn, gid)
    if plan is None:
        if all(v["units"] - v["charge"] <= 0.02 for v in devices.sync(conn, gid).values()):
            return _reject(conn, "charge", cid, "already_full", "Alle Geräte sind schon voll.")
        return _reject(conn, "charge", cid, "no_power",
                       "Hier gibt es keinen Strom, und keine Powerbank hilft dir.")
    movement.clear_destination(conn, cid)
    adv = advance_world(conn, cid, plan["minutes"], stop="halt")
    done = devices.apply_charge(conn, gid, plan, adv["spent"])
    devices.sync(conn, gid)
    names = _item_names(conn)
    listing = ", ".join(f"{names.get(i, i)} +{g * 100:.0f} %" for i, g in done.items())
    src = "am Stromnetz" if plan["source"] == "grid" else "aus der Powerbank"
    return _response(
        conn, "charge", cid, minutes_spent=adv["spent"], stopped=adv["stopped"],
        events=adv["events"], result={"source": plan["source"], "charged": done},
        narration=f"Du lädst {src} ({_duration(adv['spent'])}): {listing}.",
    )


# --- Freitext -----------------------------------------------------------
def _do_freetext(conn, cid, action) -> dict[str, Any]:
    override_text = (action.get("override_text") or "").strip()
    override_reason = (action.get("override_reason") or "").strip()
    text = (action.get("text") or "").strip()
    use_override = bool(override_text and override_reason)
    if not use_override and not text:
        return _reject(conn, "freetext", cid, "no_text", "Was möchtest du tun?")

    tick0 = _tick_now(conn)
    if use_override:
        verdict = adjudicator.override(conn, cid, override_text, override_reason)
    else:
        verdict = adjudicator.adjudicate(conn, cid, text)

    applied = verdict.get("effects_applied") or []
    effect_events: list[dict] = []
    for a in applied:
        res = a.get("result")
        if isinstance(res, dict):
            effect_events += res.get("interrupts") or []

    extra = {
        "effects_applied": applied,
        "understanding": verdict.get("understanding"),
        "feasibility": verdict.get("feasibility"),
        "escalate": verdict.get("escalate", False),
        "reason": verdict.get("reason"),
    }
    if "override_learned" in verdict:
        extra["override_learned"] = verdict["override_learned"]

    if not verdict.get("ok"):
        spent = _tick_now(conn) - tick0
        return _response(
            conn, "freetext", cid, ok=False, reason=verdict.get("reason") or "rejected",
            minutes_spent=spent, events=effect_events, narration=verdict.get("narration"),
            hint=verdict.get("hint") or "Das geht so nicht.", result=extra,
        )

    spent = _tick_now(conn) - tick0
    events = list(effect_events)
    stopped = "done"
    path = None
    arrived = None

    move = next(
        (a["result"] for a in applied
         if a.get("op") == "move_to" and isinstance(a.get("result"), dict)
         and a["result"].get("ok") and a["result"].get("path")),
        None,
    )
    if not _alive(conn, cid):
        stopped = "dead"
    elif move is not None:
        path = move["path"]
        walked = _walk_route(conn, cid, move.get("distance_m") or 0.0)
        spent += walked["spent"]
        events += walked["events"]
        stopped = walked["stopped"]
        if stopped == "arrived":
            arrived = _arrive(conn, cid, None)
    elif spent == 0:
        adv = advance_world(conn, cid, constants.FREETEXT_DEFAULT_MINUTES, stop="halt")
        spent += adv["spent"]
        events += adv["events"]
        stopped = adv["stopped"]
    elif any(_is_halting(e) for e in effect_events):
        stopped = "interrupt"

    return _response(
        conn, "freetext", cid, minutes_spent=spent, stopped=stopped, events=events,
        narration=verdict.get("narration"), hint=verdict.get("hint"),
        path=path, arrived_location_id=arrived, result=extra,
    )


# --- öffentliche Schnittstelle ------------------------------------------
_HANDLERS = {
    "walk": _do_walk,
    "loot": _do_loot,
    "search": _do_search,
    "wait": _do_wait,
    "sleep": _do_sleep,
    "read_news": _do_read_news,
    "search_survivors": _do_search_survivors,
    "download": _do_download,
    "charge": _do_charge,
    "freetext": _do_freetext,
}


def perform(conn: sqlite3.Connection, character_id: int, action: dict) -> dict[str, Any]:
    """Führt eine Spieleraktion aus: prüfen -> anwenden -> Welt vorrücken."""
    kind = action.get("kind")
    row = conn.execute(
        "SELECT is_alive FROM characters WHERE id = ?;", (character_id,)
    ).fetchone()
    if row is None:
        return _reject(conn, str(kind), character_id, "no_character", "Kein Charakter.")
    if not row["is_alive"]:
        return _reject(conn, str(kind), character_id, "dead", "Du bist tot.")
    if kind in ("eat", "prepare"):
        return _do_consume(conn, character_id, action, kind)
    handler = _HANDLERS.get(kind)
    if handler is None:
        return _reject(conn, str(kind), character_id, "unknown_kind", "Unbekannte Aktion.")
    return handler(conn, character_id, action)


def _online_actions(conn, cid, gid, here) -> list[dict[str, Any]]:
    """Laden + Online-Aktionen für die Aktionsleiste (nur, was gerade geht)."""
    out: list[dict[str, Any]] = []
    loc_id = here["id"] if here else None
    plan = devices.grid_charge_plan(conn, gid, loc_id) or devices.powerbank_charge_plan(conn, gid)
    if plan is not None:
        out.append({
            "kind": "charge",
            "label": "Laden (Stromnetz)" if plan["source"] == "grid" else "Laden (Powerbank)",
            "source": plan["source"], "minutes": plan["minutes"],
        })
    best = devices.best_online_device(conn, gid)
    if best is None:
        return out
    net = devices.network_quality(conn, loc_id)
    if not net["online"]:
        return out
    day = world_clock(conn)["day"]
    cap = devices.online_minutes_available(conn, gid)
    if online.pick_news(conn, day) is not None and cap >= constants.READ_NEWS_MINUTES[1]:
        out.append({"kind": "read_news", "label": "Nachrichten lesen",
                    "minutes": constants.READ_NEWS_MINUTES[1]})
    if online.contact_count(conn) < constants.SEARCH_SURVIVORS_MAX_CONTACTS \
            and cap >= constants.SEARCH_SURVIVORS_MINUTES:
        out.append({"kind": "search_survivors", "label": "Nach Überlebenden suchen",
                    "minutes": constants.SEARCH_SURVIVORS_MINUTES})
    drain = constants.DEVICE_DRAIN_ONLINE_PER_H[best[0]]
    options = []
    for p in online.packages():
        pr = online.progress(conn, p["id"])
        if pr["done"]:
            continue
        need = online.minutes_needed(pr["remaining_mb"], net["quality"])
        options.append({
            "package_id": p["id"], "title": p["title"], "size_mb": p["size_mb"],
            "remaining_mb": round(pr["remaining_mb"], 1),
            "progress": round(pr["fraction"], 3),
            "minutes_total": need, "battery_pct": round(need / 60.0 * drain * 100.0, 1),
        })
    if options:
        out.append({"kind": "download", "label": "Wissen herunterladen", "minutes": None,
                    "options": options, "quality": net["quality"]})
    return out


def available(conn: sqlite3.Connection, character_id: int) -> dict[str, Any]:
    """Aktuell sinnvolle Klickaktionen für die Aktionsleiste."""
    char = conn.execute(
        "SELECT group_id, hunger, sleep, is_alive FROM characters WHERE id = ?;",
        (character_id,),
    ).fetchone()
    if char is None or not char["is_alive"]:
        return {"here_location_id": None, "actions": []}

    actions: list[dict[str, Any]] = []
    here = _location_here(conn, character_id, constants.LOOT_REACH_M)
    if here is not None:
        fp = conn.execute(
            "SELECT footprint_m2 FROM locations WHERE id = ?;", (here["id"],)
        ).fetchone()["footprint_m2"]
        if not generation.is_searched(conn, here["id"]):
            actions.append({
                "kind": "search", "label": "Durchsuchen", "location_id": here["id"],
                "minutes": _search_minutes(fp),
            })
        elif _has_inventory(conn, here["id"]) and looting.plan_loot(
            conn, here["id"],
            looting.carry_capacity(conn, char["group_id"])
            - movement.carried_weight(conn, char["group_id"]),
        )[0]:
            actions.append({
                "kind": "loot", "label": "Plündern", "location_id": here["id"],
                "minutes": _loot_minutes(fp),
            })

    gid = char["group_id"]
    if char["hunger"] < _NOT_HUNGRY_ABOVE:
        options = [
            {"item_id": r["item_id"], "name": r["name"], "quantity": r["q"]}
            for r in conn.execute(
                "SELECT gi.item_id, ic.name, SUM(gi.quantity) AS q FROM group_inventory gi "
                "JOIN item_catalog ic ON ic.id = gi.item_id "
                "WHERE gi.group_id = ? AND ic.category = 'food' AND ic.needs_preparation = 0 "
                "AND gi.quantity > 0 GROUP BY gi.item_id ORDER BY ic.name;",
                (gid,),
            ).fetchall()
        ]
        if options:
            actions.append({
                "kind": "eat", "label": "Essen", "minutes": constants.EAT_MINUTES,
                "options": options,
            })

    if heat.can_provide_heat(conn, gid) is not None:
        water = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0.0) AS q FROM group_inventory "
            "WHERE group_id = ? AND item_id = 'water_1l';", (gid,),
        ).fetchone()["q"]
        options = [
            {"item_id": r["item_id"], "name": r["name"], "quantity": r["q"]}
            for r in conn.execute(
                "SELECT gi.item_id, ic.name, SUM(gi.quantity) AS q, "
                "ic.requires_water_l AS w FROM group_inventory gi "
                "JOIN item_catalog ic ON ic.id = gi.item_id "
                "WHERE gi.group_id = ? AND ic.needs_preparation = 1 "
                "AND ic.prepared_into IS NOT NULL "
                "GROUP BY gi.item_id HAVING SUM(gi.quantity) >= 1 ORDER BY ic.name;",
                (gid,),
            ).fetchall()
            if water + 1e-9 >= (r["w"] or 0.0)
        ]
        if options:
            actions.append({
                "kind": "prepare", "label": "Zubereiten",
                "minutes": constants.PREPARE_MINUTES, "options": options,
            })

    actions.extend(_online_actions(conn, character_id, gid, here))

    actions.append({"kind": "wait", "label": "1 Std. warten", "minutes": 60})
    until_morning = minutes_until_morning(conn)
    if until_morning >= _MORNING_MIN_MINUTES:
        before_wake = world_clock(conn)["datetime"].hour < constants.WAKE_HOUR
        actions.append({
            "kind": "wait",
            "label": f"Bis {constants.WAKE_HOUR} Uhr warten" if before_wake else "Bis morgen früh",
            "minutes": min(constants.WAIT_MAX_MINUTES, until_morning),
        })
    if char["sleep"] < constants.SLEEP_TARGET:
        actions.append({"kind": "sleep", "label": "Schlafen", "minutes": None})

    return {"here_location_id": here["id"] if here else None, "actions": actions}
