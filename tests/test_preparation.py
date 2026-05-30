"""Tests für app/sim/resources.py — prepare() und eat() mit needs_preparation.

Prüft: Erfolgsfall, Fehlerfälle, Bilanzfreiheit nach prepare, eat-Ablehnung roher Items.
"""
from __future__ import annotations

import pytest
from app.sim import audit, ledger
from app.sim.resources import eat, prepare


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _add_item(conn, item_id: str, qty: float, group_id: int = 1):
    """Legt Item ins Gruppen-Inventar und bucht es ins Ledger (Quelle)."""
    conn.execute(
        "INSERT OR REPLACE INTO group_inventory "
        "(group_id, item_id, quantity, quality, acquired_tick) VALUES (?,?,?,1.0,0);",
        (group_id, item_id, qty),
    )
    ledger.add(conn, item_id, qty)
    conn.commit()


def _inv(conn, item_id: str, group_id: int = 1) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory "
        "WHERE group_id=? AND item_id=?;",
        (group_id, item_id),
    ).fetchone()
    return row["q"]


def _setup_prepare_ingredients(conn):
    """Legt alle nötigen Zutaten für pasta_500g → meal_pasta bereit."""
    _add_item(conn, "pasta_500g", 2.0)   # 2 Portionen roh
    _add_item(conn, "water_1l", 2.0)     # braucht 0.5 L pro Portion → 1 L genug
    _add_item(conn, "firewood", 3.0)     # Hitzequelle (1 Scheit pro Zubereitung)


# ---------------------------------------------------------------------------
# TestPrepareSuccess
# ---------------------------------------------------------------------------

class TestPrepareSuccess:
    def test_prepare_returns_ok(self, conn):
        _setup_prepare_ingredients(conn)
        result = prepare(conn, 1)
        assert result["ok"] is True
        assert result["prepared"] == "meal_pasta"
        assert result["from"] == "pasta_500g"

    def test_prepare_creates_meal_pasta(self, conn):
        _setup_prepare_ingredients(conn)
        before = _inv(conn, "meal_pasta")
        prepare(conn, 1)
        after = _inv(conn, "meal_pasta")
        assert after == before + 1.0

    def test_prepare_consumes_raw_pasta(self, conn):
        _setup_prepare_ingredients(conn)
        before = _inv(conn, "pasta_500g")
        prepare(conn, 1)
        after = _inv(conn, "pasta_500g")
        assert abs(after - (before - 1.0)) < 1e-9

    def test_prepare_consumes_water(self, conn):
        """pasta_500g braucht requires_water_l=0.5 → 0.5 L Wasser wird verbraucht."""
        _setup_prepare_ingredients(conn)
        before = _inv(conn, "water_1l")
        prepare(conn, 1)
        after = _inv(conn, "water_1l")
        assert abs(after - (before - 0.5)) < 1e-9

    def test_prepare_consumes_firewood(self, conn):
        """1 Scheit Brennholz wird pro Zubereitung verbraucht."""
        _setup_prepare_ingredients(conn)
        before = _inv(conn, "firewood")
        prepare(conn, 1)
        after = _inv(conn, "firewood")
        assert abs(after - (before - 1.0)) < 1e-9

    def test_prepare_with_explicit_item_id(self, conn):
        _setup_prepare_ingredients(conn)
        result = prepare(conn, 1, item_id="pasta_500g")
        assert result["ok"] is True
        assert result["from"] == "pasta_500g"


# ---------------------------------------------------------------------------
# TestPrepareFailures
# ---------------------------------------------------------------------------

class TestPrepareFailures:
    def test_nothing_to_prepare(self, conn):
        """Kein zubereitbares Item → nothing_to_prepare."""
        _add_item(conn, "water_1l", 2.0)
        _add_item(conn, "firewood", 2.0)
        result = prepare(conn, 1)
        assert result["ok"] is False
        assert result["reason"] == "nothing_to_prepare"

    def test_no_water(self, conn):
        """Rohes Item vorhanden, aber kein Wasser → no_water."""
        _add_item(conn, "pasta_500g", 1.0)
        _add_item(conn, "firewood", 2.0)
        result = prepare(conn, 1)
        assert result["ok"] is False
        assert result["reason"] == "no_water"

    def test_not_enough_water(self, conn):
        """Zu wenig Wasser (< requires_water_l=0.5) → no_water."""
        _add_item(conn, "pasta_500g", 1.0)
        _add_item(conn, "water_1l", 0.1)  # Nur 0.1 L, braucht 0.5 L
        _add_item(conn, "firewood", 2.0)
        result = prepare(conn, 1)
        assert result["ok"] is False
        assert result["reason"] == "no_water"

    def test_no_heat(self, conn):
        """Rohes Item + Wasser vorhanden, aber kein Feuerholz → no_heat."""
        _add_item(conn, "pasta_500g", 1.0)
        _add_item(conn, "water_1l", 2.0)
        result = prepare(conn, 1)
        assert result["ok"] is False
        assert result["reason"] == "no_heat"

    def test_prepare_dead_character(self, conn):
        """Toter Charakter kann nicht zubereiten."""
        _setup_prepare_ingredients(conn)
        conn.execute("UPDATE characters SET is_alive=0 WHERE id=1;")
        conn.commit()
        result = prepare(conn, 1)
        assert result["ok"] is False
        assert result["reason"] == "no_such_living_character"

    def test_prepare_nonexistent_character(self, conn):
        """Nicht-existierender Charakter → Fehler."""
        _setup_prepare_ingredients(conn)
        result = prepare(conn, 9999)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# TestEatRejectsRaw
# ---------------------------------------------------------------------------

class TestEatRejectsRaw:
    def test_eat_raw_pasta_rejected(self, conn):
        """pasta_500g hat needs_preparation=1 → eat() lehnt ab."""
        _add_item(conn, "pasta_500g", 2.0)
        result = eat(conn, 1, item_id="pasta_500g")
        assert result["ok"] is False
        assert result["reason"] == "no_food"

    def test_eat_raw_pasta_inventory_unchanged(self, conn):
        """Nach abgelehntem Essen bleibt Inventar unverändert."""
        _add_item(conn, "pasta_500g", 2.0)
        before = _inv(conn, "pasta_500g")
        eat(conn, 1, item_id="pasta_500g")
        after = _inv(conn, "pasta_500g")
        assert abs(after - before) < 1e-9

    def test_eat_meal_pasta_accepted(self, conn):
        """meal_pasta (needs_preparation=0) kann gegessen werden."""
        _add_item(conn, "meal_pasta", 1.0)
        conn.execute("UPDATE characters SET hunger=0.1 WHERE id=1;")
        conn.commit()
        result = eat(conn, 1, item_id="meal_pasta")
        assert result["ok"] is True
        assert result["item"] == "meal_pasta"

    def test_eat_meal_pasta_increases_hunger(self, conn):
        """Gekochte Nudeln erhöhen den Hunger-Wert."""
        _add_item(conn, "meal_pasta", 1.0)
        conn.execute("UPDATE characters SET hunger=0.1 WHERE id=1;")
        conn.commit()
        h_before = conn.execute(
            "SELECT hunger FROM characters WHERE id=1;"
        ).fetchone()["hunger"]
        eat(conn, 1, item_id="meal_pasta")
        h_after = conn.execute(
            "SELECT hunger FROM characters WHERE id=1;"
        ).fetchone()["hunger"]
        assert h_after > h_before

    def test_eat_no_food_when_only_raw(self, conn):
        """Nur rohes Item vorhanden, kein anderes Essen → no_food."""
        _add_item(conn, "pasta_500g", 5.0)
        result = eat(conn, 1)
        assert result["ok"] is False
        assert result["reason"] == "no_food"


# ---------------------------------------------------------------------------
# TestPrepareBilanz — drift-freie Bilanz nach prepare
# ---------------------------------------------------------------------------

class TestPrepareBilanz:
    def test_no_drift_after_prepare(self, conn):
        """Nach prepare + Audit: resource_audit.flagged == 0 für alle Items."""
        _setup_prepare_ingredients(conn)
        prepare(conn, 1)

        now_tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        # Audit direkt aufrufen (kein full tick nötig)
        with conn:
            interrupts = audit.run_audit(conn, now_tick)

        flagged = conn.execute(
            "SELECT COALESCE(SUM(flagged),0) AS f FROM resource_audit;"
        ).fetchone()["f"]
        assert flagged == 0, (
            f"Bilanz-Drift nach prepare: {flagged} Flags. "
            f"Interrupts: {[i.get('message') for i in interrupts if i.get('category')=='system']}"
        )

    def test_no_drift_after_prepare_and_eat(self, conn):
        """Nach prepare + eat: Bilanz bleibt drift-frei."""
        _setup_prepare_ingredients(conn)
        prepare(conn, 1)

        conn.execute("UPDATE characters SET hunger=0.1 WHERE id=1;")
        conn.commit()
        eat(conn, 1, item_id="meal_pasta")

        now_tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        with conn:
            audit.run_audit(conn, now_tick)
        flagged = conn.execute(
            "SELECT COALESCE(SUM(flagged),0) AS f FROM resource_audit;"
        ).fetchone()["f"]
        assert flagged == 0

    def test_no_drift_after_multiple_prepares(self, conn):
        """Mehrfache Zubereitung — Bilanz bleibt drift-frei."""
        _add_item(conn, "pasta_500g", 3.0)
        _add_item(conn, "water_1l", 5.0)
        _add_item(conn, "firewood", 5.0)

        for _ in range(3):
            r = prepare(conn, 1)
            assert r["ok"] is True

        now_tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        with conn:
            audit.run_audit(conn, now_tick)
        flagged = conn.execute(
            "SELECT COALESCE(SUM(flagged),0) AS f FROM resource_audit;"
        ).fetchone()["f"]
        assert flagged == 0

    def test_prepare_ledger_entries_correct(self, conn):
        """Ledger bucht Quellen und Senken korrekt."""
        _setup_prepare_ingredients(conn)

        # Vor dem Prepare: Ledger-Stand merken
        before = ledger.expected_totals(conn)

        prepare(conn, 1)

        after = ledger.expected_totals(conn)

        # pasta_500g: -1 gebucht
        assert abs(after.get("pasta_500g", 0.0) - before.get("pasta_500g", 0.0) - (-1.0)) < 1e-9
        # water_1l: -0.5 gebucht
        assert abs(after.get("water_1l", 0.0) - before.get("water_1l", 0.0) - (-0.5)) < 1e-9
        # firewood: -1.0 gebucht
        assert abs(after.get("firewood", 0.0) - before.get("firewood", 0.0) - (-1.0)) < 1e-9
        # meal_pasta: +1.0 gebucht
        assert abs(after.get("meal_pasta", 0.0) - before.get("meal_pasta", 0.0) - 1.0) < 1e-9
