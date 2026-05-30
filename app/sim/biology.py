"""Biologie: Bedürfnisse, Performance, Sterbe-Check.

Phase 3 des Ticks (CLAUDE.md). In Schritt 1 ist nur die Hunger-Achse aktiv;
die übrigen Achsen bleiben bei 1.0 und gehen neutral (Faktor 1.0) in die
Performance ein. Kein Binär-Tod: Performance degradiert, erst unter der
kritischen Schwelle folgt ein (deterministischer) Sterbe-Wurf (DESIGN.md §5).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import constants
from .events import emit_event
from .rng import roll


def _hunger_penalty(hunger: float) -> float:
    """1.0 ab Komfort-Sättigung, linear bis 0.0 bei Hunger 0."""
    return max(0.0, min(1.0, hunger / constants.PERF_COMFORT_HUNGER))


def apply_hunger(
    conn: sqlite3.Connection, minutes: int, now_tick: int
) -> list[dict[str, Any]]:
    """Senkt die Sättigung aller Lebenden über die verstrichene Zeit.

    Emittiert einen Interrupt beim *Überschreiten* einer Schwelle (einmalig).
    """
    loss = minutes / constants.MINUTES_PER_DAY * constants.HUNGER_LOSS_PER_DAY
    interrupts = []
    for row in conn.execute(
        "SELECT id, name, hunger FROM characters WHERE is_alive = 1;"
    ).fetchall():
        old = row["hunger"]
        new = max(0.0, old - loss)
        conn.execute(
            "UPDATE characters SET hunger = ? WHERE id = ?;",
            (round(new, 6), row["id"]),
        )
        if old >= constants.HUNGER_CRIT > new:
            interrupts.append(
                emit_event(
                    conn, now_tick, "need",
                    f"{row['name']} ist kritisch hungrig.",
                    severity="soft", subject_type="character", subject_id=row["id"],
                    payload={"hunger": round(new, 4)},
                )
            )
        elif old >= constants.HUNGER_SOFT > new:
            interrupts.append(
                emit_event(
                    conn, now_tick, "need",
                    f"{row['name']} wird hungrig.",
                    severity="soft", subject_type="character", subject_id=row["id"],
                    payload={"hunger": round(new, 4)},
                )
            )
    return interrupts


def recompute_performance(conn: sqlite3.Connection) -> None:
    """Setzt die abgeleitete Performance je Lebendem (multiplikativ über Achsen).

    Schritt 1: nur die Hunger-Penalty wirkt; übrige Achsen sind 1.0.
    """
    for row in conn.execute(
        "SELECT id, hunger FROM characters WHERE is_alive = 1;"
    ).fetchall():
        perf = _hunger_penalty(row["hunger"])  # * thirst * sleep * ... (alle 1.0)
        conn.execute(
            "UPDATE characters SET performance = ? WHERE id = ?;",
            (round(perf, 4), row["id"]),
        )


def death_check(
    conn: sqlite3.Connection, now_tick: int, minutes: int, seed: int
) -> list[dict[str, Any]]:
    """Sterbe-Wurf nur unter kritischer Performance; deterministisch geseedet."""
    interrupts = []
    for row in conn.execute(
        "SELECT id, name, performance FROM characters WHERE is_alive = 1;"
    ).fetchall():
        perf = row["performance"]
        if perf >= constants.CRIT_PERFORMANCE:
            continue
        p_death = (
            (constants.CRIT_PERFORMANCE - perf)
            * constants.DEATH_K
            * (minutes / constants.MINUTES_PER_DAY)
        )
        if roll(seed, "death", row["id"], now_tick) < p_death:
            conn.execute(
                "UPDATE characters SET is_alive = 0, performance = 0 WHERE id = ?;",
                (row["id"],),
            )
            interrupts.append(
                emit_event(
                    conn, now_tick, "need",
                    f"{row['name']} ist gestorben (Entkräftung).",
                    severity="decision", subject_type="character", subject_id=row["id"],
                    payload={"performance": round(perf, 4), "p_death": round(p_death, 5)},
                )
            )
    return interrupts
