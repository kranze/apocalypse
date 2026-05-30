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

from . import generation
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

        # Auto-Discover: betreten erzeugt das Inventar, falls noch nicht geschehen.
        if loc["discovery_status"] == "undiscovered":
            at_tick = conn.execute(
                "SELECT tick FROM world WHERE id = 1;"
            ).fetchone()["tick"]
            generation.materialize(conn, loc, at_tick)

        inv = conn.execute(
            "SELECT id, item_id, quantity, quality, produced_tick "
            "FROM location_inventory WHERE location_id = ?;",
            (location_id,),
        ).fetchall()

        transferred: dict[str, float] = {}
        for row in inv:
            requested = row["quantity"] if items is None else items.get(row["item_id"], 0.0)
            take = min(row["quantity"], max(0.0, requested))
            if take <= 0:
                continue

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
