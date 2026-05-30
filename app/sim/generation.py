"""Lazy Generation: Inventar entsteht erst bei Entdeckung einer Location.

Deterministisch aus ``generation_seed`` (= hash(world_seed, osm_id), beim Loader
gesetzt): gleicher Seed -> gleiches Item-Set. Decay seit Kollaps (Tick 0) wird
zum Entdeckungszeitpunkt eingerechnet (DESIGN.md §6) — was schon verrottet ist,
findet man nicht mehr. Späte Entdeckungen liefern daher weniger Verderbliches.

Die Generierung ist die EINZIGE Ressourcen-Quelle in Schritt 1 und bucht
entsprechend ins Ledger.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import constants, ledger
from .events import emit_event
from .loot_tables import DEFAULT_TABLE_KEY, LOOT_TABLES
from .resources import quality_at
from .rng import roll


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
    bei Entdeckung ergibt sich aus dem Decay seit dann. Verrottetes (< Schwelle)
    wird nicht aufgenommen.
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


def materialize(
    conn: sqlite3.Connection, location: sqlite3.Row, at_tick: int
) -> list[dict[str, Any]]:
    """Schreibt das generierte Inventar in die DB und markiert die Location als
    entdeckt. Läuft INNERHALB der Transaktion des Aufrufers (keine eigene)."""
    rows = roll_inventory(
        location["generation_seed"], location["type"], at_tick, _halflives(conn)
    )
    for r in rows:
        conn.execute(
            "INSERT INTO location_inventory (location_id, item_id, quantity, "
            "quality, produced_tick) VALUES (?, ?, ?, ?, ?);",
            (location["id"], r["item_id"], r["quantity"], r["quality"], r["produced_tick"]),
        )
        ledger.add(conn, r["item_id"], r["quantity"])  # Quelle

    conn.execute(
        "UPDATE locations SET discovery_status = 'discovered', "
        "discovered_at_tick = ? WHERE id = ?;",
        (at_tick, location["id"]),
    )
    emit_event(
        conn, at_tick, "location",
        f"Entdeckt: {location['name'] or location['type']} "
        f"({len(rows)} Item-Stapel).",
        subject_type="location", subject_id=location["id"],
        payload={"type": location["type"], "stacks": len(rows)},
    )
    return rows


def discover(conn: sqlite3.Connection, location_id: int) -> dict[str, Any]:
    """Spieler-Aktion 'Gebäude betreten': entdeckt eine Location und generiert
    ihr Inventar (einmalig). Idempotent — erneutes Betreten regeneriert nichts."""
    with conn:
        loc = conn.execute(
            "SELECT * FROM locations WHERE id = ?;", (location_id,)
        ).fetchone()
        if loc is None:
            return {"ok": False, "reason": "no_such_location"}
        if loc["discovery_status"] != "undiscovered":
            return {
                "ok": True,
                "already": True,
                "status": loc["discovery_status"],
                "inventory": current_inventory(conn, location_id),
            }
        at_tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
        rows = materialize(conn, loc, at_tick)
    return {
        "ok": True,
        "already": False,
        "status": "discovered",
        "discovered_at_tick": at_tick,
        "inventory": rows,
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
