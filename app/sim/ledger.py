"""Ressourcen-Ledger: laufendes Soll je Item (Σ Quellen − Σ Senken).

Jede Sim-Funktion, die Gesamtbestände verändert, bucht hier:
  + Quelle (Lazy Generation), − Senke (Verbrauch, Verderb).
Plündern ist ein Transfer und bleibt ledger-neutral. Der Tick-Audit vergleicht
den Ist-Bestand gegen dieses Soll (DESIGN.md §6, CLAUDE.md Bilanz-Prüfung).

Die Funktionen öffnen KEINE eigene Transaktion — sie laufen innerhalb der
Transaktion der aufrufenden Sim-Aktion.
"""
from __future__ import annotations

import sqlite3


def add(conn: sqlite3.Connection, item_id: str, delta: float) -> None:
    """Bucht ``delta`` (Quelle > 0, Senke < 0) auf das Soll von ``item_id``."""
    conn.execute(
        "INSERT INTO resource_ledger (item_id, expected_total) VALUES (?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET "
        "expected_total = expected_total + excluded.expected_total;",
        (item_id, delta),
    )


def expected_totals(conn: sqlite3.Connection) -> dict[str, float]:
    """Liefert das Soll je Item als dict."""
    return {
        row["item_id"]: row["expected_total"]
        for row in conn.execute(
            "SELECT item_id, expected_total FROM resource_ledger;"
        ).fetchall()
    }
