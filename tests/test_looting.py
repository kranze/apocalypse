"""Tests für app/sim/looting.py — Plündern."""
from __future__ import annotations

import pytest
from app.sim.looting import loot
from app.sim.generation import discover, current_inventory


def _make_location(conn, loc_id=300, gen_seed=9999, loc_type="supermarket"):
    conn.execute(
        "INSERT INTO locations (id, osm_id, type, name, lat, lon, footprint_m2, "
        "discovery_status, generation_seed) VALUES (?, ?, ?, 'LootLoc', 49.0, 11.0, "
        "200.0, 'undiscovered', ?);",
        (loc_id, f"osm_{loc_id}", loc_type, gen_seed),
    )
    conn.commit()


def _total_quantity(conn, item_id: str | None = None) -> float:
    """Summe aller Bestände (location + group) optionally gefiltert nach item_id."""
    if item_id:
        loc = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM location_inventory WHERE item_id=?;",
            (item_id,),
        ).fetchone()["q"]
        grp = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory WHERE item_id=?;",
            (item_id,),
        ).fetchone()["q"]
    else:
        loc = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM location_inventory;"
        ).fetchone()["q"]
        grp = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory;"
        ).fetchone()["q"]
    return loc + grp


class TestLoot:
    def test_loot_nonexistent_location(self, conn):
        result = loot(conn, 9999)
        assert result["ok"] is False

    def test_auto_discover_undiscovered_location(self, conn):
        _make_location(conn)
        # Location ist 'undiscovered' → loot soll Auto-Discover durchführen
        result = loot(conn, 300)
        assert result["ok"] is True
        status = conn.execute(
            "SELECT discovery_status FROM locations WHERE id=300;"
        ).fetchone()["discovery_status"]
        assert status in ("discovered", "depleted")

    def test_loot_all_is_balance_neutral(self, conn):
        """Σ(location + group) bleibt vor/nach loot gleich."""
        _make_location(conn)
        discover(conn, 300)
        total_before = _total_quantity(conn)
        loot(conn, 300)
        total_after = _total_quantity(conn)
        assert abs(total_after - total_before) < 1e-6, (
            f"Bilanzneutralität verletzt: vorher={total_before}, nachher={total_after}"
        )

    def test_loot_all_marks_depleted(self, conn):
        """Vollständig geplünderte Location → status='depleted'."""
        _make_location(conn)
        result = loot(conn, 300)
        assert result["status"] == "depleted"

    def test_loot_partial_stays_discovered(self, conn):
        """Nur ein Item nehmen → Location bleibt 'discovered'."""
        _make_location(conn)
        discover(conn, 300)
        inv = current_inventory(conn, 300)
        if not inv:
            pytest.skip("Seed produziert kein Inventar für diesen Test")
        first_item = inv[0]["item_id"]
        result = loot(conn, 300, items={first_item: 1.0})
        # Status hängt davon ab, ob noch Items übrig sind
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM location_inventory WHERE location_id=300;"
        ).fetchone()["n"]
        expected_status = "depleted" if remaining == 0 else "discovered"
        assert result["status"] == expected_status

    def test_targeted_loot_transfers_correct_amount(self, conn):
        """Gezieltes Plündern transferiert genau die gewünschte Menge."""
        _make_location(conn)
        discover(conn, 300)
        inv = current_inventory(conn, 300)
        if not inv:
            pytest.skip("Kein Inventar für diesen Test")
        item = inv[0]["item_id"]
        avail = inv[0]["quantity"]
        take = min(1.0, avail)
        grp_before = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory WHERE item_id=?;",
            (item,),
        ).fetchone()["q"]
        result = loot(conn, 300, items={item: take})
        grp_after = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory WHERE item_id=?;",
            (item,),
        ).fetchone()["q"]
        assert abs(grp_after - grp_before - take) < 1e-6

    def test_reloot_empty_location_transfers_nothing(self, conn):
        """Re-Loot einer leeren (depleted) Location transferiert nichts."""
        _make_location(conn)
        loot(conn, 300)  # alles nehmen
        grp_before = _total_quantity(conn)
        result = loot(conn, 300)
        grp_after = _total_quantity(conn)
        assert result["ok"] is True
        assert abs(grp_after - grp_before) < 1e-6

    def test_loot_balance_per_item(self, conn):
        """Bilanzneutralität für jedes einzelne Item überprüfen."""
        _make_location(conn)
        discover(conn, 300)
        inv = current_inventory(conn, 300)
        per_item_before = {i["item_id"]: _total_quantity(conn, i["item_id"]) for i in inv}
        loot(conn, 300)
        for item_id, before in per_item_before.items():
            after = _total_quantity(conn, item_id)
            assert abs(after - before) < 1e-6, (
                f"Item {item_id}: Bilanz verletzt ({before} → {after})"
            )

    def test_loot_items_land_in_group_inventory(self, conn):
        """Nach loot ist das Gruppen-Inventar nicht mehr leer."""
        _make_location(conn)
        result = loot(conn, 300)
        if not result["transferred"]:
            pytest.skip("Seed produziert kein Inventar")
        grp_items = conn.execute(
            "SELECT COUNT(*) AS n FROM group_inventory WHERE group_id=1;"
        ).fetchone()["n"]
        assert grp_items > 0
