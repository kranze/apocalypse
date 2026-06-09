"""Tests: chunk_key, chunk_bbox, chunks_in_bbox, ensure_chunk_loaded (idempotent),
ensure_bbox_bulk (1 loader-Call, noop), Routing-Determinismus (A*, nearest_node),
Survivor-Materialisierung lazy+idempotent, /world/ensure-chunks smoke.

Kein echtes Netz: loader.load_bbox und roads.ensure_roads_for_chunk per monkeypatch.
Deterministisch, fixer Seed.  Eisernes Prinzip: nur Sim-Kern schreibt.
"""
from __future__ import annotations

import math
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config
from app.osm import loader, roads as roads_mod
from app.osm.roads import RoadGraph, build_graph
from app.sim import chunks, survivors as survivors_mod
from tests.conftest import make_conn, set_seed

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_D = config.CHUNK_DEG  # e.g. 0.01


def _conn() -> sqlite3.Connection:
    c = make_conn()
    set_seed(c, 42)
    return c


# ---------------------------------------------------------------------------
# chunk_key: deterministisch, stabil, negative Koordinaten
# ---------------------------------------------------------------------------

def test_chunk_key_deterministic():
    """Gleicher Aufruf liefert immer dasselbe Ergebnis."""
    assert chunks.chunk_key(49.5, 11.0) == chunks.chunk_key(49.5, 11.0)


def test_chunk_key_positive_coords():
    cx, cy = chunks.chunk_key(49.5, 11.0)
    assert cx == math.floor(49.5 / _D)
    assert cy == math.floor(11.0 / _D)


def test_chunk_key_negative_lat():
    """Negative Breitengrade → floor liefert korrekt negativen cx."""
    cx, cy = chunks.chunk_key(-10.005, 30.0)
    assert cx == math.floor(-10.005 / _D)
    assert cy == math.floor(30.0 / _D)


def test_chunk_key_negative_lon():
    """Negative Längengrade → korrekter cy."""
    cx, cy = chunks.chunk_key(0.0, -5.55)
    assert cx == math.floor(0.0 / _D)
    assert cy == math.floor(-5.55 / _D)


def test_chunk_key_both_negative():
    cx, cy = chunks.chunk_key(-33.8, -70.6)
    assert cx == math.floor(-33.8 / _D)
    assert cy == math.floor(-70.6 / _D)


# ---------------------------------------------------------------------------
# chunk_bbox: invers zu chunk_key
# ---------------------------------------------------------------------------

def test_chunk_bbox_contains_origin_coord():
    """chunk_bbox(cx, cy) enthält die Koordinate, die chunk_key zu (cx,cy) mappte."""
    lat, lon = 49.503, 11.007
    cx, cy = chunks.chunk_key(lat, lon)
    min_lat, min_lon, max_lat, max_lon = chunks.chunk_bbox(cx, cy)
    assert min_lat <= lat < max_lat
    assert min_lon <= lon < max_lon


def test_chunk_bbox_size():
    """chunk_bbox hat die Breite/Höhe CHUNK_DEG."""
    min_lat, min_lon, max_lat, max_lon = chunks.chunk_bbox(4950, 1100)
    assert pytest.approx(max_lat - min_lat, abs=1e-9) == _D
    assert pytest.approx(max_lon - min_lon, abs=1e-9) == _D


def test_chunk_bbox_inverse_negative():
    """Auch für negative cx/cy ist chunk_bbox invers zu chunk_key."""
    lat, lon = -10.005, -5.55
    cx, cy = chunks.chunk_key(lat, lon)
    min_lat, min_lon, max_lat, max_lon = chunks.chunk_bbox(cx, cy)
    assert min_lat <= lat < max_lat
    assert min_lon <= lon < max_lon


# ---------------------------------------------------------------------------
# chunks_in_bbox: enthält alle erwarteten Kacheln
# ---------------------------------------------------------------------------

def test_chunks_in_bbox_single():
    """Bbox die genau in einen Chunk fällt → 1 Chunk."""
    cx, cy = chunks.chunk_key(49.5, 11.0)
    min_lat = cx * _D + _D * 0.1
    max_lat = cx * _D + _D * 0.9
    min_lon = cy * _D + _D * 0.1
    max_lon = cy * _D + _D * 0.9
    result = chunks.chunks_in_bbox(min_lat, min_lon, max_lat, max_lon)
    assert (cx, cy) in result
    assert len(result) == 1


def test_chunks_in_bbox_four():
    """Bbox die vier Chunks überquert → mindestens 4 Einträge."""
    lat0, lon0 = 49.5, 11.0
    cx0, cy0 = chunks.chunk_key(lat0, lon0)
    # Von Mitte Chunk0 bis Mitte Chunk diagonal unten-rechts
    min_lat = cx0 * _D + _D * 0.5
    min_lon = cy0 * _D + _D * 0.5
    max_lat = min_lat + _D
    max_lon = min_lon + _D
    result = chunks.chunks_in_bbox(min_lat, min_lon, max_lat, max_lon)
    assert len(result) >= 4
    assert (cx0, cy0) in result


def test_chunks_in_bbox_two_horizontal():
    """Bbox, die genau zwei horizontal benachbarte Chunks abdeckt."""
    cx, cy = 4950, 1100
    # Straddle die Grenze cx+1 / cx+1
    min_lat = cx * _D + _D * 0.5
    max_lat = (cx + 1) * _D + _D * 0.5
    min_lon = cy * _D + _D * 0.3
    max_lon = cy * _D + _D * 0.7
    result = chunks.chunks_in_bbox(min_lat, min_lon, max_lat, max_lon)
    assert (cx, cy) in result
    assert (cx + 1, cy) in result


# ---------------------------------------------------------------------------
# ensure_chunk_loaded: idempotent (kein zweiter loader-Call)
# ---------------------------------------------------------------------------

def test_ensure_chunk_loaded_idempotent(monkeypatch):
    """Zweiter Aufruf ohne erneuten loader-Call; status bleibt 'loaded'."""
    conn = _conn()
    calls = []

    def fake_load(*a, **kw):
        calls.append(1)
        return 3

    monkeypatch.setattr(loader, "load_bbox", fake_load)
    monkeypatch.setattr(roads_mod, "ensure_roads_for_chunk", lambda *a, **kw: None)

    result1 = chunks.ensure_chunk_loaded(conn, 4950, 1100)
    assert result1["ok"] is True
    assert result1["loaded_now"] is True
    assert len(calls) == 1

    result2 = chunks.ensure_chunk_loaded(conn, 4950, 1100)
    assert result2["ok"] is True
    assert result2["loaded_now"] is False
    assert len(calls) == 1  # kein zweiter Loader-Call


# ---------------------------------------------------------------------------
# ensure_bbox_bulk: 1 Call für mehrere Chunks; Noop wenn alle geladen
# ---------------------------------------------------------------------------

def test_ensure_bbox_bulk_one_loader_call(monkeypatch):
    """ensure_bbox_bulk macht EINEN load_bbox-Call auch wenn mehrere Chunks nötig."""
    conn = _conn()
    calls = []

    def fake_load(*a, **kw):
        calls.append(1)
        return 5

    monkeypatch.setattr(loader, "load_bbox", fake_load)
    monkeypatch.setattr(roads_mod, "ensure_roads_for_chunk", lambda *a, **kw: None)

    # Bbox mit 2 Chunks
    result = chunks.ensure_bbox_bulk(conn, 49.0, 11.0, 49.0 + _D, 11.0)

    assert len(calls) == 1
    assert result["mode"] == "bulk"
    assert result["loaded_chunks"] >= 2
    assert result["failed_chunks"] == 0


def test_ensure_bbox_bulk_marks_all_loaded(monkeypatch):
    """Nach ensure_bbox_bulk sind alle betroffenen Chunks status='loaded'."""
    conn = _conn()
    monkeypatch.setattr(loader, "load_bbox", lambda *a, **kw: 2)
    monkeypatch.setattr(roads_mod, "ensure_roads_for_chunk", lambda *a, **kw: None)

    cell_list = chunks.chunks_in_bbox(49.0, 11.0, 49.0 + _D, 11.0)
    chunks.ensure_bbox_bulk(conn, 49.0, 11.0, 49.0 + _D, 11.0)

    for cx, cy in cell_list:
        row = conn.execute(
            "SELECT status FROM world_chunks WHERE cx = ? AND cy = ?;", (cx, cy)
        ).fetchone()
        assert row is not None and row["status"] == "loaded"


def test_ensure_bbox_bulk_noop_when_all_loaded(monkeypatch):
    """Wenn alle Chunks bereits geladen, kein loader-Call (noop)."""
    conn = _conn()
    calls = []

    def fake_load(*a, **kw):
        calls.append(1)
        return 0

    monkeypatch.setattr(loader, "load_bbox", fake_load)
    monkeypatch.setattr(roads_mod, "ensure_roads_for_chunk", lambda *a, **kw: None)

    # Erst laden
    chunks.ensure_bbox_bulk(conn, 49.0, 11.0, 49.0 + _D, 11.0)
    calls.clear()

    # Zweiter Aufruf → Noop
    result = chunks.ensure_bbox_bulk(conn, 49.0, 11.0, 49.0 + _D, 11.0)
    assert result["mode"] == "noop"
    assert len(calls) == 0


# ---------------------------------------------------------------------------
# Routing-Determinismus: build_graph / merge_ways / shortest_path / nearest_node
# ---------------------------------------------------------------------------

def _mini_osm(ways) -> dict:
    """Konstruiert ein minimales Overpass-out-geom-Dict aus einer Way-Liste.

    ways: [(osm_id, [node_id, ...], [(lat, lon), ...])]
    """
    elements = []
    for way_id, node_ids, coords in ways:
        elements.append({
            "type": "way",
            "id": way_id,
            "nodes": node_ids,
            "geometry": [{"lat": lat, "lon": lon} for lat, lon in coords],
            "tags": {"highway": "residential"},
        })
    return {"elements": elements}


# Minimaler Graph: 3 Knoten in einer Linie A-B-C
_WAYS = [
    (1001, [1, 2], [(49.500, 11.000), (49.501, 11.000)]),
    (1002, [2, 3], [(49.501, 11.000), (49.502, 11.000)]),
]
_OSM_DATA = _mini_osm(_WAYS)


def test_build_graph_nodes():
    """build_graph erzeugt alle erwarteten Knoten."""
    g = build_graph(_OSM_DATA)
    assert 1 in g.coords
    assert 2 in g.coords
    assert 3 in g.coords


def test_build_graph_edges():
    """build_graph erzeugt Kanten zwischen benachbarten Knoten."""
    g = build_graph(_OSM_DATA)
    assert 2 in g.adj[1]
    assert 1 in g.adj[2]
    assert 3 in g.adj[2]


def test_shortest_path_deterministic():
    """A* liefert bei zwei Läufen denselben Pfad (deterministisch)."""
    g1 = build_graph(_OSM_DATA)
    g2 = build_graph(_OSM_DATA)

    path1, dist1 = g1.shortest_path(1, 3)
    path2, dist2 = g2.shortest_path(1, 3)

    assert path1 == path2
    assert pytest.approx(dist1, abs=0.01) == dist2


def test_shortest_path_start_equals_goal():
    """Kurzpfad von A nach A: Pfad = [A], Distanz = 0."""
    g = build_graph(_OSM_DATA)
    path, dist = g.shortest_path(1, 1)
    assert path == [(49.500, 11.000)]
    assert dist == 0.0


def test_shortest_path_uses_intermediate_node():
    """Pfad von 1 nach 3 läuft über Knoten 2."""
    g = build_graph(_OSM_DATA)
    path, dist = g.shortest_path(1, 3)
    assert len(path) == 3
    assert path[0] == g.coords[1]
    assert path[-1] == g.coords[3]


def test_shortest_path_no_route():
    """Kein Weg möglich → leere Liste, dist = inf."""
    g = build_graph(_OSM_DATA)
    # Füge isolierten 4. Knoten ohne Kanten ein
    g.coords[99] = (50.0, 12.0)
    g._add_to_index(99, 50.0, 12.0)
    path, dist = g.shortest_path(1, 99)
    assert path == []
    assert dist == float("inf")


def test_nearest_node_correct():
    """nearest_node findet den nächstgelegenen Knoten korrekt."""
    g = build_graph(_OSM_DATA)
    # Sehr nah an Knoten 2 (49.501, 11.0)
    nid = g.nearest_node(49.5009, 11.0001)
    assert nid == 2


def test_nearest_node_empty_graph():
    """nearest_node auf leerem Graph → None."""
    g = RoadGraph()
    assert g.nearest_node(49.5, 11.0) is None


def test_merge_ways_idempotent():
    """merge_ways zweimal mit denselben Daten → gleiche Knotenanzahl."""
    g = RoadGraph()
    g.merge_ways(_OSM_DATA)
    count_after_first = g.node_count
    g.merge_ways(_OSM_DATA)
    assert g.node_count == count_after_first


def test_merge_ways_additive():
    """merge_ways mit zweitem Datensatz erweitert den Graph."""
    g = build_graph(_OSM_DATA)
    count_before = g.node_count

    extra_data = _mini_osm([
        (2001, [10, 11], [(49.510, 11.010), (49.511, 11.010)]),
    ])
    g.merge_ways(extra_data)
    assert g.node_count > count_before


# ---------------------------------------------------------------------------
# Survivor-Materialisierung: lazy + idempotent
# ---------------------------------------------------------------------------

def _insert_survivor(conn: sqlite3.Connection, sid: int, lat: float, lon: float) -> None:
    """Fügt einen Survivor mit materialized=0 ein."""
    conn.execute(
        "INSERT INTO survivors (id, lat, lon, sex, birth_tick, home_lat, home_lon, "
        "materialized, alive) VALUES (?, ?, ?, 'm', -18921600, ?, ?, 0, 1);",
        (sid, lat, lon, lat, lon),
    )
    conn.commit()


def test_materialize_in_bbox_basic(monkeypatch):
    """Survivors im Bbox werden materialisiert (materialized=1, character_id gesetzt)."""
    conn = _conn()
    # Survivor in der Bbox
    lat, lon = 49.503, 11.003
    _insert_survivor(conn, 1, lat, lon)

    result_ids = survivors_mod.materialize_in_bbox(conn, 49.50, 11.00, 49.51, 11.01)

    assert len(result_ids) == 1
    row = conn.execute("SELECT materialized, character_id FROM survivors WHERE id = 1;").fetchone()
    assert row["materialized"] == 1
    assert row["character_id"] is not None


def test_materialize_in_bbox_idempotent(monkeypatch):
    """Zweiter Aufruf materialisiert keine neuen Survivors → keine Duplikate."""
    conn = _conn()
    lat, lon = 49.503, 11.003
    _insert_survivor(conn, 1, lat, lon)

    ids1 = survivors_mod.materialize_in_bbox(conn, 49.50, 11.00, 49.51, 11.01)
    assert len(ids1) == 1

    ids2 = survivors_mod.materialize_in_bbox(conn, 49.50, 11.00, 49.51, 11.01)
    assert ids2 == []

    # Nur ein characters-Eintrag für den Survivor
    count = conn.execute("SELECT COUNT(*) FROM characters WHERE type = 'survivor';").fetchone()[0]
    assert count == 1


def test_materialize_outside_bbox_not_affected():
    """Survivor außerhalb der Bbox bleibt unmaterialisiert."""
    conn = _conn()
    # Survivor klar außerhalb
    _insert_survivor(conn, 1, 50.0, 12.0)

    ids = survivors_mod.materialize_in_bbox(conn, 49.50, 11.00, 49.51, 11.01)
    assert ids == []

    row = conn.execute("SELECT materialized FROM survivors WHERE id = 1;").fetchone()
    assert row["materialized"] == 0


def test_ensure_chunk_loaded_triggers_materialization(monkeypatch):
    """ensure_chunk_loaded materialisiert Survivors im Chunk."""
    conn = _conn()

    monkeypatch.setattr(loader, "load_bbox", lambda *a, **kw: 0)
    monkeypatch.setattr(roads_mod, "ensure_roads_for_chunk", lambda *a, **kw: None)

    # Survivor genau in Chunk (4950, 1100)
    cx, cy = 4950, 1100
    min_lat, min_lon, max_lat, max_lon = chunks.chunk_bbox(cx, cy)
    s_lat = (min_lat + max_lat) / 2
    s_lon = (min_lon + max_lon) / 2
    _insert_survivor(conn, 1, s_lat, s_lon)

    chunks.ensure_chunk_loaded(conn, cx, cy)

    row = conn.execute("SELECT materialized FROM survivors WHERE id = 1;").fetchone()
    assert row["materialized"] == 1


# ---------------------------------------------------------------------------
# /world/ensure-chunks Endpunkt Smoke (TestClient, kein echtes Netz)
# ---------------------------------------------------------------------------

def test_ensure_chunks_endpoint_smoke(monkeypatch):
    """/world/ensure-chunks liefert 200 + summary-Felder (kein echtes Netz)."""
    monkeypatch.setattr(loader, "load_bbox", lambda *a, **kw: 4)
    monkeypatch.setattr(roads_mod, "ensure_roads_for_chunk", lambda *a, **kw: None)

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as client:
        resp = client.post(
            "/world/ensure-chunks",
            json={
                "min_lat": 49.50,
                "min_lon": 11.00,
                "max_lat": 49.50 + _D,
                "max_lon": 11.00 + _D,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "loaded_chunks" in data
    assert "failed_chunks" in data
    assert "new_locations" in data
    assert "materialized" in data


def test_ensure_chunks_endpoint_bbox_too_large():
    """/world/ensure-chunks lehnt zu große Bbox mit 422 ab."""
    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as client:
        resp = client.post(
            "/world/ensure-chunks",
            json={
                "min_lat": 49.0,
                "min_lon": 11.0,
                "max_lat": 49.0 + 0.2,   # > _BBOX_MAX_DEG=0.1
                "max_lon": 11.0 + 0.2,
            },
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /api/info enthält Chunk-Parameter
# ---------------------------------------------------------------------------

def test_api_info_contains_chunk_params():
    """/api/info gibt chunk_deg und home_preload_radius_m zurück."""
    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as client:
        resp = client.get("/api/info")

    assert resp.status_code == 200
    data = resp.json()
    assert "chunk_deg" in data
    assert "home_preload_radius_m" in data
    assert data["chunk_deg"] == config.CHUNK_DEG
    assert data["home_preload_radius_m"] == config.HOME_PRELOAD_RADIUS_M
