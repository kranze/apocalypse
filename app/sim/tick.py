"""Zeit-Tick: die Herzschleife des Sim-Kerns.

``advance_tick`` rückt die Welt um eine feste Spanne vor — in exakt der
Phasen-Reihenfolge aus CLAUDE.md, vollständig in EINER DB-Transaktion (atomar,
commit-or-rollback). ``fast_forward`` ruft Ticks in Folge auf und stoppt bei
einem Interrupt (DESIGN.md §4: aggressives Interrupten) oder wenn niemand mehr
lebt.

Kein LLM, keine Agenten (Phase 4 ist erst Schritt 2+). Headless und über fixen
world_seed reproduzierbar.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import audit, biology, capabilities, constants, movement, resources
from .events import HALTING


def advance_tick(
    conn: sqlite3.Connection, minutes: int = constants.TICK_MINUTES
) -> dict[str, Any]:
    """Ein Tick. Gibt neuen Tick-Stand + emittierte Interrupts zurück."""
    interrupts: list[dict[str, Any]] = []
    with conn:  # eine Transaktion pro Tick
        world = conn.execute(
            "SELECT tick, world_seed FROM world WHERE id = 1;"
        ).fetchone()
        t0, seed = world["tick"], world["world_seed"]
        t1 = t0 + minutes

        # Phase 1 — Physik/Welt: Zeit + Bewegung (Wetter ist Schritt 1 nur Snapshot)
        conn.execute("UPDATE world SET tick = ? WHERE id = 1;", (t1,))
        distances = movement.advance_movement(conn, minutes, t1)
        interrupts += distances.pop("_interrupts", [])
        # Capabilities: Upkeep + Folgen (z.B. SSID-Beacon-Kontakte)
        interrupts += capabilities.advance(conn, minutes, t1, seed)

        # Phase 2 — Ressourcen: Verderb (Verbrauch = explizites eat(), nicht hier)
        interrupts += resources.apply_decay(conn, t1)

        # Phase 3 — Biologie: Bedürfnisse (inkl. Aktivität), Performance, Sterbe-Check
        interrupts += biology.apply_hunger(conn, minutes, t1, distances)
        biology.recompute_performance(conn)
        interrupts += biology.death_check(conn, t1, minutes, seed)

        # Phase 4 — Agenten-Ticks: erst Schritt 2+ (bewusst leer)

        # Phase 5 — Interrupts gesammelt; Bilanz-Prüfung als Sicherheitsnetz
        interrupts += audit.run_audit(conn, t1)

    return {"tick": t1, "interrupts": interrupts}


def _living_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM characters WHERE is_alive = 1;"
    ).fetchone()["n"]


def fast_forward(
    conn: sqlite3.Connection,
    *,
    max_ticks: int = 100_000,
    until_tick: int | None = None,
    minutes: int = constants.TICK_MINUTES,
) -> dict[str, Any]:
    """Ticks in Folge bis Interrupt, Ziel-Tick, Tick-Limit oder Aussterben."""
    advanced = 0
    halting: list[dict[str, Any]] = []
    reason = "max_ticks"

    for _ in range(max_ticks):
        cur = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
        if until_tick is not None and cur >= until_tick:
            reason = "until_tick"
            break
        if _living_count(conn) == 0:
            reason = "all_dead"
            break

        result = advance_tick(conn, minutes)
        advanced += 1
        notable = [i for i in result["interrupts"] if i["severity"] in HALTING]
        if notable:
            halting.extend(notable)
            reason = "interrupt"
            break

    final_tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
    return {
        "ticks_advanced": advanced,
        "tick": final_tick,
        "stopped": reason,
        "interrupts": halting,
    }
