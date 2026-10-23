"""Entdeckung und deterministische Inventar-Erzeugung von Locations.

``discover`` markiert eine Location beim Betreten nur als entdeckt — ohne
Inventar. Das Inventar entsteht erst beim **ersten Durchsuchen**
(``ensure_generated``): deterministisch aus ``generation_seed`` und der
Loot-Tabelle des Ortstyps (``loot_tables``). Gleicher Seed -> gleiche Würfe,
unabhängig von der Besuchsreihenfolge. Decay seit Kollaps (Tick 0) wird zum
Zeitpunkt der Generierung eingerechnet (DESIGN.md §6): was schon verrottet ist,
findet man nicht mehr.

"Durchsucht" wird über einen Marker in ``location_searches`` (term ``'*'``)
festgehalten; die Erzeugung ist damit idempotent. Die Generierung ist die
einzige Ressourcen-Quelle und bucht ins Ledger. Das LLM ist hier nie beteiligt.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import constants, ledger
from .events import emit_event
from .loot_tables import DEFAULT_TABLE_KEY, LOOT_TABLES
from .resources import quality_at
from .rng import roll

SEARCHED_TERM = "*"  # Marker in location_searches: Ort ist durchsucht/generiert


def _halflives(conn: sqlite3.Connection) -> dict[str, int | None]:
    return {
        row["id"]: row["decay_halflife_min"]
        for row in conn.execute(
            "SELECT id, decay_halflife_min FROM item_catalog;"
        ).fetchall()
    }


def roll_inventory(
    seed: int,
    loc_type: str,
    at_tick: int,
    halflives: dict[str, int | None],
) -> list[dict[str, Any]]:
    """Reine Funktion: würfelt das Inventar einer Location aus.

    Produktionszeit aller generierten Waren = Tick 0 (Kollaps); die Qualität
    zum Zeitpunkt ``at_tick`` ergibt sich aus dem Decay seit dann. Verrottetes
    (< Schwelle) wird nicht aufgenommen.
    """
    table = LOOT_TABLES.get(loc_type, LOOT_TABLES[DEFAULT_TABLE_KEY])
    rows: list[dict[str, Any]] = []
    for entry in table:
        item = entry["item_id"]
        if roll(seed, item, "present") >= entry["chance"]:
            continue
        span = entry["qty_max"] - entry["qty_min"] + 1
        qty = entry["qty_min"] + int(roll(seed, item, "qty") * span)
        if qty <= 0:
            continue
        hl = halflives.get(item)
        quality = quality_at(0, at_tick, hl)
        if hl is not None and quality < constants.SPOIL_THRESHOLD:
            continue  # bereits verrottet -> nicht mehr auffindbar
        rows.append(
            {
                "item_id": item,
                "quantity": float(qty),
                "quality": round(quality, 4),
                "produced_tick": 0,
            }
        )
    return rows


def is_searched(conn: sqlite3.Connection, location_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM location_searches WHERE location_id = ? AND term = ?;",
        (location_id, SEARCHED_TERM),
    ).fetchone() is not None


def ensure_generated(conn: sqlite3.Connection, location_id: int) -> dict[str, Any]:
    """Erzeugt das Inventar einer Location beim ersten Durchsuchen (einmalig).

    Atomar und idempotent: ein zweiter Aufruf ändert nichts
    (``generated: False``). Liefert die erzeugten Stapel (leer, wenn nichts
    gefunden wurde oder schon generiert war).
    """
    with conn:
        loc = conn.execute(
            "SELECT * FROM locations WHERE id = ?;", (location_id,)
        ).fetchone()
        if loc is None:
            return {"ok": False, "reason": "no_such_location"}
        if is_searched(conn, location_id):
            return {"ok": True, "generated": False, "items": []}
        at_tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
        rows = roll_inventory(
            loc["generation_seed"], loc["type"], at_tick, _halflives(conn)
        )
        for r in rows:
            conn.execute(
                "INSERT INTO location_inventory (location_id, item_id, quantity, "
                "quality, produced_tick) VALUES (?, ?, ?, ?, ?);",
                (location_id, r["item_id"], r["quantity"], r["quality"], r["produced_tick"]),
            )
            ledger.add(conn, r["item_id"], r["quantity"])  # Quelle
        conn.execute(
            "INSERT INTO location_searches (location_id, term, created_tick) "
            "VALUES (?, ?, ?);", (location_id, SEARCHED_TERM, at_tick),
        )
        if loc["discovery_status"] == "undiscovered":
            conn.execute(
                "UPDATE locations SET discovery_status = 'discovered', "
                "discovered_at_tick = ? WHERE id = ?;", (at_tick, location_id),
            )
        emit_event(
            conn, at_tick, "location",
            f"Durchsucht: {loc['name'] or loc['type']} ({len(rows)} Item-Stapel).",
            subject_type="location", subject_id=location_id,
            payload={"type": loc["type"], "stacks": len(rows)},
        )
    return {"ok": True, "generated": True, "items": rows}


def discover(conn: sqlite3.Connection, location_id: int) -> dict[str, Any]:
    """Markiert eine Location als entdeckt (ohne Inventar zu erzeugen).
    Idempotent — erneutes Betreten ändert nichts."""
    with conn:
        loc = conn.execute(
            "SELECT id, name, type, discovery_status FROM locations WHERE id = ?;",
            (location_id,),
        ).fetchone()
        if loc is None:
            return {"ok": False, "reason": "no_such_location"}
        if loc["discovery_status"] != "undiscovered":
            return {
                "ok": True, "already": True,
                "status": loc["discovery_status"],
                "inventory": current_inventory(conn, location_id),
            }
        at_tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
        conn.execute(
            "UPDATE locations SET discovery_status = 'discovered', "
            "discovered_at_tick = ? WHERE id = ?;",
            (at_tick, location_id),
        )
        emit_event(
            conn, at_tick, "location",
            f"Entdeckt: {loc['name'] or loc['type']}.",
            subject_type="location", subject_id=location_id,
            payload={"type": loc["type"]},
        )
    return {
        "ok": True, "already": False, "status": "discovered",
        "discovered_at_tick": at_tick, "inventory": [],
    }


def current_inventory(conn: sqlite3.Connection, location_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT item_id, quantity, quality FROM location_inventory "
            "WHERE location_id = ? ORDER BY item_id;",
            (location_id,),
        ).fetchall()
    ]
