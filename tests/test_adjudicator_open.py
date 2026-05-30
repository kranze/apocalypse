"""Tests für den Adjudikator mit offenem Intentions-Stub (kein Netz, kein Key).

Prüft:
- „schau dich um" -> ok, narrate, kein State-Change
- „betritt den supermarkt" -> discover (Location nötig)
- „durchsuche …" -> transfer (Location nötig)
- „ich sende über den funkmast ein ssid signal" mit Generator+Router im Inventar
  -> ok, Capability ssid_beacon aktiv
- „ich nehme das mobilfunknetz wieder in betrieb" -> ok=False, feasibility too_complex,
  escalate, KEIN State-Change
- establish ohne Strom/Sender -> ok=False, reason missing:power oder missing:transmitter
- erfundenes Ziel -> ok=False, reason no_target
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
from app.sim import adjudicator, capabilities, ledger


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


def _near_supermarket(conn, loc_id=300):
    insert_location(
        conn, loc_id=loc_id, loc_type="supermarket",
        name="Testmarkt", lat=49.001, lon=11.0,
        generation_seed=42,
    )


# ---------------------------------------------------------------------------
# 1. „schau dich um" -> ok, narrate, kein State-Change
# ---------------------------------------------------------------------------

class TestLookAround:
    def test_look_returns_ok(self, conn):
        v = adjudicator.adjudicate(conn, 1, "schau dich um")
        assert v["ok"] is True

    def test_look_no_effects_applied(self, conn):
        v = adjudicator.adjudicate(conn, 1, "schau dich um")
        # narrate-Effekt hat keinen State-Change-Applier der etwas ändert
        # Überprüfen: keine DB-Änderung an Inventar
        total_inv = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory;"
        ).fetchone()["q"]
        assert total_inv == 0.0  # Inventar unberührt

    def test_look_has_narration(self, conn):
        v = adjudicator.adjudicate(conn, 1, "schau dich um")
        assert v["narration"]

    def test_look_no_escalate(self, conn):
        v = adjudicator.adjudicate(conn, 1, "schau dich um")
        assert v["escalate"] is False

    def test_status_also_narrates(self, conn):
        """'status' wird ebenfalls als look erkannt."""
        v = adjudicator.adjudicate(conn, 1, "status")
        assert v["ok"] is True


# ---------------------------------------------------------------------------
# 2. „betritt den supermarkt" -> discover
# ---------------------------------------------------------------------------

class TestDiscover:
    def test_discover_ok_with_near_location(self, conn):
        _near_supermarket(conn)
        v = adjudicator.adjudicate(conn, 1, "betrite den supermarkt")
        assert v["ok"] is True

    def test_discover_marks_location_discovered(self, conn):
        _near_supermarket(conn)
        adjudicator.adjudicate(conn, 1, "erkunde den supermarkt")
        status = conn.execute(
            "SELECT discovery_status FROM locations WHERE id=300;"
        ).fetchone()["discovery_status"]
        assert status == "discovered"

    def test_discover_no_target_fails(self, conn):
        """Kein Supermarkt in der Nähe -> ok=False, reason no_target."""
        v = adjudicator.adjudicate(conn, 1, "erkunde den supermarkt")
        assert v["ok"] is False
        assert v["reason"] == "no_target"


# ---------------------------------------------------------------------------
# 3. „durchsuche/plündere" -> transfer
# ---------------------------------------------------------------------------

class TestTransfer:
    def test_transfer_ok_with_discovered_location(self, conn):
        _near_supermarket(conn)
        adjudicator.adjudicate(conn, 1, "erkunde den supermarkt")
        v = adjudicator.adjudicate(conn, 1, "durchsuche den supermarkt")
        assert v["ok"] is True

    def test_transfer_no_target_fails(self, conn):
        v = adjudicator.adjudicate(conn, 1, "plündere den supermarkt")
        assert v["ok"] is False
        assert v["reason"] == "no_target"


# ---------------------------------------------------------------------------
# 4. establish_capability ssid_beacon mit Generator+Router
# ---------------------------------------------------------------------------

class TestEstablishSsidBeacon:
    def _setup_equipment(self, conn):
        _add_item(conn, "generator", 1.0)
        _add_item(conn, "wifi_router", 1.0)
        _add_item(conn, "gasoline", 10.0)

    def test_establish_ok(self, conn):
        self._setup_equipment(conn)
        v = adjudicator.adjudicate(
            conn, 1, "ich sende über den funkmast ein ssid signal"
        )
        assert v["ok"] is True

    def test_establish_capability_active(self, conn):
        self._setup_equipment(conn)
        adjudicator.adjudicate(
            conn, 1, "ich sende über den funkmast ein ssid signal"
        )
        caps = capabilities.list_active(conn, owner_group=1)
        assert any(c["ctype"] == "ssid_beacon" for c in caps)

    def test_establish_has_effects_applied(self, conn):
        self._setup_equipment(conn)
        v = adjudicator.adjudicate(
            conn, 1, "ich sende über den funkmast ein ssid signal"
        )
        assert len(v["effects_applied"]) > 0

    def test_establish_beacon_in_db(self, conn):
        self._setup_equipment(conn)
        adjudicator.adjudicate(
            conn, 1, "ich sende über den funkmast ein ssid signal"
        )
        cap = conn.execute(
            "SELECT * FROM capabilities WHERE ctype='ssid_beacon' AND active=1;"
        ).fetchone()
        assert cap is not None


# ---------------------------------------------------------------------------
# 5. „mobilfunknetz reaktivieren" -> too_complex
# ---------------------------------------------------------------------------

class TestTooComplex:
    def test_mobilfunk_too_complex_not_ok(self, conn):
        v = adjudicator.adjudicate(
            conn, 1, "ich nehme das mobilfunknetz wieder in betrieb"
        )
        assert v["ok"] is False

    def test_mobilfunk_feasibility_too_complex(self, conn):
        v = adjudicator.adjudicate(
            conn, 1, "ich nehme das mobilfunknetz wieder in betrieb"
        )
        assert v["feasibility"] == "too_complex"

    def test_mobilfunk_escalates(self, conn):
        v = adjudicator.adjudicate(
            conn, 1, "ich nehme das mobilfunknetz wieder in betrieb"
        )
        assert v["escalate"] is True

    def test_mobilfunk_no_state_change(self, conn):
        """too_complex darf KEINEN State-Change bewirken."""
        before_inv = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory;"
        ).fetchone()["q"]
        before_caps = conn.execute(
            "SELECT COUNT(*) AS n FROM capabilities;"
        ).fetchone()["n"]
        adjudicator.adjudicate(
            conn, 1, "ich nehme das mobilfunknetz wieder in betrieb"
        )
        after_inv = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory;"
        ).fetchone()["q"]
        after_caps = conn.execute(
            "SELECT COUNT(*) AS n FROM capabilities;"
        ).fetchone()["n"]
        assert before_inv == after_inv
        assert before_caps == after_caps

    def test_netz_reaktiv_too_complex(self, conn):
        """Variante 'netz reaktiv' im Text."""
        v = adjudicator.adjudicate(conn, 1, "netz reaktivieren")
        assert v["ok"] is False
        assert v["feasibility"] == "too_complex"


# ---------------------------------------------------------------------------
# 6. establish ohne Strom/Sender -> missing:power oder missing:transmitter
# ---------------------------------------------------------------------------

class TestEstablishMissingRequirements:
    def test_establish_no_power_no_transmitter(self, conn):
        """Weder Generator noch Router -> validate schlägt mit missing:* an."""
        v = adjudicator.adjudicate(
            conn, 1, "ich sende über den funkmast ein ssid signal"
        )
        assert v["ok"] is False
        assert v["reason"] in ("missing:power", "missing:transmitter")
        assert v["escalate"] is True

    def test_establish_power_but_no_transmitter(self, conn):
        """Generator vorhanden, aber kein Router."""
        _add_item(conn, "generator", 1.0)
        v = adjudicator.adjudicate(
            conn, 1, "ich sende über den funkmast ein ssid signal"
        )
        assert v["ok"] is False
        assert "missing:" in (v["reason"] or "")

    def test_establish_transmitter_but_no_power(self, conn):
        """Router vorhanden, aber kein Generator."""
        _add_item(conn, "wifi_router", 1.0)
        v = adjudicator.adjudicate(
            conn, 1, "ich sende über den funkmast ein ssid signal"
        )
        assert v["ok"] is False
        assert "missing:" in (v["reason"] or "")


# ---------------------------------------------------------------------------
# 7. erfundenes Ziel -> no_target
# ---------------------------------------------------------------------------

class TestNoTarget:
    def test_discover_invented_target(self, conn):
        v = adjudicator.adjudicate(conn, 1, "erkunde die halle42xyz")
        assert v["ok"] is False

    def test_transfer_invented_target(self, conn):
        v = adjudicator.adjudicate(conn, 1, "plündere den geheimen bunker99")
        assert v["ok"] is False

    def test_no_target_has_reason(self, conn):
        v = adjudicator.adjudicate(conn, 1, "erkunde die halle42xyz")
        assert v["reason"] == "no_target"
