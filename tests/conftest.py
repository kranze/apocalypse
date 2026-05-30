"""Gemeinsame Fixtures und Hilfsfunktionen für den Sim-Kern-Test."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Projekt-Root in sys.path eintragen, damit "import app..." funktioniert.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.config as config


def make_conn(db_path: str = ":memory:") -> sqlite3.Connection:
    """Erzeugt eine frische SQLite-Connection mit Row-Factory und schema.sql."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def set_seed(conn: sqlite3.Connection, seed: int) -> None:
    """Setzt den world_seed in world(id=1)."""
    conn.execute("UPDATE world SET world_seed = ? WHERE id = 1;", (seed,))
    conn.commit()


def insert_location(
    conn: sqlite3.Connection,
    *,
    loc_id: int = 100,
    loc_type: str = "supermarket",
    name: str = "Test-Markt",
    osm_id: str | None = None,
    generation_seed: int = 9999,
    lat: float = 49.0,
    lon: float = 11.0,
) -> int:
    """Legt eine Test-Location an und gibt ihre ID zurück."""
    if osm_id is None:
        osm_id = f"test_{loc_id}"
    conn.execute(
        "INSERT INTO locations (id, osm_id, type, name, lat, lon, footprint_m2, "
        "discovery_status, generation_seed) VALUES (?, ?, ?, ?, ?, ?, 200.0, "
        "'undiscovered', ?);",
        (loc_id, osm_id, loc_type, name, lat, lon, generation_seed),
    )
    conn.commit()
    return loc_id


@pytest.fixture
def conn():
    """Frische In-Memory-DB, Seed 1337."""
    c = make_conn()
    set_seed(c, 1337)
    yield c
    c.close()


@pytest.fixture
def conn_seeded(tmp_path):
    """DB in tmp_path, Seed 1337 — für Tests, die zwei DBs vergleichen."""
    def _make(extra: str = "") -> sqlite3.Connection:
        p = tmp_path / f"test{extra}.db"
        c = sqlite3.connect(str(p))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON;")
        c.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
        c.execute("UPDATE world SET world_seed = 1337 WHERE id = 1;")
        c.commit()
        return c
    return _make
