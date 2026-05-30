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
# Tabellen via CREATE TABLE IF NOT EXISTS; Spalten via _ADD_COLUMNS (geprüft).
_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS resource_ledger ("
    " item_id TEXT PRIMARY KEY REFERENCES item_catalog(id),"
    " expected_total REAL NOT NULL DEFAULT 0.0);",
)

# (tabelle, spalte, DDL-Definition) — nur angelegt, wenn die Spalte fehlt.
_ADD_COLUMNS = (
    ("characters", "dest_lat", "REAL"),
    ("characters", "dest_lon", "REAL"),
    ("characters", "path_json", "TEXT"),
    ("item_catalog", "needs_preparation", "INTEGER NOT NULL DEFAULT 0"),
    ("item_catalog", "requires_water_l", "REAL NOT NULL DEFAULT 0.0"),
    ("item_catalog", "prepared_into", "TEXT"),
)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table});").fetchall()}


def _seed_preparation(conn: sqlite3.Connection) -> None:
    """Idempotenter Seed der Zubereitungs-Stammdaten (auch für bestehende DBs).
    Mahlzeit als Ziel-Item + rohe Nudeln als zuzubereitendes Item."""
    conn.execute(
        "INSERT OR IGNORE INTO item_catalog "
        "(id, name, category, weight_kg, kcal_per_unit, decay_halflife_min, "
        " stackable, needs_preparation, requires_water_l, prepared_into) VALUES "
        "('meal_pasta', 'Gekochte Nudeln', 'food', 0.55, 1750, 1440, 1, 0, 0.0, NULL);"
    )
    conn.execute(
        "UPDATE item_catalog SET needs_preparation = 1, requires_water_l = 0.5, "
        "prepared_into = 'meal_pasta' WHERE id = 'pasta_500g';"
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
        # Nachträgliche Spalten (SQLite kennt kein ADD COLUMN IF NOT EXISTS).
        for table, column, ddl in _ADD_COLUMNS:
            if column not in _existing_columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")
        conn.commit()

        # Zubereitungs-Stammdaten idempotent nachziehen (Seed für bestehende DBs).
        _seed_preparation(conn)
        conn.commit()

        # world_seed aus Config übernehmen, solange noch der Platzhalter (0) steht.
        row = conn.execute("SELECT world_seed FROM world WHERE id = 1;").fetchone()
        if row is not None and row["world_seed"] == 0 and config.WORLD_SEED != 0:
            conn.execute(
                "UPDATE world SET world_seed = ? WHERE id = 1;",
                (config.WORLD_SEED,),
            )
            conn.commit()

        # Spieler-Startposition: auf das Viertel-Zentrum, solange noch nicht gesetzt.
        conn.execute(
            "UPDATE characters SET lat = ?, lon = ? "
            "WHERE type = 'player' AND lat IS NULL;",
            (config.CENTER_LAT, config.CENTER_LON),
        )
        conn.commit()
    finally:
        conn.close()


def get_world_seed(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT world_seed FROM world WHERE id = 1;").fetchone()
    return int(row["world_seed"]) if row is not None else config.WORLD_SEED
