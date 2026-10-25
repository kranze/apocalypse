"""Tests: Spielerwissen — plausible Gegenstände deklarieren (Issue #54).

Kontrollierte Ausnahme vom Eisernen Prinzip: Das LLM (hier der deterministische
Stub) schlägt vor, der Sim-Kern klemmt/validiert/bucht (Ledger-Quelle
``player_declared``). Offline, Stub-Backend, fixer Seed.
"""
from __future__ import annotations

import os

import pytest

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
import app.osm.roads as roads_module
from app.llm import stub as stub_mod
from app.llm import tools as llm_tools
from app.sim import (actions, adjudicator, audit, constants, declare, devices, effects, ending,
                     energy, game, generation, home, kb, ledger, looting, requirements)
from tests.conftest import insert_location, make_conn, set_seed
from tests.test_actions import HOME, add_group_item
from tests.test_infra_gates_adjudicator import (
    _NoGraph, ctx_of, give_phone, no_balance_error, phone_charge, set_tick,
)

DAY = constants.MINUTES_PER_DAY
HOUSE_LAT = HOME[0] + 0.0005          # ~55 m nördlich: Nachbarhaus
FAR_LAT = HOME[0] + 0.006             # ~670 m nördlich: Supermarkt (außerhalb von 250 m)


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
    """Spieler in der Wohnung (echte Location), ein Nachbarhaus ~55 m, ein Supermarkt ~670 m."""
    c = make_conn()
    set_seed(c, 1337)
    kb.seed_defaults(c)
    c.execute("UPDATE characters SET lat=?, lon=?, home_lat=?, home_lon=? WHERE id=1;",
              (*HOME, *HOME))
    home.ensure_home(c, *HOME)
    insert_location(c, loc_id=200, loc_type="house", name="Nachbarhaus",
                    lat=HOUSE_LAT, lon=HOME[1])
    insert_location(c, loc_id=300, loc_type="supermarket", name="Supermarkt",
                    lat=FAR_LAT, lon=HOME[1])
    c.commit()
    yield c
    c.close()


def home_id(conn):
    return home.home_location(conn)["id"]


def declared_loc_items(conn, loc_id):
    return {r["item_id"]: r["quantity"] for r in conn.execute(
        "SELECT item_id, quantity FROM location_inventory WHERE location_id = ?;", (loc_id,))}


def say(conn, text):
    return adjudicator.adjudicate(conn, 1, text)


def clean_audit(conn):
    return not [e for e in audit.run_audit(conn, 1) if "Bilanz" in e["message"]]


# --- Wohnung als echter Ort ----------------------------------------------
def test_home_is_a_real_location(conn):
    h = home.home_location(conn)
    assert h["type"] == "home" and h["label"] == "Deine Wohnung"
    assert h["discovery_status"] == "discovered" and h["generation_seed"] is not None
    assert (h["lat"], h["lon"]) == HOME


def test_home_has_small_deterministic_base_stock(conn):
    generation.ensure_generated(conn, home_id(conn))
    first = declared_loc_items(conn, home_id(conn))
    assert first and sum(first.values()) < 60  # kleiner Grundbestand
    # gleicher Seed -> gleiche Würfe
    conn2 = make_conn()
    set_seed(conn2, 1337)
    home.ensure_home(conn2, *HOME)
    generation.ensure_generated(conn2, home_id(conn2))
    assert declared_loc_items(conn2, home_id(conn2)) == first
    assert clean_audit(conn)


def test_home_can_be_searched_and_looted(conn):
    generation.ensure_generated(conn, home_id(conn))
    before = declared_loc_items(conn, home_id(conn))
    res = looting.loot(conn, home_id(conn), 1)
    assert res["ok"] and set(res["transferred"]) == set(before)
    assert clean_audit(conn)


# --- Akzeptanz: 100-kWh-Akku ---------------------------------------------
def test_battery_declared_and_charges_phone_after_blackout(conn):
    v = say(conn, "In meinem Keller steht ein 100-kWh-Akku")
    assert v["ok"], v
    assert "in deiner Wohnung" in v["narration"]
    hid = home_id(conn)
    items = declared_loc_items(conn, hid)
    (item_id,) = [i for i in items if i.startswith("decl_")]
    st = conn.execute("SELECT * FROM energy_state WHERE item_id = ?;", (item_id,)).fetchone()
    assert st["capacity_kwh"] == 100.0
    assert st["charge_kwh"] == pytest.approx(100.0 * constants.DECLARE_DEFAULT_CHARGE_FRAC)

    # Ledger: explizite Quelle player_declared, Bilanz grün
    assert ledger.source_totals(conn, "player_declared") == {item_id: 1.0}
    assert ledger.expected_totals(conn)[item_id] == 1.0
    assert clean_audit(conn)

    # Nach dem Stromausfall: Netz tot, der Akku lädt das Handy
    give_phone(conn, 0.1)
    set_tick(conn, 4 * DAY)
    assert devices.grid_charge_plan(conn, 1, hid) is None
    kwh0 = conn.execute("SELECT charge_kwh FROM energy_state;").fetchone()[0]
    r = actions.perform(conn, 1, {"kind": "charge"})
    assert r["ok"], r
    assert r["result"]["source"] == "storage"
    assert phone_charge(conn) > 0.9
    kwh1 = conn.execute("SELECT charge_kwh FROM energy_state;").fetchone()[0]
    assert 0 < kwh0 - kwh1 < 0.1  # endlich, aber ein Handy kostet kaum etwas
    assert no_balance_error(conn)


def test_freetext_charge_uses_storage_after_blackout(conn):
    assert say(conn, "Im Keller steht ein 20-kWh-Akku")["ok"]
    give_phone(conn, 0.1)
    set_tick(conn, 4 * DAY)
    v = say(conn, "Lade mein Handy auf")
    assert v["ok"], v
    assert phone_charge(conn) > 0.9


def test_grid_is_preferred_while_it_runs(conn):
    assert say(conn, "Im Keller steht ein 20-kWh-Akku")["ok"]
    give_phone(conn, 0.1)
    kwh0 = conn.execute("SELECT charge_kwh FROM energy_state;").fetchone()[0]
    r = actions.perform(conn, 1, {"kind": "charge"})
    assert r["ok"] and r["result"]["source"] == "grid"
    assert conn.execute("SELECT charge_kwh FROM energy_state;").fetchone()[0] == kwh0


def test_storage_is_finite(conn):
    c = {"name": "Mini-Akku", "category": "tool", "weight_kg": 1.0, "capacity_kwh": 0.5,
         "charge_kwh": 0.0, "plausible": True, "reason": "ok", "where": "here"}
    ok, _, cl, loc = declare.check(conn, c, {"lat": HOME[0], "lon": HOME[1]})
    assert ok
    declare.apply(conn, cl, loc["id"])
    give_phone(conn, 0.1)
    set_tick(conn, 4 * DAY)
    assert devices.storage_charge_plan(conn, 1, home_id(conn)) is None  # leer = keine Quelle
    conn.execute("UPDATE energy_state SET charge_kwh = 0.005;")  # unter STORAGE_MIN_KWH
    assert devices.storage_charge_plan(conn, 1, home_id(conn)) is None


def test_storage_partial_charge_when_nearly_empty(conn):
    c = {"name": "Mini-Akku", "category": "tool", "weight_kg": 1.0, "capacity_kwh": 0.5,
         "charge_kwh": 0.012, "plausible": True, "reason": "ok", "where": "here"}
    ok, _, cl, loc = declare.check(conn, c, {"lat": HOME[0], "lon": HOME[1]})
    declare.apply(conn, cl, loc["id"])
    give_phone(conn, 0.0)
    set_tick(conn, 4 * DAY)
    plan = devices.storage_charge_plan(conn, 1, home_id(conn))
    assert plan and plan["source"] == "storage"
    assert sum(plan["kwh_cost"].values()) == pytest.approx(0.012)
    devices.apply_charge(conn, 1, plan)
    assert conn.execute("SELECT charge_kwh FROM energy_state;").fetchone()[0] == pytest.approx(0.0)
    assert phone_charge(conn) == pytest.approx(0.012 / constants.KWH_PER_DEVICE_CHARGE["smartphone"],
                                               rel=0.05)


def test_storage_powers_recipes_with_power_requirement(conn):
    assert say(conn, "Im Keller steht ein 1-kWh-Akku")["ok"]
    set_tick(conn, 4 * DAY)
    add_group_item(conn, "rope", 2)
    conn.execute("UPDATE energy_state SET charge_kwh = 0.7;")
    conn.commit()
    eff = {"op": "transform", "consume": [{"item": "rope", "qty": 1}],
           "produce": [{"item": "duct_tape", "qty": 1}], "requires": ["power"]}
    ctx = ctx_of(conn)
    # Staging: zwei Rezepte à 0.5 kWh gehen nicht aus 0.7 kWh
    st = effects.new_staged()
    assert effects.validate_one(conn, 1, eff, ctx, st)[0]
    ok, reason = effects.validate_one(conn, 1, eff, ctx, st)
    assert not ok and reason == "missing:power"
    # Anwenden entnimmt die Energie
    ok, applied, _ = effects.apply_atomic(conn, 1, [eff], ctx)
    assert ok
    left = conn.execute("SELECT charge_kwh FROM energy_state;").fetchone()[0]
    assert left == pytest.approx(0.7 - constants.STORAGE_RECIPE_KWH)
    assert clean_audit(conn)
    # zu wenig übrig -> kein power mehr
    assert not effects.validate_all(conn, 1, [eff], ctx)[0]


def test_carried_storage_works_anywhere(conn):
    assert say(conn, "Im Keller steht eine 2-kWh-Powerstation")["ok"]
    assert looting.loot(conn, home_id(conn), 1)["ok"]
    give_phone(conn, 0.1)
    set_tick(conn, 4 * DAY)
    plan = devices.charge_plan(conn, 1, None)  # draußen, kein Ort
    assert plan is not None and plan["source"] == "storage"
    assert requirements.satisfy(conn, 1, "power", location_id=None)["kwh"] > 0


def test_storage_at_other_location_is_not_usable_here(conn):
    assert say(conn, "Im Keller steht ein 20-kWh-Akku")["ok"]
    conn.execute("UPDATE characters SET lat=?, lon=? WHERE id=1;", (HOUSE_LAT, HOME[1]))
    conn.commit()
    assert energy.storages_at(conn, 1, 200) == []
    assert len(energy.storages_at(conn, 1, home_id(conn))) == 1


# --- Akzeptanz: Absurdes wird abgelehnt -----------------------------------
def test_a380_rejected_with_reason(conn):
    v = say(conn, "Im Keller steht ein A380")
    assert not v["ok"]
    assert v["reason"].startswith("declare_implausible:")
    assert "Das klappt so nicht" in v["narration"] and "A380" in v["narration"]
    assert conn.execute("SELECT COUNT(*) FROM item_catalog WHERE id LIKE 'decl_%';").fetchone()[0] == 0
    assert ledger.source_totals(conn, "player_declared") == {}
    assert declared_loc_items(conn, home_id(conn)) == {}


def test_core_rejects_absurd_weight_even_if_llm_says_plausible(conn):
    raw = {"name": "Riesending", "category": "tool", "weight_kg": 500000.0, "quantity": 1,
           "plausible": True, "reason": "LLM irrt", "where": "here"}
    ok, reason, _ = declare.evaluate(conn, raw)
    assert not ok and reason.startswith("declare_implausible:")


# --- Akzeptanz: nur an erlaubten Orten ------------------------------------
def test_declare_in_far_supermarket_is_not_here(conn):
    conn.execute("UPDATE characters SET lat=?, lon=? WHERE id=1;", (FAR_LAT, HOME[1]))
    conn.commit()
    v = say(conn, "Hier im Supermarkt steht ein Werkzeugkasten")
    assert not v["ok"] and v["reason"] == "declare_not_here"
    assert "Das weißt du über diesen Ort nicht." in v["narration"]
    assert declared_loc_items(conn, 300) == {}
    assert clean_audit(conn)


def test_declare_outside_any_building_is_not_here(conn):
    conn.execute("UPDATE characters SET lat=?, lon=? WHERE id=1;", (HOME[0] + 0.003, HOME[1]))
    conn.commit()
    v = say(conn, "Hier steht ein Werkzeugkasten")
    assert not v["ok"] and v["reason"] == "declare_not_here"


def test_declare_at_neighbour_building_within_radius(conn):
    conn.execute("UPDATE characters SET lat=?, lon=? WHERE id=1;", (HOUSE_LAT, HOME[1]))
    conn.commit()
    v = say(conn, "Hier steht ein Werkzeugkasten")
    assert v["ok"], v
    assert "decl_1_werkzeugkasten" in declared_loc_items(conn, 200)


def test_neighbour_text_assigns_nearby_house(conn):
    v = say(conn, "Mein Nachbar hat ein Schweißgerät in der Garage")
    assert v["ok"], v
    assert "Nachbarhaus" in v["narration"]
    assert any(i.startswith("decl_") for i in declared_loc_items(conn, 200))
    assert declared_loc_items(conn, home_id(conn)) == {}
    assert clean_audit(conn)


def test_neighbour_without_building_is_rejected(conn):
    conn.execute("DELETE FROM locations WHERE id IN (200, 300);")
    conn.commit()
    v = say(conn, "Mein Nachbar hat ein Schweißgerät")
    assert not v["ok"] and v["reason"] == "declare_no_neighbour"


# --- Klemmen --------------------------------------------------------------
def _raw(**kw):
    base = {"name": "Ding", "category": "tool", "weight_kg": 2.0, "quantity": 1,
            "plausible": True, "reason": "ok", "where": "here"}
    base.update(kw)
    return base


def test_clamps(conn):
    ok, _, c = declare.evaluate(conn, _raw(weight_kg=3000.0, quantity=9999))
    assert ok and c["weight_kg"] == constants.DECLARE_MAX_UNIT_WEIGHT_KG
    assert c["quantity"] * c["weight_kg"] <= max(constants.DECLARE_MAX_TOTAL_KG,
                                                 c["weight_kg"])
    ok, _, c = declare.evaluate(conn, _raw(quantity=9999, weight_kg=0.5))
    assert c["quantity"] == constants.DECLARE_MAX_QTY
    ok, _, c = declare.evaluate(conn, _raw(capacity_kwh=99999, charge_kwh=99999,
                                           quantity=5, weight_kg=10))
    assert c["kind"] == "storage" and c["quantity"] == 1.0
    assert c["capacity_kwh"] == constants.DECLARE_MAX_CAPACITY_KWH
    assert c["charge_kwh"] == constants.DECLARE_MAX_CAPACITY_KWH
    ok, _, c = declare.evaluate(conn, _raw(capacity_kwh=10, charge_kwh=-5))
    assert c["charge_kwh"] == 0.0
    ok, _, c = declare.evaluate(conn, _raw(provides=["heat", "telepathy", "power"]))
    assert c["provides"] == ["heat", "power"]
    ok, _, c = declare.evaluate(conn, _raw(capacity_kwh=5, provides=["power", "heat"]))
    assert c["provides"] == ["heat"]  # Strom liefert der Speicher selbst (endlich)


def test_bad_category_and_no_name_rejected(conn):
    assert declare.evaluate(conn, _raw(category="weapon"))[1] == "declare_bad_category"
    assert declare.evaluate(conn, _raw(name="  "))[1] == "declare_no_name"
    assert declare.evaluate(conn, _raw(plausible=False, reason="Quatsch"))[1] \
        == "declare_implausible:Quatsch"
    assert declare.evaluate(conn, {"name": "x", "category": "tool"})[0] is False  # kein plausible


def test_total_limit(conn):
    for i in range(constants.DECLARE_MAX_ITEMS):
        ok, _, c, loc = declare.check(conn, _raw(name=f"Ding {i}"), {"lat": HOME[0], "lon": HOME[1]})
        assert ok
        declare.apply(conn, c, loc["id"])
    ok, reason, _ = declare.evaluate(conn, _raw(name="Zuviel"))
    assert not ok and reason == "declare_limit"
    assert clean_audit(conn)


def test_water_maps_to_standard_water_and_stock_alias(conn):
    ok, _, c = declare.evaluate(conn, _raw(name="Wasserkanister", category="water",
                                           weight_kg=20.0, quantity=1))
    assert c["item_id"] == "water_1l" and c["quantity"] == 20.0
    ok, _, c = declare.evaluate(conn, _raw(name="Mein Fahrrad", weight_kg=14.0))
    assert c["item_id"] == "bicycle"


def test_provider_item_teaches_kb_with_player_declared_provenance(conn):
    v = say(conn, "Im Keller steht ein Gaskocher")
    assert v["ok"], v
    facts = kb.list_topic(conn, "provides:heat")
    mine = [f for f in facts if f["key"].startswith("decl_")]
    assert mine and mine[0]["provenance"] == "player_declared"
    # und er wird zu einem normalen Lieferanten, sobald man ihn besitzt
    assert looting.loot(conn, home_id(conn), 1)["ok"]
    assert requirements.satisfy(conn, 1, "heat")["item"] == mine[0]["key"]


# --- Ledger / Audit ---------------------------------------------------------
def test_audit_flags_declared_source_without_protocol(conn):
    assert say(conn, "Im Keller steht ein 20-kWh-Akku")["ok"]
    assert clean_audit(conn)
    conn.execute("UPDATE ledger_sources SET total = total + 3;")
    conn.commit()
    assert not clean_audit(conn)


def test_audit_flags_inventory_created_outside_the_sim_core(conn):
    assert say(conn, "Im Keller steht ein 20-kWh-Akku")["ok"]
    conn.execute("UPDATE location_inventory SET quantity = quantity + 5 "
                 "WHERE item_id LIKE 'decl_%';")
    conn.commit()
    assert not clean_audit(conn)


# --- Onboarding-Batch ---------------------------------------------------------
@pytest.fixture
def fresh(monkeypatch):
    from app.sim import survivors
    monkeypatch.setattr(survivors, "spawn_survivors", lambda *a, **k: None)
    c = make_conn()
    set_seed(c, 1337)
    yield c
    c.close()


def _new_game(c, text):
    return game.new_game(c, {"name": "Test", "lat": HOME[0], "lon": HOME[1], "sex": "f",
                             "weight_kg": 70.0, "height_cm": 170.0, "birthdate": "1990-01-01",
                             "home_inventory": text})


def test_onboarding_batch_creates_items_and_reports_each(fresh):
    r = _new_game(fresh, "In meinem Keller steht ein 100-kWh-Akku, Vorräte und ein A380. "
                         "Mein Nachbar hat ein Schweißgerät.")
    assert r["ok"]
    by = {d["name"]: d for d in r["declared"]}
    assert by["Hausakku (100 kWh)"]["status"] == "placed"
    assert by["Vorräte"]["status"] == "placed"
    assert by["A380"]["status"] == "rejected"
    assert by["Schweißgerät"]["status"] == "pending"   # Umgebung (OSM) noch nicht geladen
    # Intro-Erzählung nennt jeden Gegenstand mit Ergebnis
    assert "abgelehnt" in r["intro"] and "Hausakku" in r["intro"] and "Schweißgerät" in r["intro"]
    hid = home_id(fresh)
    items = declared_loc_items(fresh, hid)
    assert any(i.startswith("decl_") for i in items)
    assert fresh.execute("SELECT COUNT(*) FROM energy_state;").fetchone()[0] == 1
    assert clean_audit(fresh)

    # Sobald Gebäude geladen sind, wird der Nachbar-Gegenstand platziert.
    insert_location(fresh, loc_id=500, loc_type="house", name="Nachbarhaus",
                    lat=HOUSE_LAT, lon=HOME[1])
    assert declare.resolve_pending(fresh) == 1
    assert declare.resolve_pending(fresh) == 0  # idempotent
    assert len(declared_loc_items(fresh, 500)) == 1
    assert clean_audit(fresh)
    stats = ending.stats(fresh)["declared"]
    assert {d["name"] for d in stats} == {"Hausakku (100 kWh)", "Vorräte", "Schweißgerät"}
    assert next(d for d in stats if d["name"] == "Schweißgerät")["location"] == "Nachbarhaus"


def test_onboarding_without_text_declares_nothing(fresh):
    r = _new_game(fresh, "")
    assert r["ok"] and r["declared"] == [] and r["intro"] == game.INTRO
    assert home.home_location(fresh) is not None


def test_new_game_resets_declared_state(fresh):
    _new_game(fresh, "Im Keller steht ein Hausakku")
    assert fresh.execute("SELECT COUNT(*) FROM energy_state;").fetchone()[0] == 1
    _new_game(fresh, "")
    for t in ("energy_state", "declared_items", "ledger_sources"):
        assert fresh.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0] == 0
    assert fresh.execute("SELECT COUNT(*) FROM item_catalog WHERE id LIKE 'decl_%';").fetchone()[0] == 0
    assert fresh.execute("SELECT COUNT(*) FROM locations;").fetchone()[0] == 1  # nur die Wohnung
    assert clean_audit(fresh)


def _world_with_houses():
    c = make_conn()
    set_seed(c, 1337)
    home.ensure_home(c, *HOME)
    for i, dlat in enumerate((0.0005, 0.0008, 0.0012, 0.0016, 0.0020)):
        insert_location(c, loc_id=210 + i, loc_type="house", name=f"Haus {i}",
                        lat=HOME[0] + dlat, lon=HOME[1])
    return c


def test_batch_places_neighbour_deterministically_among_nearest_houses():
    spots = []
    for _ in range(2):
        c = _world_with_houses()
        res = declare.declare_batch(c, "Mein Nachbar hat einen Generator",
                                    {"lat": HOME[0], "lon": HOME[1]})
        assert res[0]["status"] == "placed"
        spots.append(res[0]["location"])
        c.close()
    assert spots[0] == spots[1]
    assert spots[0] in ("Haus 0", "Haus 1", "Haus 2")  # nur unter den drei nächsten


# --- Ending-Statistik ---------------------------------------------------------
def test_ending_stats_list_declared_items(conn):
    assert say(conn, "In meinem Keller steht ein 100-kWh-Akku")["ok"]
    assert say(conn, "Im Keller steht ein A380")["ok"] is False
    declared = ending.stats(conn)["declared"]
    assert [d["name"] for d in declared] == ["Hausakku (100 kWh)"]
    assert declared[0]["kind"] == "storage" and declared[0]["capacity_kwh"] == 100.0
    assert declared[0]["location"] == "Deine Wohnung"


# --- Stub / Tool-Vertrag -------------------------------------------------------
def test_stub_is_deterministic():
    t = "Hausakku 20 kWh, Vorräte, Fahrrad und Werkzeugkasten; Nachbar: Generator"
    assert stub_mod._declare_proposals(t) == stub_mod._declare_proposals(t)
    names = [p["name"] for p in stub_mod._declare_proposals(t)]
    assert names == ["Hausakku (20 kWh)", "Vorräte", "Fahrrad", "Werkzeugkasten", "Stromgenerator"]


@pytest.mark.parametrize("word", ["Flugzeug", "A380", "Panzer", "Atomreaktor"])
def test_stub_rejects_absurd(word):
    p = stub_mod._declare_proposals(f"Im Keller steht ein {word}")
    assert len(p) == 1 and p[0]["plausible"] is False and p[0]["reason"]


def test_declare_item_tool_is_strict_and_within_limit():
    specs = {s["name"]: s for s in llm_tools.tool_specs()}
    assert specs["propose_declare_item"].get("strict")
    assert sum(1 for s in specs.values() if s.get("strict")) <= llm_tools.MAX_STRICT_TOOLS
    assert "declare_item" in effects.OPS
