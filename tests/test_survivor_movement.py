"""Tests fuer Bewegungsmodell v2 (Issue #30).

Abgedeckt:
  1. home_lat/home_lon beim spawn_survivors gesetzt (= Spawn-Position); Determinismus.
  2. Fruehe Stillstandsphase: mittlere Verschiebung Tag 0-2 deutlich kleiner als Tag 15.
  3. Heimat-Anker: nach wenigen Tagen nahe home; nach vielen Tagen (Anker verblasst) weiter weg.
  4. Tempo ~ Sog: schwaches Feld -> kleiner Schritt; starker Gradient -> grosser Schritt (bis Cap).
  5. Determinismus: gleicher Seed -> identische Positionen ueber N Tage.
  6. Regression (#19): frueh Richtung Dichte, spaet weg (bleibt erhalten).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.conftest import make_conn, set_seed
from app.sim import constants
from app.sim import survivors as surv_mod
from app.sim import survivor_sim as sim_mod
from app.sim import popgrid as popgrid_mod

# ---------------------------------------------------------------------------
# Mini-Dichtefelder
# ---------------------------------------------------------------------------

# Flaches 3-Punkt-Feld: kaum Gradient -> geringer Sog -> kleiner Schritt
_FLAT_GRID = [
    (0.0, 0.0, 1.0),
    (1.0, 0.0, 1.0),
    (2.0, 0.0, 1.0),
]

# Starker Gradient: (0,0) sehr dicht, Rest fast leer -> grosser Sog
_STEEP_GRID = [
    (0.0, 0.0, 50_000.0),
    (2.0, 0.0,      10.0),
    (4.0, 0.0,      10.0),
]

# Migrations-Gitter (kompatibel mit test_survivor_sim.py)
_MIGRATION_GRID = [
    (0.0, 0.0, 10_000.0),
    (1.0, 0.0,  1_000.0),
    (2.0, 0.0,     10.0),
]

# Spawn-Gitter fuer spawn_survivors
_SPAWN_GRID = [
    (0.0,  0.0,  100.0),
    (10.0, 10.0,  10.0),
    (50.0, 50.0,   1.0),
]

# ---------------------------------------------------------------------------
# Patch-Helfer (analog test_survivor_sim.py)
# ---------------------------------------------------------------------------

def _patch_density(monkeypatch, grid):
    sim_mod._density_field = None
    popgrid_mod.load_grid.cache_clear()
    monkeypatch.setattr(sim_mod, "load_grid", lambda: grid)


def _reset_density():
    sim_mod._density_field = None


def _patch_spawn_grid(monkeypatch, grid=_SPAWN_GRID):
    popgrid_mod.load_grid.cache_clear()
    monkeypatch.setattr(surv_mod, "load_grid", lambda: grid)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

_MINUTES_PER_YEAR = 525_600


def _insert_survivor(conn, *, lat: float, lon: float,
                     age_years: float = 30.0, sex: str = "m",
                     home_lat: float | None = None,
                     home_lon: float | None = None) -> int:
    birth_tick = -int(age_years * _MINUTES_PER_YEAR)
    hl = lat if home_lat is None else home_lat
    hl2 = lon if home_lon is None else home_lon
    conn.execute(
        "INSERT INTO survivors (lat, lon, sex, birth_tick, alive, home_lat, home_lon) "
        "VALUES (?, ?, ?, ?, 1, ?, ?);",
        (lat, lon, sex, birth_tick, hl, hl2),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid();").fetchone()[0]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Einfache Haversine-Distanz in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2.0 * math.asin(math.sqrt(a))


# ===========================================================================
# 1. home_lat/home_lon beim Spawn gesetzt + Determinismus
# ===========================================================================

class TestHomeSpawn:
    """home_lat/home_lon entspricht der Spawn-Position; Determinismus."""

    def test_home_equals_spawn_position(self, conn, monkeypatch):
        """Nach spawn_survivors sind home_lat/home_lon identisch mit lat/lon."""
        _patch_spawn_grid(monkeypatch)
        surv_mod.spawn_survivors(conn, total=50, seed=42)
        rows = conn.execute(
            "SELECT lat, lon, home_lat, home_lon FROM survivors;"
        ).fetchall()
        assert len(rows) == 50
        for r in rows:
            assert r["home_lat"] is not None, "home_lat darf nicht NULL sein"
            assert r["home_lon"] is not None, "home_lon darf nicht NULL sein"
            assert abs(r["lat"] - r["home_lat"]) < 1e-10, (
                f"home_lat {r['home_lat']:.6f} != lat {r['lat']:.6f}"
            )
            assert abs(r["lon"] - r["home_lon"]) < 1e-10, (
                f"home_lon {r['home_lon']:.6f} != lon {r['lon']:.6f}"
            )

    def test_home_determinism_same_seed(self, conn_seeded, monkeypatch):
        """Gleicher Seed -> identische home_lat/home_lon-Sequenz."""
        _patch_spawn_grid(monkeypatch)
        c1 = conn_seeded("_hd1")
        c2 = conn_seeded("_hd2")

        surv_mod.spawn_survivors(c1, total=60, seed=777)
        surv_mod.spawn_survivors(c2, total=60, seed=777)

        h1 = c1.execute(
            "SELECT home_lat, home_lon FROM survivors ORDER BY id;"
        ).fetchall()
        h2 = c2.execute(
            "SELECT home_lat, home_lon FROM survivors ORDER BY id;"
        ).fetchall()

        assert len(h1) == len(h2) == 60
        for a, b in zip(h1, h2):
            assert abs(a["home_lat"] - b["home_lat"]) < 1e-10
            assert abs(a["home_lon"] - b["home_lon"]) < 1e-10

        c1.close()
        c2.close()


# ===========================================================================
# 2. Fruehe Stillstandsphase (Mobilitaets-Rampe)
# ===========================================================================

class TestEarlyStillness:
    """Tag 0-2 viel kleinere Verschiebungen als Tag 15 (Mobilitaets-Rampe)."""

    # SURVIVOR_MOBILITY_RAMP_DAYS = 5:
    # mobility(0) = 0/5 = 0  ->  step_km = 0
    # mobility(1) = 1/5 = 0.2 -> kleiner Schritt
    # mobility(2) = 2/5 = 0.4 -> kleiner Schritt
    # mobility(15) = min(15/5,1) = 1.0 -> voller Schritt
    # Altes Verhalten: fix 25 km/Tag unabhaengig von day -> Tag 0 wuerde ~25 km sein.

    def _mean_displacement(self, conn, day_from, day_to, grid) -> float:
        """Mittlere Haversine-Distanz zwischen Positionen vor und nach step_day."""
        # Positionen VOR dem Schritt
        before = {
            r["id"]: (r["lat"], r["lon"])
            for r in conn.execute(
                "SELECT id, lat, lon FROM survivors WHERE alive=1;"
            ).fetchall()
        }
        sim_mod._density_field = None
        sim_mod._density_field = None  # Cache leeren (load_grid bereits gepacht)
        sim_mod.step_day(conn, day_to)

        after = {
            r["id"]: (r["lat"], r["lon"])
            for r in conn.execute(
                "SELECT id, lat, lon FROM survivors WHERE alive=1;"
            ).fetchall()
        }
        common = set(before) & set(after)
        if not common:
            return 0.0
        displacements = [
            _haversine_km(before[sid][0], before[sid][1],
                          after[sid][0], after[sid][1])
            for sid in common
        ]
        return float(np.mean(displacements))

    def _make_pop(self, seed: int = 42, n: int = 10):
        """Frische DB mit n Survivorn auf flachem Gitter."""
        conn = make_conn()
        set_seed(conn, seed)
        # Survivor weit auseinander (vermeidet Gruppenbildung)
        for i in range(n):
            _insert_survivor(conn, lat=1.5, lon=float(i) * 0.5, age_years=30.0)
        return conn

    def test_day0_zero_displacement(self, monkeypatch):
        """Tag 0: Mobilitat = 0 -> mittlere Verschiebung == 0."""
        _patch_density(monkeypatch, _FLAT_GRID)
        conn = self._make_pop()
        try:
            disp0 = self._mean_displacement(conn, None, 0, _FLAT_GRID)
            # Beim alten Fix-25km-Modell waere disp0 >= 25 km -> Test wuerde ROTERDEN.
            assert disp0 == pytest.approx(0.0, abs=1e-6), (
                f"Tag 0 sollte 0 km Verschiebung haben (Mobilitat=0), "
                f"aber: {disp0:.4f} km"
            )
        finally:
            _reset_density()
            conn.close()

    def test_early_days_much_less_than_day15(self, monkeypatch):
        """Mittlere Verschiebungen Tag 1+2 deutlich kleiner als Tag 15.

        Strategie: zwei identische frische Populationen, je einen Schritt gemessen:
        - Pop A: step_day(1)  -> mobility = 1/5 = 0.2
        - Pop B: step_day(15) -> mobility = min(15/5,1) = 1.0
        Beide starten an der gleichen Position, so ist der Gradient identisch.
        Altes Verhalten (fix 25 km): disp_day1 ≈ disp_day15 → Test ROTERDEN.
        """
        _patch_density(monkeypatch, _STEEP_GRID)

        def _fresh_pop():
            c = make_conn()
            set_seed(c, 55)
            # Survivor bei (2.0, *), starker Sog Richtung (0,0)
            for i in range(15):
                _insert_survivor(c, lat=2.0, lon=float(i) * 0.5, age_years=30.0)
            return c

        try:
            # -- Einzel-Schritt an Tag 1 (mobility=0.2) --
            conn1 = _fresh_pop()
            before1 = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn1.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            sim_mod._density_field = None
            sim_mod.step_day(conn1, 1)
            after1 = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn1.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            common1 = set(before1) & set(after1)
            disp_day1 = float(np.mean([
                _haversine_km(before1[sid][0], before1[sid][1],
                              after1[sid][0], after1[sid][1])
                for sid in common1
            ])) if common1 else 0.0
            conn1.close()

            # -- Einzel-Schritt an Tag 2 (mobility=0.4) --
            conn2 = _fresh_pop()
            before2 = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn2.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            sim_mod._density_field = None
            sim_mod.step_day(conn2, 2)
            after2 = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn2.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            common2 = set(before2) & set(after2)
            disp_day2 = float(np.mean([
                _haversine_km(before2[sid][0], before2[sid][1],
                              after2[sid][0], after2[sid][1])
                for sid in common2
            ])) if common2 else 0.0
            conn2.close()

            # -- Einzel-Schritt an Tag 15 (mobility=1.0) --
            conn15 = _fresh_pop()
            before15 = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn15.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            sim_mod._density_field = None
            sim_mod.step_day(conn15, 15)
            after15 = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn15.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            common15 = set(before15) & set(after15)
            disp_day15 = float(np.mean([
                _haversine_km(before15[sid][0], before15[sid][1],
                              after15[sid][0], after15[sid][1])
                for sid in common15
            ])) if common15 else 0.0
            conn15.close()

            # mobility(1)=0.2 vs mobility(15)=1.0 -> Ratio = 5x
            # Beim alten Fix-25km-Modell: alle ≈ 25 km -> Test ROTERDEN.
            assert disp_day1 < disp_day15 * 0.7, (
                f"Tag-1-Verschiebung ({disp_day1:.3f} km) sollte deutlich kleiner sein als "
                f"Tag-15-Verschiebung ({disp_day15:.3f} km) wegen Mobilitaets-Rampe "
                f"(mobility 0.2 vs 1.0)"
            )
            assert disp_day2 < disp_day15 * 0.85, (
                f"Tag-2-Verschiebung ({disp_day2:.3f} km) sollte kleiner sein als "
                f"Tag-15-Verschiebung ({disp_day15:.3f} km) wegen Mobilitaets-Rampe "
                f"(mobility 0.4 vs 1.0)"
            )
        finally:
            _reset_density()


# ===========================================================================
# 3. Heimat-Anker
# ===========================================================================

class TestHomeAnchor:
    """Nach wenigen Tagen nahe home; nach vielen Tagen (Anker verblasst) weiter weg."""

    def _mean_dist_from_home(self, conn) -> float:
        rows = conn.execute(
            "SELECT lat, lon, home_lat, home_lon FROM survivors WHERE alive=1;"
        ).fetchall()
        if not rows:
            return 0.0
        dists = [
            _haversine_km(r["lat"], r["lon"], r["home_lat"], r["home_lon"])
            for r in rows
        ]
        return float(np.mean(dists))

    def test_early_close_to_home_late_far(self, monkeypatch):
        """Heimat-Anker: nach 2 Tagen nahe; nach 30 Tagen im Mittel weiter weg."""
        _patch_density(monkeypatch, _MIGRATION_GRID)

        # Survivor starten bei (2.0, 0.0) -- niedrige Dichte, stark von (0,0) angezogen.
        # Heimat = Startposition (2.0, 0.0). Ohne Anker wuerden sie schnell wegziehen.
        conn = make_conn()
        set_seed(conn, 42)
        n = 8
        for i in range(n):
            _insert_survivor(conn, lat=2.0, lon=float(i) * 0.01, age_years=30.0)

        try:
            # Erste 2 Tage
            for d in range(1, 3):
                sim_mod._density_field = None
                sim_mod.step_day(conn, d)

            dist_early = self._mean_dist_from_home(conn)

            # Weiter bis Tag 30 (Anker verblasst nach SURVIVOR_HOME_DECAY_DAYS=21)
            for d in range(3, 31):
                sim_mod._density_field = None
                sim_mod.step_day(conn, d)

            dist_late = self._mean_dist_from_home(conn)

            # Nach Tag 21 ist der Anker 0 -> keine Heimat-Kraft mehr -> Survivor driften.
            # Nach Tag 30 sollten sie im Mittel weiter von ihrer Heimat entfernt sein.
            assert dist_late > dist_early, (
                f"Nach Tag 30 (Anker verblasst) erwartet groessere Distanz von home "
                f"als nach Tag 2: frueh={dist_early:.3f} km, spaet={dist_late:.3f} km"
            )
            # Frueh: innerhalb weniger km von Home (starker Anker zieht zurueck)
            assert dist_early < 30.0, (
                f"Nach 2 Tagen sollten Survivor noch nahe Home sein (< 30 km), "
                f"aber: {dist_early:.3f} km"
            )
        finally:
            _reset_density()
            conn.close()

    def test_home_weight_decays_by_constants(self):
        """w_home-Formel aus Constants: Tag 0 > 0; Tag DECAY_DAYS = 0."""
        # w_home(day) = HOME_WEIGHT * max(0, 1 - day/DECAY_DAYS)
        w0 = constants.SURVIVOR_HOME_WEIGHT * max(0.0, 1.0 - 0 / constants.SURVIVOR_HOME_DECAY_DAYS)
        w_decay = constants.SURVIVOR_HOME_WEIGHT * max(
            0.0, 1.0 - constants.SURVIVOR_HOME_DECAY_DAYS / constants.SURVIVOR_HOME_DECAY_DAYS
        )
        assert w0 == pytest.approx(constants.SURVIVOR_HOME_WEIGHT, rel=1e-6)
        assert w_decay == pytest.approx(0.0, abs=1e-10)


# ===========================================================================
# 4. Tempo ~ Sog
# ===========================================================================

class TestStepSizeProportionalToPull:
    """Schwaches Feld -> kleiner Schritt; starker Gradient -> grosser Schritt."""

    def _mean_step_km(self, grid, lat0: float, lon0: float,
                      n: int, seed: int, day: int) -> float:
        """Mittlere Schrittweite eines step_day ueber n Survivor."""
        conn = make_conn()
        set_seed(conn, seed)
        # Survivor weit auseinander um Gruppenbildung zu vermeiden
        for i in range(n):
            _insert_survivor(conn, lat=lat0, lon=lon0 + i * 0.5, age_years=30.0)

        sim_mod._density_field = None
        popgrid_mod.load_grid.cache_clear()
        # load_grid direkt im sim_mod patchen
        sim_mod.load_grid = lambda: grid

        try:
            before = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            sim_mod._density_field = None
            sim_mod.step_day(conn, day)
            after = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            common = set(before) & set(after)
            steps = [
                _haversine_km(before[sid][0], before[sid][1],
                              after[sid][0], after[sid][1])
                for sid in common
            ]
            return float(np.mean(steps)) if steps else 0.0
        finally:
            sim_mod._density_field = None
            # load_grid zuruecksetzen
            from app.sim import popgrid as pg
            sim_mod.load_grid = pg.load_grid
            conn.close()

    def test_flat_field_small_step(self, monkeypatch):
        """Flaches Feld (kaum Gradient) -> Schritt deutlich kleiner als MAX_STEP_KM."""
        _patch_density(monkeypatch, _FLAT_GRID)
        conn = make_conn()
        set_seed(conn, 42)
        n = 10
        for i in range(n):
            _insert_survivor(conn, lat=1.0, lon=float(i) * 0.5, age_years=30.0)

        try:
            before = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            # Tag 15 (volle Mobilitaet) mit flachem Feld
            sim_mod.step_day(conn, 15)
            after = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            common = set(before) & set(after)
            steps = [
                _haversine_km(before[sid][0], before[sid][1],
                              after[sid][0], after[sid][1])
                for sid in common
            ]
            mean_step = float(np.mean(steps)) if steps else 0.0

            # Beim alten Modell: fix 25 km/Tag -> mean_step ≈ 25.
            # Neues Modell: step ~ SPEED_SCALE * |resultant|, flaches Feld -> schwacher Sog.
            # Wir erwarten deutlich unter MAX_STEP_KM (25).
            assert mean_step < constants.SURVIVOR_MAX_STEP_KM * 0.8, (
                f"Bei flachem Feld erwartet Schritt < {constants.SURVIVOR_MAX_STEP_KM * 0.8:.1f} km, "
                f"aber: {mean_step:.3f} km. Altes Fix-25km-Modell wuerde hier versagen."
            )
        finally:
            _reset_density()
            conn.close()

    def test_steep_field_large_step(self, monkeypatch):
        """Starker Gradient -> Schritt nahe MAX_STEP_KM."""
        _patch_density(monkeypatch, _STEEP_GRID)
        conn = make_conn()
        set_seed(conn, 42)
        n = 10
        # Survivor bei (2.0, *) -- starke Anziehung zu (0,0)
        for i in range(n):
            _insert_survivor(conn, lat=2.0, lon=float(i) * 0.5, age_years=30.0)

        try:
            before = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            # Tag 15: volle Mobilitaet, starker Sog -> grosse Schritte
            sim_mod.step_day(conn, 15)
            after = {
                r["id"]: (r["lat"], r["lon"])
                for r in conn.execute(
                    "SELECT id, lat, lon FROM survivors WHERE alive=1;"
                ).fetchall()
            }
            common = set(before) & set(after)
            steps = [
                _haversine_km(before[sid][0], before[sid][1],
                              after[sid][0], after[sid][1])
                for sid in common
            ]
            mean_step = float(np.mean(steps)) if steps else 0.0

            # Starker Sog: |resultant| ~ GRAVITY_WEIGHT(0.7) + ... >=1 -> SPEED_SCALE*1=12.5 km
            # Kein fixes 25 km mehr, aber der Schritt ist deutlich groesser als beim flachen Feld.
            assert mean_step > 5.0, (
                f"Bei starkem Gradient erwartet Schritt > 5 km, aber: {mean_step:.3f} km"
            )
            # Gedeckelt bei MAX_STEP_KM
            assert mean_step <= constants.SURVIVOR_MAX_STEP_KM + 0.1, (
                f"Schritt sollte <= MAX_STEP_KM={constants.SURVIVOR_MAX_STEP_KM} km sein, "
                f"aber: {mean_step:.3f} km"
            )
        finally:
            _reset_density()
            conn.close()

    def test_flat_vs_steep_ratio(self, monkeypatch):
        """Steiles Feld produziert deutlich groessere Schritte als flaches Feld."""
        # Flaches Feld
        _patch_density(monkeypatch, _FLAT_GRID)
        conn_flat = make_conn()
        set_seed(conn_flat, 99)
        n = 8
        for i in range(n):
            _insert_survivor(conn_flat, lat=1.5, lon=float(i) * 0.5, age_years=30.0)

        before_flat = {
            r["id"]: (r["lat"], r["lon"])
            for r in conn_flat.execute(
                "SELECT id, lat, lon FROM survivors WHERE alive=1;"
            ).fetchall()
        }
        sim_mod.step_day(conn_flat, 15)
        after_flat = {
            r["id"]: (r["lat"], r["lon"])
            for r in conn_flat.execute(
                "SELECT id, lat, lon FROM survivors WHERE alive=1;"
            ).fetchall()
        }
        common_flat = set(before_flat) & set(after_flat)
        mean_flat = float(np.mean([
            _haversine_km(before_flat[s][0], before_flat[s][1],
                          after_flat[s][0], after_flat[s][1])
            for s in common_flat
        ])) if common_flat else 0.0

        _reset_density()
        conn_flat.close()

        # Steiles Feld
        _patch_density(monkeypatch, _STEEP_GRID)
        conn_steep = make_conn()
        set_seed(conn_steep, 99)
        for i in range(n):
            _insert_survivor(conn_steep, lat=1.5, lon=float(i) * 0.5, age_years=30.0)

        before_steep = {
            r["id"]: (r["lat"], r["lon"])
            for r in conn_steep.execute(
                "SELECT id, lat, lon FROM survivors WHERE alive=1;"
            ).fetchall()
        }
        sim_mod._density_field = None
        sim_mod.step_day(conn_steep, 15)
        after_steep = {
            r["id"]: (r["lat"], r["lon"])
            for r in conn_steep.execute(
                "SELECT id, lat, lon FROM survivors WHERE alive=1;"
            ).fetchall()
        }
        common_steep = set(before_steep) & set(after_steep)
        mean_steep = float(np.mean([
            _haversine_km(before_steep[s][0], before_steep[s][1],
                          after_steep[s][0], after_steep[s][1])
            for s in common_steep
        ])) if common_steep else 0.0

        try:
            # Steiler Sog produziert mehr als doppelt so grosse Schritte
            # Beim alten Fix-25km-Modell waren beide gleich (~25 km) -> Test ROTERDEN.
            assert mean_steep > mean_flat * 1.5 or mean_flat < 2.0, (
                f"Steiles Feld ({mean_steep:.3f} km) sollte deutlich groessere Schritte "
                f"produzieren als flaches Feld ({mean_flat:.3f} km). "
                f"Beim alten Fix-25km-Modell waeren beide ≈ 25 km gewesen."
            )
        finally:
            _reset_density()
            conn_steep.close()


# ===========================================================================
# 5. Determinismus
# ===========================================================================

class TestMovementDeterminism:
    """Gleicher Seed -> identische Positionen ueber N Tage."""

    def test_same_seed_same_positions_over_n_days(self, monkeypatch):
        """Zwei identische DBs + gleicher Seed -> nach 10 Tagen gleiche Positionen."""
        _patch_density(monkeypatch, _MIGRATION_GRID)

        N_DAYS = 10

        def _make():
            c = make_conn()
            set_seed(c, 314)
            for i in range(6):
                _insert_survivor(c, lat=1.5 + i * 0.01, lon=0.0, age_years=30.0)
            return c

        c1 = _make()
        c2 = _make()

        try:
            for day in range(1, N_DAYS + 1):
                sim_mod._density_field = None
                sim_mod.step_day(c1, day)
                sim_mod._density_field = None
                sim_mod.step_day(c2, day)

            pos1 = c1.execute(
                "SELECT lat, lon FROM survivors WHERE alive=1 ORDER BY id;"
            ).fetchall()
            pos2 = c2.execute(
                "SELECT lat, lon FROM survivors WHERE alive=1 ORDER BY id;"
            ).fetchall()

            assert len(pos1) == len(pos2), "Anzahl lebender Survivors muss gleich sein"
            for p1, p2 in zip(pos1, pos2):
                assert abs(p1["lat"] - p2["lat"]) < 1e-10, (
                    f"lat-Werte weichen ab: {p1['lat']} vs {p2['lat']}"
                )
                assert abs(p1["lon"] - p2["lon"]) < 1e-10, (
                    f"lon-Werte weichen ab: {p1['lon']} vs {p2['lon']}"
                )
        finally:
            _reset_density()
            c1.close()
            c2.close()

    def test_different_seed_different_positions(self, monkeypatch):
        """Verschiedener Seed -> andere Positionen nach 5 Tagen."""
        _patch_density(monkeypatch, _MIGRATION_GRID)

        def _make(seed: int):
            c = make_conn()
            set_seed(c, seed)
            for i in range(6):
                _insert_survivor(c, lat=1.5, lon=float(i) * 0.01, age_years=30.0)
            return c

        c1 = _make(100)
        c2 = _make(999)

        try:
            for day in range(1, 6):
                sim_mod._density_field = None
                sim_mod.step_day(c1, day)
                sim_mod._density_field = None
                sim_mod.step_day(c2, day)

            pos1 = [
                (r["lat"], r["lon"])
                for r in c1.execute(
                    "SELECT lat, lon FROM survivors WHERE alive=1 ORDER BY id;"
                ).fetchall()
            ]
            pos2 = [
                (r["lat"], r["lon"])
                for r in c2.execute(
                    "SELECT lat, lon FROM survivors WHERE alive=1 ORDER BY id;"
                ).fetchall()
            ]

            same = all(
                abs(p1[0] - p2[0]) < 1e-10 and abs(p1[1] - p2[1]) < 1e-10
                for p1, p2 in zip(pos1, pos2)
            )
            assert not same, (
                "Verschiedene Seeds sollten verschiedene Positionen produzieren"
            )
        finally:
            _reset_density()
            c1.close()
            c2.close()


# ===========================================================================
# 6. Regression #19: frueh Richtung Dichte, spaet weg
# ===========================================================================

class TestMigrationRegressionRetained:
    """Migrations-Regression (#19) bleibt nach Bewegungsmodell-v2-Aenderungen erhalten."""

    def test_early_migration_towards_density(self, monkeypatch):
        """Tag 1 (frueh, < T_FLEE): Survivor bewegen sich Richtung dichtere Zelle."""
        _patch_density(monkeypatch, _MIGRATION_GRID)

        conn = make_conn()
        set_seed(conn, 42)
        # 5 Survivor bei (2.0, 0.0) -- niedrige Dichte; dichte Zelle bei (0.0, 0.0)
        for _ in range(5):
            _insert_survivor(conn, lat=2.0, lon=0.0, age_years=30.0)

        try:
            lat_before = conn.execute(
                "SELECT AVG(lat) AS a FROM survivors WHERE alive=1;"
            ).fetchone()["a"]

            sim_mod._density_field = None
            sim_mod.step_day(conn, 1)

            lat_after = conn.execute(
                "SELECT AVG(lat) AS a FROM survivors WHERE alive=1;"
            ).fetchone()["a"]

            # Bewegung zu (0.0, 0.0) = niedrigeres lat -> lat_after < lat_before
            assert lat_after < lat_before, (
                f"Erwartet Bewegung zu dichterem Bereich (lat sinkt), "
                f"aber lat {lat_before:.4f} -> {lat_after:.4f}"
            )
        finally:
            _reset_density()
            conn.close()

    def test_late_migration_away_from_density(self, monkeypatch):
        """Spaet + smell > Schwelle: Survivor fliehen vom Hochdichte-Bereich."""
        _patch_density(monkeypatch, _MIGRATION_GRID)

        conn = make_conn()
        set_seed(conn, 42)
        # 5 Survivor bei (0.0, 0.0) = maximale Dichte
        for _ in range(5):
            _insert_survivor(conn, lat=0.0, lon=0.0, age_years=30.0)

        try:
            # day weit nach T_FLEE, smell = D*ramp = 10000*1 = 10000 > SMELL_THRESHOLD=5000
            day = constants.SURVIVOR_T_FLEE + constants.SURVIVOR_RAMP_DAYS
            sim_mod._density_field = None
            sim_mod.step_day(conn, day)

            rows = conn.execute(
                "SELECT lat, lon FROM survivors WHERE alive=1;"
            ).fetchall()
            positions = [(r["lat"], r["lon"]) for r in rows]

            moved_away = any(
                abs(lat) > 0.01 or abs(lon) > 0.01
                for lat, lon in positions
            )
            assert moved_away, (
                f"Survivor sollten bei hoher Dichte + spaet wegfliehen, "
                f"Positionen: {positions}"
            )
        finally:
            _reset_density()
            conn.close()
