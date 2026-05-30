"""Deterministmus-Tests: gleicher Seed → identische Ergebnisse.

Prüft Bewegung, Hunger und Position über mehrere Ticks auf zwei frischen DBs.
"""
from __future__ import annotations

import json

import pytest
from app.sim import constants, ledger
from app.sim.movement import advance_movement
from app.sim.biology import apply_hunger


def _setup(conn, *, lat: float, lon: float, path: list):
    """Setzt Position und Pfad für char id=1."""
    conn.execute(
        "UPDATE characters SET lat=?, lon=?, path_json=? WHERE id=1;",
        (lat, lon, json.dumps(path)),
    )
    conn.commit()


def _pos(conn) -> tuple[float, float]:
    row = conn.execute("SELECT lat, lon FROM characters WHERE id=1;").fetchone()
    return row["lat"], row["lon"]


def _hunger(conn) -> float:
    return conn.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]


class TestDeterminism:
    def test_same_seed_same_position_after_movement(self, conn_seeded):
        """Zwei DBs, gleicher Seed, gleicher Pfad → identische Endposition nach N Ticks."""
        c1 = conn_seeded("_det1")
        c2 = conn_seeded("_det2")

        # Langer Pfad (2 km) — wird in mehreren Ticks abgelaufen
        path = [[0.01, 0.0], [0.02, 0.0]]  # ca. 2.2 km
        for c in [c1, c2]:
            _setup(c, lat=0.0, lon=0.0, path=path)

        n_ticks = 5
        minutes = constants.TICK_MINUTES
        for tick in range(1, n_ticks + 1):
            advance_movement(c1, minutes, tick * minutes)
            advance_movement(c2, minutes, tick * minutes)

        lat1, lon1 = _pos(c1)
        lat2, lon2 = _pos(c2)
        assert abs(lat1 - lat2) < 1e-12
        assert abs(lon1 - lon2) < 1e-12
        c1.close()
        c2.close()

    def test_same_seed_same_hunger_after_movement_ticks(self, conn_seeded):
        """Zwei DBs, gleicher Seed, gleiche Bewegung → identischer Hunger nach N Ticks."""
        c1 = conn_seeded("_dhunger1")
        c2 = conn_seeded("_dhunger2")

        path = [[0.01, 0.0]]
        for c in [c1, c2]:
            _setup(c, lat=0.0, lon=0.0, path=path)

        minutes = constants.TICK_MINUTES
        for tick_n in range(1, 6):
            now = tick_n * minutes
            d1 = advance_movement(c1, minutes, now)
            d2 = advance_movement(c2, minutes, now)
            d1.pop("_interrupts", None)
            d2.pop("_interrupts", None)
            with c1:
                apply_hunger(c1, minutes, now, distances=d1)
            with c2:
                apply_hunger(c2, minutes, now, distances=d2)

        h1 = _hunger(c1)
        h2 = _hunger(c2)
        assert abs(h1 - h2) < 1e-12
        c1.close()
        c2.close()

    def test_same_seed_same_arrival_tick(self, conn_seeded):
        """Zwei DBs — Charakter kommt im selben Tick an (path_json wird nil)."""
        c1 = conn_seeded("_darr1")
        c2 = conn_seeded("_darr2")

        # Kurzer Weg, der in einem Tick abgelaufen wird
        path = [[0.0001, 0.0]]  # ~11 m
        for c in [c1, c2]:
            _setup(c, lat=0.0, lon=0.0, path=path)
            c.execute(
                "UPDATE characters SET dest_lat=0.0001, dest_lon=0.0 WHERE id=1;"
            )
            c.commit()

        r1 = advance_movement(c1, 10, 10)
        r2 = advance_movement(c2, 10, 10)

        # Beide kommen an → path_json ist None
        pj1 = c1.execute("SELECT path_json FROM characters WHERE id=1;").fetchone()["path_json"]
        pj2 = c2.execute("SELECT path_json FROM characters WHERE id=1;").fetchone()["path_json"]
        assert pj1 is None
        assert pj2 is None

        # Interrupts identisch (Ankunft emittiert)
        assert len(r1.get("_interrupts", [])) == len(r2.get("_interrupts", []))
        c1.close()
        c2.close()

    def test_different_seed_movement_deterministic_regardless(self, conn_seeded):
        """Bewegung selbst ist vollständig deterministisch (kein RNG) —
        seed spielt keine Rolle, Endposition nur von path_json + speed abhängig."""
        c1 = conn_seeded("_dseed1")
        c2 = conn_seeded("_dseed2")
        # Verschiedene Seeds
        c1.execute("UPDATE world SET world_seed=1111 WHERE id=1;")
        c2.execute("UPDATE world SET world_seed=9999 WHERE id=1;")
        c1.commit()
        c2.commit()

        path = [[0.005, 0.0]]
        for c in [c1, c2]:
            _setup(c, lat=0.0, lon=0.0, path=path)

        for tick_n in range(1, 4):
            advance_movement(c1, constants.TICK_MINUTES, tick_n * constants.TICK_MINUTES)
            advance_movement(c2, constants.TICK_MINUTES, tick_n * constants.TICK_MINUTES)

        lat1, lon1 = _pos(c1)
        lat2, lon2 = _pos(c2)
        # Gleiche Endposition, weil Bewegung deterministisch und seed-unabhängig
        assert abs(lat1 - lat2) < 1e-12
        assert abs(lon1 - lon2) < 1e-12
        c1.close()
        c2.close()

    def test_fresh_db_same_seed_same_tick_result(self, conn_seeded):
        """Vollständiger advance_tick — gleicher Seed → gleicher Tick-Stand und Hunger."""
        from app.sim.tick import advance_tick

        c1 = conn_seeded("_fulldet1")
        c2 = conn_seeded("_fulldet2")

        # Einfache Konfiguration: kein Pfad, kein Inventar
        for c in [c1, c2]:
            c.execute("UPDATE characters SET lat=0.0, lon=0.0 WHERE id=1;")
            c.commit()

        n = 3
        for _ in range(n):
            advance_tick(c1)
            advance_tick(c2)

        tick1 = c1.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        tick2 = c2.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        h1 = _hunger(c1)
        h2 = _hunger(c2)
        assert tick1 == tick2
        assert abs(h1 - h2) < 1e-12
        c1.close()
        c2.close()
