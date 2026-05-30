"""SQLite-Zugriff für den Sim-Kern.

`get_connection()` liefert eine konfigurierte Verbindung, `init_db()` spielt
das Schema idempotent ein und setzt den `world_seed`. Der Sim-Kern ist die
einzige Schicht, die schreibt (CLAUDE.md, eisernes Prinzip).
"""
from __future__ import annotations

import sqlite3

from . import config


def get_connection() -> sqlite3.Connection:
    """Öffnet die DB mit aktivierten Foreign Keys und Row-Factory."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _schema_applied(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='world';"
    ).fetchone()
    return row is not None


# Leichtgewichtige Migrationen für bereits bestehende DBs (idempotent).
_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS resource_ledger ("
    " item_id TEXT PRIMARY KEY REFERENCES item_catalog(id),"
    " expected_total REAL NOT NULL DEFAULT 0.0);",
)


def init_db() -> None:
    """Idempotent: legt das Schema an, falls noch nicht vorhanden, und setzt
    den konfigurierten world_seed (nur wenn er noch auf dem Seed-Default 0 steht).
    Mehrfaches Aufrufen ist folgenlos."""
    conn = get_connection()
    try:
        if not _schema_applied(conn):
            sql = config.SCHEMA_PATH.read_text(encoding="utf-8")
            conn.executescript(sql)
            conn.commit()

        # Nachträgliche Tabellen für DBs, die vor einer Schema-Erweiterung entstanden.
        for stmt in _MIGRATIONS:
            conn.execute(stmt)
        conn.commit()

        # world_seed aus Config übernehmen, solange noch der Platzhalter (0) steht.
        row = conn.execute("SELECT world_seed FROM world WHERE id = 1;").fetchone()
        if row is not None and row["world_seed"] == 0 and config.WORLD_SEED != 0:
            conn.execute(
                "UPDATE world SET world_seed = ? WHERE id = 1;",
                (config.WORLD_SEED,),
            )
            conn.commit()
    finally:
        conn.close()


def get_world_seed(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT world_seed FROM world WHERE id = 1;").fetchone()
    return int(row["world_seed"]) if row is not None else config.WORLD_SEED
