"""Tests für app/sim/requirements.py — satisfy / providers.

Prüft:
- satisfy findet provides:heat (firewood, curated) wenn Item besessen
- satisfy findet provides:firewood nicht (kein solches Topic im Schema)
- satisfy gibt None zurück wenn Item nicht besessen
- satisfy gibt None zurück wenn Menge unter consume-Wert
- player_verified-Fakt für ein besessenes Item wird gefunden
- consume-Wert aus KB-Fakt korrekt
- providers listet bekannte Lieferanten
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
from app.sim import kb, ledger, requirements


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


# ---------------------------------------------------------------------------
# provides:heat — curated firewood
# ---------------------------------------------------------------------------

class TestSatisfyHeatFirewood:
    def test_satisfy_heat_with_firewood_owned(self, conn):
        """firewood (curated) + besessen -> satisfy returns nicht-None."""
        _add_item(conn, "firewood", 2.0)
        result = requirements.satisfy(conn, 1, "heat")
        assert result is not None

    def test_satisfy_heat_firewood_item_key(self, conn):
        _add_item(conn, "firewood", 2.0)
        result = requirements.satisfy(conn, 1, "heat")
        assert result["item"] == "firewood"

    def test_satisfy_heat_firewood_consume_value(self, conn):
        """KB-Seed hat consume=1 für firewood."""
        _add_item(conn, "firewood", 2.0)
        result = requirements.satisfy(conn, 1, "heat")
        assert abs(result["consume"] - 1.0) < 1e-9

    def test_satisfy_heat_none_when_not_owned(self, conn):
        """Kein firewood im Inventar -> None."""
        result = requirements.satisfy(conn, 1, "heat")
        assert result is None

    def test_satisfy_heat_none_when_insufficient(self, conn):
        """firewood vorhanden aber Menge < consume(1) -> None."""
        _add_item(conn, "firewood", 0.5)
        result = requirements.satisfy(conn, 1, "heat")
        assert result is None


# ---------------------------------------------------------------------------
# provides:power — curated generator
# ---------------------------------------------------------------------------

class TestSatisfyPower:
    def test_satisfy_power_with_generator(self, conn):
        _add_item(conn, "generator", 1.0)
        result = requirements.satisfy(conn, 1, "power")
        assert result is not None
        assert result["item"] == "generator"

    def test_satisfy_power_consume_zero(self, conn):
        """Generator verbraucht sich nicht (consume=0)."""
        _add_item(conn, "generator", 1.0)
        result = requirements.satisfy(conn, 1, "power")
        assert result["consume"] == 0.0

    def test_satisfy_power_none_without_generator(self, conn):
        result = requirements.satisfy(conn, 1, "power")
        assert result is None


# ---------------------------------------------------------------------------
# provides:transmitter — curated wifi_router
# ---------------------------------------------------------------------------

class TestSatisfyTransmitter:
    def test_satisfy_transmitter_with_router(self, conn):
        _add_item(conn, "wifi_router", 1.0)
        result = requirements.satisfy(conn, 1, "transmitter")
        assert result is not None
        assert result["item"] == "wifi_router"

    def test_satisfy_transmitter_none_without_router(self, conn):
        result = requirements.satisfy(conn, 1, "transmitter")
        assert result is None


# ---------------------------------------------------------------------------
# player_verified KB-Fakt
# ---------------------------------------------------------------------------

class TestSatisfyPlayerVerified:
    def test_player_verified_fact_found_when_owned(self, conn):
        """player_verified-Fakt für ein besessenes Item wird gefunden."""
        with conn:
            kb.add(conn, "provides:heat", "crowbar", {"consume": 1}, "player_verified", 0)
        _add_item(conn, "crowbar", 2.0)
        result = requirements.satisfy(conn, 1, "heat")
        assert result is not None
        assert result["item"] in ("firewood", "crowbar")  # firewood nicht besessen -> crowbar

    def test_player_verified_consume_value_correct(self, conn):
        """consume-Wert aus dem player_verified-Fakt wird korrekt zurückgegeben."""
        with conn:
            kb.add(conn, "provides:heat", "crowbar", {"consume": 3}, "player_verified", 0)
        _add_item(conn, "crowbar", 5.0)
        result = requirements.satisfy(conn, 1, "heat")
        assert result is not None
        assert result["item"] == "crowbar"
        assert abs(result["consume"] - 3.0) < 1e-9

    def test_player_verified_not_found_when_not_owned(self, conn):
        """player_verified-Fakt für ein Item das nicht besessen wird -> None (andere auch nicht)."""
        with conn:
            kb.add(conn, "provides:heat", "crowbar", {"consume": 1}, "player_verified", 0)
        # kein firewood, kein crowbar im Inventar
        result = requirements.satisfy(conn, 1, "heat")
        assert result is None


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------

class TestProviders:
    def test_providers_heat_includes_firewood(self, conn):
        result = requirements.providers(conn, "heat")
        assert "firewood" in result

    def test_providers_power_includes_generator(self, conn):
        result = requirements.providers(conn, "power")
        assert "generator" in result

    def test_providers_transmitter_includes_router(self, conn):
        result = requirements.providers(conn, "transmitter")
        assert "wifi_router" in result

    def test_providers_unknown_req_empty(self, conn):
        result = requirements.providers(conn, "unicorn")
        assert result == []
