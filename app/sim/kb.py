"""Knowledge Base — verbindliche Fakten für den Adjudikator (DESIGN.md §8/§9).

Provenance-Vorrang: ``curated`` > ``player_verified`` > ``llm_inferred``. Ein
höherwertiger Fakt darf nicht von einem niederwertigen überschrieben werden
(kuratiertes Wissen ist bindend). Player-Overrides reichern die KB als
``player_verified`` an — so wird das Spiel über die Spielerschaft schlauer.

Reine Lese-/Schreibhelfer auf der ``knowledge_base``-Tabelle; geschrieben wird
nur über den Sim-Kern (Adjudikator/Override), nie durch das LLM.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

_RANK = {"curated": 3, "player_verified": 2, "llm_inferred": 1}


def lookup(conn: sqlite3.Connection, topic: str, key: str) -> dict[str, Any] | None:
    """Liefert {value, provenance} für (topic,key) oder None. value ist
    JSON-dekodiert, falls möglich."""
    row = conn.execute(
        "SELECT value, provenance FROM knowledge_base WHERE topic = ? AND key = ?;",
        (topic, key),
    ).fetchone()
    if row is None:
        return None
    return {"value": _decode(row["value"]), "provenance": row["provenance"]}


def list_topic(conn: sqlite3.Connection, topic: str) -> list[dict[str, Any]]:
    """Alle Fakten eines Topics."""
    return [
        {"key": r["key"], "value": _decode(r["value"]), "provenance": r["provenance"]}
        for r in conn.execute(
            "SELECT key, value, provenance FROM knowledge_base WHERE topic = ? "
            "ORDER BY key;",
            (topic,),
        ).fetchall()
    ]


def add(
    conn: sqlite3.Connection,
    topic: str,
    key: str,
    value: Any,
    provenance: str = "player_verified",
    tick: int | None = None,
) -> bool:
    """Fügt einen Fakt hinzu/aktualisiert ihn — aber nur, wenn die neue
    Provenance mindestens so hochwertig ist wie die bestehende. Liefert True,
    wenn geschrieben wurde. Läuft innerhalb der Transaktion des Aufrufers."""
    existing = conn.execute(
        "SELECT provenance FROM knowledge_base WHERE topic = ? AND key = ?;",
        (topic, key),
    ).fetchone()
    if existing is not None and _RANK.get(provenance, 0) < _RANK.get(existing["provenance"], 0):
        return False
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    conn.execute(
        "INSERT INTO knowledge_base (topic, key, value, provenance, created_tick) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(topic, key) DO UPDATE SET "
        "value = excluded.value, provenance = excluded.provenance, "
        "created_tick = excluded.created_tick;",
        (topic, key, payload, provenance, tick),
    )
    return True


def _decode(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


# Code-definierte, kuratierte Grundfakten für Effekte des Adjudikators v2, die
# nicht im DB-Seed stehen (``barricade``: Capability-Rezept + Materiallieferanten).
_DEFAULT_FACTS: tuple[tuple[str, str, str], ...] = (
    ("capability_recipe:barricade", "barricade", '{"requires": ["barricade_material"]}'),
    ("provides:barricade_material", "rope", '{"consume": 2}'),
    ("provides:barricade_material", "duct_tape", '{"consume": 3}'),
)


def seed_defaults(conn: sqlite3.Connection) -> None:
    """Idempotent (INSERT OR IGNORE, nie Überschreiben): legt die oben
    definierten kuratierten Fakten an. Läuft in der Transaktion des Aufrufers."""
    for topic, key, value in _DEFAULT_FACTS:
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_base (topic, key, value, provenance, "
            "created_tick) VALUES (?, ?, ?, 'curated', 0);",
            (topic, key, value),
        )
