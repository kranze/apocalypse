"""Test: fetch_bbox_combined + load_bbox_combined laden Gebaeude UND Strassen
in EINEM Overpass-Request (Issue #42).

Kein echtes Netz: overpass.fetch_bbox_combined wird per monkeypatch durch
Mockdaten ersetzt, die building-Ways und highway-Ways gemischt enthalten.
Eisernes Prinzip: parser.parse nimmt nur Gebaeude; roads.merge_ways nur Strassen.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.osm import loader, overpass, roads as roads_mod
from app.osm.roads import RoadGraph
from tests.conftest import make_conn, set_seed


# ---------------------------------------------------------------------------
# Synthetische Overpass-Antwort: 1 Gebaeude + 1 Strasse
# ---------------------------------------------------------------------------

_COMBINED_DATA = {
    "elements": [
        # Gebaeude-Way (building=yes)
        {
            "type": "way",
            "id": 1001,
            "tags": {"building": "yes"},
            "nodes": [10, 11, 12, 10],
            "geometry": [
                {"lat": 49.500, "lon": 11.000},
                {"lat": 49.501, "lon": 11.000},
                {"lat": 49.501, "lon": 11.001},
                {"lat": 49.500, "lon": 11.000},
            ],
        },
        # Highway-Way (residential)
        {
            "type": "way",
            "id": 2001,
            "tags": {"highway": "residential"},
            "nodes": [20, 21, 22],
            "geometry": [
                {"lat": 49.502, "lon": 11.002},
                {"lat": 49.503, "lon": 11.002},
                {"lat": 49.504, "lon": 11.002},
            ],
        },
    ]
}


# ---------------------------------------------------------------------------
# parser.parse: ignoriert highway-Ways in kombinierten Daten
# ---------------------------------------------------------------------------

def test_parser_ignores_highway_ways():
    """parser.parse liefert nur Gebaeude, keine highway-Ways."""
    from app.osm import parser
    records = parser.parse(_COMBINED_DATA)
    osm_ids = [r["osm_id"] for r in records]
    # Gebaeude-Way muss drin sein
    assert "way/1001" in osm_ids
    # Highway-Way darf nicht als Location auftauchen
    assert "way/2001" not in osm_ids


# ---------------------------------------------------------------------------
# roads.merge_ways: ignoriert building-Ways in kombinierten Daten
# ---------------------------------------------------------------------------

def test_merge_ways_ignores_building_ways():
    """roads.merge_ways nimmt nur highway-Ways; building-Ways bleiben draussen."""
    g = RoadGraph()
    g.merge_ways(_COMBINED_DATA)
    # Nodes des highway-Ways muessen vorhanden sein
    assert 20 in g.coords
    assert 21 in g.coords
    assert 22 in g.coords
    # Nodes des building-Ways (10, 11, 12) duerfen NICHT im Graph landen
    assert 10 not in g.coords
    assert 11 not in g.coords
    assert 12 not in g.coords


# ---------------------------------------------------------------------------
# load_bbox_combined: EIN fetch, Gebaeude in DB + Strassen in Graph
# ---------------------------------------------------------------------------

def test_load_bbox_combined_locations_and_roads(monkeypatch):
    """load_bbox_combined: Locations in DB + highway-Nodes im Graph.

    Monkeypatch: fetch_bbox_combined liefert kombinierte Mockdaten (kein Netz).
    """
    conn = make_conn()
    set_seed(conn, 42)

    # Frischen Graph fuer diesen Test
    fresh_graph = RoadGraph()
    monkeypatch.setattr(roads_mod, "_graph", fresh_graph)
    monkeypatch.setattr(
        overpass, "fetch_bbox_combined", lambda *a, **kw: _COMBINED_DATA
    )

    count = loader.load_bbox_combined(49.5, 11.0, 49.51, 11.01, conn)

    # Mindestens 1 Location (das Gebaeude)
    assert count >= 1

    # Gebaeude-Way in der DB
    row = conn.execute(
        "SELECT osm_id FROM locations WHERE osm_id = 'way/1001';"
    ).fetchone()
    assert row is not None

    # Highway-Way NICHT als Location
    row2 = conn.execute(
        "SELECT osm_id FROM locations WHERE osm_id = 'way/2001';"
    ).fetchone()
    assert row2 is None

    # Highway-Nodes im Graph
    assert 20 in fresh_graph.coords
    assert 21 in fresh_graph.coords
    assert 22 in fresh_graph.coords

    # Gebaeude-Nodes NICHT im Graph
    assert 10 not in fresh_graph.coords


# ---------------------------------------------------------------------------
# load_bbox delegiert an load_bbox_combined (1 fetch)
# ---------------------------------------------------------------------------

def test_load_bbox_uses_combined_fetch(monkeypatch):
    """load_bbox ruft intern fetch_bbox_combined auf (kein separater fetch_bbox-Aufruf)."""
    conn = make_conn()
    set_seed(conn, 42)

    combined_calls = []
    old_calls = []

    def fake_combined(*a, **kw):
        combined_calls.append(1)
        return _COMBINED_DATA

    def fake_old(*a, **kw):
        old_calls.append(1)
        return {"elements": []}

    monkeypatch.setattr(overpass, "fetch_bbox_combined", fake_combined)
    monkeypatch.setattr(overpass, "fetch_bbox", fake_old)

    loader.load_bbox(49.5, 11.0, 49.51, 11.01, conn)

    assert len(combined_calls) == 1, "load_bbox soll fetch_bbox_combined aufrufen"
    assert len(old_calls) == 0, "load_bbox soll fetch_bbox NICHT separat aufrufen"


# ---------------------------------------------------------------------------
# fetch_bbox_combined nutzt eigenen Cache-Key (getrennt von fetch_bbox)
# ---------------------------------------------------------------------------

def test_combined_cache_key_differs_from_bbox_cache_key():
    """fetch_bbox_combined und fetch_bbox haben unterschiedliche Cache-Keys."""
    key_combined = overpass._bbox_combined_cache_key(49.5, 11.0, 49.51, 11.01)
    key_bbox = overpass._bbox_cache_key(49.5, 11.0, 49.51, 11.01)
    assert key_combined != key_bbox
