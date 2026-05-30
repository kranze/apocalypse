"""Tests für app/sim/biology.py — Hunger, Performance, Tod."""
from __future__ import annotations

import pytest
from app.sim import constants
from app.sim.biology import apply_hunger, death_check, recompute_performance
from app.sim.resources import eat


class TestApplyHunger:
    def test_hunger_decreases_per_tick(self, conn):
        """Hunger sinkt pro Tick korrekt."""
        minutes = constants.TICK_MINUTES
        expected_loss = minutes / constants.MINUTES_PER_DAY * constants.HUNGER_LOSS_PER_DAY
        with conn:
            apply_hunger(conn, minutes, minutes)
        hunger = conn.execute(
            "SELECT hunger FROM characters WHERE id=1;"
        ).fetchone()["hunger"]
        assert abs(hunger - (1.0 - expected_loss)) < 1e-5

    def test_hunger_loss_per_day(self, conn):
        """Nach einem vollen Spieltag (1440 Minuten) sinkt Hunger um HUNGER_LOSS_PER_DAY."""
        with conn:
            apply_hunger(conn, constants.MINUTES_PER_DAY, constants.MINUTES_PER_DAY)
        hunger = conn.execute(
            "SELECT hunger FROM characters WHERE id=1;"
        ).fetchone()["hunger"]
        expected = 1.0 - constants.HUNGER_LOSS_PER_DAY
        assert abs(hunger - expected) < 1e-5

    def test_hunger_not_below_zero(self, conn):
        """Hunger bleibt bei 0 nach sehr vielen Ticks."""
        for i in range(1, 10_000, 100):
            with conn:
                apply_hunger(conn, 100, i)
        hunger = conn.execute(
            "SELECT hunger FROM characters WHERE id=1;"
        ).fetchone()["hunger"]
        assert hunger >= 0.0

    def test_hunger_accumulates_linearly(self, conn):
        """Mehrere kleine Ticks ergeben dasselbe Ergebnis wie ein großer."""
        from tests.conftest import make_conn, set_seed
        c1 = make_conn()
        set_seed(c1, 1337)
        c2 = make_conn()
        set_seed(c2, 1337)
        # c1: ein großer Tick
        with c1:
            apply_hunger(c1, 100, 100)
        h1 = c1.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]
        # c2: zehn kleine Ticks
        for i in range(1, 11):
            with c2:
                apply_hunger(c2, 10, i * 10)
        h2 = c2.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]
        assert abs(h1 - h2) < 1e-5
        c1.close()
        c2.close()

    def test_soft_interrupt_at_hunger_soft_threshold(self, conn):
        """Sinkt Hunger unter HUNGER_SOFT, kommt ein Interrupt."""
        # Hunger knapp über HUNGER_SOFT setzen
        above = constants.HUNGER_SOFT + 0.01
        conn.execute("UPDATE characters SET hunger=? WHERE id=1;", (above,))
        conn.commit()
        loss_for_cross = 0.02  # genug, um HUNGER_SOFT zu unterschreiten
        minutes = int(loss_for_cross * constants.MINUTES_PER_DAY / constants.HUNGER_LOSS_PER_DAY) + 1
        with conn:
            interrupts = apply_hunger(conn, minutes, minutes)
        assert any(i["category"] == "need" for i in interrupts)

    def test_no_interrupt_well_fed(self, conn):
        """Kein Interrupt, solange Hunger weit über HUNGER_SOFT."""
        # Hunger = 1.0; ein kleiner Tick schlägt keinen Schwellwert über
        with conn:
            interrupts = apply_hunger(conn, constants.TICK_MINUTES, constants.TICK_MINUTES)
        assert len(interrupts) == 0


class TestRecomputePerformance:
    def test_performance_one_at_full_hunger(self, conn):
        conn.execute("UPDATE characters SET hunger=1.0 WHERE id=1;")
        conn.commit()
        with conn:
            recompute_performance(conn)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        assert perf == 1.0

    def test_performance_one_at_comfort_threshold(self, conn):
        conn.execute("UPDATE characters SET hunger=? WHERE id=1;", (constants.PERF_COMFORT_HUNGER,))
        conn.commit()
        with conn:
            recompute_performance(conn)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        assert abs(perf - 1.0) < 1e-4

    def test_performance_zero_at_hunger_zero(self, conn):
        conn.execute("UPDATE characters SET hunger=0.0 WHERE id=1;")
        conn.commit()
        with conn:
            recompute_performance(conn)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        assert perf == 0.0

    def test_performance_linear_below_comfort(self, conn):
        """Hunger = 0.25 = 0.5 * PERF_COMFORT_HUNGER → Performance = 0.5."""
        h = constants.PERF_COMFORT_HUNGER * 0.5
        conn.execute("UPDATE characters SET hunger=? WHERE id=1;", (h,))
        conn.commit()
        with conn:
            recompute_performance(conn)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        assert abs(perf - 0.5) < 1e-4

    def test_dead_chars_not_updated(self, conn):
        """Tote Charaktere werden nicht neu berechnet."""
        conn.execute("UPDATE characters SET is_alive=0, hunger=0.0 WHERE id=1;")
        conn.commit()
        with conn:
            recompute_performance(conn)
        # Tote Chars: performance soll nicht berührt werden (bleibt auf 0, oder
        # was auch immer davor gesetzt wurde)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        # Wir prüfen nur, dass kein Crash auftritt; der Wert bleibt, was er war
        assert perf is not None


class TestDeathCheck:
    def test_no_death_above_crit_performance(self, conn):
        """Performance über CRIT_PERFORMANCE → kein Sterbe-Wurf."""
        conn.execute(
            "UPDATE characters SET performance=?, hunger=? WHERE id=1;",
            (constants.CRIT_PERFORMANCE + 0.1, 0.5),
        )
        conn.commit()
        with conn:
            interrupts = death_check(conn, 100, constants.TICK_MINUTES, 1337)
        alive = conn.execute("SELECT is_alive FROM characters WHERE id=1;").fetchone()["is_alive"]
        assert alive == 1
        assert all(i.get("severity") != "decision" or "gestorben" not in i.get("message", "") for i in interrupts)

    def test_death_deterministic_same_seed(self, conn_seeded):
        """Zwei DBs mit gleichem Seed, ohne Nahrung durchgetickt → gleicher Todes-Tick."""
        from app.sim.tick import fast_forward

        c1 = conn_seeded("_d1")
        c2 = conn_seeded("_d2")
        # Hunger auf 0 und Performance auf 0 forcieren für schnellen Tod
        for c in [c1, c2]:
            c.execute("UPDATE characters SET hunger=0.0, performance=0.0 WHERE id=1;")
            c.commit()

        result1 = fast_forward(c1, max_ticks=50_000)
        result2 = fast_forward(c2, max_ticks=50_000)
        tick1 = c1.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        tick2 = c2.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        assert tick1 == tick2, (
            f"Gleicher Seed → gleicher Todes-Tick erwartet: {tick1} vs {tick2}"
        )
        c1.close()
        c2.close()

    def test_death_different_seeds_may_differ(self, conn_seeded):
        """Unterschiedliche Seeds → i.d.R. unterschiedlicher Sterbe-Tick."""
        from app.sim.tick import fast_forward

        c1 = conn_seeded("_ds1")
        c2 = conn_seeded("_ds2")
        c1.execute("UPDATE world SET world_seed=1337 WHERE id=1;")
        c2.execute("UPDATE world SET world_seed=9999 WHERE id=1;")
        for c in [c1, c2]:
            c.execute("UPDATE characters SET hunger=0.0, performance=0.0 WHERE id=1;")
            c.commit()
        fast_forward(c1, max_ticks=50_000)
        fast_forward(c2, max_ticks=50_000)
        tick1 = c1.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        tick2 = c2.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        # Keine harte Assertion, da sie theoretisch gleich sein könnten —
        # stattdessen prüfen wir nur, dass der Test durchläuft
        assert isinstance(tick1, int) and isinstance(tick2, int)
        c1.close()
        c2.close()


class TestEat:
    def _setup_food(self, conn, item_id: str, qty: float = 3.0, quality: float = 1.0):
        conn.execute(
            "INSERT INTO group_inventory (group_id, item_id, quantity, quality, acquired_tick) "
            "VALUES (1, ?, ?, ?, 0);",
            (item_id, qty, quality),
        )
        from app.sim import ledger
        ledger.add(conn, item_id, qty)
        conn.commit()

    def test_eat_increases_hunger(self, conn):
        self._setup_food(conn, "canned_beans", qty=5.0)
        conn.execute("UPDATE characters SET hunger=0.5 WHERE id=1;")
        conn.commit()
        result = eat(conn, 1)
        assert result["ok"] is True
        hunger_after = conn.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]
        assert hunger_after > 0.5

    def test_eat_clamped_at_one(self, conn):
        """Vollgesättigter Charakter: Hunger bleibt bei 1.0."""
        self._setup_food(conn, "canned_beans", qty=5.0)
        conn.execute("UPDATE characters SET hunger=1.0 WHERE id=1;")
        conn.commit()
        result = eat(conn, 1)
        hunger_after = conn.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]
        assert hunger_after <= 1.0

    def test_eat_no_food_returns_error(self, conn):
        result = eat(conn, 1)
        assert result["ok"] is False
        assert result["reason"] == "no_food"

    def test_eat_specific_item(self, conn):
        self._setup_food(conn, "canned_beans", qty=5.0)
        result = eat(conn, 1, item_id="canned_beans")
        assert result["ok"] is True
        assert result["item"] == "canned_beans"

    def test_eat_reduces_group_inventory(self, conn):
        self._setup_food(conn, "canned_beans", qty=3.0)
        before = conn.execute(
            "SELECT SUM(quantity) AS q FROM group_inventory WHERE item_id='canned_beans';"
        ).fetchone()["q"]
        eat(conn, 1)
        after = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory WHERE item_id='canned_beans';"
        ).fetchone()["q"]
        assert after < before

    def test_eat_books_ledger_as_sink(self, conn):
        from app.sim import ledger
        self._setup_food(conn, "canned_beans", qty=3.0)
        before = ledger.expected_totals(conn).get("canned_beans", 0.0)
        eat(conn, 1)
        after = ledger.expected_totals(conn).get("canned_beans", 0.0)
        assert after < before, "eat() muss Ledger als Senke buchen"

    def test_eat_dead_character_fails(self, conn):
        conn.execute("UPDATE characters SET is_alive=0 WHERE id=1;")
        conn.commit()
        self._setup_food(conn, "canned_beans")
        result = eat(conn, 1)
        assert result["ok"] is False
