"""Hitzequelle für die Zubereitung — bewusste Naht für den Adjudikator.

In Schritt 1 gibt es noch keinen Adjudikator/keine Knowledge Base. Diese Funktion
ist daher ein **Platzhalter**: sie akzeptiert Feuerholz als Hitzequelle. Was sonst
noch Hitze liefern kann — eine Feuerstelle, ein Topf über Gas, eine Mikrowelle an
einem Generator … — entscheidet ab Schritt 2 der Adjudikator anhand der Knowledge
Base und des realen Inventars. Die Zubereitungs-Logik (`resources.prepare`) ruft
nur diese Naht auf und muss dann nicht angefasst werden.

Vertrag: liefert die zu verbrauchende Hitzequelle als (item_id, menge) oder None,
wenn aktuell keine verfügbar ist.
"""
from __future__ import annotations

import sqlite3

# Schritt-1-Platzhalter: ein Scheit Brennholz pro Zubereitung.
_FUEL_ITEM = "firewood"
_FUEL_AMOUNT = 1.0


def can_provide_heat(conn: sqlite3.Connection, group_id: int) -> tuple[str, float] | None:
    """Liefert (fuel_item_id, menge) der zu verbrauchenden Hitzequelle, oder None.

    PLATZHALTER für Schritt 1 — ersetzt der Adjudikator in Schritt 2."""
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0.0) AS q FROM group_inventory "
        "WHERE group_id = ? AND item_id = ?;",
        (group_id, _FUEL_ITEM),
    ).fetchone()
    if (row["q"] or 0.0) >= _FUEL_AMOUNT:
        return (_FUEL_ITEM, _FUEL_AMOUNT)
    return None
