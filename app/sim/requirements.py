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

from . import constants, decay_world, energy, kb

# Anforderungen, die es von Haus aus gibt (zusätzlich zu allem, wofür die KB
# bereits ein ``provides:<req>``-Topic führt).
KNOWN = ("heat", "power", "transmitter")


GRID = "grid"  # Pseudo-Lieferant: Stromnetz (kein Inventar-Item)


def grid_power_ok(conn: sqlite3.Connection, location_id: int | None) -> bool:
    """Netzstrom am Ort verfügbar (lokaler Strom über der Lade-Schwelle)?"""
    if location_id is None:
        return False
    return decay_world.infra_for(conn, location_id)["power"] > constants.CHARGE_POWER_MIN


def satisfy(
    conn: sqlite3.Connection,
    group_id: int,
    req: str,
    owned_fn: Callable[[str], float] | None = None,
    location_id: int | None = None,
    kwh_used: dict[str, float] | None = None,
) -> dict | None:
    """Liefert {item, consume} eines vorhandenen Lieferanten für ``req``, oder None.

    ``consume`` = zu verbrauchende Menge (0 = nur besessen). Reine Prüfung; der
    Verbrauch passiert beim Anwenden des Effekts (ledger-gebucht).

    ``owned_fn(item_id) -> Menge`` überschreibt die Bestandsabfrage (z.B. für das
    Staging des Adjudikators: Bestand abzüglich bereits vorgemerkter Verbräuche).

    ``location_id``: Ort, an dem man steht. Nur für ``power``: das Stromnetz
    zählt dort als Lieferant (``{"item": "grid", "consume": 0}`` — kein Item,
    nichts zu verbuchen), solange der lokale Strom über ``CHARGE_POWER_MIN``
    liegt (dieselbe Schwelle wie beim Laden). Sonst greifen die KB-Lieferanten
    (Generator usw.).

    Energiespeicher (deklarierter Hausakku/Powerstation, ``energy``): für ``power``
    der Lieferant nach dem Netz — am Ort oder getragen, mit endlichem Vorrat. Das
    Ergebnis trägt ``kwh`` (Entnahme je Nutzung, ``STORAGE_RECIPE_KWH``); gebucht
    wird sie beim Anwenden über ``draw``. ``kwh_used`` = bereits vorgemerkte
    Entnahmen (Staging des Adjudikators)."""
    if req == "power" and location_id is not None and grid_power_ok(conn, location_id):
        return {"item": GRID, "consume": 0.0}
    if req == "power":
        store = energy.pick(conn, group_id, location_id, constants.STORAGE_RECIPE_KWH, kwh_used)
        if store is not None:
            return {"item": store, "consume": 0.0, "kwh": constants.STORAGE_RECIPE_KWH}
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


def draw(conn: sqlite3.Connection, src: dict | None) -> None:
    """Bucht die Energie-Entnahme eines ``satisfy``-Ergebnisses (nur Speicher
    tragen ``kwh``; Netz/Generator/Items: nichts zu tun)."""
    if src and src.get("kwh"):
        energy.draw(conn, src["item"], src["kwh"])


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
