"""Der Adjudikator — offene Spieler-Intention -> validiertes World-State-Delta.

Ablauf (DESIGN.md §8): read-only Kontext bauen -> LLM/Stub liefert ein
Vorschlags-Objekt (Verständnis, Machbarkeit, Effekte, Narration) -> bei
``too_complex``/``impossible`` begründet ablehnen -> sonst JEDEN Effekt hart
gegen die DB validieren -> nur wenn alle bestehen, atomar anwenden -> Verdict.

Kein Guess-the-Verb: der Spieler adressiert keine Verben, sondern beschreibt
frei seine Absicht. Das geschlossene, validierte Effekt-Vokabular ist intern
(siehe ``effects``). Eisernes Prinzip: Effekte sind Vorschläge; geschrieben wird
nur durch die geprüften Sim-Funktionen, Folgen berechnet der Sim-Kern.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from ..llm import get_backend
from . import capabilities, chatlog, effects, kb, requirements

_M_PER_DEG_LAT = 111_320.0
_CONTEXT_RADIUS_M = 400.0
_REQUIREMENTS = ("heat", "power", "transmitter")


def _dist(lat1, lon1, lat2, lon2) -> float:
    mlon = _M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * mlon, (lat2 - lat1) * _M_PER_DEG_LAT)


def build_context(conn: sqlite3.Connection, character_id: int) -> dict[str, Any]:
    """Read-only „kuratiertes Optionen-Set": nur real Verfügbares."""
    char = conn.execute(
        "SELECT id, group_id, lat, lon, hunger, performance, is_alive, "
        "profession, education, hobbies, self_description "
        "FROM characters WHERE id = ?;",
        (character_id,),
    ).fetchone()
    group_id = char["group_id"] if char else 1

    locations: list[dict[str, Any]] = []
    if char and char["lat"] is not None:
        for r in conn.execute(
            "SELECT id, type, name, lat, lon, discovery_status FROM locations;"
        ).fetchall():
            d = _dist(char["lat"], char["lon"], r["lat"], r["lon"])
            if d <= _CONTEXT_RADIUS_M:
                locations.append({
                    "id": r["id"], "type": r["type"], "name": r["name"],
                    "discovery_status": r["discovery_status"], "dist_m": round(d, 1),
                })
        locations.sort(key=lambda x: x["dist_m"])

    inventory = [
        {"item_id": r["item_id"], "quantity": r["quantity"],
         "needs_preparation": r["needs_preparation"], "category": r["category"]}
        for r in conn.execute(
            "SELECT gi.item_id, SUM(gi.quantity) AS quantity, ic.needs_preparation, "
            "ic.category FROM group_inventory gi JOIN item_catalog ic "
            "ON ic.id = gi.item_id WHERE gi.group_id = ? GROUP BY gi.item_id;",
            (group_id,),
        ).fetchall()
    ]
    # alle Capability-Rezepte sammeln (Topics beginnen mit 'capability_recipe:')
    recipe_keys = [
        r["topic"].split(":", 1)[1]
        for r in conn.execute(
            "SELECT DISTINCT topic FROM knowledge_base "
            "WHERE topic LIKE 'capability_recipe:%';"
        ).fetchall()
    ]
    providers = {req: requirements.providers(conn, req) for req in _REQUIREMENTS}
    profile = None
    if char:
        profile = {
            "profession": char["profession"], "education": char["education"],
            "hobbies": char["hobbies"], "self_description": char["self_description"],
        }
    return {
        "player": dict(char) if char else None,
        "profile": profile,
        "locations": locations[:25],
        "inventory": inventory,
        "capabilities": [
            {"ctype": c["ctype"], "params": c["params"]}
            for c in capabilities.list_active(conn, group_id)
        ],
        "recipes": recipe_keys,
        "providers": providers,
        "history": chatlog.recent(conn, character_id, 16),
    }


def adjudicate(conn: sqlite3.Connection, character_id: int, text: str) -> dict[str, Any]:
    context = build_context(conn, character_id)
    if not context["player"] or not context["player"]["is_alive"]:
        return _verdict(False, narration="Kein lebender Charakter.", reason="no_character")

    p = get_backend().interpret(text, context)
    feas = p.get("feasibility", "feasible")
    narration = p.get("narration", "")
    understanding = p.get("understanding", "")

    if feas in ("too_complex", "impossible"):
        result = _verdict(False, understanding=understanding, feasibility=feas,
                          narration=narration or "Das ist so nicht möglich.",
                          reason=p.get("reason") or feas, escalate=True)
        with conn:
            chatlog.append(conn, character_id, "player", text)
            chatlog.append(conn, character_id, "narrator", result["narration"])
        return result

    proposed = p.get("effects", [])
    if not proposed:
        # Reine Erzählung ohne Weltzustands-Effekt.
        result = _verdict(True, understanding=understanding, feasibility=feas,
                          narration=narration or "Du tust es.", effects_applied=[])
        with conn:
            chatlog.append(conn, character_id, "player", text)
            chatlog.append(conn, character_id, "narrator", result["narration"])
        return result

    ok, reason = effects.validate_all(conn, character_id, proposed, context)
    if not ok:
        result = _verdict(False, understanding=understanding, feasibility=feas,
                          narration=narration, reason=reason, escalate=True,
                          hint=_hint_for(reason))
        with conn:
            chatlog.append(conn, character_id, "player", text)
            chatlog.append(conn, character_id, "narrator", result["narration"])
        return result

    applied = effects.apply_all(conn, character_id, proposed, context)
    # Narration der Effekte (z.B. Such-Ergebnis) übernimmt, wenn vorhanden.
    eff_narr = " ".join(
        a["result"]["narration"] for a in applied
        if isinstance(a["result"], dict) and a["result"].get("narration")
    ).strip()
    final = eff_narr or narration or "Erledigt."

    # Soft-Fehler eines Appliers (z.B. eat ohne Essbares) sichtbar machen.
    soft_fail = next((a for a in applied
                      if isinstance(a["result"], dict) and a["result"].get("ok") is False), None)
    if soft_fail:
        result = _verdict(False, understanding=understanding, feasibility=feas,
                          narration=final or narration, reason=soft_fail["result"].get("reason"),
                          escalate=True, effects_applied=applied)
        with conn:
            chatlog.append(conn, character_id, "player", text)
            chatlog.append(conn, character_id, "narrator", result["narration"])
        return result

    result = _verdict(True, understanding=understanding, feasibility=feas,
                      narration=final, effects_applied=applied)
    with conn:
        chatlog.append(conn, character_id, "player", text)
        chatlog.append(conn, character_id, "narrator", result["narration"])
    return result


# --- Player-Override ----------------------------------------------------
def override(
    conn: sqlite3.Connection, character_id: int, text: str, reason: str
) -> dict[str, Any]:
    """Spieler ficht eine Ablehnung an: nennt er einen besessenen Gegenstand als
    Hitzequelle, wird der als ``provides:heat`` (player_verified) gelernt; danach
    erneute Adjudikation."""
    with conn:
        now_tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
        char = conn.execute(
            "SELECT group_id FROM characters WHERE id = ?;", (character_id,)
        ).fetchone()
        item = _find_owned_item_in_text(conn, char["group_id"], reason) if char else None
        if item is None:
            return _verdict(False, escalate=True, reason="override_unclear",
                            narration="Womit genau? Ich erkenne keinen passenden "
                            "Gegenstand in deiner Begründung.")
        kb.add(conn, "provides:heat", item, {"consume": 1}, "player_verified", now_tick)
    # adjudicate() schreibt den Spieler-Input (``text``) und die Narration
    # bereits selbst ins chat_log — kein erneutes Schreiben hier, um
    # Doppel-Einträge zu vermeiden.
    result = adjudicate(conn, character_id, text)
    result["override_learned"] = {"topic": "provides:heat", "key": item}
    return result


def _find_owned_item_in_text(conn, group_id, reason) -> str | None:
    r = (reason or "").lower()
    for row in conn.execute(
        "SELECT gi.item_id, ic.name FROM group_inventory gi "
        "JOIN item_catalog ic ON ic.id = gi.item_id WHERE gi.group_id = ?;",
        (group_id,),
    ).fetchall():
        if row["item_id"].lower() in r or (row["name"] or "").lower() in r:
            return row["item_id"]
    return None


# --- Verdict-Helfer -----------------------------------------------------
def _verdict(ok: bool, *, understanding="", feasibility="feasible", effects_applied=None,
             narration="", reason=None, escalate=False, hint=None) -> dict[str, Any]:
    return {
        "ok": ok, "understanding": understanding, "feasibility": feasibility,
        "effects_applied": effects_applied or [], "narration": narration,
        "reason": reason, "escalate": escalate, "hint": hint,
    }


def _hint_for(reason: str | None) -> str | None:
    if not reason:
        return None
    if reason.startswith("missing:"):
        return f"Dir fehlt: {reason.split(':', 1)[1]}."
    return {
        "no_target": "Davon ist nichts in der Nähe.",
        "no_food": "Nichts direkt Essbares dabei.",
        "no_water": "Nicht genug Wasser.",
        "no_heat": "Keine Hitzequelle.",
        "no_recipe": "So etwas kannst du (noch) nicht aufbauen.",
        "nothing_to_prepare": "Nichts zuzubereiten.",
    }.get(reason)
