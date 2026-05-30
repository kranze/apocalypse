"""Tests für Player-Override im Adjudikator (app/sim/adjudicator.py).

Prüft: prepare scheitert ohne Hitze (no_heat, escalate); override mit Begründung
die ein Inventar-Item nennt -> player_verified KB-Fakt + erneutes prepare gelingt;
override mit unklarer Begründung -> override_unclear.
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
from app.sim import adjudicator, kb, ledger


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
    c.execute("UPDATE characters SET lat=49.0, lon=11.0 WHERE id=1;")
    c.commit()
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


def _setup_pasta_no_heat(conn):
    """Pasta + Wasser, aber keine Hitzequelle (kein firewood, kein KB-Fakt)."""
    _add_item(conn, "pasta_500g", 2.0)
    _add_item(conn, "water_1l", 2.0)
    # Explizit sicherstellen: kein firewood
    conn.execute("DELETE FROM group_inventory WHERE item_id='firewood';")
    conn.commit()


# ---------------------------------------------------------------------------
# 1. prepare scheitert ohne Hitze
# ---------------------------------------------------------------------------

class TestPrepareNoHeat:
    def test_prepare_fails_no_heat(self, conn):
        _setup_pasta_no_heat(conn)
        v = adjudicator.adjudicate(conn, 1, "koche nudeln")
        assert v["ok"] is False

    def test_prepare_escalates_no_heat(self, conn):
        _setup_pasta_no_heat(conn)
        v = adjudicator.adjudicate(conn, 1, "koche nudeln")
        assert v["escalate"] is True

    def test_prepare_reason_no_heat(self, conn):
        _setup_pasta_no_heat(conn)
        v = adjudicator.adjudicate(conn, 1, "koche nudeln")
        assert v["reason"] == "no_heat"


# ---------------------------------------------------------------------------
# 2. override mit passendem Item -> KB-Fakt + prepare gelingt
# ---------------------------------------------------------------------------

class TestOverrideSuccess:
    def test_override_returns_ok(self, conn):
        _setup_pasta_no_heat(conn)
        _add_item(conn, "crowbar", 2.0)
        # Kein firewood, kein KB-Fakt für crowbar -> prepare scheitert zuerst
        adjudicator.adjudicate(conn, 1, "koche nudeln")
        # Override: Spieler erklärt, dass crowbar als Hitzequelle dient
        v = adjudicator.override(conn, 1, "koche nudeln", "ich benutze den crowbar")
        assert v["ok"] is True

    def test_override_adds_kb_fact(self, conn):
        _setup_pasta_no_heat(conn)
        _add_item(conn, "crowbar", 2.0)
        adjudicator.override(conn, 1, "koche nudeln", "ich benutze den crowbar")
        fact = kb.lookup(conn, "provides:heat", "crowbar")
        assert fact is not None
        assert fact["provenance"] == "player_verified"

    def test_override_learned_in_result(self, conn):
        _setup_pasta_no_heat(conn)
        _add_item(conn, "crowbar", 2.0)
        v = adjudicator.override(conn, 1, "koche nudeln", "crowbar als hitzequelle")
        assert "override_learned" in v
        assert v["override_learned"]["topic"] == "provides:heat"
        assert v["override_learned"]["key"] == "crowbar"

    def test_override_prepare_creates_meal(self, conn):
        _setup_pasta_no_heat(conn)
        _add_item(conn, "crowbar", 2.0)
        before = _inv(conn, "meal_pasta")
        adjudicator.override(conn, 1, "koche nudeln", "crowbar")
        after = _inv(conn, "meal_pasta")
        assert after == before + 1.0

    def test_override_consumes_provides_heat(self, conn):
        """Das als Hitzequelle deklarierte Item wird bei prepare verbraucht."""
        _setup_pasta_no_heat(conn)
        _add_item(conn, "crowbar", 3.0)
        before = _inv(conn, "crowbar")
        adjudicator.override(conn, 1, "koche nudeln", "crowbar")
        after = _inv(conn, "crowbar")
        assert after < before

    def test_override_with_item_id_in_reason(self, conn):
        """item_id direkt in der Begründung (z.B. 'crowbar')."""
        _setup_pasta_no_heat(conn)
        _add_item(conn, "crowbar", 2.0)
        v = adjudicator.override(conn, 1, "koche nudeln", "crowbar")
        assert v["ok"] is True


# ---------------------------------------------------------------------------
# 3. override mit unklarer Begründung
# ---------------------------------------------------------------------------

class TestOverrideUnclear:
    def test_override_unclear_not_ok(self, conn):
        _setup_pasta_no_heat(conn)
        # Begründung nennt kein bekanntes Item im Inventar
        v = adjudicator.override(conn, 1, "koche nudeln", "mit magie und luft")
        assert v["ok"] is False

    def test_override_unclear_reason(self, conn):
        _setup_pasta_no_heat(conn)
        v = adjudicator.override(conn, 1, "koche nudeln", "mit magie und luft")
        assert v["reason"] == "override_unclear"

    def test_override_unclear_escalates(self, conn):
        _setup_pasta_no_heat(conn)
        v = adjudicator.override(conn, 1, "koche nudeln", "mit magie und luft")
        assert v["escalate"] is True

    def test_override_unclear_no_kb_fact_added(self, conn):
        """Bei unklarer Begründung wird kein neuer KB-Fakt angelegt."""
        _setup_pasta_no_heat(conn)
        count_before = len(kb.list_topic(conn, "provides:heat"))
        adjudicator.override(conn, 1, "koche nudeln", "mit magie und luft")
        count_after = len(kb.list_topic(conn, "provides:heat"))
        assert count_before == count_after


# ---------------------------------------------------------------------------
# 4. curated nicht überschreibbar via override
# ---------------------------------------------------------------------------

class TestOverrideRespectsCurated:
    def test_override_cannot_downgrade_curated_firewood(self, conn):
        """override für firewood darf curated nicht mit player_verified ersetzen."""
        _setup_pasta_no_heat(conn)
        _add_item(conn, "firewood", 2.0)
        # Versuche, firewood mit anderem consume zu überschreiben
        # (Der Override-Mechanismus ruft kb.add auf; der Vorrang-Check soll player_verified
        # NICHT über curated setzen lassen — aber hier haben wir gleiche oder höhere Priorität:
        # player_verified < curated -> kein Überschreiben. Fakt bleibt curated.)
        adjudicator.override(conn, 1, "koche nudeln", "ich benutze das firewood")
        fact = kb.lookup(conn, "provides:heat", "firewood")
        # Der Fakt soll weiterhin curated sein (player_verified hat niedrigeren Rang)
        assert fact["provenance"] == "curated"
