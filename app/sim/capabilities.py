"""Capabilities — persistenter, sim-lesbarer Welt-State, den adjudizierte
Aktionen erzeugen (z.B. ein SSID-Beacon an einem Funkmast).

Eine Capability hat einen Typ, gehört einer Gruppe, hängt optional an einer
Location, trägt Parameter (JSON) und Upkeep (JSON: laufende Kosten je Tick). Der
Tick liest aktive Capabilities, zieht den Upkeep ein und berechnet Folgen — das
Ergebnis kommt aus dem Sim-Kern, nie aus dem LLM.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def create(
    conn: sqlite3.Connection,
    ctype: str,
    owner_group: int,
    *,
    location_id: int | None = None,
    params: dict[str, Any] | None = None,
    upkeep: dict[str, Any] | None = None,
    tick: int | None = None,
) -> int:
    """Legt eine aktive Capability an und liefert ihre id. Innerhalb der
    Transaktion des Aufrufers."""
    cur = conn.execute(
        "INSERT INTO capabilities (ctype, owner_group, location_id, params, "
        "active, created_tick, upkeep) VALUES (?, ?, ?, ?, 1, ?, ?);",
        (
            ctype, owner_group, location_id,
            json.dumps(params or {}, ensure_ascii=False),
            tick,
            json.dumps(upkeep or {}, ensure_ascii=False),
        ),
    )
    return cur.lastrowid


def list_active(
    conn: sqlite3.Connection, owner_group: int | None = None
) -> list[dict[str, Any]]:
    q = "SELECT * FROM capabilities WHERE active = 1"
    params: list[Any] = []
    if owner_group is not None:
        q += " AND owner_group = ?"
        params.append(owner_group)
    return [_row(r) for r in conn.execute(q + ";", params).fetchall()]


def deactivate(conn: sqlite3.Connection, capability_id: int) -> None:
    conn.execute("UPDATE capabilities SET active = 0 WHERE id = ?;", (capability_id,))


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"], "ctype": r["ctype"], "owner_group": r["owner_group"],
        "location_id": r["location_id"], "active": r["active"],
        "created_tick": r["created_tick"],
        "params": _decode(r["params"]), "upkeep": _decode(r["upkeep"]),
    }


def _decode(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {}


def advance(conn, minutes: int, now_tick: int, seed: int) -> list[dict[str, Any]]:
    """Tick-Folge aktiver Capabilities: Upkeep verbrauchen (Ausfall bei Mangel)
    und Beacon-Kontakte würfeln. Liefert Interrupts. Läuft in der Tick-Transaktion."""
    from . import constants, ledger, resources
    from .events import emit_event
    from .rng import roll

    interrupts: list[dict[str, Any]] = []
    for cap in list_active(conn):
        up = cap["upkeep"] or {}
        item, per = up.get("item"), float(up.get("per_tick", 0) or 0)
        if item and per > 0:
            need = per * (minutes / constants.TICK_MINUTES)  # per_tick je TICK_MINUTES-Schritt
            owned = conn.execute(
                "SELECT COALESCE(SUM(quantity),0.0) AS q FROM group_inventory "
                "WHERE group_id = ? AND item_id = ?;",
                (cap["owner_group"], item),
            ).fetchone()["q"] or 0.0
            if owned < need:
                deactivate(conn, cap["id"])
                interrupts.append(emit_event(
                    conn, now_tick, "world",
                    f"{cap['ctype']} fällt aus — kein {item} mehr.",
                    severity="soft", subject_type="world", subject_id=1,
                    payload={"capability": cap["id"]}))
                continue
            resources._consume_from_group(conn, cap["owner_group"], item, need)
            ledger.add(conn, item, -need)

        if cap["ctype"] == "ssid_beacon":
            p = constants.BEACON_CONTACT_PER_DAY * (minutes / constants.MINUTES_PER_DAY)
            if roll(seed, "beacon", cap["id"], now_tick) < p:
                interrupts.append(emit_event(
                    conn, now_tick, "world",
                    "Ein schwaches Funksignal antwortet auf deinen Beacon.",
                    severity="decision", subject_type="capability", subject_id=cap["id"],
                    payload={"capability": cap["id"], "ctype": cap["ctype"]}))
    return interrupts
