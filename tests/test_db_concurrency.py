"""Issue #52: keine "database is locked"-Fehler unter parallelen Anfragen,
lesende Endpunkte schreiben nicht, Netz-I/O läuft nie in einer offenen
Schreib-Transaktion.

Echte DB-Datei (nicht :memory:), Threads + TestClient, Overpass/Straßen gemockt
(langsam), Stub-LLM. Deterministisch bis auf das Thread-Timing; die Assertions
hängen nur an 'kein 500' bzw. 'DB unverändert'.
"""
from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
from app import config, db
from app.osm import loader, overpass, roads as roads_mod
from app.sim import companion
from tests.conftest import set_seed
from tests.test_actions import build_world
from tests.test_devices_online import give_device

_D = config.CHUNK_DEG
_SLOW_S = 1.2   # künstliche Netz-Latenz je Overpass-Request


@pytest.fixture
def live(tmp_path, monkeypatch):
    """Frische DB-Datei mit Welt, Handy und kontaktiertem Companion; Netz langsam."""
    os.environ["WASTELAND_LLM_BACKEND"] = "stub"
    llm_mod.reset_backend()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "w.db")
    db.init_db()
    conn = db.get_connection()
    set_seed(conn, 42)
    build_world(conn)
    give_device(conn, "smartphone", 1.0)
    assert companion.on_contact(conn, "reply", text="Eine Nachricht.")
    conn.close()

    calls = {"n": 0}

    def slow_fetch(*a, **k):
        calls["n"] += 1
        time.sleep(_SLOW_S)
        return {"elements": []}

    monkeypatch.setattr(overpass, "fetch_bbox_combined", slow_fetch)
    monkeypatch.setattr(roads_mod, "fetch_roads_bbox", slow_fetch)
    roads_mod._roads_loaded_chunks.clear()
    yield calls
    roads_mod._roads_loaded_chunks.clear()
    llm_mod.reset_backend()


def _dump(path) -> dict:
    """Kompletter Tabelleninhalt (nur lesend) zum Vorher/Nachher-Vergleich."""
    import sqlite3
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")]
        return {t: c.execute(f"SELECT * FROM {t} ORDER BY 1, 2;").fetchall() for t in tables}
    finally:
        c.close()


def test_read_endpoints_do_not_write(live):
    from app.main import app
    with TestClient(app) as client:
        before = _dump(config.DB_PATH)
        for url in ("/world/state", "/actions/available", "/companion",
                    "/companion/chat", "/groups/1/inventory", "/groups/1/carry",
                    "/characters", "/locations", "/locations/100",
                    "/locations/100/inventory"):
            r = client.get(url)
            assert r.status_code == 200, (url, r.text)
        after = _dump(config.DB_PATH)
    changed = [t for t in before if before[t] != after[t]]
    assert changed == [], f"Lesende Endpunkte haben geschrieben: {changed}"


def test_parallel_requests_no_lock_errors(live):
    """Parallele GETs + POST /actions (Warten) + langsames /world/ensure-chunks → kein 500."""
    from app.main import app
    results: list[tuple[str, int]] = []
    errors: list[BaseException] = []
    stop = threading.Event()

    with TestClient(app, raise_server_exceptions=False) as client:
        def reader(url):
            while not stop.is_set():
                try:
                    results.append((url, client.get(url).status_code))
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)
                    return

        def waiter():
            for _ in range(6):
                try:
                    r = client.post("/actions", json={"kind": "wait", "minutes": 10})
                    results.append(("POST /actions", r.status_code))
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)
                    return

        def chunks_loader():
            try:
                r = client.post("/world/ensure-chunks", json={
                    "min_lat": 49.50, "min_lon": 11.00,
                    "max_lat": 49.50 + _D, "max_lon": 11.00 + _D})
                results.append(("POST /world/ensure-chunks", r.status_code))
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        readers = [threading.Thread(target=reader, args=(u,)) for u in (
            "/world/state", "/actions/available", "/companion", "/groups/1/inventory")]
        workers = [threading.Thread(target=waiter), threading.Thread(target=chunks_loader)]
        for t in readers + workers:
            t.start()
        for t in workers:
            t.join(timeout=120)
        stop.set()
        for t in readers:
            t.join(timeout=30)

    assert not errors, errors
    assert results
    bad = [(u, s) for u, s in results if s != 200]
    assert not bad, f"Nicht-200-Antworten: {bad[:5]}"


def test_no_network_inside_write_transaction(live, monkeypatch):
    """Overpass/Straßen-Fetch läuft nie, während die Verbindung eine offene
    Schreib-Transaktion hält (sonst blockiert sie alle anderen)."""
    seen: list[bool] = []
    real = {}

    def probe(name):
        def fn(*a, **k):
            seen.append(any(_in_txn(c) for c in _open_conns))
            return {"elements": []}
        real[name] = fn
        return fn

    import sqlite3

    def _in_txn(c):
        try:
            return c.in_transaction
        except sqlite3.ProgrammingError:   # bereits geschlossen
            return False

    _open_conns: list[sqlite3.Connection] = []
    real_get = db.get_connection

    def tracking():
        c = real_get()
        _open_conns.append(c)
        return c

    monkeypatch.setattr(db, "get_connection", tracking)
    monkeypatch.setattr(overpass, "fetch_bbox_combined", probe("a"))
    monkeypatch.setattr(roads_mod, "fetch_roads_bbox", probe("b"))
    from app.main import app
    with TestClient(app) as client:
        r = client.post("/world/ensure-chunks", json={
            "min_lat": 49.50, "min_lon": 11.00,
            "max_lat": 49.50 + _D, "max_lon": 11.00 + _D})
        assert r.status_code == 200 and r.json()["failed_chunks"] == 0, r.text
        for _ in range(4):
            client.post("/actions", json={"kind": "wait", "minutes": 10})
        client.post("/actions", json={"kind": "walk", "location_id": 101})
    assert seen, "Es wurde gar nicht gefetcht"
    assert not any(seen), "Netz-I/O innerhalb einer offenen Schreib-Transaktion"


def test_wal_and_busy_timeout(live):
    conn = db.get_connection()
    try:
        assert conn.execute("PRAGMA journal_mode;").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout;").fetchone()[0] >= 10_000
    finally:
        conn.close()


def test_tick_contact_is_atomic(live):
    """Entsteht im Tick ein Kontakt, committet ``materialize_in_bbox_tx`` nicht
    mittendrin: Rollback der Tick-Transaktion nimmt auch die Materialisierung zurück."""
    from app.sim import survivors
    conn = db.get_connection()
    try:
        sid = conn.execute("INSERT INTO survivors (lat, lon, sex, birth_tick) "
                           "VALUES (49.0, 11.0, 'f', -10000000);").lastrowid
        conn.commit()
        ids = survivors.materialize_in_bbox_tx(conn, 48.9, 10.9, 49.1, 11.1)
        assert ids and conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT materialized FROM survivors WHERE id = ?;",
                            (sid,)).fetchone()[0] == 0
    finally:
        conn.close()
