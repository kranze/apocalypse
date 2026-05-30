"""Tests für can_provide_heat via KB (app/sim/heat.py).

Prüft: ohne Feuerholz aber mit KB-Fakt (player_verified) und vorhandenem Item
liefert can_provide_heat das Item; prepare gelingt und verbraucht es.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
llm_mod.reset_backend()

from tests.conftest import make_conn, set_seed
from app.sim import heat, kb, ledger
from app.sim.resources import prepare


@pytest.fixture(autouse=True)
def force_stub():
    os.environ["WASTELAND_LLM_BACKEND"] = "stub"
    llm_mod.reset_backend()
    yield
    llm_mod.reset_backend()


@pytest.fixture
def conn():
    c = make_conn()
    set_seed(c, 1337)
    yield c
    c.close()


def _add_item(conn, item_id: str, qty: float, group_id: int = 1):
    conn.execute(
        "INSERT OR REPLACE INTO group_inventory "
        "(group_id, item_id, quantity, quality, acquired_tick) VALUES (?,?,?,1.0,0);",
        (group_id, item_id, qty),
    )
    ledger.add(conn, item_id, qty)
    conn.commit()


def _inv(conn, item_id: str, group_id: int = 1) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory "
        "WHERE group_id=? AND item_id=?;",
        (group_id, item_id),
    ).fetchone()["q"]


# ---------------------------------------------------------------------------
# can_provide_heat ohne KB-Fakt
# ---------------------------------------------------------------------------

class TestHeatWithoutKBFact:
    def test_no_heat_without_firewood_and_no_kb(self, conn):
        """Ohne KB-Fakt und ohne Feuerholz: None."""
        # firewood ist im Schema-Seed als curated vorhanden, aber Gruppe hat keins
        result = heat.can_provide_heat(conn, 1)
        assert result is None

    def test_no_heat_with_unregistered_item(self, conn):
        """crowbar ist kein KB-Hitzequellen-Eintrag -> keine Hitze."""
        _add_item(conn, "crowbar", 2.0)
        result = heat.can_provide_heat(conn, 1)
        assert result is None


# ---------------------------------------------------------------------------
# can_provide_heat mit KB-Fakt (player_verified)
# ---------------------------------------------------------------------------

class TestHeatWithKBFact:
    def test_can_provide_heat_with_custom_fuel(self, conn):
        """crowbar als player_verified provides:heat + Gruppe besitzt crowbar -> Treffer."""
        # KB-Fakt anlegen
        with conn:
            kb.add(conn, "provides:heat", "crowbar", {"consume": 1}, "player_verified", 0)
        _add_item(conn, "crowbar", 2.0)
        result = heat.can_provide_heat(conn, 1)
        assert result is not None
        assert result[0] == "crowbar"

    def test_can_provide_heat_returns_correct_amount(self, conn):
        """Die consume-Menge aus dem KB-Fakt wird korrekt zurückgegeben."""
        with conn:
            kb.add(conn, "provides:heat", "crowbar", {"consume": 2}, "player_verified", 0)
        _add_item(conn, "crowbar", 5.0)
        result = heat.can_provide_heat(conn, 1)
        assert result is not None
        assert abs(result[1] - 2.0) < 1e-9

    def test_cannot_provide_heat_insufficient_amount(self, conn):
        """Item vorhanden, aber Menge < consume -> None."""
        with conn:
            kb.add(conn, "provides:heat", "crowbar", {"consume": 3}, "player_verified", 0)
        _add_item(conn, "crowbar", 1.0)  # nur 1, braucht 3
        result = heat.can_provide_heat(conn, 1)
        assert result is None

    def test_firewood_as_curated_works_when_owned(self, conn):
        """Feuerholz ist curated; Gruppe besitzt es -> liefert Hitze."""
        _add_item(conn, "firewood", 2.0)
        result = heat.can_provide_heat(conn, 1)
        assert result is not None
        assert result[0] == "firewood"


# ---------------------------------------------------------------------------
# prepare mit KB-basierter Hitzequelle
# ---------------------------------------------------------------------------

class TestPrepareWithKBHeat:
    def _setup_pasta(self, conn):
        _add_item(conn, "pasta_500g", 2.0)
        _add_item(conn, "water_1l", 2.0)

    def test_prepare_fails_without_any_heat(self, conn):
        """Ohne Hitzequelle: no_heat."""
        self._setup_pasta(conn)
        result = prepare(conn, 1)
        assert result["ok"] is False
        assert result["reason"] == "no_heat"

    def test_prepare_succeeds_with_kb_provides_heat(self, conn):
        """Mit player_verified KB-Fakt (crowbar) als Hitzequelle: prepare gelingt."""
        self._setup_pasta(conn)
        with conn:
            kb.add(conn, "provides:heat", "crowbar", {"consume": 1}, "player_verified", 0)
        _add_item(conn, "crowbar", 2.0)
        result = prepare(conn, 1)
        assert result["ok"] is True
        assert result["prepared"] == "meal_pasta"

    def test_prepare_consumes_kb_provides_heat(self, conn):
        """Der KB-Hitzequellen-Item wird verbraucht."""
        self._setup_pasta(conn)
        with conn:
            kb.add(conn, "provides:heat", "crowbar", {"consume": 1}, "player_verified", 0)
        _add_item(conn, "crowbar", 3.0)
        before = _inv(conn, "crowbar")
        prepare(conn, 1)
        after = _inv(conn, "crowbar")
        assert abs(after - (before - 1.0)) < 1e-9

    def test_prepare_with_kb_heat_uses_correct_fuel_field(self, conn):
        """result['fuel'] enthält den KB-Item-ID."""
        self._setup_pasta(conn)
        with conn:
            kb.add(conn, "provides:heat", "crowbar", {"consume": 1}, "player_verified", 0)
        _add_item(conn, "crowbar", 2.0)
        result = prepare(conn, 1)
        assert result["fuel"] == "crowbar"
