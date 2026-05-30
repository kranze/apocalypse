"""Tests für aktivitätsabhängigen Hunger (app/sim/biology.py — apply_hunger mit distances).

Prüft, dass gelaufene Distanz und Traglast den Hungerabfall korrekt verstärken.
"""
from __future__ import annotations

import pytest
from app.sim import constants, ledger
from app.sim.biology import apply_hunger


def _add_item(conn, item_id: str, qty: float, group_id: int = 1):
    conn.execute(
        "INSERT OR REPLACE INTO group_inventory "
        "(group_id, item_id, quantity, quality, acquired_tick) VALUES (?,?,?,1.0,0);",
        (group_id, item_id, qty),
    )
    ledger.add(conn, item_id, qty)
    conn.commit()


def _hunger(conn) -> float:
    return conn.execute(
        "SELECT hunger FROM characters WHERE id=1;"
    ).fetchone()["hunger"]


class TestActivityHunger:
    def test_no_distance_matches_base_loss(self, conn):
        """Ohne Bewegung sinkt Hunger nur um den Grundverbrauch."""
        minutes = constants.TICK_MINUTES
        expected_loss = minutes / constants.MINUTES_PER_DAY * constants.HUNGER_LOSS_PER_DAY
        with conn:
            apply_hunger(conn, minutes, minutes, distances={})
        h = _hunger(conn)
        assert abs(h - (1.0 - expected_loss)) < 1e-5

    def test_walking_increases_hunger_loss(self, conn):
        """Mit Bewegung sinkt Hunger STÄRKER als ohne."""
        minutes = constants.TICK_MINUTES
        dist_m = constants.WALK_SPEED_M_PER_MIN * minutes  # genau eine Tick-Strecke

        # Hunger ohne Bewegung
        from tests.conftest import make_conn, set_seed
        c_still = make_conn()
        set_seed(c_still, 1337)
        with c_still:
            apply_hunger(c_still, minutes, minutes, distances={})
        h_still = c_still.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]
        c_still.close()

        # Hunger mit Bewegung
        with conn:
            apply_hunger(conn, minutes, minutes, distances={1: dist_m})
        h_moving = _hunger(conn)

        assert h_moving < h_still, (
            f"Bewegung sollte mehr Hunger kosten: still={h_still:.6f}, moving={h_moving:.6f}"
        )

    def test_activity_term_formula(self, conn):
        """Aktivitätsterm = K_WALK_KCAL_PER_KG_KM * (body + load) * km / daily_kcal."""
        minutes = constants.TICK_MINUTES
        dist_m = 1000.0  # 1 km

        char = conn.execute(
            "SELECT weight_kg, daily_kcal FROM characters WHERE id=1;"
        ).fetchone()
        body_kg = char["weight_kg"] or 75.0
        daily_kcal = char["daily_kcal"]
        load_kg = 0.0  # kein Inventar

        base_loss = minutes / constants.MINUTES_PER_DAY * constants.HUNGER_LOSS_PER_DAY
        kcal = constants.K_WALK_KCAL_PER_KG_KM * (body_kg + load_kg) * (dist_m / 1000.0)
        activity_loss = kcal / daily_kcal
        expected_hunger = 1.0 - base_loss - activity_loss

        with conn:
            apply_hunger(conn, minutes, minutes, distances={1: dist_m})
        h = _hunger(conn)
        assert abs(h - expected_hunger) < 1e-5

    def test_heavier_load_more_hunger(self, conn):
        """Mehr Traglast → größerer Hungerverlust bei gleicher Distanz."""
        minutes = constants.TICK_MINUTES
        dist_m = 500.0

        from tests.conftest import make_conn, set_seed

        # DB mit leichtem Rucksack (keine Items)
        c_light = make_conn()
        set_seed(c_light, 1337)
        with c_light:
            apply_hunger(c_light, minutes, minutes, distances={1: dist_m})
        h_light = c_light.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]
        c_light.close()

        # DB mit schwerem Rucksack (firewood: 1.5 kg × 10 = 15 kg)
        c_heavy = make_conn()
        set_seed(c_heavy, 1337)
        c_heavy.execute(
            "INSERT INTO group_inventory (group_id, item_id, quantity, quality, acquired_tick) "
            "VALUES (1, 'firewood', 10.0, 1.0, 0);"
        )
        c_heavy.commit()
        with c_heavy:
            apply_hunger(c_heavy, minutes, minutes, distances={1: dist_m})
        h_heavy = c_heavy.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]
        c_heavy.close()

        assert h_heavy < h_light, (
            f"Schwerer Rucksack sollte mehr Hunger kosten: light={h_light:.6f}, heavy={h_heavy:.6f}"
        )

    def test_load_weight_formula_exact(self, conn):
        """Formel mit Last: K * (body + load) * km / daily_kcal."""
        minutes = constants.TICK_MINUTES
        dist_m = 1000.0

        # Inventar: 5× firewood à 1.5 kg = 7.5 kg Last
        _add_item(conn, "firewood", 5.0)

        char = conn.execute(
            "SELECT weight_kg, daily_kcal FROM characters WHERE id=1;"
        ).fetchone()
        body_kg = char["weight_kg"] or 75.0
        daily_kcal = char["daily_kcal"]

        from app.sim.movement import carried_weight
        load_kg = carried_weight(conn, 1)

        base_loss = minutes / constants.MINUTES_PER_DAY * constants.HUNGER_LOSS_PER_DAY
        kcal = constants.K_WALK_KCAL_PER_KG_KM * (body_kg + load_kg) * (dist_m / 1000.0)
        activity_loss = kcal / daily_kcal
        expected_hunger = 1.0 - base_loss - activity_loss

        with conn:
            apply_hunger(conn, minutes, minutes, distances={1: dist_m})
        h = _hunger(conn)
        assert abs(h - expected_hunger) < 1e-5

    def test_no_activity_loss_for_unknown_char_id(self, conn):
        """Char-ID nicht in distances → kein Aktivitätsterm (bleibt Grundverbrauch)."""
        minutes = constants.TICK_MINUTES
        expected_loss = minutes / constants.MINUTES_PER_DAY * constants.HUNGER_LOSS_PER_DAY

        # Nur char_id=99 in distances, nicht char_id=1
        with conn:
            apply_hunger(conn, minutes, minutes, distances={99: 1000.0})
        h = _hunger(conn)
        assert abs(h - (1.0 - expected_loss)) < 1e-5

    def test_hunger_not_below_zero_with_huge_distance(self, conn):
        """Auch bei enormer Distanz bleibt Hunger ≥ 0."""
        # Sehr hungrig setzen + riesige Distanz
        conn.execute("UPDATE characters SET hunger=0.001 WHERE id=1;")
        conn.commit()
        with conn:
            apply_hunger(conn, 60, 60, distances={1: 100_000.0})
        h = _hunger(conn)
        assert h >= 0.0
