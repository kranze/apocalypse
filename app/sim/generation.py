"""Entdeckung von Locations.

Früher wurde beim Betreten Inventar aus Loot-Tabellen generiert. Das ist
abgeschafft: Inhalte entstehen jetzt ausschließlich durch gezielte **Suche**
(siehe ``effects.search`` + ``llm.search_item``). ``discover`` markiert eine
Location nur noch als entdeckt — leer, bis der Spieler etwas findet.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .events import emit_event


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
