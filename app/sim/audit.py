"""Bilanz-Prüfung: Sicherheitsnetz gegen "Erzeugung aus dem Nichts".

Phase 5-Anhang des Ticks (CLAUDE.md / DESIGN.md §6). Vergleicht den Ist-Bestand
(Σ location_inventory + Σ group_inventory) gegen das Soll im ``resource_ledger``
(Σ Quellen − Σ Senken). Jede Drift — in beide Richtungen — bedeutet eine
Mutation, die nicht über eine Sim-Funktion lief, und wird geflaggt:
  Ist > Soll  -> aus dem Nichts erzeugt.
  Ist < Soll  -> spurlos verschwunden.

Legitime Quellen: Lazy Generation und ``player_declared`` (Spielerwissen, Issue #54).
Letztere ist in ``ledger_sources`` einzeln mitgeführt und wird gegen das Protokoll
``declared_items`` gegengeprüft (jede deklarierte Menge muss gebucht sein).

Plündern ist Transfer und ledger-neutral (Summe bleibt gleich). Lazy Generation
bucht ihre Quelle ins Ledger, Verbrauch/Verderb ihre Senken — daher bleibt der
Audit bei korrektem Sim-Kern immer drift-frei.

In der Tabelle ``resource_audit`` werden je Item die Soll/Ist-Werte abgelegt:
``expected_delta`` = Soll (Ledger), ``actual_delta`` = Ist-Bestand.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import constants, ledger
from .events import emit_event


def run_audit(conn: sqlite3.Connection, now_tick: int) -> list[dict[str, Any]]:
    """Schreibt pro Item einen Soll/Ist-Snapshot und flaggt jede Drift."""
    totals: dict[str, list[float]] = {}  # item_id -> [world, group]
    for row in conn.execute(
        "SELECT item_id, SUM(quantity) AS q FROM location_inventory GROUP BY item_id;"
    ).fetchall():
        totals[row["item_id"]] = [row["q"] or 0.0, 0.0]
    for row in conn.execute(
        "SELECT item_id, SUM(quantity) AS q FROM group_inventory GROUP BY item_id;"
    ).fetchall():
        totals.setdefault(row["item_id"], [0.0, 0.0])[1] = row["q"] or 0.0

    expected = ledger.expected_totals(conn)

    interrupts = []
    # Union aus Ist- und Soll-Items: erfasst auch "verschwunden" (Ist fehlt).
    for item_id in set(totals) | set(expected):
        world, group = totals.get(item_id, [0.0, 0.0])
        now_total = world + group
        soll = expected.get(item_id, 0.0)
        drift = now_total - soll
        flagged = 1 if abs(drift) > constants.AUDIT_EPS else 0

        conn.execute(
            "INSERT INTO resource_audit (tick, item_id, total_world, total_groups, "
            "expected_delta, actual_delta, flagged) VALUES (?, ?, ?, ?, ?, ?, ?);",
            (now_tick, item_id, world, group, soll, now_total, flagged),
        )
        if flagged:
            direction = "erzeugt" if drift > 0 else "verschwunden"
            interrupts.append(
                emit_event(
                    conn, now_tick, "system",
                    f"Bilanz-Alarm: {item_id} {drift:+.3f} ({direction}, "
                    f"Soll {soll:g}, Ist {now_total:g}).",
                    severity="decision", subject_type="world", subject_id=1,
                    payload={"item": item_id, "expected": soll, "actual": now_total},
                )
            )
    interrupts += _check_declared(conn, now_tick)
    return interrupts


def _check_declared(conn: sqlite3.Connection, now_tick: int) -> list[dict[str, Any]]:
    """``player_declared``: gebuchte Quelle je Item == Σ Protokoll (platzierte
    Deklarationen). Abweichung = Buchung am Protokoll vorbei -> Alarm."""
    booked = ledger.source_totals(conn, ledger.SOURCE_PLAYER_DECLARED)
    logged: dict[str, float] = {}
    for row in conn.execute(
        "SELECT item_id, SUM(quantity) AS q FROM declared_items "
        "WHERE status = 'placed' AND item_id IS NOT NULL GROUP BY item_id;"
    ).fetchall():
        logged[row["item_id"]] = row["q"] or 0.0
    out = []
    for item_id in set(booked) | set(logged):
        diff = booked.get(item_id, 0.0) - logged.get(item_id, 0.0)
        if abs(diff) > constants.AUDIT_EPS:
            out.append(emit_event(
                conn, now_tick, "system",
                f"Bilanz-Alarm: {item_id} {diff:+.3f} (player_declared stimmt nicht mit dem "
                f"Deklarations-Protokoll überein).",
                severity="decision", subject_type="world", subject_id=1,
                payload={"item": item_id, "source": ledger.SOURCE_PLAYER_DECLARED}))
    return out
