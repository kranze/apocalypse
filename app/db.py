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
    "CREATE TABLE IF NOT EXISTS survivors ("
    " id INTEGER PRIMARY KEY,"
    " lat REAL NOT NULL,"
    " lon REAL NOT NULL,"
    " materialized INTEGER NOT NULL DEFAULT 0,"
    " character_id INTEGER REFERENCES characters(id));",
    "CREATE INDEX IF NOT EXISTS idx_survivors_geo ON survivors(lat, lon);",
    "CREATE TABLE IF NOT EXISTS resource_ledger ("
    " item_id TEXT PRIMARY KEY REFERENCES item_catalog(id),"
    " expected_total REAL NOT NULL DEFAULT 0.0);",
    "CREATE TABLE IF NOT EXISTS knowledge_base ("
    " id INTEGER PRIMARY KEY, topic TEXT NOT NULL, key TEXT NOT NULL,"
    " value TEXT, provenance TEXT NOT NULL DEFAULT 'curated',"
    " created_tick INTEGER, UNIQUE(topic, key));",
    "CREATE TABLE IF NOT EXISTS capabilities ("
    " id INTEGER PRIMARY KEY, ctype TEXT NOT NULL, owner_group INTEGER,"
    " location_id INTEGER, params TEXT, active INTEGER NOT NULL DEFAULT 1,"
    " created_tick INTEGER, upkeep TEXT);",
    "CREATE TABLE IF NOT EXISTS location_searches ("
    " id INTEGER PRIMARY KEY,"
    " location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,"
    " term TEXT NOT NULL, created_tick INTEGER, UNIQUE(location_id, term));",
    "CREATE TABLE IF NOT EXISTS chat_log ("
    " id INTEGER PRIMARY KEY, character_id INTEGER NOT NULL"
    " REFERENCES characters(id) ON DELETE CASCADE, turn INTEGER NOT NULL,"
    " role TEXT NOT NULL, text TEXT NOT NULL, created_tick INTEGER);",
)

# (tabelle, spalte, DDL-Definition) — nur angelegt, wenn die Spalte fehlt.
_ADD_COLUMNS = (
    ("characters", "dest_lat", "REAL"),
    ("characters", "dest_lon", "REAL"),
    ("characters", "path_json", "TEXT"),
    ("item_catalog", "needs_preparation", "INTEGER NOT NULL DEFAULT 0"),
    ("item_catalog", "requires_water_l", "REAL NOT NULL DEFAULT 0.0"),
    ("item_catalog", "prepared_into", "TEXT"),
    ("locations", "label", "TEXT"),
    ("locations", "footprint_json", "TEXT"),
    # Onboarding-/Profil-Felder + abgeleitete Achsen.
    ("characters", "birthdate", "TEXT"),
    ("characters", "sex", "TEXT"),
    ("characters", "height_cm", "REAL"),
    ("characters", "profession", "TEXT"),
    ("characters", "education", "TEXT"),
    ("characters", "family", "TEXT"),
    ("characters", "hobbies", "TEXT"),
    ("characters", "self_description", "TEXT"),
    ("characters", "home_lat", "REAL"),
    ("characters", "home_lon", "REAL"),
    ("characters", "satisfaction", "REAL NOT NULL DEFAULT 1.0"),
    ("characters", "daily_water_l", "REAL NOT NULL DEFAULT 2.5"),
)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table});").fetchall()}


def _seed_static(conn: sqlite3.Connection) -> None:
    """Idempotenter Seed der Stammdaten (auch für bestehende DBs): Zubereitung,
    neue Geräte-Items und die Knowledge-Base-Grundfakten (provides:* + Rezepte)."""
    # Zubereitung
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
    # Geräte für emergente Aktionen (Adjudikator/Capabilities)
    for vals in (
        "('generator',   'Stromgenerator', 'tool',  25.0, NULL, NULL, 0, 0, 0.0, NULL)",
        "('wifi_router', 'WLAN-Router',    'tool',   1.0, NULL, NULL, 0, 0, 0.0, NULL)",
        "('gasoline',    'Benzin 5L',      'fuel',   4.0, NULL, NULL, 1, 0, 0.0, NULL)",
    ):
        conn.execute(
            "INSERT OR IGNORE INTO item_catalog (id, name, category, weight_kg, "
            "kcal_per_unit, decay_halflife_min, stackable, needs_preparation, "
            "requires_water_l, prepared_into) VALUES " + vals + ";"
        )
    # Knowledge Base: Lieferanten (provides:*) + Capability-Rezept
    for topic, key, value in (
        ("provides:heat", "firewood", '{"consume": 1}'),
        ("provides:power", "generator", '{"consume": 0}'),
        ("provides:transmitter", "wifi_router", '{"consume": 0}'),
        ("capability_recipe:ssid_beacon", "ssid_beacon",
         '{"requires": ["power", "transmitter"], '
         '"upkeep": {"item": "gasoline", "per_tick": 0.02}, "range_km": 1.5}'),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_base (topic, key, value, provenance, "
            "created_tick) VALUES (?, ?, ?, 'curated', 0);",
            (topic, key, value),
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

        # Statische Stammdaten idempotent nachziehen (Seed für bestehende DBs).
        _seed_static(conn)
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
