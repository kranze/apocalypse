"""Tests für das erweiterte Bedürfnis-/Biologie-System:
  - compute_targets (Mifflin-St-Jeor, Wasser)
  - apply_thirst / apply_sleep (Zerfall, Aktivitäts-Einfluss)
  - recompute_performance (multiplikativ: Hunger × Durst × Schlaf)
  - recompute_satisfaction (Ziel-Näherung, schwächste Achse, Shelter-Bonus)
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

from tests.conftest import make_conn, set_seed
from app.sim import constants
from app.sim.biology import (
    apply_thirst,
    apply_sleep,
    compute_targets,
    recompute_performance,
    recompute_satisfaction,
)


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
# compute_targets
# ---------------------------------------------------------------------------

class TestComputeTargets:
    def test_male_known_values(self):
        """Mann, 80 kg, 180 cm, 30 Jahre → kcal ca. 2492, Wasser ca. 2.8 L."""
        kcal, water = compute_targets("m", 80.0, 180.0, 30)
        # BMR = 10*80 + 6.25*180 − 5*30 + 5 = 800+1125−150+5 = 1780; ×1.4 = 2492
        assert abs(kcal - 2492.0) < 5.0, f"kcal: {kcal}"
        # Wasser: 80 × 35 / 1000 = 2.80
        assert abs(water - 2.80) < 0.05, f"water: {water}"

    def test_female_differs_from_male(self):
        """Frau hat anderen BMR-Offset → andere kcal."""
        kcal_m, _ = compute_targets("m", 70.0, 170.0, 30)
        kcal_f, _ = compute_targets("f", 70.0, 170.0, 30)
        assert kcal_m != kcal_f

    def test_female_lower_kcal(self):
        """Mifflin-St-Jeor: Frauen-Offset −161 < Männer-Offset +5 → weniger kcal."""
        kcal_m, _ = compute_targets("m", 70.0, 170.0, 30)
        kcal_f, _ = compute_targets("f", 70.0, 170.0, 30)
        assert kcal_f < kcal_m

    def test_unknown_sex_between_m_and_f(self):
        """Unbekanntes Geschlecht verwendet Mittel → liegt zwischen m und f."""
        kcal_m, _ = compute_targets("m", 70.0, 170.0, 30)
        kcal_f, _ = compute_targets("f", 70.0, 170.0, 30)
        kcal_x, _ = compute_targets("x", 70.0, 170.0, 30)
        assert kcal_f <= kcal_x <= kcal_m

    def test_none_sex_uses_average(self):
        """None als Geschlecht → robust, kein Crash."""
        kcal, water = compute_targets(None, 70.0, 170.0, 30)
        assert kcal > 0 and water > 0

    def test_all_none_uses_defaults(self):
        """Alle Felder None → Defaults, kein Crash."""
        kcal, water = compute_targets(None, None, None, None)
        # Default: 75 kg, 175 cm, 35 Jahre, Mittel-Offset
        assert kcal >= 1200.0
        assert water > 0.0

    def test_water_scales_with_weight(self):
        """Schwerer Charakter braucht mehr Wasser."""
        _, w50 = compute_targets("m", 50.0, 170.0, 30)
        _, w100 = compute_targets("m", 100.0, 170.0, 30)
        assert w100 > w50

    def test_minimum_kcal_floor(self):
        """Sehr kleiner/junger Charakter unterschreitet nie 1200 kcal."""
        kcal, _ = compute_targets("f", 30.0, 140.0, 5)
        assert kcal >= 1200.0


# ---------------------------------------------------------------------------
# apply_thirst
# ---------------------------------------------------------------------------

class TestApplyThirst:
    def test_thirst_decreases_over_time(self, conn):
        """Durst sinkt pro Tick."""
        with conn:
            apply_thirst(conn, constants.TICK_MINUTES, constants.TICK_MINUTES)
        thirst = conn.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"]
        assert thirst < 1.0

    def test_thirst_loss_rate_per_day(self, conn):
        """Nach einem Spieltag sinkt Durst um THIRST_LOSS_PER_DAY."""
        with conn:
            apply_thirst(conn, constants.MINUTES_PER_DAY, constants.MINUTES_PER_DAY)
        thirst = conn.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"]
        expected = round(1.0 - constants.THIRST_LOSS_PER_DAY, 5)
        assert abs(thirst - expected) < 1e-4

    def test_thirst_not_below_zero(self, conn):
        """Durst bleibt nach langer Zeit bei 0."""
        for i in range(1, 100):
            with conn:
                apply_thirst(conn, 1440, i * 1440)
        thirst = conn.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"]
        assert thirst >= 0.0

    def test_activity_increases_thirst_loss(self, conn):
        """Mit Bewegung sinkt Durst stärker als ohne."""
        from tests.conftest import make_conn, set_seed
        c_active = make_conn()
        set_seed(c_active, 1337)
        c_idle = make_conn()
        set_seed(c_idle, 1337)

        minutes = 60
        with c_active:
            apply_thirst(c_active, minutes, minutes, distances={1: 5000.0})  # 5 km
        with c_idle:
            apply_thirst(c_idle, minutes, minutes)

        thirst_active = c_active.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"]
        thirst_idle = c_idle.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"]
        assert thirst_active < thirst_idle
        c_active.close()
        c_idle.close()

    def test_no_distances_is_same_as_empty_dict(self, conn):
        """distances=None verhält sich wie {}."""
        from tests.conftest import make_conn, set_seed
        c1 = make_conn()
        set_seed(c1, 1337)
        c2 = make_conn()
        set_seed(c2, 1337)

        minutes = 60
        with c1:
            apply_thirst(c1, minutes, minutes, distances=None)
        with c2:
            apply_thirst(c2, minutes, minutes, distances={})
        t1 = c1.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"]
        t2 = c2.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"]
        assert abs(t1 - t2) < 1e-9
        c1.close()
        c2.close()

    def test_thirst_interrupt_at_crit(self, conn):
        """Interrupt, wenn Durst unter HUNGER_CRIT fällt."""
        from app.sim import constants as C
        above = C.HUNGER_CRIT + 0.01
        conn.execute("UPDATE characters SET thirst=? WHERE id=1;", (above,))
        conn.commit()
        loss_needed = 0.02
        minutes = int(loss_needed * C.MINUTES_PER_DAY / C.THIRST_LOSS_PER_DAY) + 1
        with conn:
            interrupts = apply_thirst(conn, minutes, minutes)
        assert any(i["category"] == "need" for i in interrupts)


# ---------------------------------------------------------------------------
# apply_sleep
# ---------------------------------------------------------------------------

class TestApplySleep:
    def test_sleep_decreases_over_time(self, conn):
        """Schlafdruck sinkt im Wachzustand."""
        with conn:
            apply_sleep(conn, constants.TICK_MINUTES, constants.TICK_MINUTES)
        sleep = conn.execute("SELECT sleep FROM characters WHERE id=1;").fetchone()["sleep"]
        assert sleep < 1.0

    def test_sleep_loss_rate_per_day(self, conn):
        """Nach einem Spieltag sinkt Schlaf um SLEEP_LOSS_PER_DAY."""
        with conn:
            apply_sleep(conn, constants.MINUTES_PER_DAY, constants.MINUTES_PER_DAY)
        sleep = conn.execute("SELECT sleep FROM characters WHERE id=1;").fetchone()["sleep"]
        expected = round(1.0 - constants.SLEEP_LOSS_PER_DAY, 5)
        assert abs(sleep - expected) < 1e-4

    def test_sleep_not_below_zero(self, conn):
        """Schlafwert bleibt nach sehr langer Zeit bei 0."""
        for i in range(1, 50):
            with conn:
                apply_sleep(conn, 1440, i * 1440)
        sleep = conn.execute("SELECT sleep FROM characters WHERE id=1;").fetchone()["sleep"]
        assert sleep >= 0.0

    def test_sleep_interrupt_at_soft_threshold(self, conn):
        """Interrupt, wenn Schlaf unter HUNGER_SOFT fällt."""
        from app.sim import constants as C
        above = C.HUNGER_SOFT + 0.01
        conn.execute("UPDATE characters SET sleep=? WHERE id=1;", (above,))
        conn.commit()
        loss_needed = 0.02
        minutes = int(loss_needed * C.MINUTES_PER_DAY / C.SLEEP_LOSS_PER_DAY) + 1
        with conn:
            interrupts = apply_sleep(conn, minutes, minutes)
        assert any(i["category"] == "need" for i in interrupts)

    def test_dead_chars_not_affected(self, conn):
        """Tote Charaktere erhalten keinen Schlafdruck."""
        conn.execute("UPDATE characters SET is_alive=0, sleep=0.8 WHERE id=1;")
        conn.commit()
        with conn:
            apply_sleep(conn, constants.TICK_MINUTES, constants.TICK_MINUTES)
        sleep = conn.execute("SELECT sleep FROM characters WHERE id=1;").fetchone()["sleep"]
        assert abs(sleep - 0.8) < 1e-9, "Toter Charakter darf nicht verändert werden"


# ---------------------------------------------------------------------------
# recompute_performance — multiplikativ: Hunger × Durst × Schlaf
# ---------------------------------------------------------------------------

class TestRecomputePerformanceMultiplicative:
    def _set_needs(self, conn, hunger, thirst, sleep):
        conn.execute(
            "UPDATE characters SET hunger=?, thirst=?, sleep=? WHERE id=1;",
            (hunger, thirst, sleep),
        )
        conn.commit()

    def test_full_needs_gives_max_performance(self, conn):
        """Alle Achsen voll → Performance 1.0."""
        self._set_needs(conn, 1.0, 1.0, 1.0)
        with conn:
            recompute_performance(conn)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        assert abs(perf - 1.0) < 1e-4

    def test_low_thirst_reduces_performance(self, conn):
        """Niedriger Durst (auch bei vollem Hunger/Schlaf) senkt Performance."""
        self._set_needs(conn, 1.0, 0.0, 1.0)
        with conn:
            recompute_performance(conn)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        assert perf == 0.0

    def test_low_sleep_reduces_performance(self, conn):
        """Niedriger Schlaf (auch bei vollem Hunger/Durst) senkt Performance."""
        self._set_needs(conn, 1.0, 1.0, 0.0)
        with conn:
            recompute_performance(conn)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        assert perf == 0.0

    def test_all_zero_gives_zero_performance(self, conn):
        """Alle Achsen = 0 → Performance = 0."""
        self._set_needs(conn, 0.0, 0.0, 0.0)
        with conn:
            recompute_performance(conn)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        assert perf == 0.0

    def test_performance_is_multiplicative(self, conn):
        """Performance = Penalty(hunger) × Penalty(thirst) × Penalty(sleep)."""
        # Alle bei PERF_COMFORT_HUNGER / 2 = 0.25 → penalty = 0.5 je Achse
        # → performance = 0.5^3 = 0.125
        from app.sim.biology import _penalty
        half_comfort = constants.PERF_COMFORT_HUNGER / 2.0
        self._set_needs(conn, half_comfort, half_comfort, half_comfort)
        with conn:
            recompute_performance(conn)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        expected = _penalty(half_comfort) ** 3
        assert abs(perf - expected) < 1e-4

    def test_performance_only_hunger_axis_old_behavior_still_works(self, conn):
        """Hunger-Penalty funktioniert wie früher (backward-compat)."""
        # Durst/Schlaf bei 1.0 (kein Abzug) → nur Hunger entscheidet
        h = constants.PERF_COMFORT_HUNGER * 0.5  # penalty = 0.5
        self._set_needs(conn, h, 1.0, 1.0)
        with conn:
            recompute_performance(conn)
        perf = conn.execute("SELECT performance FROM characters WHERE id=1;").fetchone()["performance"]
        assert abs(perf - 0.5) < 1e-4


# ---------------------------------------------------------------------------
# recompute_satisfaction
# ---------------------------------------------------------------------------

class TestRecomputeSatisfaction:
    def _set_needs(self, conn, hunger=1.0, thirst=1.0, sleep=1.0, sat=1.0):
        conn.execute(
            "UPDATE characters SET hunger=?, thirst=?, sleep=?, satisfaction=?, lat=NULL, lon=NULL WHERE id=1;",
            (hunger, thirst, sleep, sat),
        )
        conn.commit()

    def test_satisfaction_approaches_target_from_below(self, conn):
        """Wenn Bedürfnisse voll, nähert sich Zufriedenheit 1.0."""
        self._set_needs(conn, hunger=1.0, thirst=1.0, sleep=1.0, sat=0.0)
        with conn:
            recompute_satisfaction(conn, constants.MINUTES_PER_DAY)
        sat = conn.execute("SELECT satisfaction FROM characters WHERE id=1;").fetchone()["satisfaction"]
        assert sat > 0.0

    def test_satisfaction_decreases_when_needs_unmet(self, conn):
        """Bei schlechten Bedürfnissen sinkt die Zufriedenheit von 1.0 Richtung Ziel."""
        self._set_needs(conn, hunger=0.1, thirst=0.1, sleep=0.1, sat=1.0)
        with conn:
            recompute_satisfaction(conn, constants.MINUTES_PER_DAY)
        sat = conn.execute("SELECT satisfaction FROM characters WHERE id=1;").fetchone()["satisfaction"]
        assert sat < 1.0

    def test_weakest_axis_has_stronger_pull(self, conn):
        """Schwächste Achse zieht stärker (SATISFACTION_MIN_WEIGHT).

        Wenn thirst viel schlechter als hunger, drückt thirst die Zufriedenheit
        stärker als ein reiner Durchschnitt.
        """
        # Vergleich: Alle Achsen auf 0.5 vs. eine auf 0.0, Rest auf 1.0 (gleicher Schnitt)
        from tests.conftest import make_conn, set_seed
        c_avg = make_conn()
        set_seed(c_avg, 1337)
        c_weak = make_conn()
        set_seed(c_weak, 1337)

        # c_avg: Alle Achsen gleich → Durchschnitt = 0.5
        c_avg.execute("UPDATE characters SET hunger=0.5, thirst=0.5, sleep=0.5, satisfaction=0.5, lat=NULL, lon=NULL WHERE id=1;")
        c_avg.commit()
        # c_weak: Eine Achse sehr schwach → Durchschnitt auch ~0.5 aber Minimum klein
        c_weak.execute("UPDATE characters SET hunger=1.0, thirst=0.0, sleep=0.5, satisfaction=0.5, lat=NULL, lon=NULL WHERE id=1;")
        c_weak.commit()

        minutes = constants.MINUTES_PER_DAY
        with c_avg:
            recompute_satisfaction(c_avg, minutes)
        with c_weak:
            recompute_satisfaction(c_weak, minutes)

        sat_avg = c_avg.execute("SELECT satisfaction FROM characters WHERE id=1;").fetchone()["satisfaction"]
        sat_weak = c_weak.execute("SELECT satisfaction FROM characters WHERE id=1;").fetchone()["satisfaction"]
        # Bei gleicher Start-satisfaction und ähnlichem Durchschnitt drückt die
        # schwache Achse die Zufriedenheit weiter nach unten als der Durchschnitt.
        assert sat_weak < sat_avg
        c_avg.close()
        c_weak.close()

    def test_shelter_bonus_when_at_discovered_location(self, conn):
        """In einem entdeckten Gebäude: Shelter-Bonus erhöht Ziel."""
        from tests.conftest import insert_location

        # Location direkt beim Spieler
        insert_location(conn, loc_id=200, lat=49.0, lon=11.0)
        conn.execute("UPDATE locations SET discovery_status='discovered' WHERE id=200;")
        conn.execute(
            "UPDATE characters SET hunger=0.8, thirst=0.8, sleep=0.8, satisfaction=0.5, lat=49.0, lon=11.0 WHERE id=1;"
        )
        conn.commit()

        # Vergleich ohne Shelter
        from tests.conftest import make_conn, set_seed
        c_no_shelter = make_conn()
        set_seed(c_no_shelter, 1337)
        c_no_shelter.execute(
            "UPDATE characters SET hunger=0.8, thirst=0.8, sleep=0.8, satisfaction=0.5, lat=NULL, lon=NULL WHERE id=1;"
        )
        c_no_shelter.commit()

        minutes = constants.MINUTES_PER_DAY
        with conn:
            recompute_satisfaction(conn, minutes)
        with c_no_shelter:
            recompute_satisfaction(c_no_shelter, minutes)

        sat_shelter = conn.execute("SELECT satisfaction FROM characters WHERE id=1;").fetchone()["satisfaction"]
        sat_none = c_no_shelter.execute("SELECT satisfaction FROM characters WHERE id=1;").fetchone()["satisfaction"]
        assert sat_shelter > sat_none
        c_no_shelter.close()

    def test_satisfaction_stays_in_bounds(self, conn):
        """Zufriedenheit bleibt immer in [0, 1]."""
        self._set_needs(conn, hunger=0.0, thirst=0.0, sleep=0.0, sat=0.0)
        for i in range(10):
            with conn:
                recompute_satisfaction(conn, constants.MINUTES_PER_DAY)
        sat = conn.execute("SELECT satisfaction FROM characters WHERE id=1;").fetchone()["satisfaction"]
        assert 0.0 <= sat <= 1.0
