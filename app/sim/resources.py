"""Ressourcen-Fortschreibung: Verderb (Decay) und Essen (Verbrauch).

Phase 2 des Ticks (CLAUDE.md). Jede Mengen-/Qualitätsänderung läuft durch
diese Funktionen, nie inline. Verderb und Verbrauch sind die einzigen Senken
in Schritt 1 (es gibt keine Quellen).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import constants, ledger
from .events import emit_event


def quality_at(anchor_tick: int, now_tick: int, halflife_min: int | None) -> float:
    """Qualität 0..1 nach Halbwertszeit-Zerfall seit ``anchor_tick``.

    ``halflife_min is None`` -> praktisch nicht verderblich (Qualität bleibt 1.0).
    Reine Funktion der verstrichenen Zeit; bei konstantem Wetter exakt (Schritt 1).
    """
    if halflife_min is None:
        return 1.0
    elapsed = max(0, now_tick - anchor_tick)
    return 0.5 ** (elapsed / halflife_min)


def _halflives(conn: sqlite3.Connection) -> dict[str, int | None]:
    return {
        row["id"]: row["decay_halflife_min"]
        for row in conn.execute(
            "SELECT id, decay_halflife_min FROM item_catalog;"
        ).fetchall()
    }


def apply_decay(conn: sqlite3.Connection, now_tick: int) -> list[dict[str, Any]]:
    """Schreibt Qualitäten fort und entfernt Verdorbenes (Senke).

    - ``location_inventory``: Qualität je Zeile aktualisieren, verdorbene löschen.
    - ``group_inventory``: dito, aber Zeilen gleicher Qualität verschmelzen
      (Schema: UNIQUE(group_id,item_id,quality) -> "gleiche Quality stapelt").
    Liefert Interrupts (eines pro Item, das verdorben ist).
    """
    halflives = _halflives(conn)
    spoiled: dict[str, float] = {}

    # --- location_inventory: simples Update, kein Merge nötig ---
    for row in conn.execute(
        "SELECT id, item_id, quantity, produced_tick FROM location_inventory;"
    ).fetchall():
        hl = halflives.get(row["item_id"])
        if hl is None:
            continue
        q = quality_at(row["produced_tick"], now_tick, hl)
        if q < constants.SPOIL_THRESHOLD:
            spoiled[row["item_id"]] = spoiled.get(row["item_id"], 0.0) + row["quantity"]
            conn.execute("DELETE FROM location_inventory WHERE id = ?;", (row["id"],))
        else:
            conn.execute(
                "UPDATE location_inventory SET quality = ? WHERE id = ?;",
                (round(q, 4), row["id"]),
            )

    # --- group_inventory: löschen + verschmolzen neu einfügen ---
    grp_rows = conn.execute(
        "SELECT id, group_id, item_id, quantity, acquired_tick FROM group_inventory;"
    ).fetchall()
    merged: dict[tuple[int, str, float], list[float]] = {}  # -> [quantity, min_acquired]
    for row in grp_rows:
        hl = halflives.get(row["item_id"])
        if hl is None:
            continue  # nicht verderblich -> unangetastet lassen
        conn.execute("DELETE FROM group_inventory WHERE id = ?;", (row["id"],))
        q = quality_at(row["acquired_tick"], now_tick, hl)
        if q < constants.SPOIL_THRESHOLD:
            spoiled[row["item_id"]] = spoiled.get(row["item_id"], 0.0) + row["quantity"]
            continue
        key = (row["group_id"], row["item_id"], round(q, 4))
        if key in merged:
            merged[key][0] += row["quantity"]
            merged[key][1] = min(merged[key][1], row["acquired_tick"])
        else:
            merged[key] = [row["quantity"], row["acquired_tick"]]
    for (group_id, item_id, quality), (quantity, acquired_tick) in merged.items():
        conn.execute(
            "INSERT INTO group_inventory (group_id, item_id, quantity, quality, "
            "acquired_tick) VALUES (?, ?, ?, ?, ?);",
            (group_id, item_id, quantity, quality, acquired_tick),
        )

    interrupts = []
    for item_id, qty in spoiled.items():
        ledger.add(conn, item_id, -qty)  # Senke: Verderb
        interrupts.append(
            emit_event(
                conn,
                now_tick,
                "world",
                f"{item_id}: {qty:g} Einheit(en) verdorben.",
                severity="soft",
                subject_type="world",
                subject_id=1,
                payload={"item": item_id, "spoiled": qty},
            )
        )
    return interrupts


def eat(
    conn: sqlite3.Connection,
    character_id: int,
    item_id: str | None = None,
) -> dict[str, Any]:
    """Explizite Spieler-Aktion: ein Nahrungsmittel aus dem Gruppen-Inventar essen.

    Atomar (eigene Transaktion). Wählt – falls ``item_id`` offen – das Item mit
    den meisten effektiven kcal (kcal * Qualität). Stellt Hunger proportional zu
    den aufgenommenen kcal wieder her. Verbrauch ist eine Senke.
    """
    with conn:
        char = conn.execute(
            "SELECT id, group_id, hunger, daily_kcal FROM characters "
            "WHERE id = ? AND is_alive = 1;",
            (character_id,),
        ).fetchone()
        if char is None:
            return {"ok": False, "reason": "no_such_living_character"}

        params: list[Any] = [char["group_id"]]
        extra = ""
        if item_id is not None:
            extra = "AND gi.item_id = ? "
            params.append(item_id)
        food = conn.execute(
            "SELECT gi.id, gi.item_id, gi.quantity, gi.quality, ic.kcal_per_unit "
            "FROM group_inventory gi JOIN item_catalog ic ON ic.id = gi.item_id "
            "WHERE gi.group_id = ? AND ic.category = 'food' AND gi.quantity > 0 "
            + extra
            + "ORDER BY (ic.kcal_per_unit * gi.quality) DESC LIMIT 1;",
            params,
        ).fetchone()
        if food is None:
            return {"ok": False, "reason": "no_food"}

        amount = min(1.0, food["quantity"])
        remaining = food["quantity"] - amount
        if remaining <= 1e-9:
            conn.execute("DELETE FROM group_inventory WHERE id = ?;", (food["id"],))
        else:
            conn.execute(
                "UPDATE group_inventory SET quantity = ? WHERE id = ?;",
                (remaining, food["id"]),
            )

        ledger.add(conn, food["item_id"], -amount)  # Senke: Verbrauch

        kcal = (food["kcal_per_unit"] or 0.0) * food["quality"] * amount
        new_hunger = min(1.0, char["hunger"] + kcal / char["daily_kcal"])
        new_perf = max(0.0, min(1.0, new_hunger / constants.PERF_COMFORT_HUNGER))
        conn.execute(
            "UPDATE characters SET hunger = ?, performance = ? WHERE id = ?;",
            (round(new_hunger, 6), round(new_perf, 4), character_id),
        )

        now_tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
        emit_event(
            conn,
            now_tick,
            "need",
            f"Gegessen: {amount:g}x {food['item_id']} (+{kcal:.0f} kcal).",
            subject_type="character",
            subject_id=character_id,
            payload={"item": food["item_id"], "amount": amount, "kcal": kcal},
        )

    return {
        "ok": True,
        "item": food["item_id"],
        "amount": amount,
        "kcal": round(kcal, 1),
        "hunger": round(new_hunger, 4),
    }
