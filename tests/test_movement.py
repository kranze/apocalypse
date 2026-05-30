"""Tests für app/sim/movement.py — advance_movement, carried_weight, set_destination."""
from __future__ import annotations

import json
import math

import pytest
from app.osm.roads import build_graph, _dist_m
from app.sim import constants, ledger
from app.sim.movement import advance_movement, carried_weight, set_destination


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _setup_char(conn, *, lat: float = 0.0, lon: float = 0.0, path: list | None = None):
    """Setzt Position und optionalen Pfad des Default-Charakters (id=1)."""
    path_json = json.dumps(path) if path is not None else None
    conn.execute(
        "UPDATE characters SET lat=?, lon=?, path_json=? WHERE id=1;",
        (lat, lon, path_json),
    )
    conn.commit()


def _char(conn) -> dict:
    row = conn.execute(
        "SELECT lat, lon, path_json, dest_lat, dest_lon FROM characters WHERE id=1;"
    ).fetchone()
    return dict(row)


def _add_item(conn, item_id: str, qty: float, group_id: int = 1):
    """Fügt Item ins Gruppen-Inventar ein und bucht Ledger."""
    conn.execute(
        "INSERT OR REPLACE INTO group_inventory "
        "(group_id, item_id, quantity, quality, acquired_tick) VALUES (?,?,?,1.0,0);",
        (group_id, item_id, qty),
    )
    ledger.add(conn, item_id, qty)
    conn.commit()


# ---------------------------------------------------------------------------
# TestCarriedWeight
# ---------------------------------------------------------------------------

class TestCarriedWeight:
    def test_empty_inventory_zero(self, conn):
        assert carried_weight(conn, 1) == 0.0

    def test_single_item_weight(self, conn):
        """canned_beans: weight_kg=0.40; 3 Stück = 1.2 kg."""
        _add_item(conn, "canned_beans", 3.0)
        w = carried_weight(conn, 1)
        assert abs(w - 1.2) < 1e-6

    def test_multiple_items(self, conn):
        """canned_beans (0.40×2=0.80) + water_1l (1.00×1=1.00) = 1.80 kg."""
        _add_item(conn, "canned_beans", 2.0)
        _add_item(conn, "water_1l", 1.0)
        w = carried_weight(conn, 1)
        assert abs(w - 1.80) < 1e-6

    def test_zero_quantity_zero_weight(self, conn):
        """Menge 0 → 0 kg."""
        conn.execute(
            "INSERT INTO group_inventory (group_id, item_id, quantity, quality, acquired_tick) "
            "VALUES (1, 'canned_beans', 0.0, 1.0, 0);"
        )
        conn.commit()
        w = carried_weight(conn, 1)
        assert w == 0.0


# ---------------------------------------------------------------------------
# TestAdvanceMovement — direkt via path_json
# ---------------------------------------------------------------------------

class TestAdvanceMovement:
    def test_no_path_no_movement(self, conn):
        """Charakter ohne path_json bewegt sich nicht."""
        _setup_char(conn, lat=0.0, lon=0.0)
        result = advance_movement(conn, 10, 10)
        assert 1 not in result
        c = _char(conn)
        assert c["lat"] == 0.0
        assert c["lon"] == 0.0

    def test_partial_segment_interpolated(self, conn):
        """Budget reicht nicht bis zum Ziel → Position wird interpoliert."""
        # Ziel 5000 m entfernt; in 10 Minuten schafft er nur WALK_SPEED*10 = 830 m
        dist_target = 5000.0  # m
        # 1 Grad lat ≈ 111320 m → 5000 m ≈ 0.04492 Grad
        delta_lat = dist_target / 111_320.0
        start_lat, start_lon = 0.0, 0.0
        path = [[start_lat + delta_lat, start_lon]]
        _setup_char(conn, lat=start_lat, lon=start_lon, path=path)

        minutes = 10
        result = advance_movement(conn, minutes, 10)
        traveled = result.get(1, 0.0)
        expected = constants.WALK_SPEED_M_PER_MIN * minutes
        assert abs(traveled - expected) < 1.0

        c = _char(conn)
        # path_json noch vorhanden, da nicht angekommen
        assert c["path_json"] is not None
        # Position hat sich verändert
        assert abs(c["lat"] - start_lat) > 1e-8

    def test_arrives_at_single_waypoint(self, conn):
        """Budget übersteigt Distanz zum einzigen Wegpunkt → Ankunft."""
        close_lat = 0.0001  # ~11 m entfernt
        start_lat, start_lon = 0.0, 0.0
        path = [[close_lat, start_lon]]
        _setup_char(conn, lat=start_lat, lon=start_lon, path=path)
        # Setze dest_lat/dest_lon auch
        conn.execute(
            "UPDATE characters SET dest_lat=?, dest_lon=? WHERE id=1;",
            (close_lat, start_lon),
        )
        conn.commit()

        result = advance_movement(conn, 60, 60)
        assert result.get(1, 0.0) > 0.0

        c = _char(conn)
        # Angekommen → path_json und dest werden gelöscht
        assert c["path_json"] is None
        assert c["dest_lat"] is None

    def test_arrival_emits_interrupt(self, conn):
        """Bei Ankunft wird ein Interrupt „erreicht" emittiert."""
        path = [[0.0001, 0.0]]
        _setup_char(conn, lat=0.0, lon=0.0, path=path)
        result = advance_movement(conn, 60, 60)
        interrupts = result.get("_interrupts", [])
        assert len(interrupts) >= 1
        assert any("erreicht" in i.get("message", "") or "Ziel" in i.get("message", "")
                   for i in interrupts)

    def test_multi_segment_path(self, conn):
        """Mehrere Wegpunkte werden nacheinander abgearbeitet, erste Punkte werden getrimmt."""
        # Drei Punkte, alle sehr nah (< 1 m voneinander), Budget riesig
        path = [
            [0.000001, 0.0],
            [0.000002, 0.0],
            [0.000003, 0.0],
        ]
        _setup_char(conn, lat=0.0, lon=0.0, path=path)
        result = advance_movement(conn, 60, 60)
        # Charakter sollte alle Punkte abgearbeitet haben (angekommen)
        c = _char(conn)
        assert c["path_json"] is None

    def test_multiple_ticks_sum_distance(self, conn):
        """Zwei Ticks zu je 10 Minuten = genau 2 × speed × 10 gelaufen."""
        dist_target = 10_000.0  # m — weit genug, um nicht anzukommen
        delta_lat = dist_target / 111_320.0
        path = [[delta_lat, 0.0]]
        _setup_char(conn, lat=0.0, lon=0.0, path=path)

        minutes = 10
        r1 = advance_movement(conn, minutes, 10)
        r2 = advance_movement(conn, minutes, 20)
        d1 = r1.get(1, 0.0)
        d2 = r2.get(1, 0.0)
        expected_per_tick = constants.WALK_SPEED_M_PER_MIN * minutes
        assert abs(d1 - expected_per_tick) < 1.0
        assert abs(d2 - expected_per_tick) < 1.0

    def test_no_movement_when_dead(self, conn):
        """Tote Charaktere werden übersprungen."""
        path = [[0.001, 0.0]]
        _setup_char(conn, lat=0.0, lon=0.0, path=path)
        conn.execute("UPDATE characters SET is_alive=0, path_json=? WHERE id=1;",
                     (json.dumps(path),))
        conn.commit()
        result = advance_movement(conn, 60, 60)
        assert 1 not in result

    def test_position_moves_toward_target(self, conn):
        """Position nach Tick liegt näher am Ziel als vorher."""
        target_lat, target_lon = 0.05, 0.0  # ~5.5 km
        path = [[target_lat, target_lon]]
        start_lat, start_lon = 0.0, 0.0
        _setup_char(conn, lat=start_lat, lon=start_lon, path=path)

        advance_movement(conn, 10, 10)
        c = _char(conn)
        dist_before = _dist_m((start_lat, start_lon), (target_lat, target_lon))
        dist_after = _dist_m((c["lat"], c["lon"]), (target_lat, target_lon))
        assert dist_after < dist_before


# ---------------------------------------------------------------------------
# TestSetDestination — mit gemocktem Graph
# ---------------------------------------------------------------------------

class TestSetDestination:
    def _make_mock_graph(self):
        """Einfacher linearer Graph für Tests."""
        from app.osm.roads import build_graph
        return build_graph({
            "elements": [{
                "type": "way",
                "id": 1,
                "tags": {"highway": "residential"},
                "nodes": [1, 2, 3],
                "geometry": [
                    {"lat": 0.0, "lon": 0.0},
                    {"lat": 0.001, "lon": 0.0},
                    {"lat": 0.002, "lon": 0.0},
                ],
            }]
        })

    def test_set_destination_ok(self, conn, monkeypatch):
        """set_destination liefert ok=True und speichert path_json."""
        import app.osm.roads as roads_module
        mock_graph = self._make_mock_graph()
        monkeypatch.setattr(roads_module, "get_graph", lambda **kw: mock_graph)

        conn.execute("UPDATE characters SET lat=0.0, lon=0.0 WHERE id=1;")
        conn.commit()
        result = set_destination(conn, 1, 0.002, 0.0)
        assert result["ok"] is True
        assert len(result["path"]) >= 1

        c = _char(conn)
        assert c["path_json"] is not None
        assert c["dest_lat"] == pytest.approx(0.002)
        assert c["dest_lon"] == pytest.approx(0.0)

    def test_set_destination_no_position(self, conn, monkeypatch):
        """Charakter ohne Position → Fehler."""
        import app.osm.roads as roads_module
        mock_graph = self._make_mock_graph()
        monkeypatch.setattr(roads_module, "get_graph", lambda **kw: mock_graph)

        conn.execute("UPDATE characters SET lat=NULL, lon=NULL WHERE id=1;")
        conn.commit()
        result = set_destination(conn, 1, 0.001, 0.0)
        assert result["ok"] is False
        assert result["reason"] == "no_position"

    def test_set_destination_dead_char(self, conn, monkeypatch):
        """Toter Charakter → Fehler."""
        import app.osm.roads as roads_module
        mock_graph = self._make_mock_graph()
        monkeypatch.setattr(roads_module, "get_graph", lambda **kw: mock_graph)

        conn.execute("UPDATE characters SET is_alive=0, lat=0.0, lon=0.0 WHERE id=1;")
        conn.commit()
        result = set_destination(conn, 1, 0.001, 0.0)
        assert result["ok"] is False
        assert result["reason"] == "no_such_living_character"

    def test_set_destination_distance_plausible(self, conn, monkeypatch):
        """Gemeldete Distanz ist plausibel (> 0, < direkter Distanz * 2)."""
        import app.osm.roads as roads_module
        mock_graph = self._make_mock_graph()
        monkeypatch.setattr(roads_module, "get_graph", lambda **kw: mock_graph)

        conn.execute("UPDATE characters SET lat=0.0, lon=0.0 WHERE id=1;")
        conn.commit()
        result = set_destination(conn, 1, 0.002, 0.0)
        direct = _dist_m((0.0, 0.0), (0.002, 0.0))
        assert 0 < result["distance_m"] <= direct * 3
