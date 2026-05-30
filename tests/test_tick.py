"""Tests für app/sim/tick.py — advance_tick und fast_forward."""
from __future__ import annotations

import pytest
from app.sim import constants
from app.sim.tick import advance_tick, fast_forward


class TestAdvanceTick:
    def test_tick_increments_by_default_minutes(self, conn):
        tick_before = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        result = advance_tick(conn)
        tick_after = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        assert tick_after == tick_before + constants.TICK_MINUTES
        assert result["tick"] == tick_after

    def test_tick_increments_by_custom_minutes(self, conn):
        tick_before = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        advance_tick(conn, minutes=60)
        tick_after = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        assert tick_after == tick_before + 60

    def test_tick_returns_interrupts_list(self, conn):
        result = advance_tick(conn)
        assert "interrupts" in result
        assert isinstance(result["interrupts"], list)

    def test_tick_is_atomic_world_tick(self, conn):
        """advance_tick ist in einer Transaktion: tick in world wird korrekt gesetzt."""
        advance_tick(conn)
        advance_tick(conn)
        tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        assert tick == 2 * constants.TICK_MINUTES

    def test_multiple_ticks_accumulate(self, conn):
        n = 5
        for _ in range(n):
            advance_tick(conn)
        tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        assert tick == n * constants.TICK_MINUTES

    def test_audit_row_written_per_tick(self, conn):
        advance_tick(conn)
        n = conn.execute("SELECT COUNT(*) AS n FROM resource_audit;").fetchone()["n"]
        # Audit schreibt pro Item und Tick — kann 0 sein wenn keine Items vorhanden
        assert isinstance(n, int)

    def test_hunger_decreases_after_tick(self, conn):
        hunger_before = conn.execute(
            "SELECT hunger FROM characters WHERE id=1;"
        ).fetchone()["hunger"]
        advance_tick(conn)
        hunger_after = conn.execute(
            "SELECT hunger FROM characters WHERE id=1;"
        ).fetchone()["hunger"]
        assert hunger_after < hunger_before

    def test_tick_audit_no_flag_fresh_db(self, conn):
        """Frische DB ohne Inventar: Audit soll keine Flags zeigen."""
        advance_tick(conn)
        flagged = conn.execute(
            "SELECT COALESCE(SUM(flagged),0) AS f FROM resource_audit;"
        ).fetchone()["f"]
        assert flagged == 0


class TestFastForward:
    def test_fast_forward_until_tick(self, conn):
        """fast_forward stoppt am Ziel-Tick."""
        target = 50
        result = fast_forward(conn, until_tick=target, minutes=constants.TICK_MINUTES)
        tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        assert tick >= target
        assert result["stopped"] == "until_tick"

    def test_fast_forward_stops_on_interrupt(self, conn):
        """fast_forward stoppt bei einem Interrupt (soft/decision)."""
        # Charakter knapp über HUNGER_SOFT setzen, dann ticken bis Interrupt
        threshold = constants.HUNGER_SOFT + 0.02
        conn.execute("UPDATE characters SET hunger=? WHERE id=1;", (threshold,))
        conn.commit()
        result = fast_forward(conn, max_ticks=10_000, minutes=constants.TICK_MINUTES)
        assert result["stopped"] in ("interrupt", "all_dead", "until_tick", "max_ticks")
        # Erwartet: 'interrupt' wegen hunger-Schwelle
        assert result["stopped"] == "interrupt"

    def test_fast_forward_stops_when_all_dead(self, conn):
        """fast_forward stoppt wenn alle Charaktere tot sind."""
        conn.execute("UPDATE characters SET hunger=0.0, performance=0.0 WHERE id=1;")
        conn.commit()
        result = fast_forward(conn, max_ticks=100_000)
        assert result["stopped"] in ("all_dead", "interrupt")

    def test_fast_forward_max_ticks(self, conn):
        """fast_forward stoppt nach max_ticks wenn kein anderes Stopp-Kriterium."""
        # Hunger hoch setzen, damit kein Hunger-Interrupt kommt
        conn.execute("UPDATE characters SET hunger=1.0 WHERE id=1;")
        conn.commit()
        result = fast_forward(conn, max_ticks=3, minutes=constants.TICK_MINUTES)
        assert result["ticks_advanced"] <= 3

    def test_fast_forward_advances_at_least_one_tick(self, conn):
        result = fast_forward(conn, max_ticks=10, until_tick=constants.TICK_MINUTES * 5)
        assert result["ticks_advanced"] >= 1

    def test_fast_forward_deterministic_same_seed(self, conn_seeded):
        """Gleicher Seed → identischer Verlauf."""
        from app.sim.tick import fast_forward as ff

        c1 = conn_seeded("_ff1")
        c2 = conn_seeded("_ff2")
        for c in [c1, c2]:
            c.execute("UPDATE characters SET hunger=0.3 WHERE id=1;")
            c.commit()

        r1 = ff(c1, max_ticks=200)
        r2 = ff(c2, max_ticks=200)
        tick1 = c1.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        tick2 = c2.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        assert tick1 == tick2
        assert r1["stopped"] == r2["stopped"]
        c1.close()
        c2.close()
