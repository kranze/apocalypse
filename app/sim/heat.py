"""Hitzequelle für die Zubereitung — dünn über die generische Vorbedingungs-Naht.

Heat ist nur eine Anforderung unter vielen (siehe ``requirements``). Die KB führt
Hitzequellen unter Topic ``provides:heat``. Vertrag unverändert, damit
``resources.prepare`` unangetastet bleibt: liefert (item_id, menge) oder None.
"""
from __future__ import annotations

import sqlite3

from . import requirements


def can_provide_heat(conn: sqlite3.Connection, group_id: int) -> tuple[str, float] | None:
    res = requirements.satisfy(conn, group_id, "heat")
    if res is None:
        return None
    return (res["item"], res["consume"])
