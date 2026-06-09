"""Tests fuer ensure_bbox_bulk: 1 Bulk-Call, Fallback bei Fehler.

Kein echtes Netz: loader.load_bbox wird per monkeypatch ersetzt.
Eisernes Prinzip: kein status='loaded' bei fehlgeschlagenem Bulk-Fetch,
es sei denn der Fallback per ensure_chunk_loaded gelingt.
"""
from __future__ import annotations

import pytest
from tests.conftest import make_conn, set_seed
from app.sim import chunks
from app.osm import loader


def _conn():
    c = make_conn()
    set_seed(c, 42)
    return c


# ---------------------------------------------------------------------------
# Erfolg: genau 1 load_bbox-Aufruf, alle Chunks 'loaded'
# ---------------------------------------------------------------------------

def test_bulk_calls_load_bbox_exactly_once(monkeypatch):
    """ensure_bbox_bulk macht bei leerem Cache EINEN load_bbox-Aufruf."""
    conn = _conn()
    calls = []

    def fake_load(min_lat, min_lon, max_lat, max_lon, conn_arg):
        calls.append((min_lat, min_lon, max_lat, max_lon))
        return 7  # 7 neue Locations

    monkeypatch.setattr(loader, "load_bbox", fake_load)

    from app import config
    d = config.CHUNK_DEG
    # Bbox die genau 2 Chunks abdeckt
    result = chunks.ensure_bbox_bulk(conn, 49.0, 11.0, 49.0 + d, 11.0)

    assert len(calls) == 1, f"Erwartet 1 load_bbox-Aufruf, war {len(calls)}"
    assert result["mode"] == "bulk"
    assert result["new_locations"] == 7
    assert result["failed_chunks"] == 0
    assert result["loaded_chunks"] >= 1


def test_bulk_marks_all_chunks_loaded(monkeypatch):
    """Nach erfolgreichem Bulk-Fetch sind alle abgedeckten Chunks status='loaded'."""
    conn = _conn()
    monkeypatch.setattr(loader, "load_bbox", lambda *a, **kw: 3)

    from app import config
    d = config.CHUNK_DEG
    cell_list = chunks.chunks_in_bbox(49.0, 11.0, 49.0 + d, 11.0)
    chunks.ensure_bbox_bulk(conn, 49.0, 11.0, 49.0 + d, 11.0)

    for cx, cy in cell_list:
        row = conn.execute(
            "SELECT status FROM world_chunks WHERE cx = ? AND cy = ?;", (cx, cy)
        ).fetchone()
        assert row is not None and row["status"] == "loaded", (
            f"Chunk ({cx},{cy}) nicht als 'loaded' markiert"
        )


# ---------------------------------------------------------------------------
# Noop: alle Chunks schon geladen → kein Netz
# ---------------------------------------------------------------------------

def test_noop_when_all_chunks_loaded(monkeypatch):
    """Wenn alle Chunks bereits 'loaded', kein load_bbox-Aufruf."""
    conn = _conn()
    calls = []

    def fake_load(*a, **kw):
        calls.append(1)
        return 0

    monkeypatch.setattr(loader, "load_bbox", fake_load)

    from app import config
    d = config.CHUNK_DEG
    # Erst laden
    chunks.ensure_bbox_bulk(conn, 49.0, 11.0, 49.0 + d, 11.0)
    calls.clear()

    # Zweiter Aufruf: Noop erwartet
    result = chunks.ensure_bbox_bulk(conn, 49.0, 11.0, 49.0 + d, 11.0)
    assert result["mode"] == "noop"
    assert len(calls) == 0, "Kein load_bbox-Aufruf erwartet (bereits geladen)"


# ---------------------------------------------------------------------------
# Fallback: Bulk-Fehler → per-chunk-Fallback, kein raise
# ---------------------------------------------------------------------------

def test_fallback_on_bulk_error_no_raise(monkeypatch):
    """Bei Bulk-Fetch-Fehler kein raise; mode='fallback'."""
    conn = _conn()

    def bulk_fail(*a, **kw):
        raise RuntimeError("Simulierter Overpass 504")

    monkeypatch.setattr(loader, "load_bbox", bulk_fail)

    from app import config
    d = config.CHUNK_DEG
    # Keine Exception erwartet
    result = chunks.ensure_bbox_bulk(conn, 49.0, 11.0, 49.0 + d * 0.5, 11.0 + d * 0.5)
    assert result["mode"] == "fallback"


def test_fallback_chunks_loaded_via_per_chunk(monkeypatch):
    """Fallback laed Chunks per ensure_chunk_loaded, wenn Bulk fehlschlaegt.

    Trick: Erster Aufruf (bulk) schlaegt fehl; alle weiteren (per-chunk) gelingen.
    """
    conn = _conn()
    call_count = {"n": 0}

    def selective_fail(min_lat, min_lon, max_lat, max_lon, conn_arg):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Erster Aufruf ist der Bulk -> schlaegt fehl
            raise RuntimeError("Bulk 504")
        # Alle weiteren Aufrufe (per-chunk-Fallback) gelingen
        return 2

    monkeypatch.setattr(loader, "load_bbox", selective_fail)

    from app import config
    d = config.CHUNK_DEG
    result = chunks.ensure_bbox_bulk(conn, 49.0, 11.0, 49.0 + d * 0.5, 11.0 + d * 0.5)

    assert result["mode"] == "fallback"
    # mindestens 1 Chunk per Fallback geladen
    assert result["loaded_chunks"] >= 1
    assert result["failed_chunks"] == 0
