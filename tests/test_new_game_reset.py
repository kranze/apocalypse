"""Regressionstest fuer Issue #40: new_game setzt survivor_sim_day + survivor_groups zurueck.

Strategie: new_game wird mit gemocktem OSM/roads aufgerufen (kein echtes Netz).
Vorher werden survivor_groups befuellt und survivor_sim_day auf > 0 gesetzt.
Nach new_game muss gelten: survivor_sim_day=0, survivor_groups leer,
alle survivors haben group_id=NULL.

Eisernes Prinzip: nur Sim-Kern schreibt; kein LLM-Zugriff.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.conftest import make_conn, set_seed
from app.sim import survivors as surv_mod
from app.sim import popgrid as popgrid_mod

_MINI_GRID = [
    (48.0, 11.0, 100.0),
]

_FAKE_LAT = 48.137
_FAKE_LON = 11.575


def _patch_all_osm(monkeypatch):
    """Mockt alle Netz-abhaengigen Aufrufe in new_game."""
    from app.osm import geocode, overpass, roads, loader
    from app.sim import chunks

    # geocode nicht benoetigt (wir uebergeben lat/lon direkt im Profil)

    # overpass.fetch_bbox: kein Netz, gibt leeres Dict zurueck
    monkeypatch.setattr(overpass, "fetch_bbox", lambda *a, **kw: {})

    # roads.fetch_roads: kein Netz, kein Fehler
    monkeypatch.setattr(roads, "fetch_roads", lambda *a, **kw: None)

    # roads.get_graph: kein Netz
    monkeypatch.setattr(roads, "get_graph", lambda *a, **kw: None)

    # loader.load_bbox: simuliert 0 neue Locations
    monkeypatch.setattr(loader, "load_bbox", lambda *a, **kw: 0)

    # popgrid: kleines Mini-Gitter fuer schnellen Spawn
    popgrid_mod.load_grid.cache_clear()
    monkeypatch.setattr(surv_mod, "load_grid", lambda: _MINI_GRID)


def _prefill_stale_state(conn):
    """Befuellt survivor_groups + setzt survivor_sim_day > 0 (simuliert alten Spielstand)."""
    # Zwei Gruppen einfuegen (nur vorhandene Spalten lt. Schema)
    conn.execute(
        "INSERT INTO survivor_groups (id, lat, lon) VALUES (1, 48.0, 11.0);"
    )
    conn.execute(
        "INSERT INTO survivor_groups (id, lat, lon) VALUES (2, 49.0, 12.0);"
    )
    # Tageszaehler auf 3 setzen
    conn.execute("UPDATE world SET survivor_sim_day = 3 WHERE id = 1;")
    conn.commit()

    # Sicherstellen dass der alte State da ist
    groups = conn.execute("SELECT COUNT(*) FROM survivor_groups;").fetchone()[0]
    day = conn.execute("SELECT survivor_sim_day FROM world WHERE id = 1;").fetchone()[0]
    assert groups == 2, f"Erwartete 2 Gruppen als Vorbedingung, war {groups}"
    assert day == 3, f"Erwartete survivor_sim_day=3 als Vorbedingung, war {day}"


class TestNewGameReset:
    """Prueft dass new_game den alten Spielstand vollstaendig zuruecksetzt."""

    def test_survivor_sim_day_reset_to_zero(self, monkeypatch):
        """Nach new_game ist survivor_sim_day=0 (nicht der alte Wert)."""
        conn = make_conn()
        set_seed(conn, 42)
        _patch_all_osm(monkeypatch)
        _prefill_stale_state(conn)

        from app.sim.game import new_game
        result = new_game(conn, {"lat": _FAKE_LAT, "lon": _FAKE_LON})

        assert result["ok"] is True, f"new_game fehlgeschlagen: {result}"
        day = conn.execute("SELECT survivor_sim_day FROM world WHERE id = 1;").fetchone()[0]
        assert day == 0, f"survivor_sim_day nach new_game: erwartet 0, war {day}"

    def test_survivor_groups_cleared(self, monkeypatch):
        """Nach new_game ist die survivor_groups-Tabelle leer."""
        conn = make_conn()
        set_seed(conn, 42)
        _patch_all_osm(monkeypatch)
        _prefill_stale_state(conn)

        from app.sim.game import new_game
        result = new_game(conn, {"lat": _FAKE_LAT, "lon": _FAKE_LON})

        assert result["ok"] is True, f"new_game fehlgeschlagen: {result}"
        groups = conn.execute("SELECT COUNT(*) FROM survivor_groups;").fetchone()[0]
        assert groups == 0, f"survivor_groups nach new_game: erwartet 0, war {groups}"

    def test_survivors_have_no_group_id(self, monkeypatch):
        """Alle survivors nach new_game haben group_id=NULL."""
        conn = make_conn()
        set_seed(conn, 42)
        _patch_all_osm(monkeypatch)
        _prefill_stale_state(conn)

        from app.sim.game import new_game
        result = new_game(conn, {"lat": _FAKE_LAT, "lon": _FAKE_LON})

        assert result["ok"] is True, f"new_game fehlgeschlagen: {result}"
        with_group = conn.execute(
            "SELECT COUNT(*) FROM survivors WHERE group_id IS NOT NULL;"
        ).fetchone()[0]
        assert with_group == 0, f"survivors mit group_id nach new_game: {with_group} (erwartet 0)"

    def test_population_stats_consistent_after_reset(self, monkeypatch):
        """Nach new_game: day=0, groups=0 in population_stats-relevanten Feldern."""
        conn = make_conn()
        set_seed(conn, 42)
        _patch_all_osm(monkeypatch)
        _prefill_stale_state(conn)

        from app.sim.game import new_game
        result = new_game(conn, {"lat": _FAKE_LAT, "lon": _FAKE_LON})

        assert result["ok"] is True, f"new_game fehlgeschlagen: {result}"

        day = conn.execute("SELECT survivor_sim_day FROM world WHERE id = 1;").fetchone()[0]
        groups = conn.execute("SELECT COUNT(*) FROM survivor_groups;").fetchone()[0]
        grouped = conn.execute(
            "SELECT COUNT(*) FROM survivors WHERE group_id IS NOT NULL;"
        ).fetchone()[0]

        assert day == 0, f"day={day}"
        assert groups == 0, f"groups={groups}"
        assert grouped == 0, f"grouped={grouped}"
