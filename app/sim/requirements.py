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
from typing import Callable

from . import kb

# Anforderungen, die es von Haus aus gibt (zusätzlich zu allem, wofür die KB
# bereits ein ``provides:<req>``-Topic führt).
KNOWN = ("heat", "power", "transmitter")


def satisfy(
    conn: sqlite3.Connection,
    group_id: int,
    req: str,
    owned_fn: Callable[[str], float] | None = None,
) -> dict | None:
    """Liefert {item, consume} eines vorhandenen Lieferanten für ``req``, oder None.

    ``consume`` = zu verbrauchende Menge (0 = nur besessen). Reine Prüfung; der
    Verbrauch passiert beim Anwenden des Effekts (ledger-gebucht).

    ``owned_fn(item_id) -> Menge`` überschreibt die Bestandsabfrage (z.B. für das
    Staging des Adjudikators: Bestand abzüglich bereits vorgemerkter Verbräuche)."""
    for fact in kb.list_topic(conn, f"provides:{req}"):
        item_id = fact["key"]
        value = fact.get("value") or {}
        consume = float(value.get("consume", 1)) if isinstance(value, dict) else 1.0
        required_min = consume if consume > 0 else 1.0
        if owned_fn is not None:
            owned = owned_fn(item_id)
        else:
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


def known_requirement(conn: sqlite3.Connection, req: str | None) -> bool:
    """Ist ``req`` eine echte Anforderung (Default-Liste oder KB-Topic vorhanden)?"""
    if not req:
        return False
    return req in KNOWN or bool(kb.list_topic(conn, f"provides:{req}"))


def requirement_from_reason(conn: sqlite3.Connection, reason: str | None) -> str | None:
    """Leitet aus einem Ablehnungsgrund (``missing:<req>`` / ``no_<req>``) die
    Anforderung ab — nur wenn sie wirklich eine ist (Items wie ``missing:rope``
    oder Gründe wie ``no_target`` ergeben None). Basis des verallgemeinerten
    Player-Overrides."""
    if not reason:
        return None
    for prefix in ("missing:", "no_"):
        if reason.startswith(prefix):
            cand = reason[len(prefix):]
            return cand if known_requirement(conn, cand) else None
    return None
