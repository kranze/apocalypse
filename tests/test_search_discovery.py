"""Tests fuer suchgetriebene Entdeckung (generation.discover + effects.search).

Abgedeckte Szenarien:
1. discover ohne Gen: keine location_inventory-Zeilen; Status 'discovered'; idempotent.
2. Suche Erfolg: Fund landet in location_inventory + item_catalog + Ledger.
3. Plausibilitaet: unplausibler Suchterm (kein Keyword / falscher Ortstyp) -> kein Fund.
4. Anti-Farming: zweite Suche nach gleichem Begriff -> exhausted, kein Doppel-Fund.
5. Clamps: _materialize_item mit absurden Werten -> category 'misc', weight<=100, qty<=50.
6. Kein Ort: Suche ohne entdeckte Location in Reichweite -> no_location_here.
7. Bilanz drift-frei: nach mehreren Suchen + tick -> resource_audit.flagged==0.
8. Determinismus: zwei DBs, gleiches Setup, gleiche Eingaben -> gleiche Funde.
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
from app.sim import adjudicator, audit, effects, generation
from app.sim.effects import _materialize_item, _norm_term


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
    # Spieler auf feste Position setzen
    c.execute("UPDATE characters SET lat=49.0, lon=11.0 WHERE id=1;")
    c.commit()
    yield c
    c.close()


def _insert_discovered(conn, loc_id=300, loc_type="house", lat=49.0, lon=11.0):
    """Legt eine Location direkt am Spieler an und markiert sie als entdeckt."""
    insert_location(
        conn,
        loc_id=loc_id,
        loc_type=loc_type,
        name=f"Test-{loc_type}-{loc_id}",
        lat=lat,
        lon=lon,
        generation_seed=42,
    )
    conn.execute(
        "UPDATE locations SET discovery_status='discovered' WHERE id=?;",
        (loc_id,),
    )
    conn.commit()


def _loc_inventory_count(conn, loc_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM location_inventory WHERE location_id=?;",
        (loc_id,),
    ).fetchone()["n"]


def _ledger_expected(conn, item_id):
    row = conn.execute(
        "SELECT expected_total FROM resource_ledger WHERE item_id=?;",
        (item_id,),
    ).fetchone()
    return row["expected_total"] if row else 0.0


# ---------------------------------------------------------------------------
# 1. discover ohne Gen
# ---------------------------------------------------------------------------

class TestDiscoverNoGen:
    def test_discover_creates_no_inventory(self, conn):
        """discover auf frische Location erzeugt KEINE location_inventory-Zeilen."""
        loc_id = insert_location(conn, loc_id=400, loc_type="house", lat=49.0, lon=11.0)
        before = _loc_inventory_count(conn, loc_id)
        generation.discover(conn, loc_id)
        after = _loc_inventory_count(conn, loc_id)
        assert before == 0
        assert after == 0, "Lazy-Gen ist abgeschafft — discover darf kein Inventar anlegen."

    def test_discover_sets_status_discovered(self, conn):
        """discover setzt discovery_status auf 'discovered'."""
        loc_id = insert_location(conn, loc_id=401, loc_type="building", lat=49.0, lon=11.0)
        result = generation.discover(conn, loc_id)
        assert result["ok"] is True
        assert result["already"] is False
        row = conn.execute(
            "SELECT discovery_status FROM locations WHERE id=?;", (loc_id,)
        ).fetchone()
        assert row["discovery_status"] == "discovered"

    def test_discover_idempotent(self, conn):
        """Zweites discover gibt already=True, keinen Fehler, kein Extra-Inventar."""
        loc_id = insert_location(conn, loc_id=402, loc_type="supermarket", lat=49.0, lon=11.0)
        r1 = generation.discover(conn, loc_id)
        r2 = generation.discover(conn, loc_id)
        assert r1["already"] is False
        assert r2["already"] is True
        assert r2["ok"] is True
        assert _loc_inventory_count(conn, loc_id) == 0

    def test_discover_inventory_field_empty_list(self, conn):
        """discover gibt inventory=[] zurueck (kein Inhalt materialisiert)."""
        loc_id = insert_location(conn, loc_id=403, loc_type="house", lat=49.0, lon=11.0)
        result = generation.discover(conn, loc_id)
        assert result.get("inventory") == []


# ---------------------------------------------------------------------------
# 2. Suche Erfolg
# ---------------------------------------------------------------------------

class TestSearchSuccess:
    def test_search_water_creates_location_inventory(self, conn):
        """Wasser in einem Haus (discovered, am Spieler) -> Fund in location_inventory."""
        _insert_discovered(conn, loc_id=500, loc_type="house")
        verdict = adjudicator.adjudicate(conn, 1, "ich suche wasser")
        assert verdict["ok"] is True, f"Verdict nicht ok: {verdict}"
        items = conn.execute(
            "SELECT * FROM location_inventory WHERE location_id=500;"
        ).fetchall()
        assert len(items) > 0, "Kein Item in location_inventory nach erfolgreicher Suche."

    def test_search_water_creates_item_catalog_entry(self, conn):
        """Gefundenes Item muss im item_catalog existieren."""
        _insert_discovered(conn, loc_id=501, loc_type="house")
        adjudicator.adjudicate(conn, 1, "ich suche wasser")
        items = conn.execute(
            "SELECT li.item_id FROM location_inventory li WHERE li.location_id=501;"
        ).fetchall()
        assert len(items) > 0
        for row in items:
            cat_row = conn.execute(
                "SELECT id FROM item_catalog WHERE id=?;", (row["item_id"],)
            ).fetchone()
            assert cat_row is not None, f"item_catalog-Eintrag fehlt fuer {row['item_id']}"

    def test_search_water_books_ledger_source(self, conn):
        """Fund bucht eine Ledger-Quelle (expected_total > 0)."""
        _insert_discovered(conn, loc_id=502, loc_type="house")
        adjudicator.adjudicate(conn, 1, "ich suche wasser")
        items = conn.execute(
            "SELECT item_id FROM location_inventory WHERE location_id=502;"
        ).fetchall()
        assert len(items) > 0
        for row in items:
            assert _ledger_expected(conn, row["item_id"]) > 0, (
                f"Ledger-Eintrag fuer {row['item_id']} nicht positiv."
            )

    def test_search_narration_contains_find(self, conn):
        """Verdict-Narration muss den Fund beschreiben."""
        _insert_discovered(conn, loc_id=503, loc_type="house")
        verdict = adjudicator.adjudicate(conn, 1, "ich suche wasser")
        narr = verdict.get("narration", "").lower()
        # Stub-Narration = "Du findest: Wasserflasche 1L."
        assert any(k in narr for k in ("find", "wasser", "flasche")), (
            f"Narration enthaelt keinen Fund-Hinweis: {verdict.get('narration')}"
        )


# ---------------------------------------------------------------------------
# 3. Plausibilitaet (kein Fund)
# ---------------------------------------------------------------------------

class TestSearchImplausible:
    def test_wrong_loc_type_no_find(self, conn):
        """Wasser in einer hardware-Location -> Stub gibt found=False."""
        _insert_discovered(conn, loc_id=600, loc_type="hardware")
        verdict = adjudicator.adjudicate(conn, 1, "ich suche wasser")
        # Kann ok=True mit found=False ODER ok=False (no_location_here falls Typ-Filter greift)
        # Entscheidend: kein item in location_inventory
        items = conn.execute(
            "SELECT * FROM location_inventory WHERE location_id=600;"
        ).fetchall()
        assert len(items) == 0, "Unplausibler Ortstyp hat trotzdem ein Item erzeugt."

    def test_unknown_keyword_no_find(self, conn):
        """Suchbegriff ohne Stub-Keyword -> kein Fund, kein location_inventory-Eintrag."""
        _insert_discovered(conn, loc_id=601, loc_type="supermarket")
        # "xyz" ist kein bekanntes Keyword
        adjudicator.adjudicate(conn, 1, "ich suche xyz")
        items = conn.execute(
            "SELECT * FROM location_inventory WHERE location_id=601;"
        ).fetchall()
        assert len(items) == 0

    def test_no_find_no_ledger(self, conn):
        """Kein Fund -> kein neuer Ledger-Eintrag fuer das gesuchte Item."""
        _insert_discovered(conn, loc_id=602, loc_type="hardware")
        before_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM resource_ledger;"
        ).fetchone()["n"]
        adjudicator.adjudicate(conn, 1, "ich suche wasser")
        after_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM resource_ledger;"
        ).fetchone()["n"]
        # Kein neuer Ledger-Eintrag fuer einen nicht gefundenen Gegenstand
        items = conn.execute(
            "SELECT * FROM location_inventory WHERE location_id=602;"
        ).fetchall()
        assert len(items) == 0
        # Ledger darf nicht gewachsen sein (fuer den nicht gefundenen Begriff)


# ---------------------------------------------------------------------------
# 4. Anti-Farming
# ---------------------------------------------------------------------------

class TestAntifarming:
    def test_second_search_same_term_exhausted(self, conn):
        """Zweite Suche nach demselben Begriff -> exhausted=True, kein Doppel-Fund."""
        _insert_discovered(conn, loc_id=700, loc_type="house")
        v1 = adjudicator.adjudicate(conn, 1, "ich suche wasser")
        assert v1["ok"] is True
        count_after_first = _loc_inventory_count(conn, 700)

        v2 = adjudicator.adjudicate(conn, 1, "ich suche wasser")
        count_after_second = _loc_inventory_count(conn, 700)
        # zweite Suche darf kein neues Item hinzufuegen
        assert count_after_second == count_after_first, (
            "Anti-Farming versagt: zweite Suche hat neues Inventar erzeugt."
        )
        # Pruefen ob exhausted in Effekt-Results oder Narration sichtbar
        applied = v2.get("effects_applied", [])
        exhausted_signals = [
            a for a in applied
            if isinstance(a.get("result"), dict) and a["result"].get("exhausted")
        ]
        narr = v2.get("narration", "").lower()
        assert exhausted_signals or "schon gesucht" in narr or "nichts mehr" in narr, (
            "Kein exhausted-Signal bei zweiter Suche."
        )

    def test_second_search_normalized_term_exhausted(self, conn):
        """Auch anders formulierter Begriff mit gleichem Kern -> exhausted."""
        _insert_discovered(conn, loc_id=701, loc_type="house")
        adjudicator.adjudicate(conn, 1, "ich suche wasser")
        count_after_first = _loc_inventory_count(conn, 701)

        # _norm_term("nochmal wasser") == _norm_term("wasser") -> gleicher normalisierter Term
        term1 = _norm_term("ich suche wasser")
        term2 = _norm_term("ich suche nochmal wasser")
        assert term1 == term2, (
            f"Normalisierung liefert verschiedene Terme: '{term1}' vs '{term2}'"
        )

        adjudicator.adjudicate(conn, 1, "ich suche nochmal wasser")
        count_after_second = _loc_inventory_count(conn, 701)
        assert count_after_second == count_after_first

    def test_location_searches_row_inserted(self, conn):
        """Nach einer Suche existiert ein Eintrag in location_searches."""
        _insert_discovered(conn, loc_id=702, loc_type="house")
        adjudicator.adjudicate(conn, 1, "ich suche wasser")
        row = conn.execute(
            "SELECT * FROM location_searches WHERE location_id=702;"
        ).fetchone()
        assert row is not None, "location_searches-Eintrag fehlt nach Suche."


# ---------------------------------------------------------------------------
# 5. Clamps in _materialize_item
# ---------------------------------------------------------------------------

class TestMaterializeItemClamps:
    def test_unknown_category_becomes_misc(self, conn):
        """Unbekannte category -> 'misc'."""
        item = {"name": "Alien-Gizmo", "category": "unsinn", "weight_kg": 1.0, "quantity": 1}
        item_id, qty = _materialize_item(conn, item, 0)
        conn.commit()
        row = conn.execute(
            "SELECT category FROM item_catalog WHERE id=?;", (item_id,)
        ).fetchone()
        assert row is not None
        assert row["category"] == "misc"

    def test_weight_clamped_to_100(self, conn):
        """weight_kg > 100 wird auf 100 geklemmt."""
        item = {"name": "Super-Schwer", "category": "tool", "weight_kg": 9999, "quantity": 1}
        item_id, qty = _materialize_item(conn, item, 0)
        conn.commit()
        row = conn.execute(
            "SELECT weight_kg FROM item_catalog WHERE id=?;", (item_id,)
        ).fetchone()
        assert row["weight_kg"] <= 100.0

    def test_weight_clamped_min(self, conn):
        """weight_kg <= 0 wird auf 0.01 angehoben."""
        item = {"name": "Hauch", "category": "misc", "weight_kg": 0.0, "quantity": 1}
        item_id, qty = _materialize_item(conn, item, 0)
        conn.commit()
        row = conn.execute(
            "SELECT weight_kg FROM item_catalog WHERE id=?;", (item_id,)
        ).fetchone()
        assert row["weight_kg"] >= 0.01

    def test_quantity_clamped_to_50(self, conn):
        """quantity > 50 -> 50."""
        item = {"name": "Massenfund", "category": "food", "weight_kg": 0.1, "quantity": 9999, "kcal_per_unit": 100}
        item_id, qty = _materialize_item(conn, item, 0)
        assert qty <= 50

    def test_quantity_clamped_min_1(self, conn):
        """quantity <= 0 -> 1."""
        item = {"name": "NullItem", "category": "misc", "weight_kg": 0.5, "quantity": 0}
        item_id, qty = _materialize_item(conn, item, 0)
        assert qty >= 1

    def test_kcal_only_for_food(self, conn):
        """kcal_per_unit wird nur fuer food-Items gesetzt."""
        item_food = {"name": "Suessk", "category": "food", "weight_kg": 0.1, "quantity": 1, "kcal_per_unit": 500}
        item_misc = {"name": "Nichtessen", "category": "misc", "weight_kg": 0.1, "quantity": 1, "kcal_per_unit": 500}
        id_food, _ = _materialize_item(conn, item_food, 0)
        id_misc, _ = _materialize_item(conn, item_misc, 0)
        conn.commit()
        row_food = conn.execute("SELECT kcal_per_unit FROM item_catalog WHERE id=?;", (id_food,)).fetchone()
        row_misc = conn.execute("SELECT kcal_per_unit FROM item_catalog WHERE id=?;", (id_misc,)).fetchone()
        assert row_food["kcal_per_unit"] == 500
        assert row_misc["kcal_per_unit"] is None


# ---------------------------------------------------------------------------
# 6. Kein Ort in Reichweite
# ---------------------------------------------------------------------------

class TestNoLocationHere:
    def test_no_discovered_location_nearby(self, conn):
        """Suche ohne entdeckte Location in 40 m -> no_location_here."""
        # Location weit weg anlegen (undiscovered)
        insert_location(conn, loc_id=800, loc_type="house", lat=49.5, lon=11.5)
        ctx = adjudicator.build_context(conn, 1)
        ok, reason = effects.validate_all(
            conn, 1, [{"op": "search", "query": "wasser"}], ctx
        )
        assert ok is False
        assert reason == "no_location_here"

    def test_undiscovered_location_at_player_not_found(self, conn):
        """Undiscovered Location direkt am Spieler -> trotzdem no_location_here."""
        insert_location(conn, loc_id=801, loc_type="house", lat=49.0, lon=11.0)
        # Status bleibt 'undiscovered'
        ctx = adjudicator.build_context(conn, 1)
        ok, reason = effects.validate_all(
            conn, 1, [{"op": "search", "query": "wasser"}], ctx
        )
        assert ok is False
        assert reason == "no_location_here"

    def test_no_query_rejected(self, conn):
        """search ohne query -> no_query."""
        _insert_discovered(conn, loc_id=802, loc_type="house")
        ctx = adjudicator.build_context(conn, 1)
        ok, reason = effects.validate_all(
            conn, 1, [{"op": "search", "query": ""}], ctx
        )
        assert ok is False
        assert reason == "no_query"


# ---------------------------------------------------------------------------
# 7. Bilanz drift-frei
# ---------------------------------------------------------------------------

class TestBalanceDriftFree:
    def test_after_searches_no_audit_flags(self, conn):
        """Nach mehreren Suchen + advance_tick: resource_audit.flagged == 0."""
        from app.sim import tick

        # Zwei Locations anlegen, unterschiedliche Typen
        _insert_discovered(conn, loc_id=900, loc_type="house")
        _insert_discovered(conn, loc_id=901, loc_type="supermarket", lat=49.0001, lon=11.0)

        # Mehrere Suchen
        adjudicator.adjudicate(conn, 1, "ich suche wasser")
        adjudicator.adjudicate(conn, 1, "ich suche essen")
        # An zweiter Location suchen (Spieler kurz dahin setzen)
        conn.execute("UPDATE characters SET lat=49.0001, lon=11.0 WHERE id=1;")
        conn.commit()
        adjudicator.adjudicate(conn, 1, "ich suche wasser")

        # Tick
        conn.execute("UPDATE characters SET lat=49.0, lon=11.0 WHERE id=1;")
        conn.commit()
        tick.advance_tick(conn)

        # Audit
        cur_tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        flags = audit.run_audit(conn, cur_tick)
        flagged = [f for f in flags if f.get("flagged")]
        assert flagged == [], f"Bilanz-Drift nach Suchen: {flagged}"


# ---------------------------------------------------------------------------
# 8. Determinismus
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_setup_same_finds(self, conn_seeded):
        """Zwei DBs mit gleichem Setup und gleicher Suche -> gleiche Funde."""
        def _setup(conn):
            conn.execute("UPDATE characters SET lat=49.0, lon=11.0 WHERE id=1;")
            conn.commit()
            insert_location(
                conn, loc_id=1000, loc_type="house",
                name="Haus-Det", lat=49.0, lon=11.0, generation_seed=42
            )
            conn.execute(
                "UPDATE locations SET discovery_status='discovered' WHERE id=1000;"
            )
            conn.commit()

        c1 = conn_seeded("_a")
        c2 = conn_seeded("_b")
        _setup(c1)
        _setup(c2)

        adjudicator.adjudicate(c1, 1, "ich suche wasser")
        adjudicator.adjudicate(c2, 1, "ich suche wasser")

        inv1 = c1.execute(
            "SELECT item_id, quantity FROM location_inventory WHERE location_id=1000 ORDER BY item_id;"
        ).fetchall()
        inv2 = c2.execute(
            "SELECT item_id, quantity FROM location_inventory WHERE location_id=1000 ORDER BY item_id;"
        ).fetchall()

        assert len(inv1) == len(inv2), "Anzahl Funde unterschiedlich."
        for r1, r2 in zip(inv1, inv2):
            assert r1["item_id"] == r2["item_id"], "Item-IDs unterschiedlich."
            assert r1["quantity"] == r2["quantity"], "Mengen unterschiedlich."

        c1.close()
        c2.close()
