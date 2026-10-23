"""Tests: Infra-Gates in den Adjudikator-Effekten (Issue #7 Teil B, #9).

Leitungswasser (``drink``), Netzstrom als ``power``-Lieferant und der Effekt
``charge_device`` respektieren ``decay_world.infra_for`` am Standort. Offline,
Stub-Backend, fixer Seed.
"""
from __future__ import annotations

import os

import pytest

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
import app.osm.roads as roads_module
from app.llm import tools as llm_tools
from app.sim import (actions, adjudicator, audit, constants, decay_world, devices, effects,
                     kb, requirements)
from app.sim.toolbox import AdjudicationToolbox, hint_for
from tests.test_actions import add_group_item, build_world
from tests.conftest import make_conn, set_seed

DAY = constants.MINUTES_PER_DAY
LOC = 100


class _NoGraph:
    def nearest_node(self, lat, lon):
        return None


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
    c = make_conn()
    set_seed(c, 1337)
    kb.seed_defaults(c)
    build_world(c)
    # Ort entdeckt: Auto-Versorgung/Leitungswasser kennt nur entdeckte Orte.
    c.execute("UPDATE locations SET discovery_status='discovered' WHERE id=?;", (LOC,))
    c.commit()
    yield c
    c.close()


def set_tick(conn, tick):
    conn.execute("UPDATE world SET tick = ? WHERE id = 1;", (tick,))
    conn.commit()


def give_phone(conn, charge=0.3):
    add_group_item(conn, "smartphone", 1)
    devices.grant(conn, 1, "smartphone", charge, units=1)
    conn.commit()


def phone_charge(conn):
    return next(d["charge"] for d in devices.status(conn, 1) if d["item_id"] == "smartphone")


def ctx_of(conn):
    return adjudicator.build_context(conn, 1)


def validate(conn, eff):
    return effects.validate_all(conn, 1, [eff], ctx_of(conn))


def no_balance_error(conn):
    return not [e for e in audit.run_audit(conn, 1) if "Bilanz" in e["message"]]


# --- Leitungswasser -------------------------------------------------------
def test_tap_water_gate_over_timeline(conn):
    assert decay_world.infra_for(conn, LOC)["water"] > constants.WATER_TAP_MIN
    assert validate(conn, {"op": "drink"}) == (True, None)
    set_tick(conn, 4 * DAY)
    assert decay_world.infra_for(conn, LOC)["water"] <= constants.WATER_TAP_MIN
    ok, reason = validate(conn, {"op": "drink"})
    assert not ok and reason == "no_tap_water"
    assert hint_for(reason)


def test_drink_freetext_tap_is_not_a_ledger_source(conn):
    conn.execute("UPDATE characters SET thirst = 0.3 WHERE id = 1;")
    conn.commit()
    r = actions.perform(conn, 1, {"kind": "freetext", "text": "trinke aus dem wasserhahn"})
    assert r["ok"], r
    assert conn.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"] > 0.3
    assert conn.execute(
        "SELECT COUNT(*) c FROM group_inventory WHERE item_id='water_1l';").fetchone()["c"] == 0
    assert no_balance_error(conn)


def test_drink_freetext_after_pressure_loss_rejected(conn):
    set_tick(conn, 4 * DAY)
    r = actions.perform(conn, 1, {"kind": "freetext", "text": "trinke aus dem wasserhahn"})
    assert not r["ok"] and r["reason"] == "no_tap_water"


def test_drink_uses_inventory_water_without_pressure(conn):
    set_tick(conn, 4 * DAY)
    add_group_item(conn, "water_1l", 2)
    conn.execute("UPDATE characters SET thirst = 0.3 WHERE id = 1;")
    conn.commit()
    r = actions.perform(conn, 1, {"kind": "freetext", "text": "trinke wasser"})
    assert r["ok"], r
    left = conn.execute(
        "SELECT SUM(quantity) q FROM group_inventory WHERE item_id='water_1l';").fetchone()["q"]
    assert (left or 0.0) < 2.0  # Inventarwasser getrunken (Auto-Versorgung trinkt ggf. mehr)
    assert no_balance_error(conn)


# --- Netzstrom als power-Lieferant -----------------------------------------
def test_grid_power_provider_gate(conn):
    gid = 1
    assert requirements.satisfy(conn, gid, "power", location_id=LOC) is not None
    assert requirements.satisfy(conn, gid, "power") is None  # ohne Ort: kein Netz
    set_tick(conn, 4 * DAY)
    assert requirements.satisfy(conn, gid, "power", location_id=LOC) is None
    add_group_item(conn, "generator", 1)
    src = requirements.satisfy(conn, gid, "power", location_id=LOC)
    assert src and src["item"] == "generator"


def test_beacon_needs_grid_or_generator_over_timeline(conn):
    add_group_item(conn, "wifi_router", 1)
    eff = {"op": "establish_capability", "ctype": "ssid_beacon", "target": "tower"}
    assert validate(conn, eff) == (True, None)  # Tag 0: Netzstrom am Ort
    set_tick(conn, 4 * DAY)
    ok, reason = validate(conn, eff)
    assert not ok and reason == "missing:power"
    assert "Strom" in hint_for(reason)
    assert requirements.requirement_from_reason(conn, reason) == "power"
    add_group_item(conn, "generator", 1)
    assert validate(conn, eff) == (True, None)


# --- charge_device ----------------------------------------------------------
def test_charge_device_freetext_day0_with_grid(conn):
    give_phone(conn, 0.3)
    assert decay_world.infra_for(conn, LOC)["power"] > constants.CHARGE_POWER_MIN
    r = actions.perform(conn, 1, {"kind": "freetext", "text": "lade mein handy"})
    assert r["ok"], r
    assert [a["op"] for a in r["result"]["effects_applied"]] == ["charge_device"]
    assert r["result"]["effects_applied"][0]["result"]["source"] == "grid"
    assert phone_charge(conn) > 0.3 and r["minutes_spent"] >= constants.CHARGE_MIN_MINUTES
    assert no_balance_error(conn)


def test_charge_device_freetext_fails_without_power_and_powerbank(conn):
    give_phone(conn, 0.3)
    set_tick(conn, 4 * DAY)
    before = phone_charge(conn)
    devices.sync(conn, 1)
    before = phone_charge(conn)
    r = actions.perform(conn, 1, {"kind": "freetext", "text": "lade mein handy"})
    assert not r["ok"] and r["reason"] == "no_power"
    assert phone_charge(conn) <= before + 1e-9


def test_charge_device_via_powerbank_without_grid(conn):
    give_phone(conn, 0.3)
    add_group_item(conn, "powerbank", 1)
    devices.grant(conn, 1, "powerbank", 1.0, units=1)
    conn.commit()
    set_tick(conn, 4 * DAY)
    r = actions.perform(conn, 1, {"kind": "freetext", "text": "lade mein handy"})
    assert r["ok"], r
    assert r["result"]["effects_applied"][0]["result"]["source"] == "powerbank"
    assert phone_charge(conn) > 0.3


def test_charge_device_rejects_without_device_and_when_full(conn):
    assert validate(conn, {"op": "charge_device"}) == (False, "no_device")
    give_phone(conn, 1.0)
    assert validate(conn, {"op": "charge_device"}) == (False, "already_full")
    assert validate(conn, {"op": "charge_device", "source": "nuklear"})[0] is False


# --- Werkzeuge / Kontext ----------------------------------------------------
def test_charge_device_and_drink_tools_exist():
    names = {s["name"] for s in llm_tools.tool_specs()}
    assert {"propose_charge_device", "propose_drink", "get_device_status"} <= names
    assert llm_tools.to_effect("charge_device", {"source": None}) == {"op": "charge_device"}


def test_context_and_read_tool_expose_battery_and_network(conn):
    give_phone(conn, 0.5)
    ctx = ctx_of(conn)
    assert ctx["devices"] and ctx["devices"][0]["item_id"] == "smartphone"
    assert ctx["network"]["quality"] > 0 and "wifi" in ctx["network"]
    tb = AdjudicationToolbox(conn, 1, ctx)
    res = tb.call("get_device_status", {})
    assert res["ok"] and res["devices"][0]["pct"] == pytest.approx(50.0, abs=1.0)
    assert res["network"]["online"] is True
    set_tick(conn, 7 * DAY)
    res = AdjudicationToolbox(conn, 1, ctx_of(conn)).call("get_device_status", {})
    assert res["network"]["online"] is False


def test_world_state_has_infra_and_phase(conn):
    ws = ctx_of(conn)["world_state"]
    assert "day" in ws and ws["infra"]["phase"]
