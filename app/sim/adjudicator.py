"""Der Adjudikator v2 — offene Spieler-Intention -> validiertes World-State-Delta.

Ablauf (DESIGN.md §8, Slice M3): read-only Kontext bauen -> **Tool-Use-Schleife**:
das LLM-Backend ruft Lese-Werkzeuge und je Effekt ein Vorschlags-Werkzeug; der
Sim-Kern (``toolbox``) validiert JEDEN Vorschlag sofort gegen DB + gestagten
Plan und meldet ok/Grund zurück (Umplanen erlaubt) -> nach ``finish`` wird der
akzeptierte Plan atomar angewendet (``effects.apply_atomic``, alles-oder-nichts)
-> Verdict. Iterationslimit ``MAX_ITERATIONS``; bei Limit/Fehlern kommt ein
Verdict mit ``escalate``, es wird nichts geschrieben.

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
from ..llm.base import single_shot_adjudication
from . import capabilities, chatlog, devices, effects, generation, kb, requirements
from .toolbox import AdjudicationToolbox, hint_for

# --- Tunables des Adjudikators (gehören bewusst nicht in constants.py) ---
MAX_ITERATIONS = 8            # Modell-Runden je Adjudikation (Lese-/Vorschlagsschritte)
SURVIVORS_ESTIMATE = 100_000  # Orientierungswert für world_state

_M_PER_DEG_LAT = 111_320.0
_CONTEXT_RADIUS_M = 400.0
_REQUIREMENTS = requirements.KNOWN


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
                    "searched": generation.is_searched(conn, r["id"]),
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
        "world_state": _world_state(conn),
        "devices": devices.status(conn, group_id),
        "network": devices.network_quality(conn, effects._power_loc(conn, {"player": char})),
    }


def _world_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """Tag/Uhrzeit aus ``world.tick`` (Minuten) + Kurzlage; Infrastruktur aus
    ``decay_world.world_infra(conn)``, falls das Modul existiert (pluggbar —
    fehlt es oder wirft es, entfällt der Block einfach)."""
    from .actions import world_clock  # lazy: actions importiert adjudicator

    # Kalendertag + Uhrzeit wie im HUD (Start 06:00, nicht tick // 1440).
    clock = world_clock(conn)
    ws: dict[str, Any] = {
        "tick": clock["tick"],
        "day": clock["day"],
        "time": clock["time_of_day"],
        "survivors_estimate": SURVIVORS_ESTIMATE,
        "loneliness": "Extrem: weltweit nur noch rund 100.000 Überlebende.",
    }
    try:
        from . import decay_world  # type: ignore[attr-defined]

        infra = decay_world.world_infra(conn)
    except Exception:  # ImportError, AttributeError oder Fehler der Quelle
        infra = None
    if infra:
        ws["infra"] = infra
    return ws


def _run_loop(conn: sqlite3.Connection, character_id: int, text: str,
              context: dict[str, Any]) -> tuple[AdjudicationToolbox, dict[str, Any]]:
    """Treibt die Tool-Use-Schleife des Backends. Schreibt NICHTS."""
    toolbox = AdjudicationToolbox(conn, character_id, context, MAX_ITERATIONS)
    backend = get_backend()
    runner = getattr(backend, "run_adjudication", None)
    try:
        if runner is not None:
            info = runner(text, context, toolbox, MAX_ITERATIONS)
        else:  # Duck-Typing: Backends, die nur ``interpret`` kennen
            info = single_shot_adjudication(backend, text, context, toolbox)
    except Exception:
        info = {"stop": "error"}
    return toolbox, info


def _log_turn(conn, character_id, text, result) -> dict[str, Any]:
    with conn:
        chatlog.append(conn, character_id, "player", text)
        chatlog.append(conn, character_id, "narrator", result["narration"])
    return result


def adjudicate(conn: sqlite3.Connection, character_id: int, text: str) -> dict[str, Any]:
    with conn:
        kb.seed_defaults(conn)  # kuratierte Grundfakten (z.B. barricade-Rezept)
    context = build_context(conn, character_id)
    if not context["player"] or not context["player"]["is_alive"]:
        return _verdict(False, narration="Kein lebender Charakter.", reason="no_character")

    toolbox, info = _run_loop(conn, character_id, text, context)
    fin = toolbox.finished

    if fin is None:
        # Iterationslimit / Backend-Fehler / kein finish: nichts anwenden, eskalieren.
        reason = "iteration_limit" if (toolbox.limit_hit or info.get("stop") == "limit") \
            else "llm_error"
        return _log_turn(conn, character_id, text, _verdict(
            False, narration="Das bekommst du so nicht zusammengesetzt.", reason=reason,
            escalate=True, hint=hint_for(reason)))

    feas = fin["feasibility"]
    narration = fin["narration"]
    understanding = fin["understanding"]

    if feas in ("too_complex", "impossible"):
        return _log_turn(conn, character_id, text, _verdict(
            False, understanding=understanding, feasibility=feas,
            narration=narration or "Das ist so nicht möglich.",
            reason=fin["reason"] or feas, escalate=True))

    plan, rej = toolbox.plan, toolbox.rejections
    if rej and (not plan or not toolbox.allow_partial or toolbox.last_rejected):
        # Abgelehnt und nicht (erfolgreich) umgeplant: Einzelschuss -> erster Grund,
        # Schleife -> letzter Grund (der, an dem das Umplanen scheiterte).
        reason = rej[0]["reason"] if not toolbox.allow_partial else rej[-1]["reason"]
        return _log_turn(conn, character_id, text, _verdict(
            False, understanding=understanding, feasibility=feas, narration=narration,
            reason=reason, escalate=True, hint=hint_for(reason)))

    if not plan:
        # Reine Erzählung ohne Weltzustands-Effekt.
        return _log_turn(conn, character_id, text, _verdict(
            True, understanding=understanding, feasibility=feas,
            narration=narration or "Du tust es.", effects_applied=[]))

    # Sicherheitsnetz: den fertigen Plan frisch (gegen den aktuellen DB-Stand,
    # gemeinsames Staging) nochmals prüfen, bevor geschrieben wird.
    ok, reason = effects.validate_all(conn, character_id, plan, context)
    if not ok:
        return _log_turn(conn, character_id, text, _verdict(
            False, understanding=understanding, feasibility=feas, narration=narration,
            reason=reason, escalate=True, hint=hint_for(reason)))

    ok, applied, reason = effects.apply_atomic(conn, character_id, plan, context)
    if not ok:
        return _log_turn(conn, character_id, text, _verdict(
            False, understanding=understanding, feasibility=feas, narration=narration,
            reason=reason, escalate=True, hint=hint_for(reason)))

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
        return _log_turn(conn, character_id, text, _verdict(
            False, understanding=understanding, feasibility=feas,
            narration=final or narration, reason=soft_fail["result"].get("reason"),
            escalate=True, effects_applied=applied))

    return _log_turn(conn, character_id, text, _verdict(
        True, understanding=understanding, feasibility=feas,
        narration=final, effects_applied=applied))


# --- Player-Override ----------------------------------------------------
def override(
    conn: sqlite3.Connection, character_id: int, text: str, reason: str,
    *, requirement: str | None = None,
) -> dict[str, Any]:
    """Spieler ficht eine Ablehnung an. Verallgemeinert: JEDE Anforderung aus
    ``requirements`` (``missing:<req>`` / ``no_<req>``, z.B. heat, power,
    transmitter) ist anfechtbar. Nennt er in ``reason`` einen besessenen
    Gegenstand, wird dieser als ``provides:<req>`` (player_verified) gelernt;
    danach erneute Adjudikation.

    Welche Anforderung gemeint ist, ergibt sich aus der Ablehnung der Absicht:
    ein Trockenlauf der Schleife (schreibt nichts) liefert die Ablehnungsgründe;
    der erste, der eine echte Anforderung benennt, gilt (``requirement=``
    erzwingt sie explizit). Gibt es nichts Anfechtbares, wird ohne Lernen
    normal adjudiziert. Kuratierte Fakten bleiben unantastbar (KB-Vorrang)."""
    with conn:
        kb.seed_defaults(conn)
    char = conn.execute(
        "SELECT group_id FROM characters WHERE id = ?;", (character_id,)
    ).fetchone()
    item = _find_owned_item_in_text(conn, char["group_id"], reason) if char else None
    if item is None:
        return _verdict(False, escalate=True, reason="override_unclear",
                        narration="Womit genau? Ich erkenne keinen passenden "
                        "Gegenstand in deiner Begründung.")

    req = requirement
    if req is None:
        context = build_context(conn, character_id)
        if context["player"] and context["player"]["is_alive"]:
            toolbox, _ = _run_loop(conn, character_id, text, context)
            for rej in toolbox.rejections:
                req = requirements.requirement_from_reason(conn, rej["reason"])
                if req:
                    break
    if req is None or not requirements.known_requirement(conn, req):
        return adjudicate(conn, character_id, text)

    with conn:
        now_tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
        kb.add(conn, f"provides:{req}", item, {"consume": 1}, "player_verified", now_tick)
    # adjudicate() schreibt den Spieler-Input (``text``) und die Narration
    # bereits selbst ins chat_log — kein erneutes Schreiben hier.
    result = adjudicate(conn, character_id, text)
    result["override_learned"] = {"topic": f"provides:{req}", "key": item}
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


_hint_for = hint_for  # Rückwärtskompatibler Name
