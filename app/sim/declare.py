"""Spielerwissen: plausible Gegenstände deklarieren (Issue #54).

KONTROLLIERTE AUSNAHME vom Eisernen Prinzip. Der Spieler kennt den Inhalt seines
Hauses und einiger Nachbarn (100-kWh-Akku im Keller, Schweißgerät in der Garage)
und darf ihn ins Spiel einbringen. Das ist KEINE Halluzination, sondern eine vom
Spieler verantwortete, gebuchte Quelle:

* Das LLM schlägt nur vor (Name, Kategorie, Gewicht, Menge, ``provides``, Kapazität
  und ein Plausibilitätsurteil ``plausible`` + Begründung) — es schreibt nie.
* Der Sim-Kern (dieses Modul) entscheidet: Unplausibles wird mit Begründung
  abgelehnt, alles Übrige hart geklemmt (Gewicht, Menge, Kapazität, ``provides``
  nur aus ``requirements.KNOWN``, Kategorien aus fester Liste, Gesamtlimit).
* Angelegt wird per Sim-Kern-Funktion (``apply``): Katalog-Eintrag, Inventar am
  erlaubten Ort, Ledger-Buchung mit der expliziten Quelle ``player_declared``,
  Protokoll in ``declared_items``. Die Bilanz-Prüfung kennt die Quelle.
* Nur an erlaubten Orten: in der eigenen Wohnung und in Gebäuden innerhalb
  ``DECLARE_RADIUS_M`` um das Zuhause. Woanders: ``declare_not_here``.

Die Suche bleibt davon unberührt: sie erfindet weiterhin nichts (siehe
``effects._a_search``).
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from ..llm import get_backend
from . import constants, home, kb, ledger, requirements
from .events import emit_event
from .rng import roll

SOURCE = ledger.SOURCE_PLAYER_DECLARED

# Bekannte Alltagsgegenstände des Katalogs: ein deklarierter Gegenstand dieses Namens
# wird auf den vorhandenen Katalog-Eintrag abgebildet (z.B. zählt ein deklariertes
# Fahrrad für die Entscheidung am Ende wie jedes andere).
_STOCK_ALIAS = (
    ("fahrrad", "bicycle"), ("taschenlampe", "flashlight"), ("hammer", "hammer"),
    ("klebeband", "duct_tape"), ("verbandkasten", "first_aid_kit"),
)

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


# --- Vorschlag normalisieren (defensiv) --------------------------------------
def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _str_list(v: Any) -> list[str]:
    if not isinstance(v, (list, tuple)):
        return []
    out: list[str] = []
    for x in v:
        if isinstance(x, str) and x.strip() and x.strip().lower() not in out:
            out.append(x.strip().lower())
    return out


def coerce(raw: Any) -> dict[str, Any]:
    """Bringt einen LLM-Vorschlag in eine feste Form (Typen, Längen). Unbrauchbares
    wird zu None/leer; Urteile über Inhalt trifft erst ``evaluate``."""
    r = raw if isinstance(raw, dict) else {}
    where = str(r.get("where") or "here").lower()
    return {
        "name": " ".join(str(r.get("name") or "").split())[:constants.DECLARE_NAME_MAX],
        "category": str(r.get("category") or "").strip().lower(),
        "weight_kg": _num(r.get("weight_kg")),
        "quantity": _num(r.get("quantity")),
        "provides": _str_list(r.get("provides")),
        "requires": _str_list(r.get("requires")),
        "capacity_kwh": _num(r.get("capacity_kwh")),
        "charge_kwh": _num(r.get("charge_kwh")),
        "plausible": r.get("plausible") is True,
        "reason": " ".join(str(r.get("reason") or "").split())[:constants.DECLARE_REASON_MAX],
        "where": "neighbour" if where.startswith("neighbo") else "here",
    }


def declared_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM declared_items WHERE status IN ('placed','pending');"
    ).fetchone()["c"]


# --- Validierung + Klemmen (read-only) ---------------------------------------
def evaluate(conn: sqlite3.Connection, raw: Any) -> tuple[bool, str | None, dict[str, Any]]:
    """Prüft und klemmt EINEN Vorschlag. Liefert (ok, Grund, geklemmter Vorschlag).
    Schreibt nichts. Gründe: ``declare_no_name``, ``declare_implausible:<Text>``,
    ``declare_bad_category``, ``declare_limit``."""
    p = coerce(raw)
    if not p["name"]:
        return (False, "declare_no_name", p)
    if not p["plausible"]:
        return (False, "declare_implausible:" + (p["reason"] or "Das gibt es so nicht."), p)
    if p["category"] not in constants.DECLARE_CATEGORIES:
        return (False, "declare_bad_category", p)
    if declared_count(conn) >= constants.DECLARE_MAX_ITEMS:
        return (False, "declare_limit", p)

    notes: list[str] = []
    weight = p["weight_kg"] if p["weight_kg"] is not None else 1.0
    if weight > constants.DECLARE_REJECT_UNIT_WEIGHT_KG:
        # Sicherheitsnetz hinter dem LLM-Urteil: so schwer steht nichts im Keller.
        return (False, "declare_implausible:Das ist viel zu schwer und zu groß für ein Haus.", p)
    weight = max(0.01, weight)
    if weight > constants.DECLARE_MAX_UNIT_WEIGHT_KG:
        weight = constants.DECLARE_MAX_UNIT_WEIGHT_KG
        notes.append("weight_clamped")

    qty = p["quantity"] if p["quantity"] is not None else 1.0
    qty = float(max(1, min(constants.DECLARE_MAX_QTY, int(round(qty)))))

    requires = [r for r in p["requires"] if r in requirements.KNOWN]
    cap = p["capacity_kwh"]
    kind, provides, charge = "plain", [], None
    category = p["category"]
    if cap is not None and cap > 0:  # Energiespeicher: genau ein Stück, endliche kWh
        kind = "storage"
        if cap > constants.DECLARE_MAX_CAPACITY_KWH:
            cap = constants.DECLARE_MAX_CAPACITY_KWH
            notes.append("capacity_clamped")
        charge = p["charge_kwh"]
        if charge is None:
            charge = cap * constants.DECLARE_DEFAULT_CHARGE_FRAC
        charge = max(0.0, min(cap, charge))
        qty, category = 1.0, "tool"
        provides = [r for r in p["provides"] if r in requirements.KNOWN and r != "power"]
    else:
        cap = None
        provides = [r for r in p["provides"] if r in requirements.KNOWN]
        if provides:
            kind = "provider"
    if len(provides) < len(p["provides"]):
        notes.append("provides_clamped")

    item_id = None
    if category == "water":
        # Wasser ist immer das Standard-Wasser (Trinken/Versorgung kennen nur das).
        item_id = "water_1l"
        qty = float(min(constants.DECLARE_MAX_QTY * 2, max(1.0, round(weight * qty))))
        weight = 1.0
    else:
        low = p["name"].lower()
        if kind == "plain":
            item_id = next((i for k, i in _STOCK_ALIAS if k in low), None)
        if qty * weight > constants.DECLARE_MAX_TOTAL_KG:
            qty = float(max(1, int(constants.DECLARE_MAX_TOTAL_KG // weight)))
            notes.append("quantity_clamped")

    return (True, None, {
        "name": p["name"], "category": category, "item_id": item_id,
        "weight_kg": round(weight, 3), "quantity": qty, "kind": kind,
        "provides": provides, "requires": requires,
        "capacity_kwh": cap, "charge_kwh": charge, "where": p["where"],
        "kcal_per_unit": (round(weight * constants.DECLARE_KCAL_PER_KG)
                          if category == "food" else None),
        "notes": notes,
    })


# --- Ort bestimmen (read-only) -----------------------------------------------
def label_of(loc: Any) -> str:
    return (loc["name"] or loc["label"] or loc["type"]) if loc is not None else "?"


def locate(conn: sqlite3.Connection, player: Any, where: str,
           key: str) -> tuple[Any, str | None]:
    """Ort, an dem der Gegenstand steht: (Location-Zeile, None) oder (None, Grund).
    ``here``: das Gebäude, an dem der Spieler steht — nur in der Wohnung oder
    innerhalb ``DECLARE_RADIUS_M`` um sie, sonst ``declare_not_here``.
    ``neighbour``: deterministisch eines der drei nächsten Wohngebäude um das Zuhause."""
    h = home.home_location(conn)
    if h is None:
        return (None, "declare_not_here")
    if where == "neighbour":
        cands = home.neighbours(conn)
        houses = [c for c in cands if c["type"] == "house"] or cands
        if not houses:
            return (None, "declare_no_neighbour")
        pool = houses[:3]
        seed = conn.execute("SELECT world_seed FROM world WHERE id = 1;").fetchone()["world_seed"]
        pick = pool[min(len(pool) - 1, int(roll(seed, "declare_neighbour", key) * len(pool)))]
        return (conn.execute("SELECT * FROM locations WHERE id = ?;", (pick["id"],)).fetchone(), None)
    if player is None or player["lat"] is None:
        return (None, "declare_not_here")
    best, best_d = None, constants.DECLARE_HERE_REACH_M
    for r in conn.execute("SELECT * FROM locations;").fetchall():
        d = home.dist_m(player["lat"], player["lon"], r["lat"], r["lon"])
        if d <= best_d:
            best, best_d = r, d
    if best is None:
        return (None, "declare_not_here")
    if best["id"] != h["id"] and home.dist_m(
            h["lat"], h["lon"], best["lat"], best["lon"]) > constants.DECLARE_RADIUS_M:
        return (None, "declare_not_here")
    return (best, None)


def check(conn: sqlite3.Connection, raw: Any, player: Any,
          pending_ok: bool = False) -> tuple[bool, str | None, dict[str, Any], Any]:
    """Vollprüfung (Vorschlag + Ort), read-only: (ok, Grund, geklemmt, Location).
    ``pending_ok``: ein Nachbar ohne geladenes Gebäude ist kein Fehler (Onboarding)."""
    ok, reason, c = evaluate(conn, raw)
    if not ok:
        return (False, reason, c, None)
    loc, why = locate(conn, player, c["where"], c["name"].lower())
    if loc is None and not (pending_ok and why == "declare_no_neighbour"):
        return (False, why, c, None)
    return (True, None, c, loc)


# --- Anlegen (Sim-Kern-Schreiben, ledger-gebucht) -----------------------------
def _slug(name: str) -> str:
    s = "".join(ch if ch.isalnum() else "_" for ch in name.lower().translate(_UMLAUTS))
    return "_".join(x for x in s.split("_") if x)[:20] or "item"


def _ensure_catalog(conn: sqlite3.Connection, c: dict[str, Any]) -> str:
    if c["item_id"]:
        return c["item_id"]
    n = declared_count(conn) + 1
    item_id = f"decl_{n}_{_slug(c['name'])}"
    while conn.execute("SELECT 1 FROM item_catalog WHERE id = ?;", (item_id,)).fetchone():
        n += 1
        item_id = f"decl_{n}_{_slug(c['name'])}"
    conn.execute(
        "INSERT INTO item_catalog (id, name, category, weight_kg, kcal_per_unit, "
        "decay_halflife_min, stackable) VALUES (?, ?, ?, ?, ?, NULL, ?);",
        (item_id, c["name"], c["category"], c["weight_kg"], c["kcal_per_unit"],
         1 if c["category"] == "food" else 0),
    )
    return item_id


def apply(conn: sqlite3.Connection, c: dict[str, Any], location_id: int, *,
          source_text: str = "", via: str = "freetext",
          row_id: int | None = None) -> dict[str, Any]:
    """Legt einen GEPRÜFTEN, geklemmten Gegenstand am Ort an (atomar): Katalog,
    Location-Inventar, Ledger-Quelle ``player_declared``, Energiespeicher-Zustand,
    gelernte ``provides:*``-Fakten (Provenance ``player_declared``), Protokoll, Event."""
    with conn:
        tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
        loc = conn.execute("SELECT * FROM locations WHERE id = ?;", (location_id,)).fetchone()
        if loc is None:
            return {"ok": False, "reason": "no_such_location"}
        item_id = _ensure_catalog(conn, c)
        conn.execute(
            "INSERT INTO location_inventory (location_id, item_id, quantity, quality, "
            "produced_tick) VALUES (?, ?, ?, 1.0, ?);",
            (location_id, item_id, c["quantity"], tick))
        ledger.add(conn, item_id, c["quantity"], source=SOURCE)
        if c["kind"] == "storage":
            conn.execute(
                "INSERT OR REPLACE INTO energy_state (item_id, capacity_kwh, charge_kwh, "
                "updated_tick) VALUES (?, ?, ?, ?);",
                (item_id, c["capacity_kwh"], c["charge_kwh"], tick))
        for req in c["provides"]:
            kb.add(conn, f"provides:{req}", item_id, {"consume": 0}, "player_declared", tick)
        vals = (item_id, c["name"], location_id, c["where"], c["quantity"], c["kind"],
                json.dumps(c["provides"]), json.dumps(c["requires"]), c["capacity_kwh"],
                source_text[:300], via, json.dumps(c, ensure_ascii=False), tick)
        if row_id is None:
            conn.execute(
                "INSERT INTO declared_items (item_id, name, status, location_id, where_hint, "
                "quantity, kind, provides_json, requires_json, capacity_kwh, source_text, "
                "via, proposal_json, created_tick) "
                "VALUES (?, ?, 'placed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);", vals)
        else:
            conn.execute(
                "UPDATE declared_items SET item_id = ?, name = ?, status = 'placed', "
                "location_id = ?, where_hint = ?, quantity = ?, kind = ?, provides_json = ?, "
                "requires_json = ?, capacity_kwh = ?, source_text = ?, via = ?, "
                "proposal_json = ?, created_tick = ? WHERE id = ?;", vals + (row_id,))
        emit_event(
            conn, tick, "location",
            f"Spielerwissen: {c['name']} steht in {label_of(loc)}.",
            subject_type="location", subject_id=location_id,
            payload={"item": item_id, "quantity": c["quantity"], "source": SOURCE})
    return {"ok": True, "item_id": item_id, "location_id": location_id,
            "location": label_of(loc), "quantity": c["quantity"], "kind": c["kind"],
            "narration": narration_for(c, loc)}


def narration_for(c: dict[str, Any], loc: Any) -> str:
    where = "in deiner Wohnung" if loc is not None and loc["type"] == home.HOME_TYPE \
        else f"bei den Nachbarn ({label_of(loc)})"
    extra = ""
    if c["kind"] == "storage":
        # Kapazität nicht doppeln, wenn der Name sie schon trägt („Hausakku (100 kWh)“)
        cap = "" if "kwh" in c["name"].lower() else f"{c['capacity_kwh']:g} kWh, "
        extra = f" ({cap}{c['charge_kwh']:g} kWh geladen)"
    qty = f"{c['quantity']:g}x " if c["quantity"] > 1 else ""
    return f"Du weißt es genau: {qty}{c['name']}{extra} steht {where}."


def _record(conn: sqlite3.Connection, c: dict[str, Any], status: str, reason: str | None,
            source_text: str, via: str) -> int:
    tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
    cur = conn.execute(
        "INSERT INTO declared_items (name, status, where_hint, quantity, kind, reason, "
        "source_text, via, proposal_json, created_tick) VALUES (?,?,?,?,?,?,?,?,?,?);",
        (c.get("name") or "?", status, c.get("where", "here"), c.get("quantity") or 0.0,
         c.get("kind"), reason, source_text[:300], via,
         json.dumps(c, ensure_ascii=False), tick))
    return cur.lastrowid


# --- Onboarding-Batch ------------------------------------------------------
def declare_batch(conn: sqlite3.Connection, text: str, player: Any,
                  profile: dict | None = None, via: str = "onboarding") -> list[dict[str, Any]]:
    """EIN Backend-Aufruf -> mehrere Vorschläge; jeder wird einzeln geprüft/geklemmt.
    Nachbarn bekommen deterministisch eines der nächsten Wohngebäude; ist die Umgebung
    noch nicht geladen (OSM kommt erst nach dem Spielstart), bleibt der Gegenstand
    ``pending`` und wird mit ``resolve_pending`` platziert. Liefert je Vorschlag
    {name, status: placed|pending|rejected, reason, location, quantity, kind, ...}."""
    text = (text or "").strip()
    if not text:
        return []
    ctx = {"profile": profile, "known_requirements": list(requirements.KNOWN),
           "categories": list(constants.DECLARE_CATEGORIES)}
    try:
        proposals = get_backend().declare_items(text, ctx)
    except Exception:  # LLM-Ausfall darf den Spielstart nie verhindern
        proposals = []
    results: list[dict[str, Any]] = []
    for raw in (proposals or [])[:constants.DECLARE_MAX_BATCH]:
        ok, reason, c, loc = check(conn, raw, player, pending_ok=True)
        name = c.get("name") or "?"
        if not ok:
            with conn:
                _record(conn, c, "rejected", reason, text, via)
            results.append({"name": name, "status": "rejected", "reason": reason,
                            "text": reject_text(reason)})
            continue
        if loc is None:  # Nachbar, Gebäude noch nicht geladen
            with conn:
                _record(conn, c, "pending", None, text, via)
            results.append({"name": name, "status": "pending", "reason": None,
                            "quantity": c["quantity"], "kind": c["kind"]})
            continue
        res = apply(conn, c, loc["id"], source_text=text, via=via)
        results.append({"name": name, "status": "placed", "reason": None,
                        "location": res["location"], "quantity": c["quantity"],
                        "kind": c["kind"], "capacity_kwh": c["capacity_kwh"]})
    return results


def resolve_pending(conn: sqlite3.Connection) -> int:
    """Platziert wartende Nachbar-Gegenstände, sobald Gebäude um das Zuhause geladen
    sind. Liefert die Anzahl platzierter Gegenstände. Idempotent."""
    n = 0
    for row in conn.execute(
        "SELECT id, proposal_json, source_text, via FROM declared_items "
        "WHERE status = 'pending' ORDER BY id;").fetchall():
        c = json.loads(row["proposal_json"])
        loc, _ = locate(conn, None, "neighbour", c["name"].lower())
        if loc is None:
            continue
        apply(conn, c, loc["id"], source_text=row["source_text"] or "",
              via=row["via"] or "onboarding", row_id=row["id"])
        n += 1
    return n


# --- Texte -------------------------------------------------------------------
def reject_text(reason: str | None) -> str:
    """Spielersicht auf einen Ablehnungsgrund."""
    if not reason:
        return ""
    if reason.startswith("declare_implausible:"):
        return reason.split(":", 1)[1]
    return {
        "declare_not_here": "Das weißt du über diesen Ort nicht.",
        "declare_no_neighbour": "In der Nähe kennst du kein Nachbargebäude dafür.",
        "declare_bad_category": "Diese Art von Gegenstand lässt sich so nicht deklarieren.",
        "declare_limit": "Du hast schon genug Dinge deklariert.",
        "declare_no_name": "Was genau soll das sein?",
    }.get(reason, "Das geht so nicht.")


def intro_text(results: list[dict[str, Any]]) -> str:
    """Zeilen für die Intro-Erzählung: je Gegenstand angenommen/abgelehnt."""
    if not results:
        return ""
    lines = ["Du weißt, was bei dir und deinen Nachbarn steht:"]
    for r in results:
        if r["status"] == "placed":
            q = f"{r['quantity']:g}x " if r.get("quantity", 1) > 1 else ""
            cap = f" ({r['capacity_kwh']:g} kWh)" if r.get("capacity_kwh") else ""
            lines.append(f"- {q}{r['name']}{cap}: {r['location']}.")
        elif r["status"] == "pending":
            lines.append(f"- {r['name']}: bei den Nachbarn (das Gebäude wird zugeordnet, "
                         "sobald die Umgebung geladen ist).")
        else:
            lines.append(f"- {r['name']}: abgelehnt. {r.get('text') or ''}".rstrip())
    return "\n".join(lines)


def list_declared(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Protokoll für Statistik/Endbildschirm (nicht abgelehnte, in Reihenfolge)."""
    names = {r["id"]: label_of(r) for r in conn.execute(
        "SELECT id, type, label, name FROM locations;").fetchall()}
    return [
        {"name": r["name"], "quantity": r["quantity"], "status": r["status"],
         "kind": r["kind"], "capacity_kwh": r["capacity_kwh"],
         "location": names.get(r["location_id"])}
        for r in conn.execute(
            "SELECT name, quantity, status, kind, capacity_kwh, location_id FROM declared_items "
            "WHERE status IN ('placed','pending') ORDER BY id;").fetchall()
    ]


def reset(conn: sqlite3.Connection) -> None:
    """Neues Spiel: Protokoll, Energiespeicher, Quellen und deklarierte Katalog-/KB-Einträge
    entfernen (Inventare/Ledger setzt ``game.new_game`` selbst zurück)."""
    conn.execute("DELETE FROM energy_state;")
    conn.execute("DELETE FROM ledger_sources;")
    conn.execute("DELETE FROM declared_items;")
    conn.execute("DELETE FROM knowledge_base WHERE provenance = 'player_declared';")
    conn.execute("DELETE FROM item_catalog WHERE id LIKE 'decl\\_%' ESCAPE '\\';")
