"""Ereignis-/Interrupt-Schreiber.

Jede bemerkenswerte Zustandsänderung landet in der ``events``-Tabelle. Der
Tick gibt die "bemerkenswerten" (severity != info) als Interrupts zurück; der
Fast-Forward stoppt darauf (DESIGN.md §4).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

# severity-Stufen (schema.sql): reine Anzeige, weicher Stopp, harte Entscheidung.
INFO = "info"
SOFT = "soft"
DECISION = "decision"

# Bei diesen severities hält der Fast-Forward an.
HALTING = (SOFT, DECISION)


def emit_event(
    conn: sqlite3.Connection,
    tick: int,
    category: str,
    message: str,
    *,
    severity: str = INFO,
    subject_type: str | None = None,
    subject_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Schreibt ein Event und gibt es als dict zurück (für die Interrupt-Liste)."""
    conn.execute(
        "INSERT INTO events (tick, category, severity, subject_type, subject_id, "
        "message, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?);",
        (
            tick,
            category,
            severity,
            subject_type,
            subject_id,
            message,
            json.dumps(payload) if payload is not None else None,
        ),
    )
    return {
        "tick": tick,
        "category": category,
        "severity": severity,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "message": message,
    }
