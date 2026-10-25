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


# Explizite, benannte Quellen neben der Lazy Generation. ``player_declared`` =
# Spielerwissen (Issue #54): vom Spieler verantwortet, aber gebucht und auditierbar.
SOURCE_PLAYER_DECLARED = "player_declared"


def add(conn: sqlite3.Connection, item_id: str, delta: float,
        source: str | None = None) -> None:
    """Bucht ``delta`` (Quelle > 0, Senke < 0) auf das Soll von ``item_id``.
    Mit ``source`` wird der Betrag zusätzlich je benannter Quelle mitgeführt
    (``ledger_sources``), damit die Herkunft nachvollziehbar bleibt."""
    if source is not None:
        conn.execute(
            "INSERT INTO ledger_sources (item_id, source, total) VALUES (?, ?, ?) "
            "ON CONFLICT(item_id, source) DO UPDATE SET total = total + excluded.total;",
            (item_id, source, delta),
        )
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


def source_totals(conn: sqlite3.Connection, source: str) -> dict[str, float]:
    """Gebuchte Summe je Item für eine benannte Quelle (z.B. ``player_declared``)."""
    return {
        row["item_id"]: row["total"]
        for row in conn.execute(
            "SELECT item_id, total FROM ledger_sources WHERE source = ?;", (source,)
        ).fetchall()
    }
