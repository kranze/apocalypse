"""Tests für app/sim/provision.py — automatische Bedürfnis-Versorgung.

Abgedeckt:
  1. Durst: trinkt water_1l aus Rucksack
  2. Hunger: isst fertiges Essen
  3. Hunger aus Ort: Location-Vorrat, wenn Rucksack leer
  4. Auto-Kochen: rohe Zutat + Wasser + Feuerholz → meal_pasta wird erzeugt und gegessen
  5. Nur bis PROVISION_TARGET versorgen
  6. Ohne Vorrat: nichts passiert
  7. Schlaf: steigt beim Ruhen (idle), NICHT wenn path_json gesetzt
  8. Bilanz drift-frei nach mehreren Ticks
  9. Determinismus: gleicher Setup → gleiche Ergebnisse in zwei DBs
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
from app.sim import constants, ledger
from app.sim.provision import auto_provision
from app.sim.audit import run_audit


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
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _add_backpack(conn, item_id: str, qty: float, group_id: int = 1):
    """Legt Items in den Gruppen-Rucksack und bucht ins Ledger.

    Nutzt ON CONFLICT DO UPDATE (UPSERT), damit die Menge genau einmal
    akkumuliert wird und der Ledger-Delta passt.
    """
    conn.execute(
        "INSERT INTO group_inventory (group_id, item_id, quantity, quality, acquired_tick) "
        "VALUES (?,?,?,1.0,0) "
        "ON CONFLICT(group_id, item_id, quality) DO UPDATE SET quantity = quantity + excluded.quantity;",
        (group_id, item_id, qty),
    )
    ledger.add(conn, item_id, qty)
    conn.commit()


def _add_to_location(conn, loc_id: int, item_id: str, qty: float):
    """Legt Items in eine Location und bucht ins Ledger."""
    conn.execute(
        "INSERT INTO location_inventory "
        "(location_id, item_id, quantity, quality, produced_tick) VALUES (?,?,?,1.0,0);",
        (loc_id, item_id, qty),
    )
    ledger.add(conn, item_id, qty)
    conn.commit()


def _backpack_qty(conn, item_id: str, group_id: int = 1) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(quantity),0) q FROM group_inventory WHERE group_id=? AND item_id=?;",
        (group_id, item_id),
    ).fetchone()["q"]


def _char(conn, field: str, char_id: int = 1):
    return conn.execute(f"SELECT {field} FROM characters WHERE id=?;", (char_id,)).fetchone()[field]


def _set_needs(conn, hunger=1.0, thirst=1.0, sleep=1.0, path_json=None, char_id=1):
    conn.execute(
        "UPDATE characters SET hunger=?, thirst=?, sleep=?, path_json=? WHERE id=?;",
        (hunger, thirst, sleep, path_json, char_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Durst: trinkt aus Rucksack
# ---------------------------------------------------------------------------

class TestAutoProvisionThirst:
    def test_drinks_water_from_backpack_when_thirsty(self, conn):
        """Durstig + Wasser im Rucksack → trinkt."""
        _add_backpack(conn, "water_1l", 5.0)
        _set_needs(conn, thirst=0.1)
        with conn:
            events = auto_provision(conn, constants.TICK_MINUTES, 10)
        thirst_after = _char(conn, "thirst")
        assert thirst_after > 0.1

    def test_water_consumed_from_backpack(self, conn):
        """Wasser-Menge im Rucksack sinkt nach Trinken."""
        _add_backpack(conn, "water_1l", 5.0)
        _set_needs(conn, thirst=0.1)
        before = _backpack_qty(conn, "water_1l")
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        after = _backpack_qty(conn, "water_1l")
        assert after < before

    def test_no_drink_if_no_water(self, conn):
        """Kein Wasser vorhanden → kein Trinken."""
        _set_needs(conn, thirst=0.1)
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        thirst_after = _char(conn, "thirst")
        assert abs(thirst_after - 0.1) < 1e-6

    def test_drink_only_until_target(self, conn):
        """Trinkt nur bis PROVISION_TARGET, nicht darüber hinaus."""
        _add_backpack(conn, "water_1l", 50.0)
        _set_needs(conn, thirst=0.1)
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        thirst_after = _char(conn, "thirst")
        assert thirst_after <= constants.PROVISION_TARGET + 0.01

    def test_no_drink_above_trigger(self, conn):
        """Durst über PROVISION_TARGET → kein Trinken (unnötig)."""
        _add_backpack(conn, "water_1l", 5.0)
        # PROVISION_TARGET = 0.9; wenn thirst schon 0.9, kein weiteres Trinken
        _set_needs(conn, thirst=constants.PROVISION_TARGET)
        before = _backpack_qty(conn, "water_1l")
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        after = _backpack_qty(conn, "water_1l")
        assert abs(after - before) < 1e-6


# ---------------------------------------------------------------------------
# Hunger: isst Fertiges
# ---------------------------------------------------------------------------

class TestAutoProvisionHunger:
    def test_eats_ready_food_from_backpack(self, conn):
        """Hungry + canned_beans im Rucksack → isst."""
        _add_backpack(conn, "canned_beans", 3.0)
        _set_needs(conn, hunger=0.1)
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        hunger_after = _char(conn, "hunger")
        assert hunger_after > 0.1

    def test_food_consumed_from_backpack(self, conn):
        """Nahrung-Menge sinkt nach Essen."""
        _add_backpack(conn, "canned_beans", 3.0)
        _set_needs(conn, hunger=0.1)
        before = _backpack_qty(conn, "canned_beans")
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        after = _backpack_qty(conn, "canned_beans")
        assert after < before

    def test_no_eating_if_no_food(self, conn):
        """Kein Essen vorhanden → Hunger bleibt."""
        _set_needs(conn, hunger=0.1)
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        hunger_after = _char(conn, "hunger")
        assert abs(hunger_after - 0.1) < 1e-6

    def test_eat_only_until_target(self, conn):
        """Isst bis mindestens PROVISION_TARGET (Überschuss durch Item-Größe möglich)."""
        _add_backpack(conn, "canned_beans", 50.0)
        _set_needs(conn, hunger=0.1)
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        hunger_after = _char(conn, "hunger")
        # Der Hunger muss mindestens PROVISION_TARGET erreichen
        assert hunger_after >= constants.PROVISION_TARGET
        # Darf 1.0 nicht überschreiten (Clamp)
        assert hunger_after <= 1.0


# ---------------------------------------------------------------------------
# Hunger aus Ort (entdeckte Location am Spielerort)
# ---------------------------------------------------------------------------

class TestAutoProvisionFromLocation:
    def _setup_location_at_player(self, conn, loc_id=300):
        """Legt eine entdeckte Location direkt beim Spieler an."""
        insert_location(conn, loc_id=loc_id, lat=49.0, lon=11.0)
        conn.execute("UPDATE locations SET discovery_status='discovered' WHERE id=?;", (loc_id,))
        conn.execute("UPDATE characters SET lat=49.0, lon=11.0 WHERE id=1;")
        conn.commit()
        return loc_id

    def test_eats_from_location_when_backpack_empty(self, conn):
        """Rucksack leer, aber Location hat Essen → isst aus der Location."""
        loc_id = self._setup_location_at_player(conn)
        _add_to_location(conn, loc_id, "canned_beans", 3.0)
        _set_needs(conn, hunger=0.1)
        before_loc = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) q FROM location_inventory WHERE location_id=? AND item_id='canned_beans';",
            (loc_id,),
        ).fetchone()["q"]
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        hunger_after = _char(conn, "hunger")
        after_loc = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) q FROM location_inventory WHERE location_id=? AND item_id='canned_beans';",
            (loc_id,),
        ).fetchone()["q"]
        assert hunger_after > 0.1
        assert after_loc < before_loc

    def test_drinks_from_location_when_backpack_empty(self, conn):
        """Rucksack leer, aber Location hat Wasser → trinkt aus Location."""
        loc_id = self._setup_location_at_player(conn)
        _add_to_location(conn, loc_id, "water_1l", 5.0)
        _set_needs(conn, thirst=0.1)
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        thirst_after = _char(conn, "thirst")
        assert thirst_after > 0.1

    def test_undiscovered_location_not_used(self, conn):
        """Undiscovered Location (auch wenn in der Nähe) wird NICHT genutzt."""
        insert_location(conn, loc_id=301, lat=49.0, lon=11.0)
        # Status bleibt 'undiscovered'
        conn.execute("UPDATE characters SET lat=49.0, lon=11.0 WHERE id=1;")
        conn.commit()
        _add_to_location(conn, 301, "canned_beans", 5.0)
        # Menge an Location direkt ohne Entdeckung (direkt insertet): ledger schon gebucht
        _set_needs(conn, hunger=0.1)
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        hunger_after = _char(conn, "hunger")
        # Sollte NICHTS gegessen haben
        assert abs(hunger_after - 0.1) < 1e-6


# ---------------------------------------------------------------------------
# Auto-Kochen: rohe Zutat + Wasser + Feuerholz → meal_pasta
# ---------------------------------------------------------------------------

class TestAutoProvisionCooking:
    def test_auto_cook_when_only_raw_ingredient(self, conn):
        """Nur pasta_500g + Wasser + Feuerholz → meal_pasta wird erzeugt und gegessen."""
        _add_backpack(conn, "pasta_500g", 1.0)
        _add_backpack(conn, "water_1l", 2.0)
        _add_backpack(conn, "firewood", 2.0)
        _set_needs(conn, hunger=0.1)
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        hunger_after = _char(conn, "hunger")
        assert hunger_after > 0.1, "Auto-Kochen sollte Hunger erhöhen"

    def test_auto_cook_consumes_raw_ingredient(self, conn):
        """pasta_500g wird beim Auto-Kochen verbraucht."""
        _add_backpack(conn, "pasta_500g", 2.0)
        _add_backpack(conn, "water_1l", 5.0)
        _add_backpack(conn, "firewood", 5.0)
        _set_needs(conn, hunger=0.1)
        before = _backpack_qty(conn, "pasta_500g")
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        after = _backpack_qty(conn, "pasta_500g")
        assert after < before

    def test_auto_cook_consumes_firewood(self, conn):
        """Feuerholz wird beim Kochen verbraucht."""
        _add_backpack(conn, "pasta_500g", 1.0)
        _add_backpack(conn, "water_1l", 2.0)
        _add_backpack(conn, "firewood", 2.0)
        _set_needs(conn, hunger=0.1)
        before = _backpack_qty(conn, "firewood")
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        after = _backpack_qty(conn, "firewood")
        assert after < before

    def test_no_cook_without_water(self, conn):
        """Ohne Wasser: kein Kochen, Hunger bleibt."""
        _add_backpack(conn, "pasta_500g", 1.0)
        _add_backpack(conn, "firewood", 2.0)
        _set_needs(conn, hunger=0.1)
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        hunger_after = _char(conn, "hunger")
        assert abs(hunger_after - 0.1) < 1e-6

    def test_no_cook_without_firewood(self, conn):
        """Ohne Feuerholz: kein Kochen, Hunger bleibt."""
        _add_backpack(conn, "pasta_500g", 1.0)
        _add_backpack(conn, "water_1l", 2.0)
        _set_needs(conn, hunger=0.1)
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        hunger_after = _char(conn, "hunger")
        assert abs(hunger_after - 0.1) < 1e-6


# ---------------------------------------------------------------------------
# Schlaf: Ruhen vs. Unterwegs
# ---------------------------------------------------------------------------

class TestAutoProvisionSleep:
    def test_sleep_recovers_when_idle(self, conn):
        """Idle (path_json=NULL) und müde → Schlaf steigt."""
        _set_needs(conn, sleep=0.1, path_json=None)
        with conn:
            auto_provision(conn, constants.MINUTES_PER_DAY, 1440)
        sleep_after = _char(conn, "sleep")
        assert sleep_after > 0.1

    def test_sleep_does_not_recover_when_moving(self, conn):
        """Unterwegs (path_json gesetzt) → Schlaf wird NICHT erholt."""
        _set_needs(conn, sleep=0.1, path_json='[[49.0,11.0],[49.001,11.001]]')
        sleep_before = _char(conn, "sleep")
        with conn:
            auto_provision(conn, constants.MINUTES_PER_DAY, 1440)
        sleep_after = _char(conn, "sleep")
        # auto_provision ändert sleep nur wenn idle; beim Bewegen passiert nichts
        assert abs(sleep_after - sleep_before) < 1e-6

    def test_sleep_only_rests_below_threshold(self, conn):
        """Schlaf über SLEEP_REST_BELOW → kein automatisches Ruhen."""
        above = constants.SLEEP_REST_BELOW + 0.01
        _set_needs(conn, sleep=above, path_json=None)
        # Damit kein anderer Effekt den Schlaf verändert: Provision direkt aufrufen
        sleep_before = _char(conn, "sleep")
        with conn:
            auto_provision(conn, constants.TICK_MINUTES, 10)
        sleep_after = _char(conn, "sleep")
        assert abs(sleep_after - sleep_before) < 1e-6

    def test_sleep_not_above_one(self, conn):
        """Schlaf übersteigt nie 1.0."""
        _set_needs(conn, sleep=0.01, path_json=None)
        # Viele Ticks ruhen
        for i in range(10):
            with conn:
                auto_provision(conn, constants.MINUTES_PER_DAY, i * 1440)
        sleep_after = _char(conn, "sleep")
        assert sleep_after <= 1.0


# ---------------------------------------------------------------------------
# Bilanz drift-frei
# ---------------------------------------------------------------------------

class TestAutoProvisionAuditClean:
    def test_no_audit_drift_after_provision_ticks(self, conn):
        """Nach mehreren Ticks mit Versorgung: resource_audit flagged == 0."""
        from app.sim.tick import advance_tick

        _add_backpack(conn, "water_1l", 20.0)
        _add_backpack(conn, "canned_beans", 10.0)
        _set_needs(conn, hunger=0.1, thirst=0.1)

        for _ in range(5):
            advance_tick(conn, constants.TICK_MINUTES)

        flagged = conn.execute(
            "SELECT COALESCE(SUM(flagged),0) f FROM resource_audit;"
        ).fetchone()["f"]
        assert flagged == 0, f"Audit-Flags nach Versorgung: {flagged}"

    def test_no_audit_drift_after_cooking_tick(self, conn):
        """Auto-Kochen bucht korrekt; kein Drift nach Tick."""
        from app.sim.tick import advance_tick

        _add_backpack(conn, "pasta_500g", 3.0)
        _add_backpack(conn, "water_1l", 10.0)
        _add_backpack(conn, "firewood", 5.0)
        _set_needs(conn, hunger=0.1, thirst=0.8)

        advance_tick(conn, constants.TICK_MINUTES)

        flagged = conn.execute(
            "SELECT COALESCE(SUM(flagged),0) f FROM resource_audit;"
        ).fetchone()["f"]
        assert flagged == 0, f"Audit-Flags nach Auto-Kochen: {flagged}"


# ---------------------------------------------------------------------------
# Determinismus
# ---------------------------------------------------------------------------

class TestAutoProvisionDeterminism:
    def test_identical_setup_identical_results(self):
        """Zwei DBs, gleicher Seed/Setup, N Ticks → identische Bedürfnis-Achsen."""
        from app.sim.tick import advance_tick

        def _make_db():
            c = make_conn()
            set_seed(c, 1337)
            # gleicher Setup
            c.execute("UPDATE characters SET hunger=0.3, thirst=0.3, sleep=0.3 WHERE id=1;")
            c.commit()
            _add_backpack(c, "water_1l", 10.0)
            _add_backpack(c, "canned_beans", 5.0)
            return c

        c1, c2 = _make_db(), _make_db()

        ticks = 10
        for _ in range(ticks):
            advance_tick(c1, constants.TICK_MINUTES)
            advance_tick(c2, constants.TICK_MINUTES)

        for field in ("hunger", "thirst", "sleep", "performance"):
            v1 = c1.execute(f"SELECT {field} FROM characters WHERE id=1;").fetchone()[field]
            v2 = c2.execute(f"SELECT {field} FROM characters WHERE id=1;").fetchone()[field]
            assert abs(v1 - v2) < 1e-9, f"{field}: {v1} vs {v2}"

        c1.close()
        c2.close()

    def test_different_initial_inventory_diverges(self):
        """Unterschiedliche Bestände → andere Ergebnisse (kein falscher Gleichlauf)."""
        from app.sim.tick import advance_tick

        c1 = make_conn()
        set_seed(c1, 1337)
        c2 = make_conn()
        set_seed(c2, 1337)

        c1.execute("UPDATE characters SET hunger=0.1, thirst=0.1 WHERE id=1;")
        c1.commit()
        c2.execute("UPDATE characters SET hunger=0.1, thirst=0.1 WHERE id=1;")
        c2.commit()

        # c1 hat Essen, c2 nicht
        _add_backpack(c1, "canned_beans", 10.0)
        _add_backpack(c1, "water_1l", 10.0)

        for _ in range(5):
            advance_tick(c1, constants.TICK_MINUTES)
            advance_tick(c2, constants.TICK_MINUTES)

        h1 = c1.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]
        h2 = c2.execute("SELECT hunger FROM characters WHERE id=1;").fetchone()["hunger"]
        assert h1 != h2, "Mit/ohne Essen müssen unterschiedliche Hunger-Werte erzeugen"
        c1.close()
        c2.close()
