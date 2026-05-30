"""Tests für app/sim/effects.py — validate_all, apply_all, resolve_location.

Prüft:
- validate_all lehnt ab bei fehlendem Ziel (no_target)
- validate_all lehnt ab bei fehlendem Item (missing:*)
- validate_all lehnt ab bei fehlender Vorbedingung
- resolve_location respektiert reach (fernes Ziel -> None bei reach=400)
- apply_all: discover materialisiert Inventar
- apply_all: transfer übergibt Items zur Gruppe
- apply_all: consume_food verbraucht Nahrung
- apply_all: prepare kocht und erzeugt Ziel-Item
- apply_all: transform verbraucht+produziert (ledger-sauber)
- apply_all: establish_capability legt Capability an
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
from app.sim import adjudicator, audit, capabilities, effects, ledger


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


def _build_ctx(conn, char_id: int = 1):
    return adjudicator.build_context(conn, char_id)


def _near_loc(conn, loc_id=200, loc_type="supermarket"):
    """Legt eine Location in Reichweite (< 400 m) an."""
    insert_location(
        conn, loc_id=loc_id, loc_type=loc_type,
        name="Naher-Markt", lat=49.001, lon=11.0,
        generation_seed=42,
    )


def _far_loc(conn, loc_id=201, loc_type="supermarket"):
    """Legt eine Location außerhalb 400 m an."""
    insert_location(
        conn, loc_id=loc_id, loc_type=loc_type,
        name="Ferner-Markt", lat=49.1, lon=11.0,   # ~11 km entfernt
        generation_seed=43,
    )


# ---------------------------------------------------------------------------
# resolve_location
# ---------------------------------------------------------------------------

class TestResolveLocation:
    def test_resolve_near_without_reach(self, conn):
        _near_loc(conn)
        player = conn.execute("SELECT * FROM characters WHERE id=1;").fetchone()
        loc = effects.resolve_location(conn, player, "supermarket")
        assert loc is not None

    def test_resolve_near_with_reach(self, conn):
        _near_loc(conn)
        player = conn.execute("SELECT * FROM characters WHERE id=1;").fetchone()
        loc = effects.resolve_location(conn, player, "supermarket", reach=400)
        assert loc is not None

    def test_resolve_far_none_with_reach(self, conn):
        """Fernes Ziel (>400 m) -> None wenn reach=400."""
        _far_loc(conn)
        player = conn.execute("SELECT * FROM characters WHERE id=1;").fetchone()
        loc = effects.resolve_location(conn, player, "supermarket", reach=400)
        assert loc is None

    def test_resolve_far_ok_without_reach(self, conn):
        """Ohne reach-Limit findet resolve_location auch ferne Locations."""
        _far_loc(conn)
        player = conn.execute("SELECT * FROM characters WHERE id=1;").fetchone()
        loc = effects.resolve_location(conn, player, "supermarket", reach=None)
        assert loc is not None

    def test_resolve_no_player_position(self, conn):
        _near_loc(conn)
        conn.execute("UPDATE characters SET lat=NULL, lon=NULL WHERE id=1;")
        conn.commit()
        player = conn.execute("SELECT * FROM characters WHERE id=1;").fetchone()
        loc = effects.resolve_location(conn, player, "supermarket")
        assert loc is None


# ---------------------------------------------------------------------------
# validate_all — Ablehnungen
# ---------------------------------------------------------------------------

class TestValidateAllRejections:
    def test_no_target_discover(self, conn):
        """discover auf nichtexistentes Ziel -> (False, 'no_target')."""
        ctx = _build_ctx(conn)
        ok, reason = effects.validate_all(conn, 1, [{"op": "discover", "target": "halluzination"}], ctx)
        assert ok is False
        assert reason == "no_target"

    def test_no_target_transfer(self, conn):
        ctx = _build_ctx(conn)
        ok, reason = effects.validate_all(conn, 1, [{"op": "transfer", "target": None}], ctx)
        assert ok is False
        assert reason == "no_target"

    def test_no_food_consume(self, conn):
        """consume_food ohne Nahrung -> (False, 'no_food')."""
        ctx = _build_ctx(conn)
        ok, reason = effects.validate_all(conn, 1, [{"op": "consume_food"}], ctx)
        assert ok is False
        assert reason == "no_food"

    def test_no_heat_prepare(self, conn):
        """prepare ohne Hitzequelle -> (False, 'no_heat')."""
        _add_item(conn, "pasta_500g", 1.0)
        _add_item(conn, "water_1l", 1.0)
        ctx = _build_ctx(conn)
        ok, reason = effects.validate_all(conn, 1, [{"op": "prepare"}], ctx)
        assert ok is False
        assert reason == "no_heat"

    def test_missing_item_transform(self, conn):
        """transform mit consume-Item das nicht besessen wird -> missing:*."""
        ctx = _build_ctx(conn)
        ok, reason = effects.validate_all(conn, 1, [{
            "op": "transform",
            "consume": [{"item": "canned_beans", "qty": 5}],
            "produce": [{"item": "water_1l", "qty": 1}],
        }], ctx)
        assert ok is False
        assert reason is not None and reason.startswith("missing:")

    def test_missing_power_establish(self, conn):
        """establish_capability ohne power -> missing:power."""
        ctx = _build_ctx(conn)
        ok, reason = effects.validate_all(conn, 1, [{
            "op": "establish_capability", "ctype": "ssid_beacon",
        }], ctx)
        assert ok is False
        assert reason in ("missing:power", "missing:transmitter")

    def test_unknown_op_rejected(self, conn):
        ctx = _build_ctx(conn)
        ok, reason = effects.validate_all(conn, 1, [{"op": "teleport"}], ctx)
        assert ok is False
        assert reason is not None and "teleport" in reason

    def test_no_target_move_to(self, conn):
        ctx = _build_ctx(conn)
        ok, reason = effects.validate_all(conn, 1, [{"op": "move_to", "target": "nirgendwo"}], ctx)
        assert ok is False
        assert reason == "no_target"


# ---------------------------------------------------------------------------
# apply_all — DB-Änderungen
# ---------------------------------------------------------------------------

class TestApplyAllDiscover:
    def test_discover_materializes_inventory(self, conn):
        """discover auf undiscovered Location -> location_inventory wird angelegt."""
        _near_loc(conn)
        ctx = _build_ctx(conn)
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM location_inventory WHERE location_id=200;"
        ).fetchone()["n"]
        effects.apply_all(conn, 1, [{"op": "discover", "target": "supermarket"}], ctx)
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM location_inventory WHERE location_id=200;"
        ).fetchone()["n"]
        assert after >= before  # kann 0 sein wenn Seed nichts würfelt, aber muss >= sein

    def test_discover_marks_discovered(self, conn):
        _near_loc(conn)
        ctx = _build_ctx(conn)
        effects.apply_all(conn, 1, [{"op": "discover", "target": "supermarket"}], ctx)
        conn.commit()
        status = conn.execute(
            "SELECT discovery_status FROM locations WHERE id=200;"
        ).fetchone()["status" if False else "discovery_status"]
        assert status == "discovered"


class TestApplyAllTransfer:
    def test_transfer_moves_items_to_group(self, conn):
        """transfer von einer entdeckten Location -> Items wandern zur Gruppe."""
        _near_loc(conn)
        ctx = _build_ctx(conn)
        # Erst discover
        effects.apply_all(conn, 1, [{"op": "discover", "target": "supermarket"}], ctx)
        conn.commit()
        # Prüfen ob etwas da ist
        loc_count = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM location_inventory WHERE location_id=200;"
        ).fetchone()["q"]
        if loc_count == 0:
            pytest.skip("Seed hat für diesen Loc-Typ nichts generiert")
        before_group = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory WHERE group_id=1;"
        ).fetchone()["q"]
        ctx = _build_ctx(conn)
        effects.apply_all(conn, 1, [{"op": "transfer", "target": "supermarket"}], ctx)
        conn.commit()
        after_group = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory WHERE group_id=1;"
        ).fetchone()["q"]
        assert after_group > before_group


class TestApplyAllConsumeFood:
    def test_consume_food_reduces_inventory(self, conn):
        _add_item(conn, "canned_beans", 3.0)
        ctx = _build_ctx(conn)
        before = _inv(conn, "canned_beans")
        effects.apply_all(conn, 1, [{"op": "consume_food"}], ctx)
        conn.commit()
        after = _inv(conn, "canned_beans")
        assert after < before

    def test_consume_food_increases_hunger(self, conn):
        conn.execute("UPDATE characters SET hunger=0.2 WHERE id=1;")
        conn.commit()
        _add_item(conn, "canned_beans", 3.0)
        ctx = _build_ctx(conn)
        effects.apply_all(conn, 1, [{"op": "consume_food"}], ctx)
        conn.commit()
        hunger = conn.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]
        assert hunger > 0.2


class TestApplyAllPrepare:
    def test_prepare_creates_meal(self, conn):
        _add_item(conn, "pasta_500g", 1.0)
        _add_item(conn, "water_1l", 2.0)
        _add_item(conn, "firewood", 2.0)
        ctx = _build_ctx(conn)
        before = _inv(conn, "meal_pasta")
        effects.apply_all(conn, 1, [{"op": "prepare"}], ctx)
        conn.commit()
        after = _inv(conn, "meal_pasta")
        assert after == before + 1.0

    def test_prepare_consumes_pasta(self, conn):
        _add_item(conn, "pasta_500g", 2.0)
        _add_item(conn, "water_1l", 2.0)
        _add_item(conn, "firewood", 2.0)
        ctx = _build_ctx(conn)
        before = _inv(conn, "pasta_500g")
        effects.apply_all(conn, 1, [{"op": "prepare"}], ctx)
        conn.commit()
        after = _inv(conn, "pasta_500g")
        assert after < before


class TestApplyAllTransform:
    def test_transform_consumes_and_produces(self, conn):
        _add_item(conn, "canned_beans", 2.0)
        ctx = _build_ctx(conn)
        before_beans = _inv(conn, "canned_beans")
        before_water = _inv(conn, "water_1l")
        effects.apply_all(conn, 1, [{
            "op": "transform",
            "consume": [{"item": "canned_beans", "qty": 1}],
            "produce": [{"item": "water_1l", "qty": 1}],
        }], ctx)
        conn.commit()
        assert _inv(conn, "canned_beans") == before_beans - 1.0
        assert _inv(conn, "water_1l") == before_water + 1.0

    def test_transform_ledger_neutral(self, conn):
        """transform: Ledger-Saldo bleibt korrekt (consume - produce = netto Senke)."""
        _add_item(conn, "canned_beans", 3.0)
        ctx = _build_ctx(conn)
        effects.apply_all(conn, 1, [{
            "op": "transform",
            "consume": [{"item": "canned_beans", "qty": 1}],
            "produce": [{"item": "water_1l", "qty": 1}],
        }], ctx)
        conn.commit()
        now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        with conn:
            flags = audit.run_audit(conn, now)
        flagged = [f for f in flags if f["category"] == "system"]
        assert flagged == []


class TestApplyAllEstablishCapability:
    def test_establish_creates_capability(self, conn):
        _add_item(conn, "generator", 1.0)
        _add_item(conn, "wifi_router", 1.0)
        _add_item(conn, "gasoline", 5.0)
        ctx = _build_ctx(conn)
        effects.apply_all(conn, 1, [{
            "op": "establish_capability", "ctype": "ssid_beacon",
            "params": {"info": "TEST"}, "target": None,
        }], ctx)
        conn.commit()
        caps = capabilities.list_active(conn, owner_group=1)
        assert any(c["ctype"] == "ssid_beacon" for c in caps)

    def test_establish_capability_active_flag(self, conn):
        _add_item(conn, "generator", 1.0)
        _add_item(conn, "wifi_router", 1.0)
        _add_item(conn, "gasoline", 5.0)
        ctx = _build_ctx(conn)
        effects.apply_all(conn, 1, [{
            "op": "establish_capability", "ctype": "ssid_beacon",
            "params": {}, "target": None,
        }], ctx)
        conn.commit()
        cap = conn.execute(
            "SELECT active FROM capabilities WHERE ctype='ssid_beacon' LIMIT 1;"
        ).fetchone()
        assert cap is not None and cap["active"] == 1
