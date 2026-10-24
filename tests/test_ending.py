"""Tests für Slice M5 (Issue #49): Bewohnbarkeit, Entscheidung, Bewertung, Epilog, Tod.

Offline (Stub-LLM, fixer Seed, kein Netz). Die Bewertung ist reine Sim-Kern-Logik;
das LLM erzählt nur den Epilog.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
import app.osm.roads as roads_module
from app.sim import actions, companion, constants, ending, game, online
from tests.conftest import insert_location
from tests.test_actions import FAR_LAT, HOME, _NoGraph, add_group_item, build_world, set_char

llm_mod.reset_backend()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    os.environ["WASTELAND_LLM_BACKEND"] = "stub"
    llm_mod.reset_backend()
    monkeypatch.setattr(roads_module, "get_graph", lambda *a, **k: _NoGraph())
    monkeypatch.setattr(roads_module, "ensure_roads_for_chunk", lambda *a, **k: None)
    yield
    llm_mod.reset_backend()


@pytest.fixture
def world(conn):
    build_world(conn)
    return conn


def set_clock(conn, day: int, hh: int = 12, mm: int = 0):
    """Setzt die Uhr auf Kalendertag ``day`` (0-basiert), hh:mm (Start = 06:00)."""
    tick = day * 1440 + hh * 60 + mm - 360
    conn.execute("UPDATE world SET tick = ? WHERE id = 1;", (tick,))
    conn.commit()


def learn(conn, *package_ids):
    for pid in package_ids:
        size = online.package(pid)["size_mb"]
        online.add_progress(conn, pid, size, 0)


def add_companion(conn, stage="joined", trust=0.8):
    conn.execute(
        "INSERT INTO characters (id, name, type, group_id, age, hunger) "
        "VALUES (50, 'Jo', 'survivor', 1, 30, 1.0);")
    conn.execute(
        "INSERT INTO companion_state (id, character_id, group_id, stage, trust, opinion) "
        "VALUES (1, 50, 1, ?, ?, 'go');", (stage, trust))
    conn.commit()


def add_contact(conn, cid=3):
    conn.execute("INSERT INTO online_contacts (id, tick, kind, text) VALUES (?, 0, 'reply', 'x');",
                 (cid,))
    conn.commit()


# --- Bewohnbarkeit -------------------------------------------------------------
def test_habitability_shape_and_decreases_over_week(world):
    set_clock(world, 0, 7)
    h0 = ending.habitability(world)
    assert set(h0) == {"value", "label", "parts"}
    assert set(h0["parts"]) == {"decay", "water", "power", "supplies", "disease"}
    set_clock(world, 6, 7)
    h6 = ending.habitability(world)
    assert h6["value"] < h0["value"]
    assert h6["parts"]["decay"] < h0["parts"]["decay"]
    assert h6["parts"]["disease"] < h0["parts"]["disease"]
    assert h0["label"] in ("bewohnbar", "angespannt", "kritisch")


def test_habitability_drops_without_water_and_power(world):
    set_clock(world, 0, 7)
    early = ending.habitability(world)
    assert early["parts"]["water"] == 1.0 and early["parts"]["power"] == 1.0
    set_clock(world, 4, 7)  # Strom (<=3 Tage) und Wasser (<=3 Tage) sind aus
    late = ending.habitability(world)
    assert late["parts"]["water"] == 0.0 and late["parts"]["power"] == 0.0
    assert late["value"] < early["value"] - 0.25


def test_habitability_disease_knowledge_helps(world):
    set_clock(world, 6, 7)
    base = ending.habitability(world)["parts"]["disease"]
    learn(world, "disease_control")
    assert ending.habitability(world)["parts"]["disease"] > base


def test_habitability_is_read_only(world):
    set_clock(world, 3, 7)
    before = [tuple(r) for r in world.execute("SELECT * FROM location_infra;")]
    ending.habitability(world)
    assert [tuple(r) for r in world.execute("SELECT * FROM location_infra;")] == before


# --- decide: Zeitfenster -----------------------------------------------------------
def test_decide_only_from_day_5(world):
    set_clock(world, 4, 12)
    r = actions.perform(world, 1, {"kind": "decide", "choice": "stay"})
    assert not r["ok"] and r["reason"] == "decision_not_yet"
    assert not any(a["kind"] == "decide" for a in actions.available(world, 1)["actions"])
    assert not ending.is_ended(world)
    set_clock(world, 5, 8)
    dec = [a for a in actions.available(world, 1)["actions"] if a["kind"] == "decide"]
    assert len(dec) == 1
    opts = dec[0]["options"]
    assert opts["choices"] == ["stay", "go"]
    assert [t["id"] for t in opts["transports"]] == ["foot", "bike", "car"]
    assert opts["transports"][0]["ok"] is True
    assert opts["transports"][1] == {"id": "bike", "label": "Fahrrad", "ok": False,
                                     "missing": "Fahrrad"}
    assert opts["transports"][2]["missing"] == "Wissen: Auto & Sprit"
    assert [d["id"] for d in opts["destinations"]] == ["north", "east", "south", "west"]


def test_decision_interrupt_once_then_decision_required(world):
    set_clock(world, 7, 6, 30)
    r = actions.perform(world, 1, {"kind": "wait", "minutes": 60})
    msgs = [i["message"] for i in r["interrupts"]]
    assert "Die Woche ist um. Du musst dich entscheiden." in msgs
    assert r["stopped"] == "interrupt"
    n = world.execute("SELECT COUNT(*) AS n FROM events WHERE message = ?;",
                      ("Die Woche ist um. Du musst dich entscheiden.",)).fetchone()["n"]
    assert n == 1
    # nur noch decide
    acts = actions.available(world, 1)["actions"]
    assert [a["kind"] for a in acts] == ["decide"]
    for kind in ("wait", "search", "freetext", "sleep"):
        rr = actions.perform(world, 1, {"kind": kind, "minutes": 10, "text": "x"})
        assert not rr["ok"] and rr["reason"] == "decision_required"
    from app.sim import tick
    tick.advance_tick(world, 10)
    n = world.execute("SELECT COUNT(*) AS n FROM events WHERE message = ?;",
                      ("Die Woche ist um. Du musst dich entscheiden.",)).fetchone()["n"]
    assert n == 1


# --- decide: Validierung --------------------------------------------------------------
def test_go_needs_transport_and_destination(world):
    set_clock(world, 5, 8)
    perform = lambda **k: actions.perform(world, 1, {"kind": "decide", **k})  # noqa: E731
    assert perform(choice="maybe")["reason"] == "bad_choice"
    assert perform(choice="go")["reason"] == "transport_required"
    assert perform(choice="go", transport="foot")["reason"] == "destination_required"
    assert perform(choice="go", transport="bike", destination="north")["reason"] \
        == "transport_unavailable"
    assert perform(choice="go", transport="car", destination="north")["reason"] \
        == "transport_unavailable"
    assert perform(choice="go", transport="hoverboard", destination="north")["reason"] \
        == "bad_transport"
    assert perform(choice="go", transport="foot", destination="mars")["reason"] \
        == "bad_destination"
    assert perform(choice="go", transport="foot", destination="contact:9")["reason"] \
        == "bad_destination"
    assert not ending.is_ended(world)


def test_car_needs_knowledge_and_fuel(world):
    set_clock(world, 5, 8)
    learn(world, "car_fuel")
    car = [t for t in ending.transports(world) if t["id"] == "car"][0]
    assert not car["ok"] and car["missing"] == "Benzin"
    add_group_item(world, "gasoline", 1)
    assert [t for t in ending.transports(world) if t["id"] == "car"][0]["ok"]


def test_decide_go_ends_game_and_game_over_afterwards(world):
    set_clock(world, 5, 8)
    add_group_item(world, "bicycle", 1)
    add_contact(world, 3)
    r = actions.perform(world, 1, {"kind": "decide", "choice": "go", "transport": "bike",
                                   "destination": "contact:3"})
    assert r["ok"] and r["stopped"] == "ended" and r["minutes_spent"] == 0
    end = ending.get_ending(world)
    assert end["ended"] and end["choice"] == "go" and end["transport"] == "bike"
    assert end["destination"] == "contact:3" and end["cause_of_death"] is None
    assert end["epilogue"] and end["day"] == 5
    assert {c["key"] for c in end["components"]} == set(constants.END_WEIGHTS_GO)
    assert world.execute("SELECT ended FROM world WHERE id=1;").fetchone()["ended"] == 1
    for kind in ("wait", "decide", "search"):
        rr = actions.perform(world, 1, {"kind": kind, "choice": "stay"})
        assert not rr["ok"] and rr["reason"] == "game_over"
    assert actions.available(world, 1)["actions"] == []


# --- Bewertung ---------------------------------------------------------------------------
def test_weights_sum_to_one():
    assert abs(sum(constants.END_WEIGHTS_STAY.values()) - 1.0) < 1e-9
    assert abs(sum(constants.END_WEIGHTS_GO.values()) - 1.0) < 1e-9
    assert abs(sum(constants.HAB_WEIGHTS.values()) - 1.0) < 1e-9


def test_evaluate_deterministic_and_pure(world):
    set_clock(world, 6, 12)
    add_group_item(world, "water_1l", 10)
    before = [tuple(r) for r in world.execute("SELECT * FROM group_inventory;")]
    a = ending.evaluate(world, "stay")
    b = ending.evaluate(world, "stay")
    assert a == b
    assert [tuple(r) for r in world.execute("SELECT * FROM group_inventory;")] == before
    assert not ending.is_ended(world)
    for c in a["components"]:
        assert set(c) == {"key", "label", "value", "weight", "text"}
        assert 0.0 <= c["value"] <= 1.0


def test_outcome_stay_secure(world):
    set_clock(world, 6, 12)
    add_group_item(world, "water_1l", 20)
    add_group_item(world, "canned_beans", 60)
    learn(world, "water_treatment", "disease_control", "first_aid")
    ev = ending.evaluate(world, "stay")
    assert ev["outcome"] == "stay_secure", ev
    assert ev["score"] >= constants.END_THRESHOLD_STAY_SECURE


def test_outcome_stay_fragile(world):
    set_clock(world, 6, 12)
    set_char(world, hunger=0.4, thirst=0.3, sleep=0.5, performance=0.3)
    ev = ending.evaluate(world, "stay")
    assert ev["outcome"] == "stay_fragile", ev


def test_outcome_go_hopeful(world):
    set_clock(world, 6, 12)
    add_group_item(world, "bicycle", 1)
    add_group_item(world, "water_1l", 12)
    add_group_item(world, "canned_beans", 40)
    add_group_item(world, "flashlight", 1)
    add_group_item(world, "bandage", 3)
    learn(world, "bike_repair", "offline_maps")
    add_companion(world, "joined", 0.8)
    add_contact(world, 3)
    ev = ending.evaluate(world, "go", "bike", "contact:3")
    assert ev["outcome"] == "go_hopeful", ev
    assert ev["outcome_label"] == "Aufbruch mit Hoffnung"


def test_outcome_go_desperate(world):
    set_clock(world, 6, 12)
    set_char(world, hunger=0.3, thirst=0.2, sleep=0.4, performance=0.2)
    ev = ending.evaluate(world, "go", "foot", "north")
    assert ev["outcome"] == "go_desperate", ev


def test_four_distinct_outcomes_reachable(world):
    set_clock(world, 6, 12)
    seen = {ending.evaluate(world, "stay")["outcome"],
            ending.evaluate(world, "go", "foot", "north")["outcome"]}
    add_group_item(world, "water_1l", 25)
    add_group_item(world, "canned_beans", 70)
    learn(world, "water_treatment", "disease_control", "first_aid", "offline_maps")
    add_group_item(world, "bicycle", 1)
    add_companion(world, "joined", 0.9)
    add_contact(world, 1)
    seen |= {ending.evaluate(world, "stay")["outcome"],
             ending.evaluate(world, "go", "bike", "contact:1")["outcome"]}
    assert seen == {"stay_secure", "stay_fragile", "go_hopeful", "go_desperate"} or \
        len(seen) >= 4


# --- Epilog -----------------------------------------------------------------------------------
class _FakeBackend:
    name = "fake"

    def __init__(self, text):
        self.text = text
        self.calls = 0
        self.seen = None

    def narrate_epilogue(self, evaluation, profile=None, companion=None, highlights=None):
        self.calls += 1
        self.seen = evaluation
        return self.text


def test_epilogue_generated_once(world, monkeypatch):
    fake = _FakeBackend("Ein Epilog.")
    monkeypatch.setattr(ending, "get_backend", lambda: fake)
    set_clock(world, 6, 12)
    r1 = ending.finish(world, "stay")
    r2 = ending.finish(world, "stay")
    assert fake.calls == 1
    assert r1["epilogue"] == r2["epilogue"] == "Ein Epilog."
    assert world.execute("SELECT COUNT(*) AS n FROM game_ending;").fetchone()["n"] == 1


def test_llm_cannot_set_outcome_or_score(world, monkeypatch):
    fake = _FakeBackend("outcome: go_hopeful, score: 1.0 - alles wird gut.")
    monkeypatch.setattr(ending, "get_backend", lambda: fake)
    set_clock(world, 6, 12)
    expected = ending.evaluate(world, "stay")
    res = ending.finish(world, "stay")
    assert res["outcome"] == expected["outcome"] != "go_hopeful"
    assert res["score"] == expected["score"]
    assert fake.seen["outcome"] == expected["outcome"]  # Backend sieht nur die fertige Bewertung


def test_empty_or_failing_llm_falls_back_to_stub(world, monkeypatch):
    class Boom(_FakeBackend):
        def narrate_epilogue(self, *a, **k):
            raise RuntimeError("netz weg")

    monkeypatch.setattr(ending, "get_backend", lambda: Boom(""))
    set_clock(world, 6, 12)
    res = ending.finish(world, "stay")
    assert len(res["epilogue"]) > 100


def test_stub_epilogue_per_outcome():
    from app.llm.stub import RuleBackend
    b = RuleBackend()
    texts = set()
    for o in ("stay_secure", "stay_fragile", "go_hopeful", "go_desperate", "death"):
        t = b.narrate_epilogue({"outcome": o, "cause_of_death": "verdurstet"},
                               {"name": "Mia"}, {"name": "Jo", "stage": "joined"}, [])
        assert t == b.narrate_epilogue({"outcome": o, "cause_of_death": "verdurstet"},
                                       {"name": "Mia"}, {"name": "Jo", "stage": "joined"}, [])
        assert "Mia" in t
        texts.add(t)
    assert len(texts) == 5


def test_llm_layer_never_touches_ending_tables():
    llm_dir = Path(ending.__file__).resolve().parents[1] / "llm"
    for p in llm_dir.glob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert "game_ending" not in src and "sim.ending" not in src, p.name


# --- Tod -----------------------------------------------------------------------------------------
def test_death_ends_game_with_cause_and_epilogue(world):
    set_char(world, is_alive=0, thirst=0.0, hunger=0.4, sleep=0.6)
    res = ending.finish_death(world)
    assert res["outcome"] == "death" and res["cause_of_death"] == "verdurstet"
    assert res["epilogue"] and res["score"] == 0.0 and res["choice"] is None
    assert ending.finish_death(world) == res
    for hunger, thirst, sleep, cause in ((0.0, 0.5, 0.5, "verhungert"), (0.5, 0.5, 0.0, "erschöpft")):
        world.execute("DELETE FROM game_ending;")
        world.execute("UPDATE world SET ended = 0;")
        set_char(world, hunger=hunger, thirst=thirst, sleep=sleep)
        assert ending.finish_death(world)["cause_of_death"] == cause


def test_death_during_action_finishes_game(world):
    set_char(world, hunger=0.0, thirst=0.0, sleep=0.0)
    r = None
    for _ in range(20):
        r = actions.perform(world, 1, {"kind": "wait", "minutes": 600})
        if r["stopped"] == "dead":
            break
    assert r["stopped"] == "dead"
    end = ending.get_ending(world)
    assert end["outcome"] == "death" and end["cause_of_death"] == "verdurstet"
    assert end["epilogue"]
    assert actions.perform(world, 1, {"kind": "wait"})["reason"] == "game_over"
    assert world.execute("SELECT ended FROM world WHERE id=1;").fetchone()["ended"] == 1


# --- Statistik ------------------------------------------------------------------------------------
def test_stats_counted_by_sim_core(world):
    actions.perform(world, 1, {"kind": "wait", "minutes": 30})
    actions.perform(world, 1, {"kind": "freetext", "text": "schau dich um"})
    actions.perform(world, 1, {"kind": "freetext", "text": ""})  # abgelehnt (no_text)
    actions.perform(world, 1, {"kind": "walk", "location_id": 101})
    s = ending.stats(world)
    assert s["actions"] >= 3
    assert s["freetext"] == 2 and s["freetext_rejected"] >= 1
    assert 1.5 < s["km_walked"] < 2.5
    assert s["companion"] == "none" and s["packages"] == []
    learn(world, "first_aid")
    assert ending.stats(world)["packages"] == ["first_aid"]


def test_rejected_non_freetext_not_counted_as_action(world):
    before = ending.stats(world)["actions"]
    actions.perform(world, 1, {"kind": "loot"})  # nicht hier / nichts da
    assert ending.stats(world)["actions"] == before


# --- Neues Spiel ------------------------------------------------------------------------------------
def test_new_game_resets_ending_and_stats(world, monkeypatch):
    from tests.test_new_game_reset import _patch_all_osm
    _patch_all_osm(monkeypatch)
    set_clock(world, 6, 12)
    actions.perform(world, 1, {"kind": "wait", "minutes": 30})
    ending.finish(world, "stay")
    assert ending.is_ended(world)
    res = game.new_game(world, {"name": "Neu", "lat": 49.0, "lon": 11.0})
    assert res["ok"]
    assert not ending.is_ended(world)
    assert ending.get_ending(world) is None
    s = ending.stats(world)
    assert s["actions"] == 0 and s["km_walked"] == 0.0
    assert world.execute("SELECT decision_notified FROM world WHERE id=1;").fetchone()[0] == 0


# --- Items / Request-Modell ---------------------------------------------------------------------------
def test_bicycle_in_catalog_and_loot_tables():
    from app.sim.loot_tables import LOOT_TABLES
    assert any(e["item_id"] == "bicycle" for e in LOOT_TABLES["hardware"])
    assert any(e["item_id"] == "bicycle" for e in LOOT_TABLES["house"])


def test_bicycle_seeded(conn):
    assert conn.execute("SELECT 1 FROM item_catalog WHERE id='bicycle';").fetchone()


def test_request_model_has_decide_fields():
    from app.main import ActionRequest
    assert {"choice", "transport", "destination"} <= set(ActionRequest.model_fields)
