"""Tests fuer globale Ueberlebenden-Verteilung und lazy NPC-Materialisierung.

Abgedeckte Faelle (Issue #12):
a) load_grid(): nicht leer, positive Gewichte, Summe plausibel
b) spawn_survivors erzeugt exakt total Zeilen
c) Determinismus: gleicher Seed -> identische Positionen
d) Idempotenz: zweiter Aufruf mit gleichem total aendert nichts
e) Dichte-Proportionalitaet: hoehere Gewichte -> mehr Punkte
f) materialize_in_bbox: materialisiert nur Box-Punkte, setzt materialized + character_id, idempotent
g) count_near: korrekte Radius-Zaehlung
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.conftest import make_conn, set_seed
from app.sim import survivors as surv_mod
from app.sim import popgrid as popgrid_mod


# ---------------------------------------------------------------------------
# Mini-Gitter fuer schnelle, deterministische Tests
# ---------------------------------------------------------------------------
# 3 Zellen: (0.0, 0.0, 100), (10.0, 10.0, 10), (50.0, 50.0, 1)
# Klare Gewichtsunterschiede fuer Proportionalitaets-Tests.
_MINI_GRID = [
    (0.0, 0.0, 100.0),
    (10.0, 10.0, 10.0),
    (50.0, 50.0, 1.0),
]


def _patch_grid(monkeypatch, grid=_MINI_GRID):
    """Ersetzt load_grid() durch ein kleines, statisches Gitter.

    Wichtig: lru_cache muss geleert werden, da load_grid prozessweit cacht.
    Ausserdem muss survivors.py an der richtigen Stelle gepatcht werden (der
    Modul-Import in survivors.py importiert load_grid direkt).
    """
    # Cache leeren, falls er aus einem anderen Test befuellt wurde
    popgrid_mod.load_grid.cache_clear()

    monkeypatch.setattr(surv_mod, "load_grid", lambda: grid)


# ---------------------------------------------------------------------------
# a) load_grid: echtes Asset
# ---------------------------------------------------------------------------

class TestLoadGrid:
    def test_not_empty(self):
        """load_grid() liefert mindestens eine Zelle."""
        grid = popgrid_mod.load_grid()
        assert len(grid) > 0

    def test_positive_weights(self):
        """Alle Gewichte sind positiv (> 0)."""
        grid = popgrid_mod.load_grid()
        for lat, lon, weight in grid:
            assert weight > 0, f"Nicht-positives Gewicht bei ({lat}, {lon}): {weight}"

    def test_weight_sum_plausible(self):
        """Gesamtgewicht deutlich groesser als 0 (Weltbevoelkerung-Groessenordnung)."""
        grid = popgrid_mod.load_grid()
        total_weight = sum(w for _, _, w in grid)
        assert total_weight > 1_000_000, (
            f"Gewichtssumme erwartet > 1M (Weltbev.), tatsaechlich: {total_weight}"
        )

    def test_lat_lon_in_valid_range(self):
        """Koordinaten liegen im gueltigen Weltkoordinaten-Bereich."""
        grid = popgrid_mod.load_grid()
        for lat, lon, _ in grid:
            assert -90.0 <= lat <= 90.0, f"lat ausserhalb: {lat}"
            assert -180.0 <= lon <= 180.0, f"lon ausserhalb: {lon}"

    def test_result_is_list_of_triples(self):
        """load_grid() gibt eine Liste von 3-Tupeln zurueck."""
        grid = popgrid_mod.load_grid()
        assert isinstance(grid, list)
        for item in grid[:5]:
            assert len(item) == 3


# ---------------------------------------------------------------------------
# b) spawn_survivors erzeugt exakt total Zeilen
# ---------------------------------------------------------------------------

class TestSpawnTotal:
    def test_exact_total_inserted(self, conn, monkeypatch):
        """spawn_survivors(total=500) legt exakt 500 Zeilen an."""
        _patch_grid(monkeypatch)
        n = surv_mod.spawn_survivors(conn, total=500, seed=42)
        assert n == 500
        db_count = conn.execute("SELECT COUNT(*) FROM survivors;").fetchone()[0]
        assert db_count == 500

    def test_return_value_matches_total(self, conn, monkeypatch):
        """Rueckgabewert von spawn_survivors == total."""
        _patch_grid(monkeypatch)
        result = surv_mod.spawn_survivors(conn, total=200, seed=7)
        assert result == 200

    def test_all_rows_have_lat_lon(self, conn, monkeypatch):
        """Jede eingefuegte Zeile hat lat und lon."""
        _patch_grid(monkeypatch)
        surv_mod.spawn_survivors(conn, total=100, seed=1)
        rows = conn.execute("SELECT lat, lon FROM survivors;").fetchall()
        for row in rows:
            assert row["lat"] is not None
            assert row["lon"] is not None


# ---------------------------------------------------------------------------
# c) Determinismus: gleicher Seed -> identische Positionen
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_seed_same_positions(self, conn_seeded, monkeypatch):
        """Zwei frische DBs, gleicher Seed -> identische lat/lon-Liste."""
        _patch_grid(monkeypatch)

        c1 = conn_seeded("_surv_det1")
        c2 = conn_seeded("_surv_det2")
        # Beide DBs haben Seed 1337 (aus conn_seeded)

        surv_mod.spawn_survivors(c1, total=300, seed=1337)
        surv_mod.spawn_survivors(c2, total=300, seed=1337)

        rows1 = c1.execute("SELECT lat, lon FROM survivors ORDER BY id;").fetchall()
        rows2 = c2.execute("SELECT lat, lon FROM survivors ORDER BY id;").fetchall()

        assert len(rows1) == len(rows2) == 300
        for r1, r2 in zip(rows1, rows2):
            assert abs(r1["lat"] - r2["lat"]) < 1e-12
            assert abs(r1["lon"] - r2["lon"]) < 1e-12

        c1.close()
        c2.close()

    def test_different_seed_different_distribution(self, conn_seeded, monkeypatch):
        """Anderer Seed -> andere Verteilung (nicht identisch)."""
        _patch_grid(monkeypatch)

        c1 = conn_seeded("_surv_dseed1")
        c2 = conn_seeded("_surv_dseed2")

        surv_mod.spawn_survivors(c1, total=200, seed=1111)
        surv_mod.spawn_survivors(c2, total=200, seed=9999)

        lats1 = [r["lat"] for r in c1.execute("SELECT lat FROM survivors ORDER BY id;").fetchall()]
        lats2 = [r["lat"] for r in c2.execute("SELECT lat FROM survivors ORDER BY id;").fetchall()]

        # Mindestens ein Unterschied erwartet (anderer Seed)
        assert lats1 != lats2, "Verschiedene Seeds sollten verschiedene Verteilungen erzeugen"

        c1.close()
        c2.close()

    def test_same_seed_same_positions_single_db(self, conn, monkeypatch):
        """Gleicher Seed, gleicher Aufruf auf leerer DB -> deterministisch."""
        _patch_grid(monkeypatch)

        surv_mod.spawn_survivors(conn, total=150, seed=42)
        rows_first = conn.execute("SELECT lat, lon FROM survivors ORDER BY id;").fetchall()
        lats_first = [r["lat"] for r in rows_first]
        lons_first = [r["lon"] for r in rows_first]

        # Idempotenz-Reset: andere Anzahl erzwingen, dann zurueck
        # Neuen Aufruf auf frischer DB simulieren: direkt loeschen und nochmal
        conn.execute("DELETE FROM survivors;")
        conn.commit()
        surv_mod.spawn_survivors(conn, total=150, seed=42)
        rows_second = conn.execute("SELECT lat, lon FROM survivors ORDER BY id;").fetchall()
        lats_second = [r["lat"] for r in rows_second]
        lons_second = [r["lon"] for r in rows_second]

        assert lats_first == lats_second
        assert lons_first == lons_second


# ---------------------------------------------------------------------------
# d) Idempotenz: zweiter Aufruf aendert nichts
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_call_same_total_noop(self, conn, monkeypatch):
        """Zweiter spawn_survivors mit gleichem total -> keine neuen Zeilen."""
        _patch_grid(monkeypatch)

        surv_mod.spawn_survivors(conn, total=400, seed=99)
        rows_before = conn.execute("SELECT id, lat, lon FROM survivors ORDER BY id;").fetchall()

        # Zweiter Aufruf mit gleichem total
        n2 = surv_mod.spawn_survivors(conn, total=400, seed=99)
        rows_after = conn.execute("SELECT id, lat, lon FROM survivors ORDER BY id;").fetchall()

        assert n2 == 400
        assert len(rows_before) == len(rows_after) == 400

        # Positionen unveraendert
        for b, a in zip(rows_before, rows_after):
            assert b["id"] == a["id"]
            assert b["lat"] == a["lat"]
            assert b["lon"] == a["lon"]

    def test_different_total_regenerates(self, conn, monkeypatch):
        """Anderes total -> clear + neu erzeugen -> exakt neues total."""
        _patch_grid(monkeypatch)

        surv_mod.spawn_survivors(conn, total=300, seed=5)
        assert conn.execute("SELECT COUNT(*) FROM survivors;").fetchone()[0] == 300

        surv_mod.spawn_survivors(conn, total=500, seed=5)
        assert conn.execute("SELECT COUNT(*) FROM survivors;").fetchone()[0] == 500


# ---------------------------------------------------------------------------
# e) Dichte-Proportionalitaet
# ---------------------------------------------------------------------------

class TestDensityProportionality:
    def test_heavy_cell_gets_more_survivors(self, conn, monkeypatch):
        """Zelle mit Gewicht 100 erhaelt deutlich mehr Punkte als Zelle mit 1."""
        _patch_grid(monkeypatch)
        # Mini-Grid: Zelle 0 = Gewicht 100, Zelle 2 = Gewicht 1
        # Erwartetes Verhaeltnis: ca. 100:1 (+/- grosszuegige Toleranz)
        total = 2000
        surv_mod.spawn_survivors(conn, total=total, seed=1337)

        # Zelle 0 bei (0.0, 0.0) mit half_cell=0.125 -> Box [-0.125, 0.125]
        c0_count = conn.execute(
            "SELECT COUNT(*) FROM survivors "
            "WHERE lat BETWEEN -0.125 AND 0.125 AND lon BETWEEN -0.125 AND 0.125;"
        ).fetchone()[0]
        # Zelle 2 bei (50.0, 50.0)
        c2_count = conn.execute(
            "SELECT COUNT(*) FROM survivors "
            "WHERE lat BETWEEN 49.875 AND 50.125 AND lon BETWEEN 49.875 AND 50.125;"
        ).fetchone()[0]
        # Zelle 1 bei (10.0, 10.0)
        c1_count = conn.execute(
            "SELECT COUNT(*) FROM survivors "
            "WHERE lat BETWEEN 9.875 AND 10.125 AND lon BETWEEN 9.875 AND 10.125;"
        ).fetchone()[0]

        # Zelle 0 (Gewicht 100) muss mehr haben als Zelle 1 (Gewicht 10)
        assert c0_count > c1_count, (
            f"Zelle 0 (w=100): {c0_count} <= Zelle 1 (w=10): {c1_count}"
        )
        # Zelle 1 (Gewicht 10) muss mehr haben als Zelle 2 (Gewicht 1)
        assert c1_count > c2_count, (
            f"Zelle 1 (w=10): {c1_count} <= Zelle 2 (w=1): {c2_count}"
        )
        # Zelle 0 sollte grob ~10x mehr als Zelle 1 haben (Toleranz: Faktor 5-20)
        if c1_count > 0:
            ratio = c0_count / c1_count
            assert ratio > 5, (
                f"Erwartet c0/c1 > 5, tatsaechlich: {ratio:.1f} "
                f"(c0={c0_count}, c1={c1_count})"
            )

    def test_all_survivors_placed_in_known_cells(self, conn, monkeypatch):
        """Bei Mini-Gitter liegen alle Punkte in einer der drei Zell-Bboxen."""
        _patch_grid(monkeypatch)
        total = 500
        surv_mod.spawn_survivors(conn, total=total, seed=7)

        def in_cell(lat_center, lon_center, rows):
            """Zaehlt Punkte innerhalb ±0.125 um Zellzentrum."""
            half = 0.125
            return sum(
                1 for r in rows
                if abs(r["lat"] - lat_center) <= half
                and abs(r["lon"] - lon_center) <= half
            )

        rows = conn.execute("SELECT lat, lon FROM survivors;").fetchall()
        c0 = in_cell(0.0, 0.0, rows)
        c1 = in_cell(10.0, 10.0, rows)
        c2 = in_cell(50.0, 50.0, rows)

        assert c0 + c1 + c2 == total, (
            f"Nicht alle Punkte in Zell-Bboxen: c0={c0} c1={c1} c2={c2} sum={c0+c1+c2} != {total}"
        )


# ---------------------------------------------------------------------------
# f) materialize_in_bbox
# ---------------------------------------------------------------------------

class TestMaterializeInBbox:
    def _spawn_few(self, conn, monkeypatch):
        """Hilfsfunktion: spawnt 200 Survivor mit Mini-Gitter."""
        _patch_grid(monkeypatch)
        surv_mod.spawn_survivors(conn, total=200, seed=42)

    def test_materializes_only_in_box(self, conn, monkeypatch):
        """materialize_in_bbox beruehrt nur Punkte in der Box."""
        self._spawn_few(conn, monkeypatch)

        # Box um Zelle 0 (lat ~0, lon ~0), streng ±0.1
        char_ids = surv_mod.materialize_in_bbox(conn, -0.1, -0.1, 0.1, 0.1)

        # Alle materialisierten Eintraege liegen wirklich in der Box
        mat_rows = conn.execute(
            "SELECT lat, lon FROM survivors WHERE materialized = 1;"
        ).fetchall()
        for r in mat_rows:
            assert -0.1 <= r["lat"] <= 0.1, f"lat {r['lat']} ausserhalb Box"
            assert -0.1 <= r["lon"] <= 0.1, f"lon {r['lon']} ausserhalb Box"

        # Punkte ausserhalb der Box bleiben nicht-materialisiert
        outside_mat = conn.execute(
            "SELECT COUNT(*) FROM survivors "
            "WHERE (lat < -0.1 OR lat > 0.1 OR lon < -0.1 OR lon > 0.1) "
            "AND materialized = 1;"
        ).fetchone()[0]
        assert outside_mat == 0

    def test_sets_materialized_flag_and_character_id(self, conn, monkeypatch):
        """materialize_in_bbox setzt materialized=1 und character_id fuer jeden."""
        self._spawn_few(conn, monkeypatch)

        char_ids = surv_mod.materialize_in_bbox(conn, -0.1, -0.1, 0.1, 0.1)

        if not char_ids:
            pytest.skip("Keine Survivors in dieser Box (Seed-abhaengig)")

        mat_rows = conn.execute(
            "SELECT materialized, character_id FROM survivors WHERE materialized = 1;"
        ).fetchall()
        for r in mat_rows:
            assert r["materialized"] == 1
            assert r["character_id"] is not None

    def test_creates_characters_of_type_survivor(self, conn, monkeypatch):
        """Materialisierte Survivors bekommen characters(type='survivor')."""
        self._spawn_few(conn, monkeypatch)

        char_ids = surv_mod.materialize_in_bbox(conn, -0.1, -0.1, 0.1, 0.1)

        if not char_ids:
            pytest.skip("Keine Survivors in dieser Box")

        for cid in char_ids:
            row = conn.execute(
                "SELECT type FROM characters WHERE id = ?;", (cid,)
            ).fetchone()
            assert row is not None, f"character_id {cid} nicht in characters"
            assert row["type"] == "survivor", f"Erwartet type='survivor', got '{row['type']}'"

    def test_idempotent_second_call_no_duplicates(self, conn, monkeypatch):
        """Zweiter materialize_in_bbox-Aufruf erzeugt keine Duplikate."""
        self._spawn_few(conn, monkeypatch)

        ids1 = surv_mod.materialize_in_bbox(conn, -0.1, -0.1, 0.1, 0.1)
        count_chars_after_first = conn.execute("SELECT COUNT(*) FROM characters;").fetchone()[0]
        count_surv_mat_first = conn.execute(
            "SELECT COUNT(*) FROM survivors WHERE materialized = 1;"
        ).fetchone()[0]

        ids2 = surv_mod.materialize_in_bbox(conn, -0.1, -0.1, 0.1, 0.1)
        count_chars_after_second = conn.execute("SELECT COUNT(*) FROM characters;").fetchone()[0]
        count_surv_mat_second = conn.execute(
            "SELECT COUNT(*) FROM survivors WHERE materialized = 1;"
        ).fetchone()[0]

        # Zweiter Aufruf darf keine neuen characters erzeugen
        assert count_chars_after_second == count_chars_after_first
        # Anzahl materialisierter Survivors unveraendert
        assert count_surv_mat_second == count_surv_mat_first
        # Zweiter Aufruf gibt leere Liste zurueck (keine neuen)
        assert ids2 == []

    def test_empty_box_returns_empty_list(self, conn, monkeypatch):
        """Box ohne Survivors -> leere Liste, keine characters eingefuegt."""
        self._spawn_few(conn, monkeypatch)

        # Box weit weg von allen Zellen
        result = surv_mod.materialize_in_bbox(conn, 80.0, 80.0, 90.0, 90.0)
        assert result == []

        # Keine characters ausser dem Standard-Player (id=1)
        count = conn.execute(
            "SELECT COUNT(*) FROM characters WHERE type = 'survivor';"
        ).fetchone()[0]
        assert count == 0

    def test_returns_correct_character_ids(self, conn, monkeypatch):
        """Zurueckgegebene char_ids stimmen mit characters-Tabelle ueberein."""
        self._spawn_few(conn, monkeypatch)

        char_ids = surv_mod.materialize_in_bbox(conn, -0.1, -0.1, 0.1, 0.1)

        if not char_ids:
            pytest.skip("Keine Survivors in dieser Box")

        for cid in char_ids:
            row = conn.execute("SELECT id FROM characters WHERE id = ?;", (cid,)).fetchone()
            assert row is not None, f"character_id {cid} fehlt in characters"

        # Genau so viele survivor-characters wie char_ids
        db_count = conn.execute(
            "SELECT COUNT(*) FROM characters WHERE type = 'survivor';"
        ).fetchone()[0]
        assert db_count == len(char_ids)


# ---------------------------------------------------------------------------
# g) count_near
# ---------------------------------------------------------------------------

class TestCountNear:
    def _insert_survivor_at(self, conn, lat, lon):
        """Fuegt einen Survivor manuell an einer bekannten Position ein."""
        conn.execute("INSERT INTO survivors (lat, lon) VALUES (?, ?);", (lat, lon))
        conn.commit()

    def test_counts_survivor_in_radius(self, conn):
        """Survivor innerhalb des Radius wird gezaehlt."""
        # Platziere Survivor genau 500 m noerdlich des Mittelpunkts
        center_lat, center_lon = 10.0, 10.0
        # ~500 m noerdlich: 500 / 111320 ≈ 0.00449 Grad
        offset_lat = 500.0 / 111_320.0
        self._insert_survivor_at(conn, center_lat + offset_lat * 0.8, center_lon)

        count = surv_mod.count_near(conn, center_lat, center_lon, 1000.0)
        assert count == 1

    def test_does_not_count_outside_radius(self, conn):
        """Survivor ausserhalb des Radius wird nicht gezaehlt."""
        center_lat, center_lon = 10.0, 10.0
        # Platziere weit weg (10 km noerdlich)
        offset_lat = 10_000.0 / 111_320.0
        self._insert_survivor_at(conn, center_lat + offset_lat * 1.2, center_lon)

        count = surv_mod.count_near(conn, center_lat, center_lon, 1000.0)
        assert count == 0

    def test_counts_multiple_survivors(self, conn):
        """Mehrere Survivors im Radius werden alle gezaehlt."""
        center_lat, center_lon = 20.0, 20.0
        half_deg = 0.0001  # sehr nah (ca. 11 m)
        for i in range(5):
            self._insert_survivor_at(conn, center_lat + i * half_deg, center_lon)

        count = surv_mod.count_near(conn, center_lat, center_lon, 1000.0)
        assert count == 5

    def test_empty_table_returns_zero(self, conn):
        """Leere Tabelle -> count_near == 0."""
        count = surv_mod.count_near(conn, 0.0, 0.0, 5000.0)
        assert count == 0

    def test_boundary_exactly_at_radius(self, conn):
        """Punkt exakt am Radius: Haversine-Grenzfall."""
        center_lat, center_lon = 5.0, 5.0
        # Exakt 1000 m noerdlich
        exact_offset = 1000.0 / 111_320.0
        self._insert_survivor_at(conn, center_lat + exact_offset, center_lon)

        # Radius 1001 m -> drin
        assert surv_mod.count_near(conn, center_lat, center_lon, 1001.0) == 1
        # Radius 999 m -> draussen
        assert surv_mod.count_near(conn, center_lat, center_lon, 999.0) == 0

    def test_mixed_in_and_out(self, conn):
        """Einige Survivors in, andere ausserhalb des Radius."""
        center_lat, center_lon = 0.0, 0.0
        # 3 nah (< 500 m)
        for i in range(3):
            near = 300.0 / 111_320.0
            self._insert_survivor_at(conn, center_lat + near * (i + 1) * 0.3, center_lon)
        # 2 weit (> 2000 m)
        for i in range(2):
            far = 3000.0 / 111_320.0
            self._insert_survivor_at(conn, center_lat + far, center_lon + far * (i + 1))

        count = surv_mod.count_near(conn, center_lat, center_lon, 500.0)
        assert count == 3


# ---------------------------------------------------------------------------
# h) spawn_survivors leert survivor_groups im Regenerations-Pfad (Issue #41)
# ---------------------------------------------------------------------------

class TestSpawnClearsSurvivorGroups:
    def test_force_clears_survivor_groups(self, conn, monkeypatch):
        """spawn_survivors(force=True) loescht survivor_groups und setzt alle group_id auf NULL."""
        _patch_grid(monkeypatch)

        # Vorab: survivor_groups mit zwei Zeilen befuellen
        conn.execute(
            "INSERT INTO survivor_groups (id, lat, lon) VALUES (1, 0.0, 0.0);"
        )
        conn.execute(
            "INSERT INTO survivor_groups (id, lat, lon) VALUES (2, 10.0, 10.0);"
        )
        conn.commit()

        groups_before = conn.execute("SELECT COUNT(*) FROM survivor_groups;").fetchone()[0]
        assert groups_before == 2, "Voraussetzung: 2 Gruppen vorhanden"

        # Regeneration erzwingen
        surv_mod.spawn_survivors(conn, total=50, seed=42, force=True)

        # survivor_groups muss leer sein
        groups_after = conn.execute("SELECT COUNT(*) FROM survivor_groups;").fetchone()[0]
        assert groups_after == 0, f"Erwartet 0 survivor_groups nach force-spawn, got {groups_after}"

        # Alle survivors muessen group_id = NULL haben
        non_null_group = conn.execute(
            "SELECT COUNT(*) FROM survivors WHERE group_id IS NOT NULL;"
        ).fetchone()[0]
        assert non_null_group == 0, (
            f"Erwartet alle survivors group_id=NULL, aber {non_null_group} haben einen Wert"
        )

        # Anzahl survivors stimmt
        total_survivors = conn.execute("SELECT COUNT(*) FROM survivors;").fetchone()[0]
        assert total_survivors == 50

    def test_noop_path_does_not_clear_survivor_groups(self, conn, monkeypatch):
        """Im No-op-Pfad (gleicher total, keine NULL-Spalten) bleiben survivor_groups unveraendert."""
        _patch_grid(monkeypatch)

        # Erst Survivors anlegen
        surv_mod.spawn_survivors(conn, total=50, seed=42)

        # Dann survivor_groups manuell befuellen
        conn.execute(
            "INSERT INTO survivor_groups (id, lat, lon) VALUES (1, 0.0, 0.0);"
        )
        conn.commit()

        # No-op-Aufruf (gleicher total, kein force)
        surv_mod.spawn_survivors(conn, total=50, seed=42)

        # survivor_groups muss unveraendert sein
        groups_after = conn.execute("SELECT COUNT(*) FROM survivor_groups;").fetchone()[0]
        assert groups_after == 1, (
            f"Im No-op-Pfad darf survivor_groups nicht geloescht werden, got {groups_after}"
        )
