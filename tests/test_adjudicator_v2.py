"""Tests Adjudikator v2 (Slice M3, Issue #47/#6): Tool-Use-Schleife mit gestagter
Validierung, atomarem Anwenden, erweitertem Vokabular, verallgemeinertem Override.

Offline & deterministisch: Stub-Backend bzw. skriptgesteuerte Test-Backends,
fake Anthropic-Client. Kein Netz.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
import app.osm.roads as roads_module
from app.llm import lore, tools as llm_tools
from app.llm.base import EFFECT_OPS, LLMBackend
from app.sim import (actions, adjudicator, audit, capabilities, constants, effects, kb,
                     ledger, requirements)
from app.sim.toolbox import AdjudicationToolbox
from tests.conftest import insert_location, make_conn, set_seed

HOME = (49.0, 11.0)


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
    c.execute("UPDATE characters SET lat=?, lon=? WHERE id=1;", HOME)
    kb.seed_defaults(c)
    c.commit()
    yield c
    c.close()


# --- Helfer ------------------------------------------------------------
def give(conn, item, qty, gid=1):
    conn.execute(
        "INSERT INTO group_inventory (group_id,item_id,quantity,quality,acquired_tick) "
        "VALUES (?,?,?,1.0,0) ON CONFLICT(group_id,item_id,quality) DO UPDATE SET "
        "quantity = quantity + excluded.quantity;", (gid, item, qty))
    ledger.add(conn, item, qty)
    conn.commit()


def owned(conn, item, gid=1):
    return conn.execute(
        "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory "
        "WHERE group_id=? AND item_id=?;", (gid, item)).fetchone()["q"]


def place(conn, loc_id=100, loc_type="supermarket", items=()):
    """Entdeckter + durchsuchter Ort am Standort des Spielers mit festem Inventar."""
    insert_location(conn, loc_id=loc_id, loc_type=loc_type, name="Markt",
                    lat=HOME[0], lon=HOME[1])
    conn.execute("UPDATE locations SET discovery_status='discovered' WHERE id=?;", (loc_id,))
    conn.execute("INSERT INTO location_searches (location_id, term, created_tick) "
                 "VALUES (?, '*', 0);", (loc_id,))
    for item, qty in items:
        conn.execute("INSERT INTO location_inventory (location_id,item_id,quantity,quality,"
                     "produced_tick) VALUES (?,?,?,1.0,0);", (loc_id, item, qty))
        ledger.add(conn, item, qty)
    conn.commit()
    return loc_id


_TABLES = ("group_inventory", "location_inventory", "characters", "resource_ledger",
           "events", "world", "capabilities", "knowledge_base")


def dump(conn):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1;")]
            for t in _TABLES}


def audit_clean(conn):
    now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
    with conn:
        flags = audit.run_audit(conn, now)
    return [f for f in flags if f["category"] == "system"] == []


class ScriptBackend(LLMBackend):
    """Test-Backend: führt ein Skript von Werkzeugaufrufen aus.
    Skript = Liste von Runden; Runde = Liste von (tool, args)."""
    name = "script"

    def __init__(self, rounds, finish=None):
        self.rounds = rounds
        self.finish = finish or {"understanding": "u", "feasibility": "feasible",
                                 "reason": "", "narration": "Fertig."}
        self.results = []

    def run_adjudication(self, text, context, toolbox, max_iterations=8):
        for rnd in self.rounds:
            if not toolbox.begin_turn():
                return {"stop": "limit"}
            for name, args in rnd:
                self.results.append(toolbox.call(name, args))
        if not toolbox.begin_turn():
            return {"stop": "limit"}
        toolbox.call("finish", self.finish)
        return {"stop": "finish"}


def use_backend(monkeypatch, backend):
    monkeypatch.setattr(adjudicator, "get_backend", lambda: backend)


# --- Erfolg / Ablehnung / Umplanen (Stub) -----------------------------------
class TestStubLoop:
    def test_success(self, conn):
        give(conn, "pasta_500g", 1)
        give(conn, "water_1l", 1)
        give(conn, "firewood", 1)
        v = adjudicator.adjudicate(conn, 1, "koche nudeln")
        assert v["ok"] and [a["op"] for a in v["effects_applied"]] == ["prepare"]
        assert owned(conn, "meal_pasta") == 1.0
        assert audit_clean(conn)

    def test_rejection_leaves_db_unchanged(self, conn):
        give(conn, "pasta_500g", 1)
        give(conn, "water_1l", 1)
        before = dump(conn)
        v = adjudicator.adjudicate(conn, 1, "koche nudeln")
        assert not v["ok"] and v["reason"] == "no_heat" and v["escalate"] and v["hint"]
        assert dump(conn) == before

    def test_replan_fill_water_then_prepare(self, conn):
        """prepare scheitert an no_water -> Stub füllt Wasser ab und versucht erneut;
        das Wasser aus dem Staging zählt für das zweite prepare."""
        give(conn, "pasta_500g", 1)
        give(conn, "firewood", 1)
        place(conn, items=[("water_1l", 3.0)])
        v = adjudicator.adjudicate(conn, 1, "koche nudeln")
        assert v["ok"], v
        assert [a["op"] for a in v["effects_applied"]] == ["fill_water", "prepare"]
        assert owned(conn, "meal_pasta") == 1.0
        assert owned(conn, "water_1l") == pytest.approx(2.5)
        assert audit_clean(conn)

    def test_replan_fails_when_retry_rejected(self, conn):
        """Auch nach Wasser fehlt Hitze -> Gesamtplan scheitert, nichts geschrieben."""
        give(conn, "pasta_500g", 1)
        place(conn, items=[("water_1l", 3.0)])
        before = dump(conn)
        v = adjudicator.adjudicate(conn, 1, "koche nudeln")
        assert not v["ok"] and v["reason"] == "no_heat"
        assert dump(conn) == before

    def test_too_complex_and_narration_only(self, conn):
        v = adjudicator.adjudicate(conn, 1, "reaktiviere das mobilfunknetz")
        assert not v["ok"] and v["feasibility"] == "too_complex" and v["escalate"]
        v = adjudicator.adjudicate(conn, 1, "schau dich um")
        assert v["ok"]


# --- Schleifen-Mechanik (skriptgesteuert) ---------------------------------------
class TestLoopMechanics:
    def test_iteration_limit(self, conn, monkeypatch):
        class Endless(LLMBackend):
            name = "endless"

            def run_adjudication(self, text, context, toolbox, max_iterations=8):
                while toolbox.begin_turn():
                    toolbox.call("get_inventory", {})
                return {"stop": "limit"}

        use_backend(monkeypatch, Endless())
        before = dump(conn)
        v = adjudicator.adjudicate(conn, 1, "irgendwas")
        assert not v["ok"] and v["reason"] == "iteration_limit" and v["escalate"]
        assert dump(conn) == before

    def test_iteration_limit_is_module_tunable(self, conn, monkeypatch):
        monkeypatch.setattr(adjudicator, "MAX_ITERATIONS", 2)
        seen = {}

        class Probe(LLMBackend):
            def run_adjudication(self, text, context, toolbox, max_iterations=8):
                seen["max"] = max_iterations
                return {"stop": "limit"}

        use_backend(monkeypatch, Probe())
        adjudicator.adjudicate(conn, 1, "x")
        assert seen["max"] == 2

    def test_backend_error_is_escalated(self, conn, monkeypatch):
        class Boom(LLMBackend):
            def run_adjudication(self, *a, **k):
                raise RuntimeError("api down")

        use_backend(monkeypatch, Boom())
        v = adjudicator.adjudicate(conn, 1, "x")
        assert not v["ok"] and v["reason"] == "llm_error" and v["escalate"]

    def test_staged_double_consumption_rejected(self, conn):
        give(conn, "canned_beans", 1)
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        assert tb.call("propose_consume_food", {})["ok"]
        r = tb.call("propose_consume_food", {})
        assert not r["ok"] and r["reason"] == "no_food"
        assert len(tb.plan) == 1

    def test_staged_double_transform_and_prepare_rejected(self, conn):
        give(conn, "canned_beans", 1)
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        t = {"consume": [{"item": "canned_beans", "qty": 1}],
             "produce": [{"item": "water_1l", "qty": 1}], "requires": []}
        assert tb.call("propose_transform", t)["ok"]
        assert tb.call("propose_transform", t)["reason"] == "missing:canned_beans"
        give(conn, "pasta_500g", 1)
        give(conn, "water_1l", 5)
        give(conn, "firewood", 5)
        assert tb.call("propose_prepare", {})["ok"]
        assert tb.call("propose_prepare", {})["reason"] == "nothing_to_prepare"

    def test_staged_gain_enables_later_proposal(self, conn):
        """Beute aus transfer (durchsuchter Ort) steht späteren Vorschlägen zur Verfügung."""
        place(conn, items=[("canned_beans", 2.0)])
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        assert not tb.call("propose_consume_food", {})["ok"]
        assert tb.call("propose_transfer", {"target": "supermarket", "items": None})["ok"]
        assert tb.call("propose_consume_food", {})["ok"]
        # nichts wurde geschrieben
        assert owned(conn, "canned_beans") == 0

    def test_staged_location_stock_not_taken_twice(self, conn):
        place(conn, items=[("water_1l", 2.0)])
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        assert tb.call("propose_transfer", {"target": "supermarket", "items": None})["ok"]
        r = tb.call("propose_fill_water", {"liters": None})
        assert not r["ok"] and r["reason"] == "no_water_source"

    def test_atomic_rollback_on_exception(self, conn, monkeypatch):
        give(conn, "canned_beans", 2)
        before = dump(conn)

        def boom(*a, **k):
            raise RuntimeError("applier crash")

        monkeypatch.setitem(effects._APPLIERS, "narrate", boom)
        use_backend(monkeypatch, ScriptBackend([[
            ("propose_consume_food", {"item": None}), ("propose_narrate", {})]]))
        v = adjudicator.adjudicate(conn, 1, "iss was und erzähl")
        assert not v["ok"] and v["reason"].startswith("apply_error") and v["escalate"]
        d = dump(conn)
        assert d == before  # Essen, Hunger, Ledger, Events: alles zurückgerollt
        assert conn.execute("SELECT COUNT(*) AS n FROM chat_log;").fetchone()["n"] == 2

    def test_atomic_rollback_on_soft_failure(self, conn, monkeypatch):
        give(conn, "canned_beans", 2)
        before = dump(conn)
        monkeypatch.setitem(effects._APPLIERS, "narrate",
                            lambda *a, **k: {"ok": False, "reason": "boom"})
        use_backend(monkeypatch, ScriptBackend([[
            ("propose_consume_food", {}), ("propose_narrate", {})]]))
        v = adjudicator.adjudicate(conn, 1, "x")
        assert not v["ok"] and v["reason"] == "boom"
        assert dump(conn) == before

    def test_atomic_success_multi_effect(self, conn, monkeypatch):
        give(conn, "canned_beans", 2)
        use_backend(monkeypatch, ScriptBackend([[
            ("propose_consume_food", {}), ("propose_narrate", {})]]))
        v = adjudicator.adjudicate(conn, 1, "x")
        assert v["ok"] and owned(conn, "canned_beans") == 1.0
        assert audit_clean(conn)

    def test_replan_after_rejection_applies_accepted_plan(self, conn, monkeypatch):
        give(conn, "canned_beans", 1)
        be = ScriptBackend([[("propose_prepare", {})], [("propose_consume_food", {})]])
        use_backend(monkeypatch, be)
        v = adjudicator.adjudicate(conn, 1, "x")
        assert not be.results[0]["ok"] and be.results[0]["reason"] == "nothing_to_prepare"
        assert v["ok"] and owned(conn, "canned_beans") == 0

    def test_last_proposal_rejected_fails_whole(self, conn, monkeypatch):
        give(conn, "canned_beans", 1)
        be = ScriptBackend([[("propose_consume_food", {}), ("propose_prepare", {})]])
        use_backend(monkeypatch, be)
        v = adjudicator.adjudicate(conn, 1, "x")
        assert not v["ok"] and v["reason"] == "nothing_to_prepare"
        assert owned(conn, "canned_beans") == 1.0

    def test_single_shot_duck_typed_backend(self, conn, monkeypatch):
        """Backend nur mit ``interpret`` (kein run_adjudication): gleiche
        gestagte Validierung; eine Ablehnung verwirft den Plan."""
        give(conn, "canned_beans", 1)

        class Old:
            name = "old"

            def interpret(self, text, context):
                return {"understanding": "", "feasibility": "feasible", "reason": "",
                        "narration": "Du isst zweimal.",
                        "effects": [{"op": "consume_food"}, {"op": "consume_food"}]}

        use_backend(monkeypatch, Old())
        v = adjudicator.adjudicate(conn, 1, "iss zweimal")
        assert not v["ok"] and v["reason"] == "no_food"
        assert owned(conn, "canned_beans") == 1.0

    def test_unknown_tool_and_bad_op(self, conn):
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        assert tb.call("hack_db", {})["reason"].startswith("unknown_tool")
        assert tb.call("propose_teleport", {})["reason"].startswith("unknown_tool")
        assert tb.call("propose_move_to", {"target": "nirgendwo"})["reason"] == "no_target"
        assert tb.plan == []

    def test_finish_blocks_further_calls(self, conn):
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        tb.call("finish", {"feasibility": "bogus", "narration": "n"})
        assert tb.finished["feasibility"] == "feasible"
        assert tb.call("propose_narrate", {})["reason"] == "already_finished"


# --- Lese-Werkzeuge ---------------------------------------------------------
class TestReadTools:
    def test_location_inventory_only_when_searched(self, conn):
        insert_location(conn, loc_id=100, name="Haus", lat=HOME[0], lon=HOME[1])
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        r = tb.call("get_location_inventory", {"location_id": 100})
        assert not r["ok"] and r["reason"] == "not_searched"
        place(conn, loc_id=101, items=[("water_1l", 2.0)])
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        r = tb.call("get_location_inventory", {"location_id": 101})
        assert r["ok"] and r["items"][0]["item_id"] == "water_1l"
        assert tb.call("get_location_inventory", {"location_id": 999})["reason"] == "out_of_reach"

    def test_nearby_inventory_kb_providers_caps_world(self, conn):
        give(conn, "firewood", 2)
        place(conn)
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        assert tb.call("get_nearby_locations", {})["locations"][0]["searched"] is True
        assert any(i["item_id"] == "firewood" for i in tb.call("get_inventory", {})["inventory"])
        facts = tb.call("kb_lookup", {"topic": "provides:heat", "key": None})["facts"]
        assert facts[0]["key"] == "firewood"
        assert tb.call("kb_lookup", {"topic": "provides:heat", "key": "nix"})["facts"] == []
        prov = tb.call("get_requirement_providers", {"requirement": "heat"})
        assert prov["requirements"]["heat"]["satisfied_by"]["item"] == "firewood"
        assert tb.call("get_capabilities", {})["capabilities"] == []
        assert tb.call("get_world_state", {})["world_state"]["day"] == 0

    def test_specs_are_strict_and_complete(self):
        specs = llm_tools.tool_specs()
        names = {s["name"] for s in specs}
        assert {f"propose_{op}" for op in EFFECT_OPS} <= names and "finish" in names
        for s in specs:
            # alle Schemas strict-tauglich; das Flag selbst nur für Vorschläge +
            # finish (API-Limit von 20 strikten Werkzeugen, siehe test_tool_limits)
            sch = s["input_schema"]
            assert sch["additionalProperties"] is False
            assert set(sch["required"]) == set(sch["properties"])
            if s["name"].startswith("propose_") or s["name"] == "finish":
                assert s["strict"] is True


# --- Erweitertes Vokabular --------------------------------------------------
class TestNewEffects:
    def test_fill_water(self, conn):
        place(conn, items=[("water_1l", 3.0), ("canned_beans", 1.0)])
        v = adjudicator.adjudicate(conn, 1, "fülle wasser ab")
        assert v["ok"] and owned(conn, "water_1l") == 3.0 and owned(conn, "canned_beans") == 0
        assert audit_clean(conn)

    def test_fill_water_without_source_rejected(self, conn):
        place(conn, items=[("canned_beans", 1.0)])
        v = adjudicator.adjudicate(conn, 1, "fülle wasser ab")
        assert not v["ok"] and v["reason"] == "no_water_source"

    def test_fill_water_needs_searched_place(self, conn):
        insert_location(conn, loc_id=100, lat=HOME[0], lon=HOME[1])
        conn.execute("UPDATE locations SET discovery_status='discovered';")
        conn.commit()
        v = adjudicator.adjudicate(conn, 1, "fülle wasser ab")
        assert not v["ok"] and v["reason"] == "not_searched"

    def test_leave_note(self, conn):
        place(conn)
        v = adjudicator.adjudicate(conn, 1, 'hinterlasse einen zettel: "Bin nach Norden gegangen"')
        assert v["ok"]
        ev = conn.execute("SELECT * FROM events WHERE category='note';").fetchone()
        assert ev["message"] == "Bin nach Norden gegangen" and ev["subject_type"] == "location"

    def test_leave_note_validation(self, conn):
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        assert tb.call("propose_leave_note", {"text": "hi"})["reason"] == "no_target"
        place(conn)
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        assert tb.call("propose_leave_note", {"text": "  "})["reason"] == "no_text"
        assert tb.call("propose_leave_note", {"text": "x" * 500})["reason"] == "text_too_long"

    def test_barricade_via_kb_recipe(self, conn):
        place(conn)
        v = adjudicator.adjudicate(conn, 1, "barrikadiere die tür")
        assert not v["ok"] and v["reason"] == "missing:barricade_material"
        give(conn, "rope", 3)
        v = adjudicator.adjudicate(conn, 1, "barrikadiere die tür")
        assert v["ok"]
        assert any(c["ctype"] == "barricade" for c in capabilities.list_active(conn, 1))
        assert owned(conn, "rope") == 1.0  # Rezept verbraucht 2 Seil
        assert audit_clean(conn)

    def test_barricade_needs_a_place(self, conn):
        give(conn, "rope", 3)
        v = adjudicator.adjudicate(conn, 1, "barrikadiere die tür")
        assert not v["ok"] and v["reason"] == "no_target"

    def test_transform_cannot_invent_items_or_mass(self, conn):
        give(conn, "canned_beans", 1)
        tb = AdjudicationToolbox(conn, 1, adjudicator.build_context(conn, 1))
        r = tb.call("propose_transform", {"consume": [{"item": "canned_beans", "qty": 1}],
                                          "produce": [{"item": "gold_bar", "qty": 1}],
                                          "requires": []})
        assert r["reason"] == "unknown_item:gold_bar"
        r = tb.call("propose_transform", {"consume": [{"item": "canned_beans", "qty": 1}],
                                          "produce": [{"item": "water_1l", "qty": 9}],
                                          "requires": []})
        assert r["reason"] == "produce_exceeds_input"


# --- Verallgemeinerter Override ----------------------------------------------
class TestGeneralisedOverride:
    def _beacon_setup(self, conn):
        give(conn, "wifi_router", 1)
        give(conn, "crowbar", 1)  # Kein generator -> missing:power

    def test_power_requirement_rejection_then_override(self, conn):
        self._beacon_setup(conn)
        text = "sende ein funksignal über den mast"
        v = adjudicator.adjudicate(conn, 1, text)
        assert not v["ok"] and v["reason"] == "missing:power"
        assert requirements.requirement_from_reason(conn, v["reason"]) == "power"
        v = adjudicator.override(conn, 1, text, "ich nutze das crowbar als stromquelle")
        assert v["ok"], v
        assert v["override_learned"] == {"topic": "provides:power", "key": "crowbar"}
        fact = kb.lookup(conn, "provides:power", "crowbar")
        assert fact["provenance"] == "player_verified"
        assert any(c["ctype"] == "ssid_beacon" for c in capabilities.list_active(conn, 1))

    def test_override_never_downgrades_curated(self, conn):
        give(conn, "wifi_router", 1)
        give(conn, "generator", 1)
        v = adjudicator.override(conn, 1, "sende ein funksignal über den mast",
                                 "der generator liefert strom")
        assert kb.lookup(conn, "provides:power", "generator")["provenance"] == "curated"

    def test_override_with_nothing_to_contest_learns_nothing(self, conn):
        give(conn, "crowbar", 1)
        before = kb.list_topic(conn, "provides:heat")
        v = adjudicator.override(conn, 1, "schau dich um", "mit dem crowbar")
        assert "override_learned" not in v
        assert kb.list_topic(conn, "provides:heat") == before

    def test_requirement_from_reason(self, conn):
        f = requirements.requirement_from_reason
        assert f(conn, "no_heat") == "heat"
        assert f(conn, "missing:transmitter") == "transmitter"
        assert f(conn, "missing:rope") is None       # Item, keine Anforderung
        assert f(conn, "no_target") is None
        assert f(conn, None) is None

    def test_explicit_requirement_kwarg(self, conn):
        give(conn, "crowbar", 1)
        v = adjudicator.override(conn, 1, "schau dich um", "crowbar", requirement="heat")
        assert v["override_learned"]["topic"] == "provides:heat"


# --- Freitext über actions.perform ------------------------------------------
class TestFreetextViaActions:
    def test_freetext_fill_water(self, conn):
        place(conn, items=[("water_1l", 2.0)])
        r = actions.perform(conn, 1, {"kind": "freetext", "text": "Fülle Wasser ab"})
        assert r["ok"], r
        assert [a["op"] for a in r["result"]["effects_applied"]] == ["fill_water"]
        assert owned(conn, "water_1l") == 2.0
        json.dumps(r)

    def test_freetext_replan_through_actions(self, conn):
        give(conn, "pasta_500g", 1)
        give(conn, "firewood", 1)
        place(conn, items=[("water_1l", 2.0)])
        r = actions.perform(conn, 1, {"kind": "freetext", "text": "koche nudeln"})
        assert r["ok"] and owned(conn, "meal_pasta") == 1.0

    def test_freetext_rejection_keeps_state(self, conn):
        give(conn, "pasta_500g", 1)
        give(conn, "water_1l", 1)
        r = actions.perform(conn, 1, {"kind": "freetext", "text": "koche nudeln"})
        assert not r["ok"] and r["reason"] == "no_heat" and r["result"]["escalate"]

    def test_freetext_override_passthrough(self, conn):
        give(conn, "pasta_500g", 1)
        give(conn, "water_1l", 1)
        give(conn, "crowbar", 1)
        r = actions.perform(conn, 1, {"kind": "freetext", "override_text": "koche nudeln",
                                      "override_reason": "crowbar als hitzequelle"})
        assert r["ok"] and r["result"]["override_learned"]["topic"] == "provides:heat"


# --- world_state / Lore (#6) --------------------------------------------------
class TestWorldStateAndLore:
    def test_world_state_day_and_time(self, conn):
        conn.execute("UPDATE world SET tick=?;", (3 * 1440 + 125,))
        conn.commit()
        from app.sim import actions
        ws = adjudicator.build_context(conn, 1)["world_state"]
        clock = actions.world_clock(conn)
        assert ws["day"] == clock["day"] and ws["time"] == clock["time_of_day"]
        # decay_world existiert -> Infrastruktur-Block ist dabei
        assert "infra" in ws and ws["infra"]["phase"]

    def test_world_state_pluggable_decay_world(self, conn, monkeypatch):
        import app.sim as sim_pkg

        fake = SimpleNamespace(world_infra=lambda c: {"power": 0.4, "mobile": 0.1})
        monkeypatch.setitem(sys.modules, "app.sim.decay_world", fake)
        monkeypatch.setattr(sim_pkg, "decay_world", fake, raising=False)
        ws = adjudicator.build_context(conn, 1)["world_state"]
        assert ws["infra"] == {"power": 0.4, "mobile": 0.1}

        def broken(c):
            raise RuntimeError("x")

        monkeypatch.setattr(fake, "world_infra", broken)
        assert "infra" not in adjudicator.build_context(conn, 1)["world_state"]

    def test_lore_loaded_from_files_and_editable(self, tmp_path, monkeypatch):
        assert "Virus" in lore.world_bible() and "propose_" in lore.adjudicator_system()
        assert lore.world_bible() in lore.system_prompt()
        p = tmp_path / "adj.md"
        p.write_text("ANDERER PROMPT", encoding="utf-8")
        monkeypatch.setattr(lore, "PROMPT_PATH", p)
        lore.reload()
        try:
            assert lore.system_prompt().startswith("ANDERER PROMPT")
            monkeypatch.setattr(lore, "PROMPT_PATH", tmp_path / "fehlt.md")
            lore.reload()
            assert "Adjudikator" in lore.adjudicator_system()  # Default-Fallback
        finally:
            monkeypatch.undo()
            lore.reload()

    def test_stub_is_deterministic(self, conn):
        give(conn, "pasta_500g", 1)
        place(conn, items=[("water_1l", 2.0)])
        give(conn, "firewood", 1)
        a = adjudicator.adjudicate(conn, 1, "koche nudeln")
        assert a["ok"]


# --- Claude-Backend (fake Client) & CLI-Emulation --------------------------------
def _blk(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def _resp(blocks, stop="tool_use"):
    return SimpleNamespace(content=blocks, stop_reason=stop)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=lambda o: repr(o))))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _claude(responses):
    from app.llm.claude import ClaudeBackend
    from app.llm.stub import RuleBackend

    be = object.__new__(ClaudeBackend)
    be._fallback = RuleBackend()
    be._client = SimpleNamespace(messages=FakeMessages(responses))
    return be


class TestClaudeLoop:
    def test_loop_with_replan_and_finish(self, conn, monkeypatch):
        give(conn, "canned_beans", 1)
        be = _claude([
            _resp([_blk("thinking", thinking="", signature="s"),
                   _blk("tool_use", id="t1", name="propose_prepare", input={"item": None})]),
            _resp([_blk("tool_use", id="t2", name="propose_consume_food", input={"item": None}),
                   _blk("tool_use", id="t3", name="finish",
                        input={"understanding": "u", "feasibility": "feasible",
                               "reason": "", "narration": "zu früh"})]),
            _resp([_blk("tool_use", id="t4", name="finish",
                        input={"understanding": "essen", "feasibility": "feasible",
                               "reason": "", "narration": "Du isst die Bohnen."})]),
        ])
        use_backend(monkeypatch, be)
        v = adjudicator.adjudicate(conn, 1, "iss was")
        assert v["ok"] and owned(conn, "canned_beans") == 0
        calls = be._client.messages.calls
        assert len(calls) == 3
        assert all(c["tool_choice"] == {"type": "auto"} for c in calls)
        assert calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert "Welt-Bibel" in calls[0]["system"][0]["text"]
        # tool_result der 1. Runde enthält die Ablehnung
        first = calls[1]["messages"][-1]["content"][0]
        assert first["type"] == "tool_result" and "nothing_to_prepare" in first["content"]
        # finish zusammen mit anderen Calls wird zurückgewiesen
        second = calls[2]["messages"][-1]["content"]
        assert any("finish_must_be_alone" in c["content"] for c in second)

    def test_text_only_reply_finishes(self, conn, monkeypatch):
        be = _claude([_resp([_blk("text", text="Du siehst dich um.")], stop="end_turn")])
        use_backend(monkeypatch, be)
        v = adjudicator.adjudicate(conn, 1, "schau dich um")
        assert v["ok"] and v["narration"] == "Du siehst dich um."

    def test_api_error_falls_back_to_stub(self, conn, monkeypatch):
        give(conn, "canned_beans", 1)
        be = _claude([RuntimeError("503")])
        use_backend(monkeypatch, be)
        v = adjudicator.adjudicate(conn, 1, "iss was")
        assert v["ok"] and owned(conn, "canned_beans") == 0

    def test_iteration_limit_in_claude_loop(self, conn, monkeypatch):
        be = _claude([_resp([_blk("tool_use", id=f"t{i}", name="get_inventory", input={})])
                      for i in range(adjudicator.MAX_ITERATIONS + 2)])
        use_backend(monkeypatch, be)
        v = adjudicator.adjudicate(conn, 1, "x")
        assert not v["ok"] and v["reason"] == "iteration_limit"
        assert len(be._client.messages.calls) == adjudicator.MAX_ITERATIONS


class TestCliEmulation:
    def _cli(self, outputs):
        from app.llm.cli import CliBackend
        from app.llm.stub import RuleBackend

        be = object.__new__(CliBackend)
        be._fallback = RuleBackend()
        outs = list(outputs)
        be.prompts = []

        def fake_json(prompt):
            be.prompts.append(prompt)
            return outs.pop(0) if outs else None

        be._json = fake_json
        return be

    def test_json_turn_protocol(self, conn, monkeypatch):
        give(conn, "canned_beans", 1)
        be = self._cli([
            {"calls": [{"tool": "get_inventory", "input": {}},
                       {"tool": "propose_consume_food", "input": {}}]},
            {"calls": [{"tool": "finish", "input": {
                "understanding": "u", "feasibility": "feasible", "reason": "",
                "narration": "Du isst."}}]},
        ])
        use_backend(monkeypatch, be)
        v = adjudicator.adjudicate(conn, 1, "iss was")
        assert v["ok"] and owned(conn, "canned_beans") == 0
        assert "propose_consume_food" in be.prompts[1]  # Verlauf mit Ergebnis

    def test_garbage_falls_back_to_stub(self, conn, monkeypatch):
        give(conn, "canned_beans", 1)
        be = self._cli([None])
        use_backend(monkeypatch, be)
        assert adjudicator.adjudicate(conn, 1, "iss was")["ok"]


def test_effect_ops_in_sync():
    assert set(EFFECT_OPS) == set(effects.OPS)
