"""Tests: ensure_chunk_loaded und ensure_chunks_in_bbox tolerieren Fetch-Fehler.

Kein Netz nötig: loader.load_bbox wird per monkeypatch ersetzt.
Eisernes Prinzip gewahrt: kein status='loaded' bei fehlgeschlagenem Fetch.
"""
from __future__ import annotations

import pytest
from tests.conftest import make_conn, set_seed
from app.sim import chunks
from app.osm import loader


# ---------------------------------------------------------------------------
# ensure_chunk_loaded: Fehler → ok=False, kein 'loaded'-Eintrag
# ---------------------------------------------------------------------------

def test_ensure_chunk_loaded_error_returns_ok_false(monkeypatch):
    """Bei Fetch-Fehler liefert ensure_chunk_loaded ok=False, kein loaded-Status."""
    conn = make_conn()
    set_seed(conn, 42)

    def fail_load(*args, **kwargs):
        raise RuntimeError("Simulierter Overpass 504")

    monkeypatch.setattr(loader, "load_bbox", fail_load)

    result = chunks.ensure_chunk_loaded(conn, 4900, 1100)

    assert result["ok"] is False
    assert "reason" in result
    assert result["loaded_now"] is False

    # Darf NICHT als 'loaded' in der DB stehen
    row = conn.execute(
        "SELECT status FROM world_chunks WHERE cx = 4900 AND cy = 1100;"
    ).fetchone()
    # Zeile kann fehlen oder status='error' haben, aber nie 'loaded'
    if row is not None:
        assert row["status"] != "loaded"


def test_ensure_chunk_loaded_success(monkeypatch):
    """Bei Erfolg liefert ensure_chunk_loaded ok=True und status='loaded'."""
    conn = make_conn()
    set_seed(conn, 42)

    monkeypatch.setattr(loader, "load_bbox", lambda *a, **kw: 5)

    result = chunks.ensure_chunk_loaded(conn, 4900, 1100)

    assert result["ok"] is True
    assert result["loaded_now"] is True
    assert result["building_count"] == 5

    row = conn.execute(
        "SELECT status FROM world_chunks WHERE cx = 4900 AND cy = 1100;"
    ).fetchone()
    assert row is not None
    assert row["status"] == "loaded"


# ---------------------------------------------------------------------------
# ensure_chunks_in_bbox: Ein Chunk-Fehler bricht nicht ab; failed_chunks >= 1
# ---------------------------------------------------------------------------

def test_ensure_chunks_in_bbox_partial_failure(monkeypatch):
    """Ein Chunk-Fetch-Fehler bricht ensure_chunks_in_bbox nicht ab.

    Testet: failed_chunks >= 1, loaded_chunks >= 0, keine Exception.
    """
    conn = make_conn()
    set_seed(conn, 42)

    call_count = {"n": 0}

    def sometimes_fail(min_lat, min_lon, max_lat, max_lon, conn_arg):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Simulierter Overpass 504")
        return 3  # 3 Gebäude für alle anderen Chunks

    monkeypatch.setattr(loader, "load_bbox", sometimes_fail)

    # Bbox die genau 2 Chunks abdeckt (0.01° Chunk-Größe)
    from app import config
    d = config.CHUNK_DEG
    result = chunks.ensure_chunks_in_bbox(conn, 49.0, 11.0, 49.0 + d, 11.0)

    # Keine Exception geworfen
    assert "failed_chunks" in result
    assert result["failed_chunks"] >= 1
    # Der zweite Chunk muss geladen worden sein
    assert result["loaded_chunks"] >= 1
    # Summe muss konsistent sein
    total = result["loaded_chunks"] + result["skipped_chunks"] + result["failed_chunks"]
    assert total == result["total_chunks"]


def test_ensure_chunks_in_bbox_all_fail(monkeypatch):
    """Alle Chunks fehlgeschlagen → keine Exception, failed_chunks = total_chunks."""
    conn = make_conn()
    set_seed(conn, 42)

    monkeypatch.setattr(loader, "load_bbox", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("504")))

    from app import config
    d = config.CHUNK_DEG
    result = chunks.ensure_chunks_in_bbox(conn, 49.0, 11.0, 49.0 + d * 0.5, 11.0 + d * 0.5)

    assert result["failed_chunks"] == result["total_chunks"]
    assert result["loaded_chunks"] == 0
