"""Tests: Zuhause zählt als Infra-Ort (Hahn, Steckdose) auch ohne locations-Zeile,
und abgelehnte Freitext-Pläne zeigen nie die Erfolgs-Erzählung des Modells.
Offline, Stub-Backend, fixer Seed."""
from __future__ import annotations

import os

import pytest

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
import app.osm.roads as roads_module
from app.sim import actions, adjudicator, constants, decay_world, devices, kb, provision
from tests.test_actions import HOME, add_group_item
from tests.conftest import make_conn, set_seed
from tests.test_infra_gates_adjudicator import (
    _NoGraph, give_phone, no_balance_error, phone_charge, set_tick,
)

DAY = constants.MINUTES_PER_DAY
SUCCESS_NARRATION = "Du trinkst etwas Wasser."


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    os.environ["WASTELAND_LLM_BACKEND"] = "stub"
    llm_mod.reset_backend()
    monkeypatch.setattr(roads_module, "get_graph", lambda *a, **k: _NoGraph())
    monkeypatch.setattr(roads_module, "ensure_roads_for_chunk", lambda *a, **k: None)
    yield
    llm_mod.reset_backend()


@pytest.fixture
def conn():
    """Spieler zu Hause, KEIN Gebäude in Reichweite (Heimat ist keine Location)."""
    c = make_conn()
    set_seed(c, 1337)
    kb.seed_defaults(c)
    c.execute("UPDATE characters SET lat=?, lon=?, home_lat=?, home_lon=? WHERE id=1;",
              (*HOME, *HOME))
    c.commit()
    yield c
    c.close()


def _drink(conn):
    conn.execute("UPDATE characters SET thirst = 0.3 WHERE id = 1;")
    conn.commit()
    return actions.perform(conn, 1, {
        "kind": "freetext", "text": "Ich trinke einen Schluck Wasser aus dem Hahn"})


def test_drink_tap_at_home_day0(conn):
    r = _drink(conn)
    assert r["ok"], r
    assert conn.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"] > 0.3
    assert conn.execute(
        "SELECT COUNT(*) c FROM group_inventory WHERE item_id='water_1l';").fetchone()["c"] == 0
    assert no_balance_error(conn)


def test_drink_at_home_after_pressure_loss_rejected(conn):
    set_tick(conn, 4 * DAY)
    r = _drink(conn)
    assert not r["ok"] and r["reason"] == "no_tap_water"
    assert SUCCESS_NARRATION not in (r.get("narration") or "")
    assert "Das klappt so nicht" in r["narration"]


def test_auto_provision_tap_at_home_day0(conn):
    conn.execute("UPDATE characters SET thirst = 0.1 WHERE id = 1;")
    conn.commit()
    assert provision.tap_here(conn, *HOME)
    with conn:
        provision.auto_provision(conn, 1, 1)
    assert conn.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"] > 0.1
    assert no_balance_error(conn)


def test_tap_not_available_far_from_home(conn):
    assert not provision.tap_here(conn, HOME[0] + 0.01, HOME[1])


def test_grid_charge_at_home_day0(conn):
    give_phone(conn, 0.2)
    assert decay_world.infra_loc(conn, *HOME, constants.LOOT_REACH_M) == decay_world.HOME_LOC_ID
    r = actions.perform(conn, 1, {"kind": "charge", "source": "grid"})
    assert r["ok"], r
    assert phone_charge(conn) > 0.2


def test_grid_charge_at_home_blackout_rejected(conn):
    give_phone(conn, 0.2)
    set_tick(conn, 60 * DAY)
    assert devices.grid_charge_plan(conn, 1, decay_world.HOME_LOC_ID) is None


def test_rejected_freetext_has_no_success_narration(conn):
    set_tick(conn, 4 * DAY)
    v = adjudicator.adjudicate(conn, 1, "trinke aus dem wasserhahn")
    assert not v["ok"]
    assert SUCCESS_NARRATION not in v["narration"]
    assert v["hint"] in v["narration"]
