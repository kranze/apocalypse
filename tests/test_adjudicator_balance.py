"""Tests für Bilanz-Drift-Freiheit nach adjudizierten Aktionen.

Iron-Principle-Beleg: discover -> loot -> prepare -> eat + tick.advance_tick
erzeugt KEINE resource_audit-Flags. Selbst LLM-getriebene Aktionen
können nichts aus dem Nichts erschaffen.
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

from tests.conftest import make_conn, set_seed, insert_location
from app.sim import adjudicator, audit, ledger, tick


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
    c.execute("UPDATE characters SET lat=49.0, lon=11.0, hunger=0.5 WHERE id=1;")
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


def _near_supermarket(conn):
    insert_location(
        conn,
        loc_id=400,
        loc_type="supermarket",
        name="Balance-Markt",
        lat=49.0002,
        lon=11.0,
        generation_seed=1337,
    )


def _count_flagged(conn):
    return conn.execute(
        "SELECT COALESCE(SUM(flagged),0) AS f FROM resource_audit;"
    ).fetchone()["f"]


# ---------------------------------------------------------------------------
# Vollständige Aktionsfolge discover -> loot -> prepare -> eat -> tick
# ---------------------------------------------------------------------------

class TestBalanceFullSequence:
    def test_no_drift_after_discover(self, conn):
        _near_supermarket(conn)
        adjudicator.adjudicate(conn, 1, "betrete den supermarkt")
        now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        with conn:
            audit.run_audit(conn, now)
        assert _count_flagged(conn) == 0

    def test_no_drift_after_loot(self, conn):
        _near_supermarket(conn)
        adjudicator.adjudicate(conn, 1, "betrete den supermarkt")
        adjudicator.adjudicate(conn, 1, "plündere den balance-markt")
        now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        with conn:
            audit.run_audit(conn, now)
        assert _count_flagged(conn) == 0

    def test_no_drift_after_prepare(self, conn):
        """prepare aus vorher korrekt via Ledger gebuchten Zutaten: drift-frei."""
        _add_item(conn, "pasta_500g", 2.0)
        _add_item(conn, "water_1l", 2.0)
        _add_item(conn, "firewood", 3.0)
        adjudicator.adjudicate(conn, 1, "koche nudeln")
        now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        with conn:
            audit.run_audit(conn, now)
        assert _count_flagged(conn) == 0

    def test_no_drift_after_eat(self, conn):
        _add_item(conn, "pasta_500g", 2.0)
        _add_item(conn, "water_1l", 2.0)
        _add_item(conn, "firewood", 3.0)
        adjudicator.adjudicate(conn, 1, "koche nudeln")
        conn.execute("UPDATE characters SET hunger=0.1 WHERE id=1;")
        conn.commit()
        adjudicator.adjudicate(conn, 1, "iss etwas")
        now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        with conn:
            audit.run_audit(conn, now)
        assert _count_flagged(conn) == 0

    def test_no_drift_after_full_sequence_and_tick(self, conn):
        """Vollständige Folge: prepare -> eat -> tick ohne gemischte Loot+add-Items.

        Nur Sim-Kern-Funktionen via Adjudikator. Keine gemischten _add_item-Aufrufe
        nach Loot, um Ledger-Konflikte zu vermeiden.
        Iron-Principle-Beleg: 0 flagged rows nach allen Aktionen.
        """
        # Alle Zutaten korrekt per Ledger einbuchen
        _add_item(conn, "pasta_500g", 2.0)
        _add_item(conn, "water_1l", 2.0)
        _add_item(conn, "firewood", 3.0)
        _add_item(conn, "canned_beans", 2.0)
        # prepare
        adjudicator.adjudicate(conn, 1, "koche nudeln")
        # eat (zuerst prepared meal, dann canned_beans)
        conn.execute("UPDATE characters SET hunger=0.1 WHERE id=1;")
        conn.commit()
        adjudicator.adjudicate(conn, 1, "iss etwas")
        # tick
        tick.advance_tick(conn)
        assert _count_flagged(conn) == 0

    def test_no_drift_after_discover_loot_sequence(self, conn):
        """Discover + loot via Adjudikator: Bilanz drift-frei (nur Sim-Kern schreibt)."""
        _near_supermarket(conn)
        adjudicator.adjudicate(conn, 1, "betrete den supermarkt")
        adjudicator.adjudicate(conn, 1, "plündere den balance-markt")
        tick.advance_tick(conn)
        assert _count_flagged(conn) == 0

    def test_no_drift_after_wait(self, conn):
        """wait = advance_tick; Bilanz muss danach sauber sein."""
        adjudicator.adjudicate(conn, 1, "warte")
        assert _count_flagged(conn) == 0

    def test_no_drift_after_look(self, conn):
        """look ändert nichts; Audit direkt danach: keine Flags."""
        adjudicator.adjudicate(conn, 1, "schau dich um")
        now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        with conn:
            audit.run_audit(conn, now)
        assert _count_flagged(conn) == 0

    def test_no_drift_after_multiple_ticks(self, conn):
        """Mehrere Ticks nach Aktionen: keine Bilanz-Drift."""
        _add_item(conn, "canned_beans", 3.0)
        adjudicator.adjudicate(conn, 1, "iss etwas")
        tick.advance_tick(conn)
        tick.advance_tick(conn)
        assert _count_flagged(conn) == 0
