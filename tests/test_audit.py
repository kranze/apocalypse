"""Tests für app/sim/audit.py und app/sim/ledger.py."""
from __future__ import annotations

import pytest
from app.sim import ledger
from app.sim.audit import run_audit
from app.sim.generation import discover
from app.sim.looting import loot
from app.sim.resources import apply_decay, eat


def _make_location(conn, loc_id=400, gen_seed=9999, loc_type="supermarket"):
    conn.execute(
        "INSERT INTO locations (id, osm_id, type, name, lat, lon, footprint_m2, "
        "discovery_status, generation_seed) VALUES (?, ?, ?, 'AuditLoc', 49.0, 11.0, "
        "200.0, 'undiscovered', ?);",
        (loc_id, f"osm_{loc_id}", loc_type, gen_seed),
    )
    conn.commit()


class TestLedger:
    def test_add_positive(self, conn):
        ledger.add(conn, "canned_beans", 10.0)
        conn.commit()
        totals = ledger.expected_totals(conn)
        assert abs(totals.get("canned_beans", 0.0) - 10.0) < 1e-9

    def test_add_negative(self, conn):
        ledger.add(conn, "canned_beans", 10.0)
        ledger.add(conn, "canned_beans", -3.0)
        conn.commit()
        totals = ledger.expected_totals(conn)
        assert abs(totals.get("canned_beans", 0.0) - 7.0) < 1e-9

    def test_add_upsert(self, conn):
        ledger.add(conn, "water_1l", 5.0)
        ledger.add(conn, "water_1l", 5.0)
        conn.commit()
        totals = ledger.expected_totals(conn)
        assert abs(totals.get("water_1l", 0.0) - 10.0) < 1e-9

    def test_expected_totals_empty(self, conn):
        totals = ledger.expected_totals(conn)
        assert isinstance(totals, dict)
        # Kann leer oder mit 0-Einträgen sein, aber kein Crash


class TestAudit:
    def test_no_drift_after_discover(self, conn):
        """Nach discover: run_audit meldet keinen Flag."""
        _make_location(conn)
        discover(conn, 400)
        with conn:
            flags = run_audit(conn, 1)
        flagged = conn.execute(
            "SELECT SUM(flagged) AS f FROM resource_audit;"
        ).fetchone()["f"] or 0
        assert flagged == 0, "Kein Drift nach korrekter Lazy Generation erwartet"

    def test_no_drift_after_loot(self, conn):
        """Nach loot (bilanzneutral): run_audit meldet keinen Flag."""
        _make_location(conn)
        discover(conn, 400)
        loot(conn, 400)
        with conn:
            run_audit(conn, 2)
        flagged = conn.execute(
            "SELECT SUM(flagged) AS f FROM resource_audit;"
        ).fetchone()["f"] or 0
        assert flagged == 0

    def test_direct_insert_without_ledger_triggers_flag(self, conn):
        """Direkte SQL-Einfügung ohne Ledger-Buchung → Audit flaggt Drift nach oben."""
        # Erst entdecken (damit Ledger stimmt)
        _make_location(conn)
        discover(conn, 400)
        # Jetzt direkt ohne Sim-Funktion einfügen (am Sim-Kern vorbei)
        conn.execute(
            "INSERT INTO location_inventory (location_id, item_id, quantity, quality, "
            "produced_tick) VALUES (400, 'canned_beans', 999.0, 1.0, 0);"
        )
        conn.commit()
        with conn:
            run_audit(conn, 3)
        flagged = conn.execute(
            "SELECT SUM(flagged) AS f FROM resource_audit;"
        ).fetchone()["f"] or 0
        assert flagged >= 1, "Direktes Einfügen ohne Ledger muss Audit-Flag erzeugen"

    def test_direct_delete_without_ledger_triggers_flag(self, conn):
        """Direktes Löschen ohne Ledger-Buchung → Audit flaggt Drift nach unten."""
        _make_location(conn)
        discover(conn, 400)
        inv = conn.execute(
            "SELECT id FROM location_inventory WHERE location_id=400 LIMIT 1;"
        ).fetchone()
        if inv is None:
            pytest.skip("Kein Inventar für diesen Test")
        conn.execute("DELETE FROM location_inventory WHERE id=?;", (inv["id"],))
        conn.commit()
        with conn:
            run_audit(conn, 4)
        flagged = conn.execute(
            "SELECT SUM(flagged) AS f FROM resource_audit;"
        ).fetchone()["f"] or 0
        assert flagged >= 1, "Direktes Löschen ohne Ledger muss Audit-Flag erzeugen"

    def test_ledger_matches_location_inventory_after_generation(self, conn):
        """Ledger-Soll == Ist-Bestand nach discover (kein Drift)."""
        _make_location(conn)
        discover(conn, 400)
        expected = ledger.expected_totals(conn)
        actual: dict[str, float] = {}
        for row in conn.execute(
            "SELECT item_id, SUM(quantity) AS q FROM location_inventory GROUP BY item_id;"
        ).fetchall():
            actual[row["item_id"]] = row["q"]
        for item_id, qty in expected.items():
            actual_qty = actual.get(item_id, 0.0)
            assert abs(actual_qty - qty) < 1e-6, (
                f"{item_id}: Ledger={qty}, Ist={actual_qty}"
            )

    def test_no_drift_after_decay(self, conn):
        """Nach apply_decay: Ledger wird korrekt gebucht → kein Audit-Flag."""
        # Bread_loaf mit hoher Menge direkt via discover einsetzen
        _make_location(conn, loc_type="house", gen_seed=9999)
        discover(conn, 400)
        # Weit in der Zukunft → alles verderbliche verdirbt
        with conn:
            apply_decay(conn, 4320 * 100)
        with conn:
            run_audit(conn, 5)
        flagged = conn.execute(
            "SELECT SUM(flagged) AS f FROM resource_audit;"
        ).fetchone()["f"] or 0
        assert flagged == 0
