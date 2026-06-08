"""Tests für globale Überlebenden-Dynamik (Issue #21).

Abgedeckt:
  a) Eager-Anlage (#18): sex/birth_tick/alive=1 nach spawn; Determinismus.
  b) Materialisierung (#18): nächstes house; Fallback ohne house; keine Duplikate.
  c) Migration (#19): gemocktes Mini-Dichtefeld; früh hinauf, spät hinunter;
     Determinismus der Positionen.
  d) Gruppenbildung (#19): nahe Survivor → gleiche group_id; weit entfernte nicht.
  e) Sterben (#20): Säugling allein stirbt früh; Säugling in Gruppe mit
     Erwachsenem überlebt länger; Greis stirbt eher als Erwachsener;
     alive sinkt monoton; Determinismus.
  f) Tick-Integration: advance_tick über Tagesgrenze → genau ein step_day;
     N Tage → N Schritte; kein Doppellauf ohne Tageswechsel.
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
from app.sim import survivors as surv_mod
from app.sim import survivor_sim as sim_mod
from app.sim import popgrid as popgrid_mod

# ---------------------------------------------------------------------------
# Mini-Dichtefelder und Patch-Helper
# ---------------------------------------------------------------------------

# 3×3-Gitter mit klar unterschiedlichen Dichten:
# • hohe Dichte in (0°,0°)  → weight 10000
# • mittlere Dichte in (1°,0°) → weight 1000
# • niedrige Dichte überall sonst → weight 10
# Für Migration-Tests: Überlebende bewegen sich früh zu (0,0) hin.
_MINI_GRID_MIGRATION = [
    (0.0, 0.0, 10_000.0),
    (1.0, 0.0, 1_000.0),
    (2.0, 0.0, 10.0),
]

_MINI_GRID_SPAWN = [
    (0.0,  0.0,  100.0),
    (10.0, 10.0,  10.0),
    (50.0, 50.0,   1.0),
]


def _patch_grid_spawn(monkeypatch, grid=_MINI_GRID_SPAWN):
    """Patcht load_grid() in survivors.py und leert den lru_cache."""
    popgrid_mod.load_grid.cache_clear()
    monkeypatch.setattr(surv_mod, "load_grid", lambda: grid)


def _patch_density_field(monkeypatch, grid):
    """
    Ersetzt das gecachte _density_field im survivor_sim-Modul durch ein
    deterministisches Mini-Feld, das aus `grid` gebaut wird.
    Muss VOR step_day() aufgerufen werden, der sich auf _density_field stützt.
    """
    # Modul-Cache zurücksetzen
    sim_mod._density_field = None
    # load_grid so ersetzen, dass _build_density_field das Mini-Grid benutzt
    popgrid_mod.load_grid.cache_clear()
    monkeypatch.setattr(sim_mod, "load_grid", lambda: grid)


def _reset_density_cache():
    """Leert den Modul-Cache von survivor_sim (zwischen Tests)."""
    sim_mod._density_field = None


# ---------------------------------------------------------------------------
# Hilfsfunktionen für direkte DB-Inserts
# ---------------------------------------------------------------------------
_MINUTES_PER_YEAR = 525_600


def _insert_survivor(
    conn,
    *,
    lat: float,
    lon: float,
    sex: str = "m",
    age_years: float = 30.0,
    alive: int = 1,
    group_id: int | None = None,
) -> int:
    """Legt einen Survivor direkt in die DB ein, gibt seine id zurück."""
    birth_tick = -int(age_years * _MINUTES_PER_YEAR)
    conn.execute(
        "INSERT INTO survivors (lat, lon, sex, birth_tick, alive, group_id) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        (lat, lon, sex, birth_tick, alive, group_id),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid();").fetchone()[0]


def _insert_group(conn, *, lat: float = 0.0, lon: float = 0.0) -> int:
    """Legt eine Gruppe in survivor_groups an und gibt die id zurück."""
    conn.execute(
        "INSERT INTO survivor_groups (created_tick, lat, lon) VALUES (0, ?, ?);",
        (lat, lon),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid();").fetchone()[0]


def _insert_house(conn, *, lat: float, lon: float, loc_id: int | None = None) -> int:
    """Legt ein Haus (type='house') in locations ein."""
    if loc_id is None:
        loc_id = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM locations;").fetchone()[0]
    conn.execute(
        "INSERT INTO locations (id, osm_id, type, name, lat, lon, footprint_m2, "
        "discovery_status, generation_seed) "
        "VALUES (?, ?, 'house', 'Testhaus', ?, ?, 50.0, 'undiscovered', 1);",
        (loc_id, f"house_{loc_id}", lat, lon),
    )
    conn.commit()
    return loc_id


# ===========================================================================
# a) Eager-Anlage (#18)
# ===========================================================================

class TestEagerSpawn:
    """Nach spawn_survivors haben alle Survivor sex, birth_tick, alive=1."""

    def test_all_have_sex(self, conn, monkeypatch):
        _patch_grid_spawn(monkeypatch)
        surv_mod.spawn_survivors(conn, total=50, seed=42)
        rows = conn.execute("SELECT sex FROM survivors;").fetchall()
        for r in rows:
            assert r["sex"] in ("m", "f"), f"Ungültiges sex: {r['sex']}"

    def test_all_have_birth_tick(self, conn, monkeypatch):
        _patch_grid_spawn(monkeypatch)
        surv_mod.spawn_survivors(conn, total=50, seed=42)
        rows = conn.execute("SELECT birth_tick FROM survivors;").fetchall()
        for r in rows:
            assert r["birth_tick"] is not None, "birth_tick darf nicht NULL sein"
            # birth_tick ist negativ (VOR Kollaps geboren)
            assert r["birth_tick"] <= 0, f"birth_tick sollte ≤ 0 sein: {r['birth_tick']}"

    def test_all_alive_equals_one(self, conn, monkeypatch):
        _patch_grid_spawn(monkeypatch)
        surv_mod.spawn_survivors(conn, total=50, seed=42)
        not_alive = conn.execute(
            "SELECT COUNT(*) FROM survivors WHERE alive != 1;"
        ).fetchone()[0]
        assert not_alive == 0, f"{not_alive} Survivor mit alive != 1"

    def test_group_id_null_after_spawn(self, conn, monkeypatch):
        """Direkt nach spawn ist group_id NULL (noch keine Gruppenbildung)."""
        _patch_grid_spawn(monkeypatch)
        surv_mod.spawn_survivors(conn, total=30, seed=7)
        with_group = conn.execute(
            "SELECT COUNT(*) FROM survivors WHERE group_id IS NOT NULL;"
        ).fetchone()[0]
        assert with_group == 0

    def test_determinism_same_seed_same_attributes(self, conn_seeded, monkeypatch):
        """Gleicher Seed → identische sex- und birth_tick-Sequenz."""
        _patch_grid_spawn(monkeypatch)

        c1 = conn_seeded("_ea_det1")
        c2 = conn_seeded("_ea_det2")

        surv_mod.spawn_survivors(c1, total=80, seed=1337)
        surv_mod.spawn_survivors(c2, total=80, seed=1337)

        attrs1 = c1.execute(
            "SELECT sex, birth_tick FROM survivors ORDER BY id;"
        ).fetchall()
        attrs2 = c2.execute(
            "SELECT sex, birth_tick FROM survivors ORDER BY id;"
        ).fetchall()

        assert len(attrs1) == len(attrs2) == 80
        for a1, a2 in zip(attrs1, attrs2):
            assert a1["sex"] == a2["sex"]
            assert a1["birth_tick"] == a2["birth_tick"]

        c1.close()
        c2.close()

    def test_determinism_different_seed_different_attributes(self, conn_seeded, monkeypatch):
        """Verschiedener Seed → andere sex-/birth_tick-Verteilung."""
        _patch_grid_spawn(monkeypatch)

        c1 = conn_seeded("_ea_diff1")
        c2 = conn_seeded("_ea_diff2")

        surv_mod.spawn_survivors(c1, total=100, seed=111)
        surv_mod.spawn_survivors(c2, total=100, seed=999)

        births1 = [r["birth_tick"] for r in c1.execute(
            "SELECT birth_tick FROM survivors ORDER BY id;"
        ).fetchall()]
        births2 = [r["birth_tick"] for r in c2.execute(
            "SELECT birth_tick FROM survivors ORDER BY id;"
        ).fetchall()]

        assert births1 != births2

        c1.close()
        c2.close()

    def test_age_distribution_plausible(self, conn, monkeypatch):
        """Es gibt sowohl junge als auch alte Survivor (keine Mono-Klasse)."""
        _patch_grid_spawn(monkeypatch)
        surv_mod.spawn_survivors(conn, total=200, seed=42)
        births = [r["birth_tick"] for r in conn.execute(
            "SELECT birth_tick FROM survivors;"
        ).fetchall()]
        # Mindest- und Maximalalter auseinander
        min_bt = min(births)
        max_bt = max(births)
        age_spread_years = (max_bt - min_bt) / _MINUTES_PER_YEAR
        assert age_spread_years > 10, (
            f"Altersverteilung zu schmal: {age_spread_years:.1f} Jahre"
        )


# ===========================================================================
# b) Materialisierung (#18)
# ===========================================================================

class TestMaterializeNearestHouse:
    """NPC landet am nächsten type='house'."""

    def test_survivor_placed_at_nearest_house(self, conn, monkeypatch):
        """Survivor ohne Haus in Reichweite bleibt an Originalkoordinate;
        mit nahem Haus wird er dorthin gesetzt."""
        _patch_grid_spawn(monkeypatch)

        # Survivor direkt inserieren bei (10.0, 10.0)
        s_lat, s_lon = 10.0, 10.0
        conn.execute(
            "INSERT INTO survivors (lat, lon, sex, birth_tick) VALUES (?, ?, 'm', -10000000);",
            (s_lat, s_lon),
        )
        conn.commit()

        # Haus 50 m nördlich: ~50/111320 ≈ 0.000449 Grad
        house_lat = s_lat + 50.0 / 111_320.0
        house_lon = s_lon
        _insert_house(conn, lat=house_lat, lon=house_lon)

        # Materialisieren in einer Box die beide enthält
        surv_mod.materialize_in_bbox(conn, s_lat - 0.01, s_lon - 0.01, s_lat + 0.01, s_lon + 0.01)

        # Der character sollte bei (house_lat, house_lon) platziert sein
        char_row = conn.execute(
            "SELECT lat, lon FROM characters WHERE type = 'survivor';"
        ).fetchone()
        assert char_row is not None
        assert abs(char_row["lat"] - house_lat) < 1e-8
        assert abs(char_row["lon"] - house_lon) < 1e-8

    def test_fallback_without_house(self, conn, monkeypatch):
        """Ohne Haus in Reichweite bleibt Survivor an Originalkoordinate."""
        _patch_grid_spawn(monkeypatch)

        s_lat, s_lon = 20.0, 20.0
        conn.execute(
            "INSERT INTO survivors (lat, lon, sex, birth_tick) VALUES (?, ?, 'f', -5000000);",
            (s_lat, s_lon),
        )
        conn.commit()

        # Kein Haus in der Nähe; Haus weit weg (> HOUSE_SEARCH_RADIUS_M = 150m)
        _insert_house(conn, lat=s_lat + 1.0, lon=s_lon)

        surv_mod.materialize_in_bbox(conn, s_lat - 0.01, s_lon - 0.01, s_lat + 0.01, s_lon + 0.01)

        char_row = conn.execute(
            "SELECT lat, lon FROM characters WHERE type = 'survivor';"
        ).fetchone()
        assert char_row is not None
        # Fallback: Originalkoordinate (innerhalb Floating-Point-Genauigkeit)
        assert abs(char_row["lat"] - s_lat) < 1e-8
        assert abs(char_row["lon"] - s_lon) < 1e-8

    def test_no_duplicates_on_second_call(self, conn, monkeypatch):
        """Zweiter materialize-Aufruf erzeugt keine neuen Survivor-Characters."""
        _patch_grid_spawn(monkeypatch)

        conn.execute(
            "INSERT INTO survivors (lat, lon, sex, birth_tick) VALUES (30.0, 30.0, 'm', -8000000);",
        )
        conn.commit()

        surv_mod.materialize_in_bbox(conn, 29.99, 29.99, 30.01, 30.01)
        count_after_first = conn.execute(
            "SELECT COUNT(*) FROM characters WHERE type = 'survivor';"
        ).fetchone()[0]

        result2 = surv_mod.materialize_in_bbox(conn, 29.99, 29.99, 30.01, 30.01)
        count_after_second = conn.execute(
            "SELECT COUNT(*) FROM characters WHERE type = 'survivor';"
        ).fetchone()[0]

        assert result2 == [], "Zweiter Aufruf muss leere Liste zurückgeben"
        assert count_after_second == count_after_first

    def test_materialized_flag_set(self, conn, monkeypatch):
        """Nach Materialisierung ist materialized=1 gesetzt."""
        _patch_grid_spawn(monkeypatch)

        conn.execute(
            "INSERT INTO survivors (lat, lon, sex, birth_tick) VALUES (5.0, 5.0, 'f', -7000000);"
        )
        conn.commit()

        surv_mod.materialize_in_bbox(conn, 4.99, 4.99, 5.01, 5.01)

        mat = conn.execute(
            "SELECT materialized FROM survivors WHERE lat = 5.0;"
        ).fetchone()
        assert mat["materialized"] == 1


# ===========================================================================
# c) Migration (#19)
# ===========================================================================

class TestMigration:
    """
    Bewegungsrichtung: früh (day < T_FLEE) zu dichteren Zellen,
    spät (day > T_FLEE und smell > Schwelle) weg davon.
    """

    def _make_small_pop_conn(self):
        """Frische in-memory DB mit 5 Survivorn bei (2.0, 0.0) (niedrige Dichte)."""
        conn = make_conn()
        set_seed(conn, 42)
        for _ in range(5):
            _insert_survivor(conn, lat=2.0, lon=0.0, age_years=30.0)
        return conn

    def test_early_migration_towards_density(self, monkeypatch):
        """day < T_FLEE: Survivor bewegen sich im Mittel zu dichteren Zellen hin."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        conn = self._make_small_pop_conn()
        try:
            lat_before = conn.execute(
                "SELECT AVG(lat) AS a FROM survivors WHERE alive=1;"
            ).fetchone()["a"]

            # Ein Schritt früh in der Sim (day=1, weit vor T_FLEE=30)
            sim_mod.step_day(conn, 1)

            lat_after = conn.execute(
                "SELECT AVG(lat) AS a FROM survivors WHERE alive=1;"
            ).fetchone()["a"]

            # Die Survivor starteten bei lat=2.0 (niedrige Dichte) und sollten
            # sich zu lat=0.0 (hohe Dichte) bewegen → lat_after < lat_before
            assert lat_after < lat_before, (
                f"Erwartete Bewegung zu niedrigerem Breitengrad (dichtere Zelle), "
                f"aber lat ging von {lat_before:.4f} auf {lat_after:.4f}"
            )
        finally:
            _reset_density_cache()
            conn.close()

    def test_late_migration_away_from_density(self, monkeypatch):
        """
        day > T_FLEE und smell > SMELL_THRESHOLD: Survivor fliehen vom Hochdichte-Bereich.
        Wir platzieren die Survivor bei (0.0, 0.0) — maximale Dichte —
        und erwarten Bewegung weg davon (lat_after > lat_before oder Varianz steigt).
        """
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        conn = make_conn()
        set_seed(conn, 42)
        # 5 Survivor bei (0.0, 0.0) = höchste Dichte = 10_000
        for _ in range(5):
            _insert_survivor(conn, lat=0.0, lon=0.0, age_years=30.0)

        try:
            # smell = D * ramp; D=10000, ramp=1 (day >= RAMP_DAYS) → smell=10000 > 5000
            day = constants.SURVIVOR_T_FLEE + constants.SURVIVOR_RAMP_DAYS

            sim_mod.step_day(conn, day)

            # Survivor sollten sich von (0,0) wegbewegen
            rows = conn.execute(
                "SELECT lat, lon FROM survivors WHERE alive=1;"
            ).fetchall()
            positions = [(r["lat"], r["lon"]) for r in rows]

            # Mindestens einer muss sich weg von (0,0) bewegt haben
            moved_away = any(
                abs(lat) > 0.01 or abs(lon) > 0.01
                for lat, lon in positions
            )
            assert moved_away, (
                f"Survivor sollten bei hoher Dichte + spätem Tag wegfliehen, "
                f"Positionen: {positions}"
            )
        finally:
            _reset_density_cache()
            conn.close()

    def test_migration_determinism(self, monkeypatch):
        """Gleicher Seed + gleiche Ausgangspositionen → identische Positionen nach step_day."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        try:
            def _make():
                c = make_conn()
                set_seed(c, 999)
                for _ in range(4):
                    _insert_survivor(c, lat=1.5, lon=0.0, age_years=25.0)
                return c

            c1 = _make()
            c2 = _make()

            sim_mod.step_day(c1, 5)
            sim_mod.step_day(c2, 5)

            pos1 = c1.execute(
                "SELECT lat, lon FROM survivors WHERE alive=1 ORDER BY id;"
            ).fetchall()
            pos2 = c2.execute(
                "SELECT lat, lon FROM survivors WHERE alive=1 ORDER BY id;"
            ).fetchall()

            assert len(pos1) == len(pos2)
            for p1, p2 in zip(pos1, pos2):
                assert abs(p1["lat"] - p2["lat"]) < 1e-10
                assert abs(p1["lon"] - p2["lon"]) < 1e-10

            c1.close()
            c2.close()
        finally:
            _reset_density_cache()


# ===========================================================================
# d) Gruppenbildung (#19)
# ===========================================================================

class TestGroupFormation:
    """Nahe Survivor erhalten dieselbe group_id; weit entfernte nicht."""

    def test_close_survivors_get_same_group(self, monkeypatch):
        """Zwei Survivor < MEET_DIST_KM voneinander → gleiche group_id nach step_day."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        conn = make_conn()
        set_seed(conn, 42)

        # Beide 10 m voneinander (weit unter MEET_DIST_KM = 2 km)
        lat_a = 5.0
        lon_a = 5.0
        lat_b = lat_a + 10.0 / 111_320.0

        id_a = _insert_survivor(conn, lat=lat_a, lon=lon_a, age_years=25.0)
        id_b = _insert_survivor(conn, lat=lat_b, lon=lon_a, age_years=30.0)

        try:
            sim_mod.step_day(conn, 1)

            gid_a = conn.execute(
                "SELECT group_id FROM survivors WHERE id=?;", (id_a,)
            ).fetchone()["group_id"]
            gid_b = conn.execute(
                "SELECT group_id FROM survivors WHERE id=?;", (id_b,)
            ).fetchone()["group_id"]

            assert gid_a is not None, "Survivor A sollte group_id haben"
            assert gid_b is not None, "Survivor B sollte group_id haben"
            assert gid_a == gid_b, (
                f"Nahe Survivor sollten gleiche group_id haben, "
                f"aber A={gid_a}, B={gid_b}"
            )
        finally:
            _reset_density_cache()
            conn.close()

    def test_far_survivors_no_group(self, monkeypatch):
        """Zwei Survivor > MEET_DIST_KM voneinander → keine gemeinsame Gruppe."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        conn = make_conn()
        set_seed(conn, 42)

        # 10 km voneinander (weit über MEET_DIST_KM = 2 km)
        lat_a = 5.0
        lon_a = 5.0
        lat_b = lat_a + 10.0 / 111.32  # ~10 km

        id_a = _insert_survivor(conn, lat=lat_a, lon=lon_a, age_years=30.0)
        id_b = _insert_survivor(conn, lat=lat_b, lon=lon_a, age_years=30.0)

        try:
            sim_mod.step_day(conn, 1)

            gid_a = conn.execute(
                "SELECT group_id FROM survivors WHERE id=?;", (id_a,)
            ).fetchone()["group_id"]
            gid_b = conn.execute(
                "SELECT group_id FROM survivors WHERE id=?;", (id_b,)
            ).fetchone()["group_id"]

            # Entweder beide haben keine Gruppe oder verschiedene Gruppen
            if gid_a is not None and gid_b is not None:
                assert gid_a != gid_b, (
                    "Weit entfernte Survivor sollten NICHT dieselbe group_id haben"
                )
        finally:
            _reset_density_cache()
            conn.close()

    def test_group_entry_created_in_table(self, monkeypatch):
        """Wenn Gruppe gebildet, existiert ein Eintrag in survivor_groups."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        conn = make_conn()
        set_seed(conn, 42)

        lat_a = 10.0
        lat_b = lat_a + 5.0 / 111_320.0

        _insert_survivor(conn, lat=lat_a, lon=10.0, age_years=25.0)
        _insert_survivor(conn, lat=lat_b, lon=10.0, age_years=28.0)

        try:
            sim_mod.step_day(conn, 1)

            groups = conn.execute("SELECT COUNT(*) FROM survivor_groups;").fetchone()[0]
            grouped = conn.execute(
                "SELECT COUNT(*) FROM survivors WHERE group_id IS NOT NULL AND alive=1;"
            ).fetchone()[0]

            assert groups >= 1, "Mindestens eine Gruppe sollte angelegt worden sein"
            assert grouped >= 2, "Mindestens zwei Survivor sollten in einer Gruppe sein"
        finally:
            _reset_density_cache()
            conn.close()

    def test_group_merges_existing_groups(self, monkeypatch):
        """Wenn Survivor aus verschiedenen Gruppen nah beieinander sind, werden sie gemergt."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        conn = make_conn()
        set_seed(conn, 42)

        # Drei Survivor alle < MEET_DIST_KM beieinander
        base_lat = 7.0
        for i in range(3):
            _insert_survivor(
                conn,
                lat=base_lat + i * 5.0 / 111_320.0,
                lon=7.0,
                age_years=30.0,
            )

        try:
            sim_mod.step_day(conn, 1)

            group_ids = [
                r["group_id"] for r in conn.execute(
                    "SELECT group_id FROM survivors WHERE alive=1 ORDER BY id;"
                ).fetchall()
                if r["group_id"] is not None
            ]

            # Alle, die eine Gruppe haben, sollten dieselbe haben
            if group_ids:
                assert len(set(group_ids)) == 1, (
                    f"Alle nahen Survivor sollten einer Gruppe angehören, "
                    f"aber es gibt {len(set(group_ids))} Gruppen: {set(group_ids)}"
                )
        finally:
            _reset_density_cache()
            conn.close()


# ===========================================================================
# e) Sterben (#20)
# ===========================================================================

class TestDeathModel:
    """
    Sterbe-Dynamik: Säugling allein stirbt früh; Säugling in Gruppe überleben
    länger; Greis stirbt eher als Erwachsener; alive sinkt monoton; Determinismus.
    """

    def test_infant_alone_dies_within_few_days(self, monkeypatch):
        """Allein lebender Säugling (age < 1 Jahr) stirbt nach wenigen Tagen."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        # Kleinstpopulation um LLM-Kosten zu sparen: 1 Säugling
        conn = make_conn()
        set_seed(conn, 42)

        # Säugling: birth_tick = 0 (d.h. age = 0/525600 = 0 < 1 Jahr)
        conn.execute(
            "INSERT INTO survivors (lat, lon, sex, birth_tick, alive) "
            "VALUES (0.0, 0.0, 'm', 0, 1);"
        )
        conn.commit()

        try:
            # Basis-p_survive = 0.30/Tag → nach 5 Tagen p_alive ≈ 0.30^5 ≈ 0.002
            # In fast allen Seed-Varianten sollte er nach 10 Tagen tot sein.
            for day in range(1, 11):
                sim_mod.step_day(conn, day)
                alive = conn.execute(
                    "SELECT alive FROM survivors;"
                ).fetchone()["alive"]
                if alive == 0:
                    break  # Gestorben — Test erfolgreich
            else:
                pytest.fail(
                    "Säugling allein überlebte 10 Tage — erwartet war Tod in < 10 Tagen "
                    f"(p_survive_base=0.30/Tag)"
                )
        finally:
            _reset_density_cache()
            conn.close()

    def test_infant_in_adult_group_survives_longer(self, monkeypatch):
        """Säugling in Gruppe mit Erwachsenem überlebt länger als allein (stat. Test)."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        # Wir messen die mittlere Überlebensdauer über mehrere Seeds:
        # Säugling allein vs. Säugling + 1 Erwachsener in gleicher Gruppe.
        N_SEEDS = 5
        MAX_DAYS = 15

        def _survival_days(seed: int, has_adult: bool) -> int:
            _reset_density_cache()
            conn = make_conn()
            set_seed(conn, seed)

            # Säugling bei (0.0, 0.0)
            conn.execute(
                "INSERT INTO survivors (lat, lon, sex, birth_tick, alive) "
                "VALUES (0.0, 0.0, 'm', 0, 1);"
            )
            infant_id = conn.execute("SELECT last_insert_rowid();").fetchone()[0]

            if has_adult:
                # Erwachsener 10 m nebenan → innerhalb MEET_DIST_KM → gleiche Gruppe
                adult_lat = 0.0 + 10.0 / 111_320.0
                conn.execute(
                    "INSERT INTO survivors (lat, lon, sex, birth_tick, alive) "
                    "VALUES (?, 0.0, 'f', ?, 1);",
                    (adult_lat, -int(30 * _MINUTES_PER_YEAR)),
                )

            conn.commit()

            survived = 0
            for day in range(1, MAX_DAYS + 1):
                sim_mod.step_day(conn, day)
                alive = conn.execute(
                    "SELECT alive FROM survivors WHERE id=?;", (infant_id,)
                ).fetchone()["alive"]
                if alive == 1:
                    survived = day
                else:
                    break
            conn.close()
            return survived

        alone_days = [_survival_days(seed=s, has_adult=False) for s in range(N_SEEDS)]
        group_days = [_survival_days(seed=s, has_adult=True) for s in range(N_SEEDS)]

        mean_alone = sum(alone_days) / N_SEEDS
        mean_group = sum(group_days) / N_SEEDS

        assert mean_group > mean_alone, (
            f"Säugling in Gruppe sollte länger überleben als allein, "
            f"aber mean_alone={mean_alone:.1f} >= mean_group={mean_group:.1f}"
        )

    def test_elder_dies_faster_than_adult_by_constants(self):
        """Greis-p_survive < Erwachsenen-p_survive (Konstanten-Verifikation).

        Der Sterbe-Sim nutzt die constants-Tabelle: niedrigere p_survive = höhere
        Sterbewahrscheinlichkeit. Wir verifizieren die Design-Absicht direkt.
        """
        # Altersklassen: 5=Greis(80+), 3=Erwachsener(13-64)
        p_elder = constants.SURVIVOR_BASE_SURVIVE_PER_DAY[5]
        p_adult = constants.SURVIVOR_BASE_SURVIVE_PER_DAY[3]

        assert p_elder < p_adult, (
            f"Greis-p_survive={p_elder} sollte < Erwachsenen-p_survive={p_adult}"
        )

    def test_elder_lower_p_survive_than_senior(self):
        """Greis (80+) stirbt häufiger als Senior (65-79): Abstufung korrekt."""
        p_senior = constants.SURVIVOR_BASE_SURVIVE_PER_DAY[4]
        p_elder = constants.SURVIVOR_BASE_SURVIVE_PER_DAY[5]
        assert p_elder < p_senior, (
            f"Greis={p_elder} sollte kleiner als Senior={p_senior}"
        )

    def test_infant_lower_p_survive_than_child(self):
        """Säugling (<1 J.) stirbt häufiger als Kind (5-12 J.)."""
        p_infant = constants.SURVIVOR_BASE_SURVIVE_PER_DAY[0]
        p_child = constants.SURVIVOR_BASE_SURVIVE_PER_DAY[2]
        assert p_infant < p_child

    def test_alive_count_monotonically_decreasing(self, monkeypatch):
        """Gesamtpopulation alive sinkt monoton über viele step_day."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        conn = make_conn()
        set_seed(conn, 42)

        # 20 Survivor in verschiedenen Altersklassen; keine Gruppen
        for i in range(5):
            _insert_survivor(conn, lat=float(i), lon=0.0, age_years=0.1)   # Säugling
        for i in range(5):
            _insert_survivor(conn, lat=float(i), lon=1.0, age_years=80.0)  # Greis
        for i in range(5):
            _insert_survivor(conn, lat=float(i), lon=2.0, age_years=30.0)  # Erwachsener
        for i in range(5):
            _insert_survivor(conn, lat=float(i), lon=3.0, age_years=3.0)   # Kleinkind

        try:
            prev_alive = conn.execute(
                "SELECT COUNT(*) FROM survivors WHERE alive=1;"
            ).fetchone()[0]

            for day in range(1, 31):
                sim_mod.step_day(conn, day)
                alive_now = conn.execute(
                    "SELECT COUNT(*) FROM survivors WHERE alive=1;"
                ).fetchone()[0]
                assert alive_now <= prev_alive, (
                    f"Tag {day}: alive stieg von {prev_alive} auf {alive_now} "
                    f"— Monotonizität verletzt!"
                )
                prev_alive = alive_now
        finally:
            _reset_density_cache()
            conn.close()

    def test_death_determinism(self, monkeypatch):
        """Gleicher Seed → gleicher Sterbe-Verlauf."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        try:
            def _make_pop():
                c = make_conn()
                set_seed(c, 12345)
                for i in range(3):
                    _insert_survivor(c, lat=float(i)*0.001, lon=0.0, age_years=0.5)
                for i in range(3):
                    _insert_survivor(c, lat=float(i)*0.001, lon=1.0, age_years=85.0)
                return c

            c1 = _make_pop()
            c2 = _make_pop()

            for day in range(1, 8):
                _reset_density_cache()
                sim_mod.step_day(c1, day)
                _reset_density_cache()
                sim_mod.step_day(c2, day)

            alive1 = [
                r["alive"] for r in c1.execute(
                    "SELECT alive FROM survivors ORDER BY id;"
                ).fetchall()
            ]
            alive2 = [
                r["alive"] for r in c2.execute(
                    "SELECT alive FROM survivors ORDER BY id;"
                ).fetchall()
            ]

            assert alive1 == alive2, (
                f"Gleiches Seed → gleicher Verlauf erwartet, "
                f"aber alive1={alive1}, alive2={alive2}"
            )

            c1.close()
            c2.close()
        finally:
            _reset_density_cache()


# ===========================================================================
# f) Tick-Integration
# ===========================================================================

class TestTickIntegration:
    """advance_tick über Tagesgrenze → genau ein step_day."""

    def test_one_day_boundary_triggers_one_step(self, conn, monkeypatch):
        """Ein advance_tick über eine Tagesgrenze erhöht survivor_sim_day um 1.

        Hinweis: step_day gibt bei leerer survivors-Tabelle früh zurück,
        ohne survivor_sim_day zu aktualisieren (bekanntes Verhalten). Daher
        brauchen wir mindestens einen lebenden Survivor.
        """
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        from app.sim.tick import advance_tick

        # Mindestens einen Survivor einfügen, sonst kehrt step_day early-return zurück
        _insert_survivor(conn, lat=0.0, lon=0.0, age_years=30.0)

        # tick=1430 → t1=1440 → new_day = 1440//1440 = 1
        # last_sim_day=0 → range(1, 2) → step_day(1) → survivor_sim_day=1
        conn.execute("UPDATE world SET tick = 1430, survivor_sim_day = 0 WHERE id = 1;")
        conn.commit()

        sim_day_before = conn.execute(
            "SELECT survivor_sim_day FROM world WHERE id=1;"
        ).fetchone()["survivor_sim_day"]

        try:
            advance_tick(conn, minutes=constants.TICK_MINUTES)
        finally:
            _reset_density_cache()

        sim_day_after = conn.execute(
            "SELECT survivor_sim_day FROM world WHERE id=1;"
        ).fetchone()["survivor_sim_day"]

        assert sim_day_after == sim_day_before + 1, (
            f"Erwartet survivor_sim_day={sim_day_before + 1}, "
            f"aber bekommen {sim_day_after}"
        )

    def test_n_days_triggers_n_steps(self, conn, monkeypatch):
        """N advance_ticks über N Tagesgrenzen → N step_day-Aufrufe.

        Hinweis: step_day gibt bei leerer survivors-Tabelle früh zurück,
        ohne survivor_sim_day zu setzen. Daher mindestens einen Survivor einfügen.
        """
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        from app.sim.tick import advance_tick

        N = 3

        # Mindestens einen Survivor einfügen (step_day braucht ihn zum Weiterlaufen)
        _insert_survivor(conn, lat=0.0, lon=0.0, age_years=30.0)

        # Auf Tages-Beginn setzen
        conn.execute("UPDATE world SET tick = 0, survivor_sim_day = 0 WHERE id = 1;")
        conn.commit()

        # MINUTES_PER_DAY / TICK_MINUTES = 1440 / 10 = 144 Ticks pro Tag
        ticks_per_day = constants.MINUTES_PER_DAY // constants.TICK_MINUTES

        try:
            for _ in range(N * ticks_per_day):
                _reset_density_cache()
                advance_tick(conn, minutes=constants.TICK_MINUTES)
        finally:
            _reset_density_cache()

        sim_day = conn.execute(
            "SELECT survivor_sim_day FROM world WHERE id=1;"
        ).fetchone()["survivor_sim_day"]

        assert sim_day == N, (
            f"Nach {N} Tagen sollte survivor_sim_day={N}, aber bekommen {sim_day}"
        )

    def test_no_double_step_within_same_day(self, conn, monkeypatch):
        """Tick ohne Tageswechsel erhöht survivor_sim_day NICHT."""
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        from app.sim.tick import advance_tick

        # Tick mitten im Tag: 720 Minuten (halber Tag)
        conn.execute("UPDATE world SET tick = 720, survivor_sim_day = 0 WHERE id = 1;")
        conn.commit()

        # Ein Tick, der nicht die nächste Tagesgrenze überschreitet (1440)
        # 720 + 10 = 730 < 1440 → kein neuer Tag
        try:
            advance_tick(conn, minutes=constants.TICK_MINUTES)
        finally:
            _reset_density_cache()

        sim_day = conn.execute(
            "SELECT survivor_sim_day FROM world WHERE id=1;"
        ).fetchone()["survivor_sim_day"]

        assert sim_day == 0, (
            f"Kein Tageswechsel → survivor_sim_day sollte 0 bleiben, aber bekommen {sim_day}"
        )

    def test_tick_without_survivors_no_error(self, conn, monkeypatch):
        """advance_tick mit leerer survivors-Tabelle wirft keinen Fehler.

        Hinweis: Bei leerer Tabelle ruft step_day früh zurück und aktualisiert
        survivor_sim_day NICHT (bekanntes app/-Verhalten). Wir testen nur,
        dass kein Ausnahmefehler geworfen wird und der Tick-Zähler korrekt steigt.
        """
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        from app.sim.tick import advance_tick

        conn.execute("UPDATE world SET tick = 1430, survivor_sim_day = 0 WHERE id = 1;")
        conn.commit()

        try:
            # Darf keinen Fehler werfen
            result = advance_tick(conn, minutes=constants.TICK_MINUTES)
        finally:
            _reset_density_cache()

        # Tick-Zähler steigt korrekt
        tick = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
        assert tick == 1440
        # Kein Fehler im result
        assert "tick" in result


# ===========================================================================
# g) population_stats
# ===========================================================================

class TestPopulationStats:
    """population_stats gibt korrekte Zahlen zurück."""

    def test_empty_db(self, conn):
        stats = sim_mod.population_stats(conn)
        assert stats["alive"] == 0
        assert stats["dead"] == 0
        assert stats["total"] == 0
        assert stats["groups"] == 0
        assert stats["grouped"] == 0
        assert stats["alone"] == 0

    def test_counts_alive_and_dead(self, conn, monkeypatch):
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)
        _patch_grid_spawn(monkeypatch)

        try:
            surv_mod.spawn_survivors(conn, total=20, seed=42)

            # Einige tot setzen
            conn.execute(
                "UPDATE survivors SET alive=0 WHERE id IN (SELECT id FROM survivors LIMIT 5);"
            )
            conn.commit()

            stats = sim_mod.population_stats(conn)
            assert stats["alive"] == 15
            assert stats["dead"] == 5
            assert stats["total"] == 20
        finally:
            _reset_density_cache()

    def test_groups_counted(self, conn, monkeypatch):
        _patch_density_field(monkeypatch, _MINI_GRID_MIGRATION)

        try:
            # Zwei Gruppen anlegen
            g1 = _insert_group(conn)
            g2 = _insert_group(conn)

            _insert_survivor(conn, lat=0.0, lon=0.0, age_years=25.0, group_id=g1)
            _insert_survivor(conn, lat=0.0, lon=0.0, age_years=30.0, group_id=g1)
            _insert_survivor(conn, lat=1.0, lon=1.0, age_years=35.0, group_id=g2)
            _insert_survivor(conn, lat=2.0, lon=2.0, age_years=28.0)  # allein

            stats = sim_mod.population_stats(conn)
            assert stats["groups"] == 2
            assert stats["grouped"] == 3
            assert stats["alone"] == 1
            assert stats["alive"] == 4
        finally:
            _reset_density_cache()
