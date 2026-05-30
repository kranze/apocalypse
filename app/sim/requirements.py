"""Generische Vorbedingungen über die Knowledge Base.

Eine Anforderung (``req``) wie ``heat``, ``power`` oder ``transmitter`` wird
durch ein Item erfüllt, das laut KB diese Eigenschaft liefert (Topic
``provides:<req>``). Verallgemeinert die frühere heat-Naht: jede Fähigkeit, die
etwas „braucht", fragt hier nach — und der Spieler kann per Override neue
Lieferanten beibringen (player_verified).

``value``-Konvention je KB-Fakt: ``{"consume": n}`` — n>0 = wird bei Nutzung
verbraucht (z.B. Feuerholz), n=0 = wird nur besessen/genutzt (z.B. Generator,
Sender).
"""
from __future__ import annotations

import sqlite3

from . import kb


def satisfy(conn: sqlite3.Connection, group_id: int, req: str) -> dict | None:
    """Liefert {item, consume} eines vorhandenen Lieferanten für ``req``, oder None.

    ``consume`` = zu verbrauchende Menge (0 = nur besessen). Reine Prüfung; der
    Verbrauch passiert beim Anwenden des Effekts (ledger-gebucht)."""
    for fact in kb.list_topic(conn, f"provides:{req}"):
        item_id = fact["key"]
        value = fact.get("value") or {}
        consume = float(value.get("consume", 1)) if isinstance(value, dict) else 1.0
        required_min = consume if consume > 0 else 1.0
        owned = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0.0) AS q FROM group_inventory "
            "WHERE group_id = ? AND item_id = ?;",
            (group_id, item_id),
        ).fetchone()["q"]
        if (owned or 0.0) >= required_min:
            return {"item": item_id, "consume": consume}
    return None


def providers(conn: sqlite3.Connection, req: str) -> list[str]:
    """Alle bekannten Lieferanten-Items für eine Anforderung (für Kontext/Hinweise)."""
    return [f["key"] for f in kb.list_topic(conn, f"provides:{req}")]
