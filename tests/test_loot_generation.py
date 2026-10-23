"""Tests für Loot-Tabellen, Aktion "search" und den 7-Tage-Klick-Durchlauf (Issue #50).

Deterministisch (fixer Seed, Stub-LLM, kein Netz, Luftlinien-Routing).
"""
from __future__ import annotations

import os

import pytest

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
import app.osm.roads as roads_module
import app.osm.tagmap as tagmap
from app.sim import actions, audit, constants, generation
from app.sim.loot_tables import DEFAULT_TABLE_KEY, LOOT_TABLES
from tests.conftest import insert_location, make_conn, set_seed

llm_mod.reset_backend()

HOME = (49.0, 11.0)
SHOP = (49.003, 11.0)  # ~330 m nördlich


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


def make_world(seed_house=11, seed_shop=22):
    conn = make_conn()
    set_seed(conn, 1337)
    conn.execute("UPDATE characters SET lat=?, lon=? WHERE id=1;", HOME)
    insert_location(conn, loc_id=1, loc_type="house", name="Haus",
                    lat=HOME[0], lon=HOME[1], generation_seed=seed_house)
    insert_location(conn, loc_id=2, loc_type="supermarket", name="Supermarkt",
                    lat=SHOP[0], lon=SHOP[1], generation_seed=seed_shop)
    conn.commit()
    return conn


def inv(conn, loc_id):
    return {r["item_id"]: r["quantity"] for r in conn.execute(
        "SELECT item_id, quantity FROM location_inventory WHERE location_id=?;", (loc_id,))}


# --- Loot-Tabellen ------------------------------------------------------
def test_tables_reference_only_catalog_items_and_cover_all_types():
    conn = make_conn()
    catalog = {r["id"] for r in conn.execute("SELECT id FROM item_catalog;")}
    for loc_type, table in LOOT_TABLES.items():
        for e in table:
            assert e["item_id"] in catalog, f"{loc_type}: {e['item_id']} fehlt im Katalog"
            assert 0 < e["chance"] <= 1 and 1 <= e["qty_min"] <= e["qty_max"]
    # alle Typen, die der Tagmap erzeugt
    produced = set(tagmap._SHOP.values()) | set(tagmap._AMENITY.values()) | {"house", "building"}
    assert produced <= set(LOOT_TABLES)
    assert DEFAULT_TABLE_KEY in LOOT_TABLES


def test_water_in_realistic_places():
    for t in ("supermarket", "fuel_station", "house", "hospital"):
        assert any(e["item_id"] == "water_1l" for e in LOOT_TABLES[t]), t
    assert any(e["item_id"] == "bandage" for e in LOOT_TABLES["pharmacy"])
    assert any(e["item_id"] == "hammer" for e in LOOT_TABLES["hardware"])


def test_same_seed_same_inventory_regardless_of_visit_order():
    a, b = make_world(), make_world()
    generation.ensure_generated(a, 1)
    generation.ensure_generated(a, 2)
    generation.ensure_generated(b, 2)
    generation.ensure_generated(b, 1)
    assert inv(a, 1) == inv(b, 1) and inv(a, 2) == inv(b, 2)
    assert inv(a, 2)  # Supermarkt ist nicht leer


def test_decay_since_tick_zero_perishables_gone_late():
    conn = make_world()
    conn.execute("UPDATE world SET tick = ?;", (constants.MINUTES_PER_DAY * 30,))
    conn.commit()
    generation.ensure_generated(conn, 2)
    assert "milk_1l" not in inv(conn, 2) and "bread_loaf" not in inv(conn, 2)
    assert "canned_beans" in inv(conn, 2)


# --- Aktion search ------------------------------------------------------
def test_search_costs_time_generates_once_then_loot_offered():
    conn = make_world()
    av = actions.available(conn, 1)
    kinds = [a["kind"] for a in av["actions"]]
    assert "search" in kinds and "loot" not in kinds
    s = next(a for a in av["actions"] if a["kind"] == "search")
    assert s["label"] == "Durchsuchen"
    assert constants.SEARCH_MIN_MINUTES <= s["minutes"] <= constants.SEARCH_MAX_MINUTES

    r = actions.perform(conn, 1, {"kind": "search", "location_id": 1})
    assert r["ok"] and r["minutes_spent"] == s["minutes"]
    first = inv(conn, 1)
    assert {f["item_id"]: f["quantity"] for f in r["result"]["found"]} == first
    assert all(f["name"] for f in r["result"]["found"])

    again = actions.perform(conn, 1, {"kind": "search", "location_id": 1})
    assert not again["ok"] and again["reason"] == "already_searched"
    assert again["minutes_spent"] == 0 and inv(conn, 1) == first

    kinds = [a["kind"] for a in actions.available(conn, 1)["actions"]]
    assert "search" not in kinds and "loot" in kinds
    taken = actions.perform(conn, 1, {"kind": "loot", "location_id": 1})
    assert taken["ok"] and taken["result"]["transferred"] == first
    assert inv(conn, 1) == {}


def test_search_too_far_rejected_and_empty_location_message():
    conn = make_world()
    r = actions.perform(conn, 1, {"kind": "search", "location_id": 2})
    assert not r["ok"] and r["reason"] == "not_here"
    insert_location(conn, loc_id=3, loc_type="building", name="Leer",
                    lat=HOME[0], lon=HOME[1], generation_seed=5)
    # Seed suchen, bei dem das generische Gebäude leer bleibt
    for seed in range(500):
        if not generation.roll_inventory(seed, "building", 0, {}):
            break
    conn.execute("UPDATE locations SET generation_seed=? WHERE id=3;", (seed,))
    conn.commit()
    r = actions.perform(conn, 1, {"kind": "search", "location_id": 3})
    assert r["ok"] and r["result"]["empty"] and "nichts" in r["narration"]
    assert not any(a["kind"] == "loot" and a["location_id"] == 3
                   for a in actions.available(conn, 1)["actions"])


def test_search_ledger_and_audit_clean():
    conn = make_world()
    actions.perform(conn, 1, {"kind": "search", "location_id": 1})
    actions.perform(conn, 1, {"kind": "loot", "location_id": 1})
    tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
    assert [f for f in audit.run_audit(conn, tick) if f.get("flagged")] == []


# --- 7 Tage nur mit Klickaktionen --------------------------------------
def _click_policy(conn) -> dict:
    """Wählt die nächste Klickaktion ausschließlich aus available() (plus walk)."""
    av_all = actions.available(conn, 1)
    av = av_all["actions"]
    char = conn.execute("SELECT hunger, sleep FROM characters WHERE id=1;").fetchone()

    def by(kind):
        return next((a for a in av if a["kind"] == kind), None)

    for kind in ("search", "loot"):
        if by(kind):
            return by(kind)
    if char["hunger"] < 0.6 and by("eat"):
        return by("eat")
    if char["sleep"] < 0.4 and by("sleep"):
        return by("sleep")
    if av_all["here_location_id"] != 2 and not generation.is_searched(conn, 2):
        return {"kind": "walk", "location_id": 2}
    return next(a for a in av if a["kind"] == "wait" and a["minutes"] == 60)


def test_seven_days_survivable_with_click_actions_only():
    conn = make_world()
    steps = 0
    while actions.world_clock(conn)["day"] < constants.DAYS_TOTAL:
        a = _click_policy(conn)
        payload = {k: v for k, v in a.items() if k not in ("label", "minutes", "options")}
        r = actions.perform(conn, 1, payload)
        steps += 1
        assert steps < 2000, "Endlosschleife in der Klick-Policy"
        assert r["player"]["is_alive"], f"Tod an Tag {r['day']} nach {steps} Aktionen"
    assert conn.execute("SELECT is_alive FROM characters WHERE id=1;").fetchone()["is_alive"]
    assert generation.is_searched(conn, 1) and generation.is_searched(conn, 2)
    tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
    assert [f for f in audit.run_audit(conn, tick) if f.get("flagged")] == []
