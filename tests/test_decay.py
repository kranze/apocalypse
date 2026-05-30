"""Tests für app/sim/resources.py — quality_at und apply_decay."""
from __future__ import annotations

import pytest
from app.sim import constants
from app.sim.resources import apply_decay, quality_at


class TestQualityAt:
    def test_no_decay_when_halflife_none(self):
        assert quality_at(0, 999999, None) == 1.0

    def test_halflife_at_exactly_one_halflife(self):
        q = quality_at(0, 100, 100)
        assert abs(q - 0.5) < 1e-9

    def test_halflife_two_halflives(self):
        q = quality_at(0, 200, 100)
        assert abs(q - 0.25) < 1e-9

    def test_quality_at_anchor_is_one(self):
        assert quality_at(50, 50, 1000) == 1.0

    def test_negative_elapsed_clamped_to_one(self):
        """now_tick < anchor_tick → elapsed=0 → Qualität 1.0."""
        assert quality_at(100, 50, 1000) == 1.0

    def test_large_elapsed_approaches_zero(self):
        q = quality_at(0, 10_000_000, 1000)
        assert q < 1e-9


class TestApplyDecay:
    def _setup_location(self, conn, item_id: str, quantity: float, produced_tick: int, quality: float = 1.0):
        """Entdeckt location_id=100 und legt manuell ein Inventar-Item rein."""
        conn.execute(
            "INSERT INTO locations (id, osm_id, type, name, lat, lon, footprint_m2, "
            "discovery_status, generation_seed) VALUES (100, 'test_100', 'house', "
            "'TestHaus', 49.0, 11.0, 100.0, 'discovered', 1337);"
        )
        conn.execute(
            "INSERT INTO location_inventory (location_id, item_id, quantity, quality, "
            "produced_tick) VALUES (100, ?, ?, ?, ?);",
            (item_id, quantity, quality, produced_tick),
        )
        conn.commit()

    def test_non_perishable_items_untouched(self, conn):
        """canned_beans hat halflife=NULL → bleibt unverändert nach sehr vielen Ticks."""
        self._setup_location(conn, "canned_beans", 5.0, 0)
        with conn:
            apply_decay(conn, 9_999_999)
        rows = conn.execute(
            "SELECT quantity, quality FROM location_inventory WHERE location_id=100;"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["quantity"] == 5.0
        assert rows[0]["quality"] == 1.0

    def test_water_1l_non_perishable(self, conn):
        """water_1l hat halflife=NULL → ebenfalls unvergänglich."""
        self._setup_location(conn, "water_1l", 3.0, 0)
        with conn:
            apply_decay(conn, 9_999_999)
        rows = conn.execute(
            "SELECT quantity FROM location_inventory WHERE location_id=100;"
        ).fetchall()
        assert len(rows) == 1

    def test_crowbar_non_perishable(self, conn):
        """crowbar → halvlife=NULL."""
        self._setup_location(conn, "crowbar", 1.0, 0)
        with conn:
            apply_decay(conn, 9_999_999)
        rows = conn.execute(
            "SELECT quantity FROM location_inventory WHERE location_id=100;"
        ).fetchall()
        assert len(rows) == 1

    def test_perishable_bread_spoils_after_many_halflives(self, conn):
        """bread_loaf halflife=4320 min. Nach sehr vielen Ticks verdorben → gelöscht."""
        # Qualität so niedrig setzen, dass es beim nächsten decay spoilt
        # bread_loaf halflife=4320; SPOIL_THRESHOLD=0.1 → nach ~14*4320 min
        # Einfacher: direkt mit sehr alter produced_tick einsetzen
        self._setup_location(conn, "bread_loaf", 2.0, 0, quality=1.0)
        # Weit in der Zukunft ticken: 100 Halflives → 0.5^100 ≈ 0
        with conn:
            apply_decay(conn, 4320 * 100)
        rows = conn.execute(
            "SELECT id FROM location_inventory WHERE location_id=100;"
        ).fetchall()
        assert len(rows) == 0, "bread_loaf sollte nach 100 Halflives verschwunden sein"

    def test_perishable_milk_quality_updated(self, conn):
        """milk_1l halflife=2880. Nach 1 Halbwertszeit sollte quality ≈ 0.5."""
        self._setup_location(conn, "milk_1l", 4.0, 0, quality=1.0)
        hl = 2880
        with conn:
            apply_decay(conn, hl)
        rows = conn.execute(
            "SELECT quality FROM location_inventory WHERE location_id=100;"
        ).fetchall()
        # Falls nicht gespoilt, überprüfen wir die Qualität
        if rows:
            q = rows[0]["quality"]
            assert abs(q - 0.5) < 0.01
        else:
            # Könnte bereits gespoilt sein bei Threshold von 0.1 — aber 0.5 > 0.1
            pytest.fail("milk_1l sollte nach 1 Halbwertszeit noch vorhanden sein (quality≈0.5)")

    def test_spoiled_item_ledger_reduced(self, conn):
        """Verderbliches Item verdirbt → Ledger-Buchung ist negativ."""
        from app.sim import ledger
        # bread_loaf manuell ins Ledger buchen und Location erstellen
        self._setup_location(conn, "bread_loaf", 3.0, 0, quality=1.0)
        with conn:
            ledger.add(conn, "bread_loaf", 3.0)
        before = ledger.expected_totals(conn).get("bread_loaf", 0.0)
        with conn:
            apply_decay(conn, 4320 * 100)
        after = ledger.expected_totals(conn).get("bread_loaf", 0.0)
        assert after < before, "Ledger muss nach Verderb geringer sein"

    def test_decay_returns_interrupts_for_spoiled(self, conn):
        """apply_decay gibt Interrupts zurück für verdorbene Items."""
        self._setup_location(conn, "bread_loaf", 2.0, 0, quality=1.0)
        with conn:
            interrupts = apply_decay(conn, 4320 * 100)
        assert any("bread_loaf" in i.get("message", "") or
                   i.get("category") == "world" for i in interrupts)


class TestGroupInventoryDecayMerge:
    """Riskanter Pfad: apply_decay löscht verderbliche group_inventory-Zeilen und
    fügt sie nach Qualität verschmolzen wieder ein. Fallen zwei Stapel auf
    dieselbe Qualität, MUSS gemerged werden — sonst verletzt das Re-Insert
    UNIQUE(group_id, item_id, quality)."""

    def _add_group_row(self, conn, item_id, qty, quality, acquired_tick):
        conn.execute(
            "INSERT INTO group_inventory (group_id, item_id, quantity, quality, "
            "acquired_tick) VALUES (1, ?, ?, ?, ?);",
            (item_id, qty, quality, acquired_tick),
        )

    def test_two_stacks_collapsing_to_same_quality_merge(self, conn):
        """Zwei milk_1l-Stapel mit gleichem Anker (acquired_tick=0), aber
        unterschiedlicher Start-Qualität (1.0 vs 0.8 -> zwei erlaubte Zeilen).
        apply_decay rechnet beide aus dem Anker neu -> identische neue Qualität
        -> Merge zu EINER Zeile mit summierter Menge, ohne Constraint-Fehler."""
        with conn:
            self._add_group_row(conn, "milk_1l", 2.0, 1.0, 0)
            self._add_group_row(conn, "milk_1l", 3.0, 0.8, 0)
        # 1 Halbwertszeit (milk_1l = 2880) -> beide auf ~0.5
        with conn:
            apply_decay(conn, 2880)
        rows = conn.execute(
            "SELECT quantity, quality FROM group_inventory WHERE item_id='milk_1l';"
        ).fetchall()
        assert len(rows) == 1, "Stapel gleicher Qualität müssen verschmelzen"
        assert abs(rows[0]["quantity"] - 5.0) < 1e-9, "Menge muss summiert werden"
        assert abs(rows[0]["quality"] - 0.5) < 0.01

    def test_merge_keeps_earliest_acquired_tick(self, conn):
        """Beim Verschmelzen bleibt der früheste acquired_tick als Decay-Anker."""
        with conn:
            self._add_group_row(conn, "milk_1l", 2.0, 1.0, 0)
            self._add_group_row(conn, "milk_1l", 3.0, 0.8, 0)
        with conn:
            apply_decay(conn, 2880)
        row = conn.execute(
            "SELECT acquired_tick FROM group_inventory WHERE item_id='milk_1l';"
        ).fetchone()
        assert row["acquired_tick"] == 0

    def test_distinct_qualities_stay_separate(self, conn):
        """Stapel, die NICHT auf dieselbe Qualität fallen (verschiedene Anker),
        bleiben getrennt."""
        with conn:
            # Verschiedene Start-Qualität (legal) UND verschiedene Anker:
            # nach Decay -> verschiedene neue Qualität -> bleiben getrennt.
            self._add_group_row(conn, "milk_1l", 2.0, 1.0, 0)
            self._add_group_row(conn, "milk_1l", 3.0, 0.9, 1440)  # halbe HWZ jünger
        with conn:
            apply_decay(conn, 2880)
        rows = conn.execute(
            "SELECT quality FROM group_inventory WHERE item_id='milk_1l' "
            "ORDER BY quality;"
        ).fetchall()
        assert len(rows) == 2, "unterschiedlich alte Stapel bleiben getrennt"
