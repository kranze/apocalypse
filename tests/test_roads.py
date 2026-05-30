"""Tests für app/osm/roads.py — build_graph, nearest_node, shortest_path, _dist_m."""
from __future__ import annotations

import math

import pytest
from app.osm.roads import RoadGraph, build_graph, _dist_m


# ---------------------------------------------------------------------------
# Synthetische Overpass-Antworten
# ---------------------------------------------------------------------------

def _make_overpass(*ways) -> dict:
    """Baut eine minimale Overpass-Antwort aus einer Liste von Way-Dicts."""
    return {"elements": list(ways)}


def _way(way_id: int, node_ids: list[int], coords: list[tuple[float, float]]) -> dict:
    """Erzeugt ein Way-Element mit nodes + geometry."""
    return {
        "type": "way",
        "id": way_id,
        "tags": {"highway": "residential"},
        "nodes": node_ids,
        "geometry": [{"lat": lat, "lon": lon} for lat, lon in coords],
    }


# Einfache lineare Straße: A–B–C
#   Node 1 (0.0, 0.0) – Node 2 (0.001, 0.0) – Node 3 (0.002, 0.0)
SIMPLE_OVERPASS = _make_overpass(
    _way(1, [1, 2, 3], [(0.0, 0.0), (0.001, 0.0), (0.002, 0.0)])
)

# Y-förmige Straße: A–B, B–C, B–D (geteilter Knoten B)
BRANCH_OVERPASS = _make_overpass(
    _way(10, [10, 11], [(0.0, 0.0), (0.001, 0.001)]),
    _way(11, [11, 12], [(0.001, 0.001), (0.002, 0.001)]),
    _way(12, [11, 13], [(0.001, 0.001), (0.001, 0.003)]),
)

# Zwei getrennte, nicht verbundene Wege
DISCONNECTED_OVERPASS = _make_overpass(
    _way(20, [20, 21], [(0.0, 0.0), (0.001, 0.0)]),
    _way(21, [22, 23], [(1.0, 1.0), (1.001, 1.0)]),
)


# ---------------------------------------------------------------------------
# TestBuildGraph
# ---------------------------------------------------------------------------

class TestBuildGraph:
    def test_returns_road_graph_instance(self):
        g = build_graph(SIMPLE_OVERPASS)
        assert isinstance(g, RoadGraph)

    def test_node_count_linear_way(self):
        g = build_graph(SIMPLE_OVERPASS)
        assert g.node_count == 3

    def test_coords_stored_correctly(self):
        g = build_graph(SIMPLE_OVERPASS)
        assert g.coords[1] == (0.0, 0.0)
        assert g.coords[2] == (0.001, 0.0)
        assert g.coords[3] == (0.002, 0.0)

    def test_shared_node_connects_two_ways(self):
        """Node 11 wird von beiden Ways geteilt → im Graph nur einmal vorhanden."""
        g = build_graph(BRANCH_OVERPASS)
        # Node 11 muss Nachbarn aus beiden Ways haben
        assert 11 in g.adj
        neighbors = set(g.adj[11].keys())
        assert 10 in neighbors
        assert 12 in neighbors
        assert 13 in neighbors

    def test_disconnected_ways_have_no_shared_edges(self):
        g = build_graph(DISCONNECTED_OVERPASS)
        assert g.node_count == 4
        # Way 1 und Way 2 sind nicht verbunden
        assert 22 not in g.adj.get(20, {})
        assert 23 not in g.adj.get(21, {})

    def test_edges_are_symmetric(self):
        """Ungerichteter Graph: a→b impliziert b→a mit gleicher Distanz."""
        g = build_graph(SIMPLE_OVERPASS)
        assert 2 in g.adj[1]
        assert 1 in g.adj[2]
        assert abs(g.adj[1][2] - g.adj[2][1]) < 1e-9

    def test_exclude_motorway(self):
        """Motorways werden ignoriert."""
        data = _make_overpass({
            "type": "way",
            "id": 99,
            "tags": {"highway": "motorway"},
            "nodes": [99, 100],
            "geometry": [{"lat": 0.0, "lon": 0.0}, {"lat": 0.001, "lon": 0.0}],
        })
        g = build_graph(data)
        assert g.node_count == 0

    def test_way_with_mismatched_nodes_geometry_skipped(self):
        """Way mit nodes != geometry Länge wird übersprungen."""
        data = _make_overpass({
            "type": "way",
            "id": 1,
            "tags": {"highway": "residential"},
            "nodes": [1, 2, 3],
            "geometry": [{"lat": 0.0, "lon": 0.0}],  # Länge passt nicht
        })
        g = build_graph(data)
        assert g.node_count == 0

    def test_single_node_way_skipped(self):
        """Way mit nur einem Knoten (< 2) wird übersprungen."""
        data = _make_overpass({
            "type": "way",
            "id": 1,
            "tags": {"highway": "residential"},
            "nodes": [1],
            "geometry": [{"lat": 0.0, "lon": 0.0}],
        })
        g = build_graph(data)
        assert g.node_count == 0

    def test_empty_overpass_response(self):
        g = build_graph({"elements": []})
        assert g.node_count == 0

    def test_non_way_elements_skipped(self):
        data = _make_overpass(
            {"type": "node", "id": 1, "lat": 0.0, "lon": 0.0},
        )
        g = build_graph(data)
        assert g.node_count == 0


# ---------------------------------------------------------------------------
# TestNearestNode
# ---------------------------------------------------------------------------

class TestNearestNode:
    def test_returns_none_on_empty_graph(self):
        g = build_graph({"elements": []})
        assert g.nearest_node(0.0, 0.0) is None

    def test_nearest_to_exact_position(self):
        g = build_graph(SIMPLE_OVERPASS)
        assert g.nearest_node(0.0, 0.0) == 1
        assert g.nearest_node(0.001, 0.0) == 2
        assert g.nearest_node(0.002, 0.0) == 3

    def test_nearest_to_midpoint(self):
        """Midpoint zwischen Node 1 und 2 → einer der beiden."""
        g = build_graph(SIMPLE_OVERPASS)
        result = g.nearest_node(0.0005, 0.0)
        assert result in {1, 2}

    def test_nearest_close_to_one_of_three(self):
        g = build_graph(SIMPLE_OVERPASS)
        # Sehr nah an Node 3
        result = g.nearest_node(0.00199, 0.0)
        assert result == 3

    def test_nearest_in_branch_graph(self):
        g = build_graph(BRANCH_OVERPASS)
        # Exakt auf Node 12 (0.002, 0.001)
        result = g.nearest_node(0.002, 0.001)
        assert result == 12


# ---------------------------------------------------------------------------
# TestShortestPath
# ---------------------------------------------------------------------------

class TestShortestPath:
    def test_start_equals_goal(self):
        """start == goal → ein Wegpunkt, Distanz 0."""
        g = build_graph(SIMPLE_OVERPASS)
        waypoints, dist = g.shortest_path(1, 1)
        assert dist == 0.0
        assert len(waypoints) == 1
        assert waypoints[0] == g.coords[1]

    def test_adjacent_nodes(self):
        """Direkt verbundene Knoten: Weg mit zwei Punkten, Distanz > 0."""
        g = build_graph(SIMPLE_OVERPASS)
        waypoints, dist = g.shortest_path(1, 2)
        assert dist > 0.0
        assert waypoints[0] == g.coords[1]
        assert waypoints[-1] == g.coords[2]

    def test_path_through_intermediate(self):
        """Pfad 1→3 läuft über Knoten 2."""
        g = build_graph(SIMPLE_OVERPASS)
        waypoints, dist = g.shortest_path(1, 3)
        assert len(waypoints) == 3
        assert waypoints[0] == g.coords[1]
        assert waypoints[1] == g.coords[2]
        assert waypoints[2] == g.coords[3]

    def test_distance_plausible_vs_dist_m(self):
        """Distanz 1→2 stimmt mit _dist_m überein."""
        g = build_graph(SIMPLE_OVERPASS)
        _, dist = g.shortest_path(1, 2)
        expected = _dist_m(g.coords[1], g.coords[2])
        assert abs(dist - expected) < 1e-6

    def test_distance_additive_through_chain(self):
        """Distanz 1→3 = dist(1→2) + dist(2→3)."""
        g = build_graph(SIMPLE_OVERPASS)
        _, d13 = g.shortest_path(1, 3)
        d12 = _dist_m(g.coords[1], g.coords[2])
        d23 = _dist_m(g.coords[2], g.coords[3])
        assert abs(d13 - (d12 + d23)) < 1e-6

    def test_no_path_disconnected_graph(self):
        """Kein Weg zwischen getrennten Teilgraphen → ([], inf)."""
        g = build_graph(DISCONNECTED_OVERPASS)
        waypoints, dist = g.shortest_path(20, 22)
        assert waypoints == []
        assert dist == float("inf")

    def test_branch_graph_shortest_path(self):
        """In Y-Topologie: Pfad von 10 nach 12 läuft durch Knoten 11."""
        g = build_graph(BRANCH_OVERPASS)
        waypoints, dist = g.shortest_path(10, 12)
        assert dist < float("inf")
        # Wegpunkte: 10 → 11 → 12
        assert len(waypoints) == 3
        ids_in_path = [nid for nid, coord in g.coords.items() if coord in waypoints]
        assert 11 in ids_in_path

    def test_branch_graph_detour_vs_shortcut(self):
        """Direkter Weg soll kürzer sein als Umweg, falls kein Umweg existiert."""
        g = build_graph(BRANCH_OVERPASS)
        # 10 → 13 geht nur über 11 → 13 (kein Abkürzung)
        wp, dist = g.shortest_path(10, 13)
        assert dist < float("inf")
        assert len(wp) >= 3  # mindestens 10, 11, 13

    def test_reverse_path_same_distance(self):
        """Ungerichteter Graph: A→C und C→A haben gleiche Distanz."""
        g = build_graph(SIMPLE_OVERPASS)
        _, d_fwd = g.shortest_path(1, 3)
        _, d_rev = g.shortest_path(3, 1)
        assert abs(d_fwd - d_rev) < 1e-6


# ---------------------------------------------------------------------------
# TestDistM
# ---------------------------------------------------------------------------

class TestDistM:
    def test_same_point_zero(self):
        assert _dist_m((10.0, 10.0), (10.0, 10.0)) == 0.0

    def test_distance_positive(self):
        assert _dist_m((0.0, 0.0), (0.001, 0.0)) > 0.0

    def test_approx_111m_per_degree_lat(self):
        """1 Grad Latitude ≈ 111 320 m."""
        d = _dist_m((0.0, 0.0), (1.0, 0.0))
        assert abs(d - 111_320.0) < 200  # 0.2 km Toleranz

    def test_symmetric(self):
        a, b = (48.0, 11.0), (48.001, 11.001)
        assert abs(_dist_m(a, b) - _dist_m(b, a)) < 1e-9
