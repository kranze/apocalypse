"""Ende der Woche: Bewohnbarkeit, Entscheidung, Bewertung, Epilog (Slice M5, #49).

Eisernes Prinzip: Der Sim-Kern *bewertet* (``evaluate`` ist rein und
deterministisch: Inventar, Bedürfnisse, Wissen, Companion -> Komponenten ->
Score -> Outcome). Das LLM erzählt danach nur den Epilog (``narrate_epilogue``)
auf Basis der fertigen Bewertung; es setzt weder Score noch Outcome. Der Epilog
wird genau einmal erzeugt und gespeichert. Netz-I/O (LLM) läuft NIE innerhalb
einer Schreib-Transaktion (Issue #52): erst lesen + erzählen, dann in einer
kurzen Transaktion persistieren.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ..llm import get_backend
from ..llm.stub import RuleBackend
from . import companion, constants, decay_world, effects, generation, online
from .events import DECISION, emit_event

PLAYER_ID = 1

OUTCOME_LABELS = {
    "stay_secure": "Bleiben in Sicherheit",
    "stay_fragile": "Bleiben auf dünnem Eis",
    "go_hopeful": "Aufbruch mit Hoffnung",
    "go_desperate": "Aufbruch aus Verzweiflung",
    "death": "Der Tod",
}
CHOICES = ("stay", "go")
TRANSPORT_LABELS = {"foot": "zu Fuß", "bike": "Fahrrad", "car": "Auto"}
DIRECTIONS = (("north", "Richtung Norden"), ("east", "Richtung Osten"),
              ("south", "Richtung Süden"), ("west", "Richtung Westen"))
DECISION_MESSAGE = "Die Woche ist um. Du musst dich entscheiden."


# --- kleine Helfer ------------------------------------------------------------
def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _world(conn):
    return conn.execute(
        "SELECT tick, start_datetime, ended, decision_notified FROM world WHERE id = 1;"
    ).fetchone()


def _day_at(start_datetime: str, tick: int) -> int:
    start = datetime.fromisoformat(start_datetime)
    return (start + timedelta(minutes=tick)).date().toordinal() - start.date().toordinal()


def current_day(conn) -> int:
    w = _world(conn)
    return _day_at(w["start_datetime"], w["tick"])


def is_ended(conn) -> bool:
    return bool(_world(conn)["ended"])


def decision_open(conn) -> bool:
    """Ab Tag ``DECISION_FROM_DAY`` darf entschieden werden."""
    return current_day(conn) >= constants.DECISION_FROM_DAY


def deadline_reached(conn) -> bool:
    """Tag 7, 07:00 (Spielzeit) erreicht: ab jetzt nur noch ``decide``."""
    w = _world(conn)
    start = datetime.fromisoformat(w["start_datetime"])
    dl = (datetime.combine(start.date(), datetime.min.time())
          + timedelta(days=constants.DECISION_DEADLINE_DAY, hours=constants.DECISION_DEADLINE_HOUR))
    return start + timedelta(minutes=w["tick"]) >= dl


def check_deadline(conn, tick: int) -> list[dict[str, Any]]:
    """Phase 5 des Ticks: sendet den Entscheidungs-Interrupt genau einmal.
    Läuft in der Tick-Transaktion; nur DB-Schreiben, kein Netz."""
    w = _world(conn)
    if w["ended"] or w["decision_notified"] or not deadline_reached(conn):
        return []
    conn.execute("UPDATE world SET decision_notified = 1 WHERE id = 1;")
    return [emit_event(conn, tick, "world", DECISION_MESSAGE, severity=DECISION,
                       subject_type="character", subject_id=PLAYER_ID,
                       payload={"decision": True})]


# --- Statistik (vom Sim-Kern gezählt) -------------------------------------------
def bump_stats(conn, *, actions: int = 0, freetext: int = 0, freetext_rejected: int = 0,
               meters: float = 0.0) -> None:
    conn.execute("INSERT OR IGNORE INTO game_stats (id) VALUES (1);")
    conn.execute(
        "UPDATE game_stats SET actions = actions + ?, freetext = freetext + ?, "
        "freetext_rejected = freetext_rejected + ?, meters_walked = meters_walked + ? "
        "WHERE id = 1;", (actions, freetext, freetext_rejected, meters))


def stats(conn) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM game_stats WHERE id = 1;").fetchone()
    comp = companion.get(conn)
    return {
        "actions": r["actions"] if r else 0,
        "freetext": r["freetext"] if r else 0,
        "freetext_rejected": r["freetext_rejected"] if r else 0,
        "km_walked": round((r["meters_walked"] if r else 0.0) / 1000.0, 1),
        "packages": [p["id"] for p in online.packages() if online.has_knowledge(conn, p["id"])],
        "companion": comp["stage"] if comp else "none",
    }


def reset(conn) -> None:
    """Neues Spiel: Ende + Statistik zurücksetzen."""
    with conn:
        conn.execute("DELETE FROM game_ending;")
        conn.execute("DELETE FROM game_stats;")
        conn.execute("INSERT INTO game_stats (id) VALUES (1);")
        conn.execute("UPDATE world SET ended = 0, decision_notified = 0 WHERE id = 1;")


# --- Inventar-Lesehilfen --------------------------------------------------------
def _player(conn):
    return conn.execute("SELECT * FROM characters WHERE id = ?;", (PLAYER_ID,)).fetchone()


def _gid(conn) -> int:
    p = _player(conn)
    return p["group_id"] if p and p["group_id"] is not None else 1


def _qty(conn, gid: int, item_id: str) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(quantity), 0.0) AS q FROM group_inventory "
        "WHERE group_id = ? AND item_id = ?;", (gid, item_id)).fetchone()["q"]


def _water_l(conn, gid: int) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(gi.quantity), 0.0) AS q FROM group_inventory gi "
        "JOIN item_catalog ic ON ic.id = gi.item_id "
        "WHERE gi.group_id = ? AND ic.category = 'water';", (gid,)).fetchone()["q"]


def _food_kcal(conn, gid: int) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(gi.quantity * ic.kcal_per_unit), 0.0) AS k FROM group_inventory gi "
        "JOIN item_catalog ic ON ic.id = gi.item_id "
        "WHERE gi.group_id = ? AND ic.category = 'food' AND ic.kcal_per_unit IS NOT NULL;",
        (gid,)).fetchone()["k"]


def _persons(conn) -> int:
    c = companion.get(conn)
    return 2 if c and c["stage"] == "joined" else 1


# --- Bewohnbarkeit --------------------------------------------------------------
def _here(conn):
    p = _player(conn)
    if p is None or p["lat"] is None:
        return None
    return effects.resolve_location(conn, p, None, reach=constants.LOOT_REACH_M)


def habitability(conn) -> dict[str, Any]:
    """Bewohnbarkeit (0..1, deterministisch, rein lesend) des Viertels um den
    Spieler: ``{value, label, parts}``; ``parts`` = {key: 0..1 (1 = gut)}."""
    w = _world(conn)
    day = w["tick"] / constants.MINUTES_PER_DAY
    p = _player(conn)
    lat = p["lat"] if p else None
    lon = p["lon"] if p else None

    # Dichte + erreichbare Vorräte (nur bei bekannter Position)
    density, supply = 0.0, 0.0
    if lat is not None:
        r = max(constants.HAB_DENSITY_RADIUS_M, constants.HAB_SUPPLY_RADIUS_M)
        dlat = r / 111_000.0
        dlon = r / (111_000.0 * max(0.2, math.cos(math.radians(lat))))
        n_dense = 0
        for loc in conn.execute(
            "SELECT id, type, lat, lon FROM locations WHERE lat BETWEEN ? AND ? "
            "AND lon BETWEEN ? AND ?;", (lat - dlat, lat + dlat, lon - dlon, lon + dlon)
        ).fetchall():
            d = effects._dist(lat, lon, loc["lat"], loc["lon"])
            if d <= constants.HAB_DENSITY_RADIUS_M:
                n_dense += 1
            if d <= constants.HAB_SUPPLY_RADIUS_M and not generation.is_searched(conn, loc["id"]):
                if loc["type"] == "supermarket":
                    supply += constants.HAB_SUPPLY_SUPERMARKET
                elif loc["type"] == "house":
                    supply += constants.HAB_SUPPLY_HOUSE
        density = _clamp01(n_dense / constants.HAB_DENSITY_FULL)

    smell = _clamp01(day / constants.HAB_DECAY_FULL_DAYS) * (0.4 + 0.6 * density)
    here = _here(conn)
    if here is not None:
        infra = decay_world.infra_for(conn, here["id"])
        water, power = infra["water"], infra["power"]
    else:
        wi = decay_world.world_infra(conn)
        water, power = wi["water"], wi["power"]
    risk = _clamp01(day / constants.HAB_DISEASE_FULL_DAYS) * constants.HAB_DISEASE_MAX
    if online.has_knowledge(conn, "disease_control"):
        risk *= constants.HAB_DISEASE_KNOWLEDGE_FACTOR
    parts = {
        "decay": round(1.0 - smell, 4),
        "water": round(_clamp01(water), 4),
        "power": round(_clamp01(power), 4),
        "supplies": round(_clamp01(supply / constants.HAB_SUPPLY_FULL), 4),
        "disease": round(1.0 - risk, 4),
    }
    value = round(sum(constants.HAB_WEIGHTS[k] * v for k, v in parts.items()), 4)
    label = ("bewohnbar" if value >= constants.HAB_LABEL_OK
             else "angespannt" if value >= constants.HAB_LABEL_TENSE else "kritisch")
    return {"value": value, "label": label, "parts": parts}


# --- Entscheidungs-Optionen ------------------------------------------------------
def _contacts(conn) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, text FROM online_contacts WHERE kind = 'reply' ORDER BY id;").fetchall()]


def transports(conn) -> list[dict[str, Any]]:
    gid = _gid(conn)
    out = [{"id": "foot", "label": TRANSPORT_LABELS["foot"], "ok": True}]
    bike = _qty(conn, gid, "bicycle") >= 1
    t = {"id": "bike", "label": TRANSPORT_LABELS["bike"], "ok": bike}
    if not bike:
        t["missing"] = "Fahrrad"
    out.append(t)
    knows = online.has_knowledge(conn, "car_fuel")
    fuel = _qty(conn, gid, "gasoline") >= 1
    t = {"id": "car", "label": TRANSPORT_LABELS["car"], "ok": knows and fuel}
    if not knows:
        t["missing"] = "Wissen: Auto & Sprit"
    elif not fuel:
        t["missing"] = "Benzin"
    out.append(t)
    return out


def destinations(conn) -> list[dict[str, str]]:
    out = [{"id": i, "label": label} for i, label in DIRECTIONS]
    comp = companion.view(conn)
    for c in _contacts(conn):
        who = comp["name"] if comp else "dem Überlebenden, der sich gemeldet hat"
        out.append({"id": f"contact:{c['id']}", "label": f"Zu {who}"})
    return out


def decision_options(conn) -> dict[str, Any]:
    return {"choices": list(CHOICES), "transports": transports(conn),
            "destinations": destinations(conn)}


# --- Bewertung (rein, deterministisch) ----------------------------------------------
def _knowledge_value(conn, relevance: dict[str, float]) -> tuple[float, list[str]]:
    have = [k for k in relevance if online.has_knowledge(conn, k)]
    total = sum(relevance.values())
    return (sum(relevance[k] for k in have) / total if total else 0.0), have


def _days_text(days: float, what: str) -> str:
    if days < 0.5:
        return f"Kaum {what} übrig"
    return f"{what.capitalize()} für {days:.0f} Tage" if days >= 1 else f"{what.capitalize()} für unter einen Tag"


def _comp(key, label, value, weight, text) -> dict[str, Any]:
    return {"key": key, "label": label, "value": round(_clamp01(value), 4),
            "weight": weight, "text": text}


def _health(conn) -> tuple[float, str]:
    p = _player(conn)
    needs = (p["hunger"], p["thirst"], p["sleep"])
    v = 0.5 * p["performance"] + 0.5 * (sum(needs) / 3.0)
    text = ("Du bist bei Kräften" if v >= 0.7 else "Du bist angeschlagen" if v >= 0.4
            else "Du bist am Ende deiner Kräfte")
    return _clamp01(v), text


def _companion_value(conn) -> tuple[float, str]:
    c = companion.get(conn)
    if c is None or c["stage"] in ("left", "dead", "contacted", "approaching"):
        return 0.0, "Du bist allein"
    if c["stage"] == "joined":
        return (constants.END_COMPANION_JOINED_BASE
                + (1.0 - constants.END_COMPANION_JOINED_BASE) * c["trust"],
                f"Ein Begleiter ist bei dir (Vertrauen {c['trust'] * 100:.0f} %)")
    return constants.END_COMPANION_MET, "Ihr kennt euch, aber er gehört nicht zu dir"


def evaluate(conn, choice: str, transport: str | None = None,
             destination: str | None = None) -> dict[str, Any]:
    """Bewertet den aktuellen Zustand für die gewählte Option. Liefert
    ``{choice, transport, destination, components, score, outcome, outcome_label}``.
    Rein lesend und deterministisch — das LLM ist nicht beteiligt."""
    stay = choice == "stay"
    weights = constants.END_WEIGHTS_STAY if stay else constants.END_WEIGHTS_GO
    target = constants.END_TARGET_DAYS_STAY if stay else constants.END_TARGET_DAYS_GO
    p = _player(conn)
    gid = _gid(conn)
    persons = _persons(conn)

    # Vorrat in Tagen = Vorrat / tatsächlicher Verbrauch (die Biologie verbraucht je Tag nur
    # THIRST/HUNGER_LOSS_PER_DAY des Tagesbedarfs; sonst wäre die Bewertung 2-3x strenger).
    water_days = _water_l(conn, gid) / max(
        0.1, p["daily_water_l"] * constants.THIRST_LOSS_PER_DAY * persons)
    treat = stay and online.has_knowledge(conn, "water_treatment")
    water_eff = water_days + (constants.END_WATER_TREATMENT_BONUS_DAYS if treat else 0.0)
    water_text = _days_text(water_days, "Wasser") + (" (plus Aufbereitung)" if treat else "")
    food_days = _food_kcal(conn, gid) / max(
        1.0, p["daily_kcal"] * constants.HUNGER_LOSS_PER_DAY * persons)

    health, health_text = _health(conn)
    kv, have = _knowledge_value(
        conn, constants.END_KNOWLEDGE_STAY if stay else constants.END_KNOWLEDGE_GO)
    comp_v, comp_text = _companion_value(conn)

    vals: dict[str, tuple[str, float, str]] = {
        "water": ("Wasser", water_eff / target, water_text),
        "food": ("Essen", food_days / target, _days_text(food_days, "Essen")),
        "health": ("Gesundheit", health, health_text),
        "knowledge": ("Wissen", kv,
                      f"{len(have)} nützliche Pakete geladen" if have else "Kein nützliches Wissen"),
        "companion": ("Begleitung", comp_v, comp_text),
    }
    if stay:
        hab = habitability(conn)
        vals["habitability"] = ("Bewohnbarkeit", hab["value"], f"Das Viertel ist {hab['label']}")
    else:
        tv = constants.END_TRANSPORT_VALUE.get(transport or "", 0.0)
        gear_n = sum(1 for i in constants.END_GEAR_ITEMS if _qty(conn, gid, i) >= 1)
        eq = _clamp01(gear_n / constants.END_GEAR_FULL_COUNT)
        share = constants.END_GEAR_TRANSPORT_SHARE
        vals["gear"] = ("Ausrüstung und Transport", share * tv + (1 - share) * eq,
                        f"{TRANSPORT_LABELS.get(transport or '', '?')}, {gear_n} Ausrüstungsteile")
        contact = (destination or "").startswith("contact:")
        vals["destination"] = (
            "Ziel", constants.END_DEST_CONTACT if contact else constants.END_DEST_DIRECTION,
            "Jemand erwartet dich" if contact else "Ein Weg ins Ungewisse")

    components = [_comp(k, *vals[k][:2], weights[k], vals[k][2]) for k in weights]
    score = round(sum(c["value"] * c["weight"] for c in components), 4)
    if stay:
        outcome = "stay_secure" if score >= constants.END_THRESHOLD_STAY_SECURE else "stay_fragile"
    else:
        outcome = "go_hopeful" if score >= constants.END_THRESHOLD_GO_HOPEFUL else "go_desperate"
    return {"choice": choice, "transport": None if stay else transport,
            "destination": None if stay else destination, "components": components,
            "score": score, "outcome": outcome, "outcome_label": OUTCOME_LABELS[outcome]}


def cause_of_death(conn) -> str:
    """Schwächstes Bedürfnis zum Todeszeitpunkt (Gleichstand: Durst, Hunger, Schlaf)."""
    p = _player(conn)
    order = (("thirst", "verdurstet"), ("hunger", "verhungert"), ("sleep", "erschöpft"))
    return min(order, key=lambda kv: p[kv[0]])[1]


# --- Epilog + Persistenz -----------------------------------------------------------
def _highlights(conn, limit: int = 8) -> list[str]:
    rows = conn.execute(
        "SELECT message FROM events WHERE severity != 'info' AND category IN "
        "('contact', 'companion', 'location', 'world') ORDER BY id DESC LIMIT 40;").fetchall()
    seen, out = set(), []
    for r in rows:
        m = r["message"]
        if m not in seen and "hat das Ziel erreicht" not in m:
            seen.add(m)
            out.append(m)
        if len(out) >= limit:
            break
    return list(reversed(out))


def _profile(conn) -> dict[str, Any]:
    p = _player(conn)
    return {k: p[k] for k in ("name", "age", "profession", "family", "hobbies",
                              "self_description")}


def _companion_ctx(conn) -> dict[str, Any] | None:
    v = companion.view(conn)
    if v is None:
        return None
    return {k: v[k] for k in ("name", "profession", "stage", "trust", "opinion",
                              "personality", "short_bio")}


def _epilogue(conn, evaluation: dict[str, Any]) -> str:
    """Holt den Epilog vom LLM-Backend (reiner Text); bei Fehler/Leere der Stub.
    Das Backend bekommt die fertige Bewertung und kann sie nicht verändern."""
    args = (json.loads(json.dumps(evaluation)), _profile(conn), _companion_ctx(conn),
            _highlights(conn))
    text = ""
    try:
        text = get_backend().narrate_epilogue(*args)
    except Exception:
        text = ""
    if not isinstance(text, str) or not text.strip():
        text = RuleBackend().narrate_epilogue(*args)
    return text.strip()[: constants.EPILOGUE_MAX_CHARS]


def _persist(conn, evaluation: dict[str, Any], epilogue: str, cause: str | None) -> bool:
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO game_ending (id, outcome, choice, transport, destination, "
            "score, components_json, epilogue, cause_of_death, ended_tick) "
            "VALUES (1,?,?,?,?,?,?,?,?, (SELECT tick FROM world WHERE id = 1));",
            (evaluation["outcome"], evaluation["choice"], evaluation["transport"],
             evaluation["destination"], evaluation["score"],
             json.dumps(evaluation["components"], ensure_ascii=False), epilogue, cause))
        conn.execute("UPDATE world SET ended = 1 WHERE id = 1;")
    return cur.rowcount > 0


def finish(conn, choice: str, transport: str | None = None,
           destination: str | None = None) -> dict[str, Any]:
    """Beendet das Spiel mit der gewählten Option (Eingaben vorher validiert).
    Idempotent: ein zweiter Aufruf ändert nichts."""
    if is_ended(conn):
        return get_ending(conn)
    ev = evaluate(conn, choice, transport, destination)
    _persist(conn, ev, _epilogue(conn, ev), None)
    return get_ending(conn)


def finish_death(conn) -> dict[str, Any]:
    """Der Spieler ist gestorben: Spiel beenden (outcome ``death``), Epilog erzählen."""
    if is_ended(conn):
        return get_ending(conn)
    cause = cause_of_death(conn)
    ev = {"choice": None, "transport": None, "destination": None, "components": [],
          "score": 0.0, "outcome": "death", "outcome_label": OUTCOME_LABELS["death"],
          "cause_of_death": cause}
    _persist(conn, ev, _epilogue(conn, ev), cause)
    return get_ending(conn)


def get_ending(conn) -> dict[str, Any] | None:
    """Der Ending-Vertrag (GET /ending) oder None, solange das Spiel läuft."""
    r = conn.execute("SELECT * FROM game_ending WHERE id = 1;").fetchone()
    if r is None:
        return None
    w = _world(conn)
    return {
        "ended": True, "outcome": r["outcome"], "outcome_label": OUTCOME_LABELS[r["outcome"]],
        "choice": r["choice"], "transport": r["transport"], "destination": r["destination"],
        "day": _day_at(w["start_datetime"], r["ended_tick"]),
        "cause_of_death": r["cause_of_death"], "score": r["score"],
        "components": json.loads(r["components_json"]),
        "epilogue": r["epilogue"], "stats": stats(conn),
    }
