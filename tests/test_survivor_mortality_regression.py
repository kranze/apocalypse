"""Regressions-Tests für den Sterbe-RNG-Bug (Issue #20-fix).

Hintergrund (der Bug, jetzt gefixt):
    Die Sterbe-Ziehung im täglichen step_day() nutzte eine schwache LCG-Technik,
    die für alle Survivors an einem Tag ~0,5003 lieferte. Effekt: NUR Säuglinge
    (p_survive=0,30) starben je — Erwachsene (p≈0,997), Greise (p≈0,975) und
    Senioren waren faktisch UNSTERBLICH.

Fix:
    np.random.default_rng(SeedSequence([world_seed, day])) → echte uniforme Werte.

Diese Tests prüfen das SIMULATIONSVERHALTEN über beobachtbare Ausgaben — kein
Zugriff auf interne RNG-Details. Sie wären unter dem alten Bug ROT gewesen.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.conftest import make_conn, set_seed
from app.sim import constants
from app.sim import survivor_sim as sim_mod
from app.sim import popgrid as popgrid_mod

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------
_MINUTES_PER_YEAR = 525_600
# MEET_DIST_KM = 2.0 → 2 km / 111.32 km/° ≈ 0.018°.
# Survivors 0,2° (~22 km) auseinander → garantiert KEINE Gruppenbildung.
_GRID_SPACING_DEG = 0.2

# Minimales 1-Punkt-Dichtefeld für die Migrations-Infrastruktur
_FLAT_GRID = [(0.0, 0.0, 1.0)]


# ---------------------------------------------------------------------------
# Test-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _reset_density():
    sim_mod._density_field = None


def _patch_flat_density(monkeypatch):
    """Patcht load_grid() auf ein winziges flaches Feld → kein Migrations-Bias."""
    _reset_density()
    popgrid_mod.load_grid.cache_clear()
    monkeypatch.setattr(sim_mod, "load_grid", lambda: _FLAT_GRID)


def _make_db(seed: int = 42) -> object:
    """Frische In-Memory-DB mit festem world_seed."""
    conn = make_conn()
    set_seed(conn, seed)
    return conn


def _insert_survivor(conn, *, lat: float, lon: float, age_years: float, sex: str = "m"):
    birth_tick = -int(age_years * _MINUTES_PER_YEAR)
    conn.execute(
        "INSERT INTO survivors (lat, lon, sex, birth_tick, alive, group_id) "
        "VALUES (?, ?, ?, ?, 1, NULL);",
        (lat, lon, sex, birth_tick),
    )


def _alive_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM survivors WHERE alive=1;").fetchone()[0]


def _dead_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM survivors WHERE alive=0;").fetchone()[0]


# ---------------------------------------------------------------------------
# Test 1: Erwachsene sterben über Zeit (nicht null)
#
# Unter dem alten Bug: rand≈0.5003 für alle → 0.5003 < 0.997 → NIEMAND stirbt.
# Mit dem Fix:  uniforme rand-Werte → ~0.3% sterben pro Tag.
# Nach 100 Tagen: erwartet 1 - 0.997^100 ≈ 26% Tote (bei 3000 Survivors ~780).
# Toleranzband: > 0 und < 80% (sehr großzügig, fängt aber Bug sicher ab).
# ---------------------------------------------------------------------------

class TestAdultMortalityIsNonZero:
    """Erwachsene müssen über 100 Tage tatsächlich sterben (nicht 0 Tote)."""

    N_SURVIVORS = 3_000
    N_DAYS = 100
    # 0.997^100 ≈ 0.740 → ~26% sterben.  Erwartungsbereich: > 1% und < 80%.
    MIN_DEAD_FRACTION = 0.01
    MAX_DEAD_FRACTION = 0.80

    def test_adults_die_over_100_days(self, monkeypatch):
        """
        3000 allein lebende Erwachsene, 100 Tage step_day → messbare Todesfälle.

        Alter Bug: rand ≈ 0,5003 < p_survive=0,997 → niemand stirbt → Test ROT.
        Fix:       echte Uniform-Ziehung → ~26% Tote nach 100 Tagen → Test GRÜN.
        """
        _patch_flat_density(monkeypatch)
        conn = _make_db(seed=42)

        # Survivors auf einem 55×55-Gitter, 0,2° Abstand → je ~22 km auseinander
        # → weit über MEET_DIST_KM=2 km → keine Gruppenbildung möglich.
        cols = 55
        for i in range(self.N_SURVIVORS):
            lat = float(i // cols) * _GRID_SPACING_DEG
            lon = float(i  % cols) * _GRID_SPACING_DEG
            _insert_survivor(conn, lat=lat, lon=lon, age_years=30.0)
        conn.commit()

        initial_alive = _alive_count(conn)
        assert initial_alive == self.N_SURVIVORS

        try:
            for day in range(1, self.N_DAYS + 1):
                _reset_density()
                sim_mod.step_day(conn, day)
        finally:
            _reset_density()

        dead = _dead_count(conn)
        dead_fraction = dead / self.N_SURVIVORS

        assert dead > 0, (
            f"Erwachsene sollten nach {self.N_DAYS} Tagen NICHT alle überleben — "
            f"0 Tote zeigen den alten RNG-Bug an (rand≈0.5003 < 0.997 für alle)."
        )
        assert dead_fraction >= self.MIN_DEAD_FRACTION, (
            f"Sterblichkeit zu niedrig: {dead_fraction:.1%} < {self.MIN_DEAD_FRACTION:.1%}. "
            f"Tote: {dead}/{self.N_SURVIVORS}."
        )
        assert dead_fraction <= self.MAX_DEAD_FRACTION, (
            f"Sterblichkeit zu hoch: {dead_fraction:.1%} > {self.MAX_DEAD_FRACTION:.1%}. "
            f"Tote: {dead}/{self.N_SURVIVORS}."
        )


# ---------------------------------------------------------------------------
# Test 2: Greise sterben IM SIMULATIONSVERLAUF schneller als Erwachsene
#
# Unter dem alten Bug: rand≈0.5003 für alle → 0.5003 < 0.975 UND < 0.997
# → BEIDE Kohorten haben 0 Tote → Greis-Anteil == Erwachsenen-Anteil == 0 %
# → Test ROT (assert elder_dead_fraction > adult_dead_fraction + margin).
# Mit Fix: Greise ~2.5%/Tag vs. Erwachsene ~0.3%/Tag → klarer Unterschied.
# ---------------------------------------------------------------------------

class TestElderDiesFasterThanAdultInSimulation:
    """Greise sterben im Simulationsverlauf schneller als Erwachsene."""

    N_PER_COHORT = 2_000
    N_DAYS = 60
    # Nach 60 Tagen: elder 1-0.975^60≈77%, adult 1-0.997^60≈16%.
    # Mindest-Abstand zwischen den Fraktionen: 30 Prozentpunkte.
    MIN_MARGIN = 0.30

    def test_elder_dead_fraction_exceeds_adult(self, monkeypatch):
        """
        Greise-Kohorte vs. Erwachsene-Kohorte nach 60 Tagen: Greise-Anteil >> Erwachsene.

        Alter Bug: beide Kohorten 0 Tote → Differenz = 0 → Test ROT.
        Fix:       elder ~77% tot, adult ~16% tot → Differenz ≈ 61 pp → Test GRÜN.
        """
        _patch_flat_density(monkeypatch)
        conn = _make_db(seed=7)

        cols = 45   # 45 × 45 = 2025 > N_PER_COHORT=2000
        # Erwachsene: Alter 30, lon-Offset = 0
        for i in range(self.N_PER_COHORT):
            lat = float(i // cols) * _GRID_SPACING_DEG
            lon = float(i  % cols) * _GRID_SPACING_DEG
            _insert_survivor(conn, lat=lat, lon=lon, age_years=30.0, sex="m")
        # Greise: Alter 85, lon-Offset groß genug um keine Überlappung mit Erwachsenen
        lon_offset = cols * _GRID_SPACING_DEG + _GRID_SPACING_DEG
        for i in range(self.N_PER_COHORT):
            lat = float(i // cols) * _GRID_SPACING_DEG
            lon = float(i  % cols) * _GRID_SPACING_DEG + lon_offset
            _insert_survivor(conn, lat=lat, lon=lon, age_years=85.0, sex="f")
        conn.commit()

        # ID-Bereiche: Erwachsene zuerst eingefügt → niedrigere IDs
        adult_max_id = conn.execute(
            "SELECT MAX(id) FROM survivors WHERE birth_tick = ?;",
            (-int(30.0 * _MINUTES_PER_YEAR),),
        ).fetchone()[0]

        try:
            for day in range(1, self.N_DAYS + 1):
                _reset_density()
                sim_mod.step_day(conn, day)
        finally:
            _reset_density()

        adult_dead = conn.execute(
            "SELECT COUNT(*) FROM survivors WHERE alive=0 AND id <= ?;",
            (adult_max_id,),
        ).fetchone()[0]
        elder_dead = conn.execute(
            "SELECT COUNT(*) FROM survivors WHERE alive=0 AND id > ?;",
            (adult_max_id,),
        ).fetchone()[0]

        adult_frac = adult_dead / self.N_PER_COHORT
        elder_frac = elder_dead / self.N_PER_COHORT

        assert elder_dead > 0, (
            "Greise-Kohorte sollte nach 60 Tagen Tote haben — "
            "0 zeigt den alten RNG-Bug (rand≈0.5003 < 0.975 für alle)."
        )
        assert adult_dead > 0, (
            "Erwachsene-Kohorte sollte nach 60 Tagen Tote haben — "
            "0 zeigt den alten RNG-Bug."
        )
        assert elder_frac > adult_frac + self.MIN_MARGIN, (
            f"Greise-Sterblichkeit ({elder_frac:.1%}) sollte deutlich über "
            f"Erwachsene ({adult_frac:.1%}) liegen — Mindestabstand {self.MIN_MARGIN:.0%}. "
            f"Kleiner Abstand zeigt Gleichverteilung-Bug (z. B. beide ≈0)."
        )


# ---------------------------------------------------------------------------
# Test 3: Tagweise Variation der Sterbeziehung
#
# Unter dem alten Bug: rand≈0.5003 jeden Tag → Erwachsene (p≈0.997) NIEMALS tot.
# Über viele Tage kumulativer Anstieg der Toten: strikt monoton steigend UND
# an den meisten Tagen tatsächlich > 0 neue Tote (kein "Alles-auf-einmal").
#
# Zusatz: Greise sterben an VERSCHIEDENEN Tagen (nicht alle am gleichen Tag).
# ---------------------------------------------------------------------------

class TestDailyVariationInDeathDraws:
    """Tagesvarianz der Sterbe-Ziehung: schrittweise kumulative Sterblichkeit."""

    N_ELDERS = 500
    N_DAYS = 30
    # Nach 30 Tagen: p_alive = 0.975^30 ≈ 0.468 → ~53% tot
    # Wir erwarten, dass an den meisten Tagen mind. 1 Greis stirbt.
    MIN_DAYS_WITH_DEATHS = 15   # von 30 Tagen muss an mind. 15 jemand sterben

    def test_deaths_spread_over_multiple_days(self, monkeypatch):
        """
        500 allein lebende Greise über 30 Tage: an jedem zweiten Tag mind. 1 Tod.

        Alter Bug: rand≈0.5003 < 0.975 für ALLE Greise → NIEMALS ein Todesfall
        → Tage mit Toten = 0 → Test ROT.
        Fix:       echte Uniform-Werte → ~53% sterben verteilt über 30 Tage → GRÜN.
        """
        _patch_flat_density(monkeypatch)
        conn = _make_db(seed=99)

        cols = 23   # 23 × 22 = 506 > N_ELDERS=500
        for i in range(self.N_ELDERS):
            lat = float(i // cols) * _GRID_SPACING_DEG
            lon = float(i  % cols) * _GRID_SPACING_DEG
            _insert_survivor(conn, lat=lat, lon=lon, age_years=82.0, sex="m")
        conn.commit()

        days_with_deaths = 0
        prev_dead = 0

        try:
            for day in range(1, self.N_DAYS + 1):
                _reset_density()
                sim_mod.step_day(conn, day)
                current_dead = _dead_count(conn)
                if current_dead > prev_dead:
                    days_with_deaths += 1
                prev_dead = current_dead
        finally:
            _reset_density()

        total_dead = _dead_count(conn)

        assert total_dead > 0, (
            "Nach 30 Tagen sollte mindestens ein Greis gestorben sein — "
            "0 Tote zeigen den alten RNG-Bug (rand≈0.5003 < 0.975 stets)."
        )
        assert days_with_deaths >= self.MIN_DAYS_WITH_DEATHS, (
            f"Todesfälle sollten über viele Tage verteilt sein — "
            f"nur {days_with_deaths}/{self.N_DAYS} Tage hatten Todesfälle "
            f"(Mindestanforderung: {self.MIN_DAYS_WITH_DEATHS}). "
            f"Wenige Tage mit Toten zeigen geklumpte oder fehlende Ziehung."
        )
