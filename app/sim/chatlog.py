"""Chat-Log — Persistentes Gesprächs-Gedächtnis für den Adjudikator (DESIGN.md §8).

Nur der Sim-Kern schreibt in ``chat_log``. Das LLM liest es allenfalls (über Kontext),
schreibt es NIE selbst. Turns sind pro Character fortlaufend (beginnend bei 1) und
dienen dem Adjudikator als Replay-Basis für Kontext-Priming.

Schreibvorgänge laufen über die Sim-Kern-Funktionen; Aufrufer ist verantwortlich
für commit (entweder eigene ``with conn:`` oder aufrufer-seitig).
"""
from __future__ import annotations

import sqlite3


def append(conn: sqlite3.Connection, character_id: int, role: str, text: str) -> int:
    """Hängt einen Eintrag an die Chat-Log eines Characters an.

    Falls ``text`` leer oder None ist, wird nichts eingefügt und 0 zurückgegeben.
    Sonst wird der nächste ``turn`` bestimmt (max(turn) + 1 pro Character, Start: 1),
    ``created_tick`` aus der ``world``-Tabelle gelesen und der Eintrag INSERT-ed.

    Gibt den neuen ``turn`` zurück (oder 0 bei leerer/None-Text).
    Transaktion wird NICHT erzwungen — Aufrufer ist verantwortlich für commit.
    """
    if not text:  # None oder leerer String
        return 0

    # Nächsten Turn bestimmen
    row = conn.execute(
        "SELECT COALESCE(MAX(turn), 0) + 1 AS next_turn FROM chat_log WHERE character_id = ?;",
        (character_id,),
    ).fetchone()
    next_turn = row["next_turn"] if row else 1

    # Aktuellen Tick lesen
    world_row = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()
    created_tick = world_row["tick"] if world_row else None

    # Eintrag hinzufügen
    conn.execute(
        "INSERT INTO chat_log (character_id, turn, role, text, created_tick) "
        "VALUES (?, ?, ?, ?, ?);",
        (character_id, next_turn, role, text, created_tick),
    )

    return next_turn


def recent(conn: sqlite3.Connection, character_id: int, limit: int = 16) -> list[dict]:
    """Liefert die letzten N Turns eines Characters, chronologisch aufsteigend.

    Rückgabe: Liste von ``{turn, role, text}`` (in aufsteigender turn-Reihenfolge).
    """
    rows = conn.execute(
        "SELECT turn, role, text FROM chat_log "
        "WHERE character_id = ? AND turn IN ("
        "  SELECT turn FROM chat_log WHERE character_id = ? "
        "  ORDER BY turn DESC LIMIT ?"
        ") ORDER BY turn ASC;",
        (character_id, character_id, limit),
    ).fetchall()
    return [{"turn": r["turn"], "role": r["role"], "text": r["text"]} for r in rows]


def clear(conn: sqlite3.Connection, character_id: int) -> None:
    """Löscht alle Chat-Log-Einträge eines Characters (z.B. bei Neues Spiel)."""
    conn.execute("DELETE FROM chat_log WHERE character_id = ?;", (character_id,))
