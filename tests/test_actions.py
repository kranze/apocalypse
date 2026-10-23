"""Tests für app/sim/actions.py — aktionsbasierte Zeit (Slice M1).

Deterministisch (fixer Seed, Stub-LLM, kein Netz, Luftlinien-Routing).
"""
from __future__ import annotations

import os

import pytest

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
import app.osm.roads as roads_module
from app.llm.base import proposal
from app.sim import actions, adjudicator, constants, ledger
from tests.conftest import insert_location

llm_mod.reset_backend()

HOME = (49.0, 11.0)
FAR_LAT = 49.018  # ~2 km nördlich -> ~25 Min Fußweg


class _NoGraph:
    """Kein Straßennetz -> set_destination fällt auf die Luftlinie zurück."""

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


def build_world(conn) -> None:
    """Spieler zu Hause; Ort 100 (Supermarkt) dort, Ort 101 (Apotheke) ~2 km entfernt."""
    conn.execute("UPDATE characters SET lat=?, lon=? WHERE id=1;", HOME)
    insert_location(conn, loc_id=100, loc_type="supermarket", name="Markt",
                    lat=HOME[0], lon=HOME[1])
    insert_location(conn, loc_id=101, loc_type="pharmacy", name="Apotheke",
                    lat=FAR_LAT, lon=HOME[1])
    conn.commit()


def add_group_item(conn, item_id, qty, group_id=1):
    conn.execute(
        "INSERT INTO group_inventory (group_id,item_id,quantity,quality,acquired_tick) "
        "VALUES (?,?,?,1.0,0) ON CONFLICT(group_id,item_id,quality) DO UPDATE SET "
        "quantity = quantity + excluded.quantity;",
        (group_id, item_id, qty),
    )
    ledger.add(conn, item_id, qty)
    conn.commit()


def add_loc_item(conn, loc_id, item_id, qty):
    conn.execute(
        "INSERT INTO location_inventory (location_id,item_id,quantity,quality,produced_tick) "
        "VALUES (?,?,?,1.0,0);", (loc_id, item_id, qty),
    )
    ledger.add(conn, item_id, qty)
    conn.commit()


def mark_searched(conn, loc_id):
    """Ort gilt als durchsucht (Inventar manuell gesetzt, keine Generierung mehr)."""
    conn.execute(
        "INSERT OR IGNORE INTO location_searches (location_id, term, created_tick) "
        "VALUES (?, '*', 0);", (loc_id,),
    )
    conn.commit()


def set_char(conn, **cols):
    for k, v in cols.items():
        conn.execute(f"UPDATE characters SET {k}=? WHERE id=1;", (v,))
    conn.commit()


def tick_of(conn):
    return conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]


def snapshot(conn):
    return (
        dict(conn.execute("SELECT * FROM characters WHERE id=1;").fetchone()),
        [tuple(r) for r in conn.execute(
            "SELECT group_id,item_id,quantity,quality FROM group_inventory "
            "ORDER BY group_id,item_id,quality;")],
        [tuple(r) for r in conn.execute(
            "SELECT location_id,item_id,quantity,quality FROM location_inventory "
            "ORDER BY location_id,item_id,quality;")],
        tick_of(conn),
    )


def audit_clean(conn) -> bool:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM resource_audit WHERE flagged = 1;"
    ).fetchone()["n"] == 0


@pytest.fixture
def world(conn):
    build_world(conn)
    return conn


# --- Walk ---------------------------------------------------------------
def test_walk_stops_on_arrival_and_sets_location(world):
    r = actions.perform(world, 1, {"kind": "walk", "location_id": 101})
    assert r["ok"] and r["stopped"] == "arrived"
    assert r["arrived_location_id"] == 101
    assert 20 <= r["minutes_spent"] <= 40
    assert r["tick"] == r["minutes_spent"] == tick_of(world)
    assert r["path"] and r["path"][-1] == [FAR_LAT, HOME[1]]
    pl = world.execute("SELECT path_json, lat FROM characters WHERE id=1;").fetchone()
    assert pl["path_json"] is None and pl["lat"] == pytest.approx(FAR_LAT)
    status = world.execute("SELECT discovery_status FROM locations WHERE id=101;").fetchone()
    assert status["discovery_status"] == "discovered"
    assert r["player"]["id"] == 1 and r["day"] == 0


def test_walk_to_coordinates_and_max_stop(world, monkeypatch):
    monkeypatch.setattr(constants, "WALK_MAX_MINUTES", 10)
    r = actions.perform(world, 1, {"kind": "walk", "lat": FAR_LAT, "lon": HOME[1]})
    assert r["ok"] and r["stopped"] == "max" and r["minutes_spent"] == 10
    assert r["arrived_location_id"] is None


def test_walk_already_there_costs_nothing(world):
    r = actions.perform(world, 1, {"kind": "walk", "location_id": 100})
    assert r["ok"] and r["stopped"] == "arrived" and r["minutes_spent"] == 0
    assert r["arrived_location_id"] == 100 and tick_of(world) == 0


def test_walk_rejects_unknown_target(world):
    before = snapshot(world)
    for act in ({"kind": "walk", "location_id": 999}, {"kind": "walk"}):
        r = actions.perform(world, 1, act)
        assert not r["ok"] and r["minutes_spent"] == 0 and r["hint"]
    assert snapshot(world) == before


# --- Loot ---------------------------------------------------------------
def test_loot_transfers_and_costs_time(world):
    add_loc_item(world, 100, "canned_beans", 3)
    r = actions.perform(world, 1, {"kind": "loot", "location_id": 100})
    assert r["ok"] and r["stopped"] == "done"
    assert r["result"]["transferred"] == {"canned_beans": 3.0}
    assert constants.LOOT_MIN_MINUTES <= r["minutes_spent"] <= constants.LOOT_MAX_MINUTES
    assert tick_of(world) == r["minutes_spent"]
    got = world.execute(
        "SELECT quantity FROM group_inventory WHERE item_id='canned_beans';"
    ).fetchone()
    assert got["quantity"] >= 2.0  # nach Zeit evtl. via Auto-Versorgung angegessen
    assert audit_clean(world)


def test_loot_cost_scales_with_footprint(world):
    assert actions._loot_minutes(10) == constants.LOOT_MIN_MINUTES
    assert actions._loot_minutes(5000) == constants.LOOT_MAX_MINUTES
    mid = actions._loot_minutes(260)
    assert constants.LOOT_MIN_MINUTES < mid < constants.LOOT_MAX_MINUTES


def test_loot_not_here_rejected_unchanged(world):
    add_loc_item(world, 101, "canned_beans", 3)
    before = snapshot(world)
    r = actions.perform(world, 1, {"kind": "loot", "location_id": 101})
    assert not r["ok"] and r["reason"] == "not_here" and r["minutes_spent"] == 0
    assert r["hint"]
    assert snapshot(world) == before


def test_loot_unsearched_rejected(world):
    before = snapshot(world)
    r = actions.perform(world, 1, {"kind": "loot", "location_id": 100})
    assert not r["ok"] and r["reason"] == "not_searched"
    assert snapshot(world) == before


def test_loot_empty_rejected(world):
    mark_searched(world, 100)
    before = snapshot(world)
    r = actions.perform(world, 1, {"kind": "loot", "location_id": 100})
    assert not r["ok"] and r["reason"] == "empty"
    assert snapshot(world) == before


# --- Eat / Prepare ------------------------------------------------------
def test_eat_happy_path(world):
    add_group_item(world, "canned_beans", 2)
    set_char(world, hunger=0.4)
    r = actions.perform(world, 1, {"kind": "eat", "item_id": "canned_beans"})
    assert r["ok"] and r["minutes_spent"] == constants.EAT_MINUTES
    assert r["stopped"] == "done" and r["result"]["item"] == "canned_beans"
    assert r["player"]["hunger"] > 0.4
    assert tick_of(world) == constants.EAT_MINUTES
    assert audit_clean(world)


def test_eat_rejections_leave_world_unchanged(world):
    set_char(world, hunger=0.4)
    before = snapshot(world)
    r = actions.perform(world, 1, {"kind": "eat", "item_id": "canned_beans"})
    assert not r["ok"] and r["reason"] == "no_food" and r["minutes_spent"] == 0
    assert snapshot(world) == before
    add_group_item(world, "canned_beans", 1)
    set_char(world, hunger=1.0)
    before = snapshot(world)
    r = actions.perform(world, 1, {"kind": "eat", "item_id": "canned_beans"})
    assert not r["ok"] and r["reason"] == "not_hungry"
    assert snapshot(world) == before


def test_prepare_happy_path_and_rejection(world):
    add_group_item(world, "pasta_500g", 1)
    before = snapshot(world)
    r = actions.perform(world, 1, {"kind": "prepare", "item_id": "pasta_500g"})
    assert not r["ok"] and r["reason"] in ("no_water", "no_heat") and r["minutes_spent"] == 0
    assert snapshot(world) == before
    add_group_item(world, "water_1l", 2)
    add_group_item(world, "firewood", 2)
    r = actions.perform(world, 1, {"kind": "prepare", "item_id": "pasta_500g"})
    assert r["ok"] and r["minutes_spent"] == constants.PREPARE_MINUTES
    assert r["result"]["prepared"] == "meal_pasta"
    assert audit_clean(world)


# --- Wait / Sleep -------------------------------------------------------
def test_wait_runs_through_and_clamps(world):
    r = actions.perform(world, 1, {"kind": "wait", "minutes": 45})
    assert r["ok"] and r["minutes_spent"] == 45 and r["stopped"] == "done"
    assert tick_of(world) == 45  # 10er-Schritte + Rest 5
    r = actions.perform(world, 1, {"kind": "wait", "minutes": 1})
    assert r["minutes_spent"] == constants.WAIT_MIN_MINUTES
    r = actions.perform(world, 1, {"kind": "wait", "minutes": 10**6})
    assert r["minutes_spent"] <= constants.WAIT_MAX_MINUTES


def test_wait_stops_on_halting_interrupt(world):
    set_char(world, hunger=0.5005)  # kreuzt 0.5 im ersten Tick -> "wird hungrig"
    r = actions.perform(world, 1, {"kind": "wait", "minutes": 120})
    assert r["ok"] and r["stopped"] == "interrupt"
    assert r["minutes_spent"] == constants.TICK_MINUTES
    assert any(i["severity"] in ("soft", "decision") for i in r["interrupts"])


def test_sleep_raises_sleep_to_target_even_above_rest_threshold(world):
    set_char(world, sleep=0.6)  # über SLEEP_REST_BELOW: Auto-Ruhen würde NICHT greifen
    assert 0.6 > constants.SLEEP_REST_BELOW
    r = actions.perform(world, 1, {"kind": "sleep"})
    assert r["ok"] and r["stopped"] == "done"
    assert r["player"]["sleep"] >= constants.SLEEP_TARGET
    assert 0 < r["minutes_spent"] < constants.SLEEP_MAX_MINUTES
    assert audit_clean(world)


def test_sleep_rejected_when_rested_and_stops_at_max(world, monkeypatch):
    before = snapshot(world)
    r = actions.perform(world, 1, {"kind": "sleep"})
    assert not r["ok"] and r["reason"] == "not_tired" and snapshot(world) == before
    monkeypatch.setattr(constants, "SLEEP_MAX_MINUTES", 30)
    set_char(world, sleep=0.2)
    r = actions.perform(world, 1, {"kind": "sleep"})
    assert r["ok"] and r["stopped"] == "max" and r["minutes_spent"] == 30


def test_default_tick_does_not_rest_above_threshold(world):
    from app.sim import tick
    set_char(world, sleep=0.8)
    tick.advance_tick(world, 60)
    assert world.execute("SELECT sleep FROM characters WHERE id=1;").fetchone()["sleep"] < 0.8


# --- Freetext -----------------------------------------------------------
class _FakeBackend:
    name = "fake"

    def __init__(self, prop):
        self.prop = prop

    def interpret(self, text, context):
        return self.prop


def test_freetext_default_minutes_without_time_effect(world):
    r = actions.perform(world, 1, {"kind": "freetext", "text": "Schau dich um"})
    assert r["ok"] and r["narration"]
    assert r["minutes_spent"] == constants.FREETEXT_DEFAULT_MINUTES
    assert tick_of(world) == constants.FREETEXT_DEFAULT_MINUTES
    assert "effects_applied" in r["result"]
    import json
    json.dumps(r)  # Antwort muss JSON-serialisierbar sein


def test_freetext_advance_time_clamped_and_chunked(world, monkeypatch):
    fake = _FakeBackend(proposal(effects=[{"op": "advance_time", "minutes": 100000}],
                                 narration="Du wartest sehr lange."))
    monkeypatch.setattr(adjudicator, "get_backend", lambda: fake)
    r = actions.perform(world, 1, {"kind": "freetext", "text": "warte ewig"})
    assert r["ok"] and r["minutes_spent"] == constants.FREETEXT_MAX_MINUTES
    assert tick_of(world) == constants.FREETEXT_MAX_MINUTES


def test_freetext_move_to_continues_like_walk(world):
    r = actions.perform(world, 1, {"kind": "freetext", "text": "Geh zur Apotheke"})
    assert r["ok"] and r["stopped"] == "arrived"
    assert r["arrived_location_id"] == 101 and r["path"]
    assert r["minutes_spent"] >= 20


def test_freetext_rejection_and_empty_text(world):
    before = snapshot(world)
    r = actions.perform(world, 1, {"kind": "freetext", "text": "Reaktiviere das Mobilfunknetz"})
    assert not r["ok"] and r["minutes_spent"] == 0 and r["hint"]
    assert r["result"]["escalate"] is True
    r = actions.perform(world, 1, {"kind": "freetext", "text": "  "})
    assert not r["ok"] and r["reason"] == "no_text"
    assert tick_of(world) == before[3]


def test_freetext_override_passthrough(world):
    add_group_item(world, "pasta_500g", 1)
    add_group_item(world, "water_1l", 2)
    add_group_item(world, "crowbar", 1)
    r = actions.perform(world, 1, {"kind": "freetext", "text": "koche Nudeln"})
    assert not r["ok"] and r["reason"] == "no_heat"
    r = actions.perform(world, 1, {
        "kind": "freetext", "text": "koche Nudeln",
        "override_text": "koche Nudeln", "override_reason": "Ich verbrenne den Kuhfuß",
    })
    assert r["result"].get("override_learned", {}).get("key") == "crowbar"


# --- Allgemein ----------------------------------------------------------
def test_dead_character_and_unknown_kind(world):
    set_char(world, is_alive=0)
    r = actions.perform(world, 1, {"kind": "wait", "minutes": 60})
    assert not r["ok"] and r["reason"] == "dead" and r["minutes_spent"] == 0
    set_char(world, is_alive=1)
    r = actions.perform(world, 1, {"kind": "dance"})
    assert not r["ok"] and r["reason"] == "unknown_kind"


def test_available_lists_sensible_actions(world):
    add_loc_item(world, 100, "canned_beans", 2)
    mark_searched(world, 100)
    add_group_item(world, "canned_beans", 1)
    set_char(world, hunger=0.6, sleep=0.5)
    av = actions.available(world, 1)
    assert av["here_location_id"] == 100
    kinds = [a["kind"] for a in av["actions"]]
    assert {"loot", "eat", "wait", "sleep"} <= set(kinds)
    eat = next(a for a in av["actions"] if a["kind"] == "eat")
    assert eat["label"] == "Essen: Dose Bohnen" and eat["item_id"] == "canned_beans"
    morning = next(a for a in av["actions"] if a["label"] == "Bis 7 Uhr warten")
    assert morning["minutes"] == 60  # Start 06:00 -> 07:00
    assert next(a for a in av["actions"] if a["kind"] == "sleep")["minutes"] is None
    set_char(world, hunger=0.9, sleep=0.9)
    far = actions.perform(world, 1, {"kind": "walk", "location_id": 101})
    assert far["ok"]
    av = actions.available(world, 1)
    assert av["here_location_id"] == 101
    assert "loot" not in [a["kind"] for a in av["actions"]]


def test_world_clock(world):
    c = actions.world_clock(world)
    assert c["day"] == 0 and c["time_of_day"] == "06:00"
    actions.perform(world, 1, {"kind": "wait", "minutes": 120})
    assert actions.world_clock(world)["time_of_day"] == "08:00"
    assert actions.minutes_until_morning(world) == 23 * 60


# --- Determinismus & Woche ----------------------------------------------
def _scripted_run(c):
    build_world(c)
    add_group_item(c, "canned_beans", 6)
    add_group_item(c, "water_1l", 4)
    add_loc_item(c, 101, "canned_meat", 3)
    set_char(c, hunger=0.7, sleep=0.5)
    out = []
    for act in (
        {"kind": "walk", "location_id": 101},
        {"kind": "loot", "location_id": 101},
        {"kind": "eat", "item_id": "canned_meat"},
        {"kind": "wait", "minutes": 90},
        {"kind": "sleep"},
        {"kind": "walk", "location_id": 100},
    ):
        r = actions.perform(c, 1, act)
        out.append((r["ok"], r["minutes_spent"], r["stopped"]))
    return out, snapshot(c)


def test_same_seed_same_actions_same_state(conn_seeded):
    a, b = conn_seeded("a"), conn_seeded("b")
    assert _scripted_run(a) == _scripted_run(b)
    assert audit_clean(a)


def test_seven_days_survival_scripted(world):
    add_group_item(world, "canned_beans", 40)
    add_group_item(world, "water_1l", 40)
    add_loc_item(world, 101, "canned_meat", 5)
    horizon = constants.DAYS_TOTAL * constants.MINUTES_PER_DAY
    cycle = (
        {"kind": "walk", "location_id": 101},
        {"kind": "loot", "location_id": 101},
        {"kind": "walk", "location_id": 100},
        {"kind": "wait", "minutes": 480},
        {"kind": "sleep"},
        {"kind": "wait", "minutes": 240},
    )
    for _ in range(300):
        for act in cycle:
            r = actions.perform(world, 1, act)
            assert r["stopped"] != "dead", r
        if tick_of(world) >= horizon:
            break
    assert tick_of(world) >= horizon
    assert actions.world_clock(world)["day"] >= constants.DAYS_TOTAL
    assert world.execute("SELECT is_alive FROM characters WHERE id=1;").fetchone()["is_alive"]
    assert audit_clean(world)


# --- Playtest-Fixes (#45) ------------------------------------------------
def _set_tick(conn, t):
    conn.execute("UPDATE world SET tick=? WHERE id=1;", (t,))
    conn.commit()


def test_day_counts_calendar_days(world):
    from datetime import datetime, timedelta
    start = "2026-09-01T06:00:00"
    assert actions.calendar_day(start, 0) == 0
    assert actions.calendar_day(start, 17 * 60 + 59) == 0       # 23:59
    assert actions.calendar_day(start, 18 * 60) == 1            # 00:00
    assert actions.calendar_day(start, 18 * 60 + 97) == 1       # 01:37
    assert actions.calendar_day(start, 24 * 60) == 1            # 06:00 Tag 2
    _set_tick(world, 18 * 60 + 97)
    assert actions.world_clock(world)["day"] == 1
    r = actions.perform(world, 1, {"kind": "wait", "minutes": 30})
    assert r["day"] == 1
    import app.main as main
    assert main.actions_mod.calendar_day(start, 18 * 60 + 97) == 1


def test_morning_option_label_and_threshold(world):
    def morning(av):
        return [a for a in av["actions"] if a["label"] in ("Bis 7 Uhr warten", "Bis morgen früh")]
    _set_tick(world, 8)  # 06:08
    m = morning(actions.available(world, 1))
    assert m[0]["label"] == "Bis 7 Uhr warten" and m[0]["minutes"] == 52
    _set_tick(world, 55)  # 06:55 -> nur 5 Min -> versteckt
    assert morning(actions.available(world, 1)) == []
    _set_tick(world, 60)  # 07:00 -> bis morgen 07:00
    m = morning(actions.available(world, 1))
    assert m[0]["label"] == "Bis morgen früh" and m[0]["minutes"] == 24 * 60         or m[0]["minutes"] == constants.WAIT_MAX_MINUTES
    _set_tick(world, 18 * 60 + 97)  # 01:37 -> noch heute 07:00
    m = morning(actions.available(world, 1))
    assert m[0]["label"] == "Bis 7 Uhr warten" and m[0]["minutes"] == 5 * 60 + 23


def test_interrupts_deduplicated_in_response():
    ev = [
        {"severity": "soft", "message": "A wird müde."},
        {"severity": "soft", "message": "B wird durstig."},
        {"severity": "soft", "message": "A wird müde."},
        {"severity": "info", "message": "x"},
    ]
    out = actions._public_interrupts(ev)
    assert [i["message"] for i in out] == ["A wird müde.", "B wird durstig."]


def test_tiredness_interrupt_not_repeated_by_resting_oscillation(world):
    """Idle-Ruhen unter 0.5 + Schlafdruck pendelte um die Schwelle: früher
    feuerte 'wird müde' alle ~3 Ticks erneut."""
    from app.sim import tick
    set_char(world, sleep=0.5004)
    n = 0
    for _ in range(40):
        r = tick.advance_tick(world, 10)  # untätig -> Auto-Ruhen
        n += sum("müde" in e["message"] for e in r["interrupts"])
    assert n == 0
    # Gehende (Route gesetzt) ermüden dagegen genau einmal pro Kreuzung.
    set_char(world, sleep=0.5004)
    world.execute("UPDATE characters SET path_json='[]' WHERE id=1;")
    world.commit()
    from app.sim import biology
    ev = biology.apply_sleep(world, 10, 10)
    assert sum("müde" in e["message"] for e in ev) == 1


def test_npc_need_events_do_not_interrupt_player(world):
    world.execute(
        "INSERT INTO characters (name,type,lat,lon,hunger,thirst,sleep,injury,exposure,"
        "satisfaction,performance,is_alive,daily_kcal,daily_water_l) "
        "VALUES ('Martin Werner','survivor',49.0,11.0,1,1,1,1,1,1,1,1,2000,2.0);"
    )
    npc = world.execute("SELECT MAX(id) AS i FROM characters;").fetchone()["i"]
    world.execute("UPDATE characters SET thirst=0.5005 WHERE id=?;", (npc,))
    world.commit()
    r = actions.perform(world, 1, {"kind": "wait", "minutes": 60})
    assert r["stopped"] == "done" and r["interrupts"] == []
    # Das Event existiert trotzdem in der DB (nur nicht als Spieler-Interrupt).
    assert world.execute(
        "SELECT COUNT(*) AS n FROM events WHERE message LIKE 'Martin Werner wird durstig%';"
    ).fetchone()["n"] == 1


def test_sleep_continues_through_soft_need_but_stops_on_critical(world):
    set_char(world, sleep=0.3, thirst=0.5005, hunger=0.5005)
    r = actions.perform(world, 1, {"kind": "sleep"})
    assert r["stopped"] == "done" and r["player"]["sleep"] >= constants.SLEEP_TARGET
    assert any("durstig" in i["message"] for i in r["interrupts"])  # gemeldet, aber kein Stopp
    # kritisch: Schlaf bricht ab
    set_char(world, sleep=0.3, thirst=0.2005, hunger=1.0)
    r = actions.perform(world, 1, {"kind": "sleep"})
    assert r["stopped"] == "interrupt"
    assert any("kritisch durstig" in i["message"] for i in r["interrupts"])
    # wait bleibt: weiche Warnung stoppt
    set_char(world, thirst=0.5005, hunger=1.0, sleep=1.0)
    r = actions.perform(world, 1, {"kind": "wait", "minutes": 120})
    assert r["stopped"] == "interrupt"
