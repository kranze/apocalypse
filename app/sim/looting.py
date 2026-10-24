"""Plündern: Transfer Location -> Gruppe (bilanzneutral).

Verschiebt Items aus dem Location-Inventar in das Gruppen-Inventar. Da Quelle
und Senke summiert gleich bleiben, ist der Vorgang ledger-neutral — der
Gesamtbestand ändert sich nicht (DESIGN.md §6: "Plünderung = Transfer").

Der Decay-Anker (``produced_tick`` der Location) wird als ``acquired_tick`` der
Gruppe übernommen, damit die Qualität nahtlos weiterzerfällt und nicht
"zurückgesetzt" wird.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import constants, generation
from .events import emit_event


def loot(
    conn: sqlite3.Connection,
    location_id: int,
    group_id: int = 1,
    items: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Plündert eine Location. ``items`` = {item_id: menge} für gezieltes Nehmen,
    ``None`` = alles. Betritt undeckte Locations automatisch (Auto-Discover).

    Atomar. Liefert die transferierten Mengen und den neuen Location-Status.
    """
    with conn:
        loc = conn.execute(
            "SELECT * FROM locations WHERE id = ?;", (location_id,)
        ).fetchone()
        if loc is None:
            return {"ok": False, "reason": "no_such_location"}

        # Auto-Discover: betreten markiert nur (kein Auto-Inventar mehr —
        # Inhalte entstehen durch Suche). Geplündert wird, was vor Ort liegt.
        if loc["discovery_status"] == "undiscovered":
            at_tick = conn.execute(
                "SELECT tick FROM world WHERE id = 1;"
            ).fetchone()["tick"]
            conn.execute(
                "UPDATE locations SET discovery_status = 'discovered', "
                "discovered_at_tick = ? WHERE id = ?;",
                (at_tick, location_id),
            )

        inv = conn.execute(
            "SELECT id, item_id, quantity, quality, produced_tick "
            "FROM location_inventory WHERE location_id = ?;",
            (location_id,),
        ).fetchall()

        transferred: dict[str, float] = {}
        budget = None if items is None else dict(items)  # je Item über Stapel hinweg
        for row in inv:
            if budget is None:
                requested = row["quantity"]
            else:
                requested = budget.get(row["item_id"], 0.0)
            take = min(row["quantity"], max(0.0, requested))
            if take <= 0:
                continue
            if budget is not None:
                budget[row["item_id"]] = requested - take

            # Location verringern (bzw. Zeile entfernen, wenn leer).
            remaining = row["quantity"] - take
            if remaining <= 1e-9:
                conn.execute(
                    "DELETE FROM location_inventory WHERE id = ?;", (row["id"],)
                )
            else:
                conn.execute(
                    "UPDATE location_inventory SET quantity = ? WHERE id = ?;",
                    (remaining, row["id"]),
                )

            # Gruppe erhöhen; gleiche (item, quality) stapelt. Decay-Anker erhalten.
            conn.execute(
                "INSERT INTO group_inventory (group_id, item_id, quantity, quality, "
                "acquired_tick) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(group_id, item_id, quality) DO UPDATE SET "
                "quantity = quantity + excluded.quantity;",
                (group_id, row["item_id"], take, row["quality"], row["produced_tick"]),
            )
            transferred[row["item_id"]] = transferred.get(row["item_id"], 0.0) + take

        # Leergeplündert -> depleted.
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM location_inventory WHERE location_id = ?;",
            (location_id,),
        ).fetchone()["n"]
        new_status = "depleted" if left == 0 else "discovered"
        conn.execute(
            "UPDATE locations SET discovery_status = ? WHERE id = ?;",
            (new_status, location_id),
        )

        at_tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
        emit_event(
            conn, at_tick, "location",
            f"Geplündert: {loc['name'] or loc['type']} "
            f"({sum(transferred.values()):g} Einheiten).",
            subject_type="location", subject_id=location_id,
            payload={"transferred": transferred, "status": new_status},
        )

    return {"ok": True, "transferred": transferred, "status": new_status}


# --- Tragegrenze --------------------------------------------------------
def carry_capacity(conn: sqlite3.Connection, group_id: int) -> float:
    """Tragekapazität der Gruppe in kg: Summe über lebende Mitglieder von
    clamp(CARRY_BODY_FRACTION * Körpergewicht, CARRY_MIN_KG, CARRY_MAX_KG)."""
    total = 0.0
    for r in conn.execute(
        "SELECT weight_kg FROM characters WHERE group_id = ? AND is_alive = 1;",
        (group_id,),
    ).fetchall():
        body = r["weight_kg"] or constants.CARRY_DEFAULT_BODY_KG
        total += max(
            constants.CARRY_MIN_KG,
            min(constants.CARRY_MAX_KG, constants.CARRY_BODY_FRACTION * body),
        )
    return total


def _priority(category: str, needs_prep: int) -> int:
    """Plünder-Priorität: Wasser > fertiges Essen > Essen mit Zubereitung >
    Medizin > Werkzeug > Rest."""
    if category == "water":
        return 0
    if category == "food":
        return 2 if needs_prep else 1
    if category == "medical":
        return 3
    if category == "tool":
        return 4
    return 5


def plan_loot(
    conn: sqlite3.Connection, location_id: int, free_kg: float
) -> tuple[dict[str, float], dict[str, float]]:
    """Wählt deterministisch, was bis ``free_kg`` mitgenommen wird.

    Rückgabe ``(take, left)`` als {item_id: menge}. Es werden nur ganze
    Einheiten genommen (Brüche nur, wenn die ganze Restmenge passt);
    gewichtslose Items passen immer.
    """
    rows = conn.execute(
        "SELECT li.item_id, SUM(li.quantity) AS qty, ic.weight_kg AS w, "
        "ic.category AS cat, ic.needs_preparation AS np, "
        "COALESCE(ic.kcal_per_unit, 0.0) AS kcal "
        "FROM location_inventory li JOIN item_catalog ic ON ic.id = li.item_id "
        "WHERE li.location_id = ? AND li.quantity > 0 GROUP BY li.item_id;",
        (location_id,),
    ).fetchall()
    ordered = sorted(
        rows,
        key=lambda r: (
            _priority(r["cat"], r["np"]),
            -(r["kcal"] / r["w"]) if r["cat"] == "food" and r["w"] else 0.0,
            r["item_id"],
        ),
    )
    take: dict[str, float] = {}
    left: dict[str, float] = {}
    free = max(0.0, free_kg)
    # Wasser darf den Rucksack nicht allein füllen: höchstens ein Anteil des freien Platzes
    water_room = free * constants.LOOT_WATER_MAX_SHARE
    for r in ordered:
        qty, w = r["qty"], r["w"] or 0.0
        room = min(free, water_room) if r["cat"] == "water" else free
        if w <= 0:
            n = qty
        elif qty * w <= room + 1e-9:
            n = qty
        else:
            n = float(int((room + 1e-9) // w))
        if n > 0:
            take[r["item_id"]] = n
            free -= n * w
            if r["cat"] == "water":
                water_room -= n * w
        if qty - n > 1e-9:
            left[r["item_id"]] = qty - n
    return take, left


def loot_limited(
    conn: sqlite3.Connection, location_id: int, group_id: int = 1
) -> dict[str, Any]:
    """Plündern bis zur Tragegrenze (Priorität siehe ``plan_loot``). Der Rest
    bleibt am Ort. Ergebnis wie ``loot`` plus ``left``, ``carried_kg``,
    ``capacity_kg``; ``reason='too_heavy'`` wenn gar nichts mehr passt."""
    from . import movement

    capacity = carry_capacity(conn, group_id)
    carried = movement.carried_weight(conn, group_id)
    take, left = plan_loot(conn, location_id, capacity - carried)
    if not take:
        return {"ok": False, "reason": "too_heavy", "left": left,
                "carried_kg": round(carried, 2), "capacity_kg": round(capacity, 2)}
    res = loot(conn, location_id, group_id, items=take)
    if not res.get("ok"):
        return res
    res["left"] = left
    res["carried_kg"] = round(movement.carried_weight(conn, group_id), 2)
    res["capacity_kg"] = round(capacity, 2)
    return res


# --- Gruppe -> Gruppe (Companion, Slice M4) ------------------------------
def transfer_between_groups(
    conn: sqlite3.Connection, from_gid: int, to_gid: int, item_id: str,
    qty: float | None = None,
) -> float:
    """Verschiebt bis zu ``qty`` (None = alles) von ``item_id`` aus dem Inventar
    der Gruppe ``from_gid`` in das von ``to_gid`` (bilanzneutral: Quelle und
    Senke summieren gleich, der Ledger bleibt unberührt). Frischeste Qualität
    zuerst; der Decay-Anker (``acquired_tick``) wandert mit. Schreibt ohne
    eigenes Commit (Aufrufer hält die Transaktion). Liefert die bewegte Menge."""
    rows = conn.execute(
        "SELECT id, quantity, quality, acquired_tick FROM group_inventory "
        "WHERE group_id = ? AND item_id = ? AND quantity > 0 "
        "ORDER BY quality DESC, id;",
        (from_gid, item_id),
    ).fetchall()
    budget = float("inf") if qty is None else max(0.0, float(qty))
    moved = 0.0
    for row in rows:
        take = min(row["quantity"], budget)
        if take <= 1e-12:
            continue
        budget -= take
        remaining = row["quantity"] - take
        if remaining <= 1e-9:
            conn.execute("DELETE FROM group_inventory WHERE id = ?;", (row["id"],))
        else:
            conn.execute("UPDATE group_inventory SET quantity = ? WHERE id = ?;",
                         (remaining, row["id"]))
        conn.execute(
            "INSERT INTO group_inventory (group_id, item_id, quantity, quality, "
            "acquired_tick) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(group_id, item_id, quality) DO UPDATE SET "
            "quantity = quantity + excluded.quantity;",
            (to_gid, item_id, take, row["quality"], row["acquired_tick"]),
        )
        moved += take
        if budget <= 1e-12:
            break
    return moved
