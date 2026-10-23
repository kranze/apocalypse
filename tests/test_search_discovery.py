"""Tests für Entdeckung, deterministische Generierung und Freitext-Suche (Issue #50).

Abgedeckte Szenarien:
1. discover erzeugt kein Inventar (Betreten ist nicht Durchsuchen); idempotent.
2. ensure_generated: einmalig, idempotent, Ledger-Quelle, Marker 'durchsucht'.
3. Freitext-Suche gleicht NUR gegen das vorhandene Inventar ab: keine neuen
   Items/Mengen, auch nicht, wenn ein (halluzinierendes) Backend welche behauptet.
4. Kein Ort / keine Anfrage -> Validator lehnt ab.
5. Bilanz drift-frei nach Suchen + Tick.
6. Determinismus: zwei DBs, gleiches Setup -> gleiches Inventar.
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
from app.llm.stub import RuleBackend
from app.sim import adjudicator, audit, effects, generation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


def _insert_discovered(conn, loc_id=300, loc_type="supermarket", lat=49.0, lon=11.0, seed=42):
    """Legt eine Location direkt am Spieler an und markiert sie als entdeckt."""
    insert_location(
        conn, loc_id=loc_id, loc_type=loc_type, name=f"Test-{loc_type}-{loc_id}",
        lat=lat, lon=lon, generation_seed=seed,
    )
    conn.execute("UPDATE locations SET discovery_status='discovered' WHERE id=?;", (loc_id,))
    conn.commit()


def _loc_inv(conn, loc_id):
    return {
        r["item_id"]: r["quantity"] for r in conn.execute(
            "SELECT item_id, quantity FROM location_inventory WHERE location_id=?;", (loc_id,)
        ).fetchall()
    }


def _catalog_ids(conn):
    return {r["id"] for r in conn.execute("SELECT id FROM item_catalog;").fetchall()}


def _ledger_expected(conn, item_id):
    row = conn.execute(
        "SELECT expected_total FROM resource_ledger WHERE item_id=?;", (item_id,)
    ).fetchone()
    return row["expected_total"] if row else 0.0


# ---------------------------------------------------------------------------
# 1. discover ohne Gen
# ---------------------------------------------------------------------------

class TestDiscoverNoGen:
    def test_discover_creates_no_inventory(self, conn):
        loc_id = insert_location(conn, loc_id=400, loc_type="house", lat=49.0, lon=11.0)
        generation.discover(conn, loc_id)
        assert _loc_inv(conn, loc_id) == {}
        assert not generation.is_searched(conn, loc_id)

    def test_discover_sets_status_discovered(self, conn):
        loc_id = insert_location(conn, loc_id=401, loc_type="building", lat=49.0, lon=11.0)
        result = generation.discover(conn, loc_id)
        assert result["ok"] is True and result["already"] is False
        row = conn.execute(
            "SELECT discovery_status FROM locations WHERE id=?;", (loc_id,)
        ).fetchone()
        assert row["discovery_status"] == "discovered"

    def test_discover_idempotent(self, conn):
        loc_id = insert_location(conn, loc_id=402, loc_type="supermarket", lat=49.0, lon=11.0)
        r1 = generation.discover(conn, loc_id)
        r2 = generation.discover(conn, loc_id)
        assert r1["already"] is False and r2["already"] is True and r2["ok"] is True
        assert _loc_inv(conn, loc_id) == {}

    def test_discover_inventory_field_empty_list(self, conn):
        loc_id = insert_location(conn, loc_id=403, loc_type="house", lat=49.0, lon=11.0)
        assert generation.discover(conn, loc_id).get("inventory") == []


# ---------------------------------------------------------------------------
# 2. ensure_generated
# ---------------------------------------------------------------------------

class TestEnsureGenerated:
    def test_generates_once_and_marks_searched(self, conn):
        _insert_discovered(conn, loc_id=500, loc_type="supermarket")
        r1 = generation.ensure_generated(conn, 500)
        assert r1["ok"] and r1["generated"] and r1["items"]
        inv1 = _loc_inv(conn, 500)
        assert inv1 and generation.is_searched(conn, 500)
        r2 = generation.ensure_generated(conn, 500)
        assert r2["ok"] and r2["generated"] is False
        assert _loc_inv(conn, 500) == inv1  # nichts verdoppelt

    def test_books_ledger_source(self, conn):
        _insert_discovered(conn, loc_id=501, loc_type="supermarket")
        generation.ensure_generated(conn, 501)
        for item_id, qty in _loc_inv(conn, 501).items():
            assert _ledger_expected(conn, item_id) == pytest.approx(qty)

    def test_all_items_in_catalog(self, conn):
        _insert_discovered(conn, loc_id=502, loc_type="supermarket")
        generation.ensure_generated(conn, 502)
        assert set(_loc_inv(conn, 502)) <= _catalog_ids(conn)

    def test_unknown_location(self, conn):
        assert generation.ensure_generated(conn, 99999)["ok"] is False


# ---------------------------------------------------------------------------
# 3. Freitext-Suche erfindet nichts
# ---------------------------------------------------------------------------

class TestFreetextSearch:
    def test_search_triggers_generation_and_matches(self, conn):
        _insert_discovered(conn, loc_id=600, loc_type="supermarket")
        verdict = adjudicator.adjudicate(conn, 1, "ich suche wasser")
        assert verdict["ok"] is True, verdict
        assert generation.is_searched(conn, 600)
        res = verdict["effects_applied"][0]["result"]
        assert res["found"] is True
        assert [i["item"] for i in res["items"]] == ["water_1l"]
        # Menge = vorhandene Menge, nicht erfunden
        assert res["items"][0]["quantity"] == _loc_inv(conn, 600)["water_1l"]

    def test_search_creates_no_catalog_rows_and_no_extra_inventory(self, conn):
        _insert_discovered(conn, loc_id=601, loc_type="supermarket")
        generation.ensure_generated(conn, 601)
        cat, inv = _catalog_ids(conn), _loc_inv(conn, 601)
        ledger_before = {i: _ledger_expected(conn, i) for i in inv}
        adjudicator.adjudicate(conn, 1, "ich suche einen fernseher")
        adjudicator.adjudicate(conn, 1, "ich suche wasser")
        assert _catalog_ids(conn) == cat
        assert _loc_inv(conn, 601) == inv
        assert {i: _ledger_expected(conn, i) for i in inv} == ledger_before

    def test_unmatched_query_finds_nothing(self, conn):
        _insert_discovered(conn, loc_id=602, loc_type="supermarket")
        verdict = adjudicator.adjudicate(conn, 1, "ich suche einen fernseher")
        res = verdict["effects_applied"][0]["result"]
        assert res["found"] is False and res["items"] == []

    def test_hallucinating_backend_is_ignored(self, conn, monkeypatch):
        """Ein Backend, das fremde IDs behauptet, kann nichts erzeugen: der
        Sim-Kern verwirft alles, was nicht in der Kandidatenliste steht."""
        class Liar(RuleBackend):
            def match_items(self, query, location, candidates, profile=None, history=None):
                return {"item_ids": ["gold_bar", "generator", candidates[0]["item_id"]],
                        "narration": "Du findest 999 Goldbarren!"}
        monkeypatch.setattr(effects, "get_backend", lambda: Liar())
        _insert_discovered(conn, loc_id=603, loc_type="supermarket")
        cat = _catalog_ids(conn)
        verdict = adjudicator.adjudicate(conn, 1, "ich suche gold")
        res = verdict["effects_applied"][0]["result"]
        inv = _loc_inv(conn, 603)
        assert [i["item"] for i in res["items"]] and all(i["item"] in inv for i in res["items"])
        assert "gold_bar" not in inv and "generator" not in inv
        assert _catalog_ids(conn) == cat

    def test_empty_location_says_so(self, conn):
        _insert_discovered(conn, loc_id=604, loc_type="supermarket")
        generation.ensure_generated(conn, 604)
        conn.execute("DELETE FROM location_inventory WHERE location_id=604;")
        conn.commit()
        verdict = adjudicator.adjudicate(conn, 1, "ich suche wasser")
        res = verdict["effects_applied"][0]["result"]
        assert res["found"] is False and "nichts" in res["narration"].lower()


# ---------------------------------------------------------------------------
# 4. Validatoren
# ---------------------------------------------------------------------------

class TestNoLocationHere:
    def test_no_discovered_location_nearby(self, conn):
        insert_location(conn, loc_id=800, loc_type="house", lat=49.5, lon=11.5)
        ctx = adjudicator.build_context(conn, 1)
        ok, reason = effects.validate_all(conn, 1, [{"op": "search", "query": "wasser"}], ctx)
        assert ok is False and reason == "no_location_here"

    def test_undiscovered_location_at_player_not_found(self, conn):
        insert_location(conn, loc_id=801, loc_type="house", lat=49.0, lon=11.0)
        ctx = adjudicator.build_context(conn, 1)
        ok, reason = effects.validate_all(conn, 1, [{"op": "search", "query": "wasser"}], ctx)
        assert ok is False and reason == "no_location_here"

    def test_no_query_rejected(self, conn):
        _insert_discovered(conn, loc_id=802, loc_type="house")
        ctx = adjudicator.build_context(conn, 1)
        ok, reason = effects.validate_all(conn, 1, [{"op": "search", "query": ""}], ctx)
        assert ok is False and reason == "no_query"


# ---------------------------------------------------------------------------
# 5. Bilanz drift-frei
# ---------------------------------------------------------------------------

class TestBalanceDriftFree:
    def test_after_searches_no_audit_flags(self, conn):
        from app.sim import tick

        _insert_discovered(conn, loc_id=900, loc_type="house")
        _insert_discovered(conn, loc_id=901, loc_type="supermarket", lat=49.0001, lon=11.0, seed=7)
        adjudicator.adjudicate(conn, 1, "ich suche wasser")
        adjudicator.adjudicate(conn, 1, "ich suche essen")
        conn.execute("UPDATE characters SET lat=49.0001, lon=11.0 WHERE id=1;")
        conn.commit()
        adjudicator.adjudicate(conn, 1, "ich suche wasser")
        conn.execute("UPDATE characters SET lat=49.0, lon=11.0 WHERE id=1;")
        conn.commit()
        tick.advance_tick(conn)
        cur_tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        flagged = [f for f in audit.run_audit(conn, cur_tick) if f.get("flagged")]
        assert flagged == [], f"Bilanz-Drift nach Suchen: {flagged}"


# ---------------------------------------------------------------------------
# 6. Determinismus
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_setup_same_inventory(self, conn_seeded):
        def _setup(conn):
            conn.execute("UPDATE characters SET lat=49.0, lon=11.0 WHERE id=1;")
            insert_location(conn, loc_id=1000, loc_type="house", name="Haus-Det",
                            lat=49.0, lon=11.0, generation_seed=42)
            conn.execute("UPDATE locations SET discovery_status='discovered' WHERE id=1000;")
            conn.commit()

        c1, c2 = conn_seeded("_a"), conn_seeded("_b")
        _setup(c1)
        _setup(c2)
        adjudicator.adjudicate(c1, 1, "ich suche wasser")
        adjudicator.adjudicate(c2, 1, "ich suche wasser")
        assert _loc_inv(c1, 1000) == _loc_inv(c2, 1000)
        c1.close()
        c2.close()
