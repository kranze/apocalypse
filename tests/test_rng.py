"""Tests für app/sim/rng.py — deterministischer Zufall."""
from __future__ import annotations

import pytest
from app.sim.rng import roll


class TestRollDeterminism:
    def test_same_inputs_same_output(self):
        r1 = roll(1337, "death", 1, 100)
        r2 = roll(1337, "death", 1, 100)
        assert r1 == r2

    def test_different_seed_different_output(self):
        r1 = roll(1337, "death", 1, 100)
        r2 = roll(9999, "death", 1, 100)
        assert r1 != r2

    def test_different_parts_different_output(self):
        r1 = roll(1337, "qty", "canned_beans")
        r2 = roll(1337, "qty", "water_1l")
        assert r1 != r2

    def test_in_range(self):
        for i in range(50):
            r = roll(1337, "test", i)
            assert 0.0 <= r < 1.0

    def test_zero_seed(self):
        r = roll(0, "x")
        assert 0.0 <= r < 1.0

    def test_order_of_parts_matters(self):
        r1 = roll(1337, "a", "b")
        r2 = roll(1337, "b", "a")
        assert r1 != r2

    def test_different_ticks_different_output(self):
        r1 = roll(1337, "death", 1, 10)
        r2 = roll(1337, "death", 1, 20)
        assert r1 != r2

    def test_no_parts(self):
        """roll ohne weitere parts sollte funktionieren."""
        r = roll(42)
        assert 0.0 <= r < 1.0
