"""Slice M1-Politur (#51): Tragelast, Plünder-Priorität, Essen-Auto-Wahl,
Erzähltext, Anzeigenamen. Deterministisch, kein Netz."""
from __future__ import annotations

import os

os.environ["WASTELAND_LLM_BACKEND"] = "stub"

from app.sim import actions, constants, looting, movement
from tests.test_actions import (  # noqa: F401  (Fixtures/Helfer)
    add_group_item, add_loc_item, audit_clean, mark_searched, offline, set_char,
    world,
)


def test_carry_capacity_formula(world):
    set_char(world, weight_kg=70.0)
    assert looting.carry_capacity(world, 1) == 70.0 * constants.CARRY_BODY_FRACTION
    set_char(world, weight_kg=20.0)
    assert looting.carry_capacity(world, 1) == constants.CARRY_MIN_KG
    set_char(world, weight_kg=200.0)
    assert looting.carry_capacity(world, 1) == constants.CARRY_MAX_KG


def test_loot_respects_capacity_and_priority(world):
    set_char(world, weight_kg=70.0)  # 21 kg
    add_loc_item(world, 100, "water_1l", 45)
    add_loc_item(world, 100, "crowbar", 1)
    add_loc_item(world, 100, "canned_beans", 60)
    mark_searched(world, 100)
    r = actions.perform(world, 1, {"kind": "loot", "location_id": 100})
    assert r["ok"]
    # Wasser hat Vorrang, belegt aber höchstens LOOT_WATER_MAX_SHARE des freien Platzes;
    # der Rest geht an Essen (Rucksack nicht nur voll Wasser).
    water = float(int(21.0 * constants.LOOT_WATER_MAX_SHARE))
    beans = float(int((21.0 - water) / 0.4 + 1e-9))
    assert r["result"]["transferred"] == {"water_1l": water, "canned_beans": beans}
    assert movement.carried_weight(world, 1) <= 21.0 + 1e-9
    assert r["result"]["left"]["water_1l"] == 45.0 - water
    assert "crowbar" in r["result"]["left"]
    left = world.execute(
        "SELECT SUM(quantity) q FROM location_inventory WHERE item_id='water_1l';"
    ).fetchone()["q"]
    assert left == 45.0 - water
    assert "Wasser 1L" in r["narration"] and "lässt du zurück" in r["narration"]
    assert audit_clean(world)


def test_loot_takes_everything_when_it_fits(world):
    set_char(world, weight_kg=70.0)
    add_loc_item(world, 100, "crowbar", 1)
    add_loc_item(world, 100, "bandage", 2)
    add_loc_item(world, 100, "canned_beans", 5)
    mark_searched(world, 100)
    r = actions.perform(world, 1, {"kind": "loot", "location_id": 100})
    assert r["ok"] and r["result"]["left"] == {}
    assert "Hier ist nichts mehr übrig" in r["narration"]


def test_loot_full_backpack_rejected_unchanged(world):
    set_char(world, weight_kg=70.0)
    add_group_item(world, "water_1l", 21)
    add_loc_item(world, 100, "canned_beans", 3)
    mark_searched(world, 100)
    before = world.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
    r = actions.perform(world, 1, {"kind": "loot", "location_id": 100})
    assert not r["ok"] and r["reason"] == "too_heavy" and r["minutes_spent"] == 0
    assert world.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"] == before
    assert r["narration"]


def test_eat_without_item_picks_best_and_narrates(world):
    add_group_item(world, "canned_beans", 2)
    set_char(world, hunger=0.5)
    r = actions.perform(world, 1, {"kind": "eat"})
    assert r["ok"] and r["result"]["item"] == "canned_beans"
    assert r["narration"].startswith("Du isst Dose Bohnen")


def test_available_has_single_eat_with_options(world):
    add_group_item(world, "canned_beans", 2)
    add_group_item(world, "canned_meat", 1)
    set_char(world, hunger=0.5)
    acts = actions.available(world, 1)["actions"]
    eats = [a for a in acts if a["kind"] == "eat"]
    assert len(eats) == 1 and eats[0]["label"] == "Essen"
    assert {o["item_id"] for o in eats[0]["options"]} == {"canned_beans", "canned_meat"}
    assert len([a for a in acts if a["kind"] == "prepare"]) <= 1


def test_narration_for_wait_sleep_walk(world):
    w = actions.perform(world, 1, {"kind": "wait", "minutes": 90})
    assert w["narration"].startswith("Du wartest 1 Std. 30 min")
    set_char(world, sleep=0.3)
    s = actions.perform(world, 1, {"kind": "sleep"})
    assert s["narration"].startswith("Du schläfst")
    g = actions.perform(world, 1, {"kind": "walk", "location_id": 101})
    assert "Apotheke" in g["narration"]
