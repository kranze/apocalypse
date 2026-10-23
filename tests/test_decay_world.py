"""Tests für Verfall-Simulator (Issue #5/#7/#9, Slice M2 Teil A).

Determinismus, monotone Timeline, Kühlung folgt Strom, Leitungswasser-Gate,
Verderb ohne Kühlung, Bilanz. Deterministisch, kein Netz.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ["WASTELAND_LLM_BACKEND"] = "stub"

from tests.conftest import make_conn, set_seed, insert_location
from app.sim import constants, decay_world, ledger, resources
from app.sim.provision import auto_provision
from app.sim.tick import advance_tick

DAY = constants.MINUTES_PER_DAY
LOC = 100


@pytest.fixture
def conn():
    c = make_conn()
    set_seed(c, 1337)
    yield c
    c.close()


def _set_tick(conn, tick):
    conn.execute("UPDATE world SET tick = ? WHERE id = 1;", (tick,))
    conn.commit()


def _discovered_loc(conn, loc_id=LOC, at_player=True):
    insert_location(conn, loc_id=loc_id)
    conn.execute("UPDATE locations SET discovery_status='discovered' WHERE id=?;", (loc_id,))
    if at_player:
        conn.execute("UPDATE characters SET lat=49.0, lon=11.0 WHERE id=1;")
    conn.commit()


def _add_group(conn, item, qty, tick=0):
    conn.execute(
        "INSERT INTO group_inventory (group_id,item_id,quantity,quality,acquired_tick) "
        "VALUES (1,?,?,1.0,?);", (item, qty, tick))
    ledger.add(conn, item, qty)
    conn.commit()


# --- Determinismus & Timeline ----------------------------------------------

def test_deterministic_two_dbs():
    a, b = make_conn(), make_conn()
    for c in (a, b):
        set_seed(c, 42)
        _discovered_loc(c, at_player=False)
    for day in (0, 1, 2, 3, 5, 100):
        for c in (a, b):
            _set_tick(c, day * DAY + 123)
        assert decay_world.infra_for(a, LOC) == decay_world.infra_for(b, LOC)
        assert decay_world.world_infra(a) == decay_world.world_infra(b)


def test_seed_changes_jitter():
    vals = {decay_world.values_at(s, LOC, int(2 * DAY))["power"] for s in range(1, 12)}
    assert len(vals) > 1


@pytest.mark.parametrize("loc_id", [1, 2, 3, 77, 1234])
def test_monotone_falls(loc_id):
    prev = None
    for i in range(0, 12 * 20):  # 0..12 Tage in 0,05-Tag-Schritten
        v = decay_world.values_at(1337, loc_id, int(i * 0.05 * DAY))
        assert v["refrigeration"] == v["power"]  # Kühlung folgt Strom
        if prev:
            for k in ("power", "water", "refrigeration", "structural"):
                assert v[k] <= prev[k] + 1e-9, k
            assert v["overgrowth"] >= prev["overgrowth"] - 1e-9
        prev = v
    assert prev["power"] == 0.0 and prev["water"] == 0.0


def test_day0_intact_and_everything_off_by_day3():
    for loc_id in range(1, 40):
        v0 = decay_world.values_at(1337, loc_id, 0)
        assert v0["power"] == 1.0 and v0["water"] == 1.0 and v0["refrigeration"] == 1.0
        v3 = decay_world.values_at(1337, loc_id, int(3.01 * DAY))
        assert v3["power"] == 0.0 and v3["refrigeration"] == 0.0 and v3["water"] == 0.0
        # Wasser hält bis ~Tag 1,5
        assert decay_world.values_at(1337, loc_id, int(1.4 * DAY))["water"] == 1.0
    # erste lokale Stromausfälle ab ~Tag 1 (Fallfenster darf früher beginnen)
    assert min(decay_world.values_at(1337, i, int(1.0 * DAY))["power"] for i in range(1, 60)) < 1.0


def test_long_term_structure_and_overgrowth():
    v = decay_world.values_at(1337, LOC, 7 * DAY)
    assert v["structural"] == 1.0 and v["overgrowth"] == 0.0
    v = decay_world.values_at(1337, LOC, 5 * 365 * DAY)
    assert v["structural"] < 1.0 and v["overgrowth"] > 0.5


def test_global_phase_boundaries(conn):
    def label(day):
        _set_tick(conn, int(day * DAY))
        return decay_world.global_phase(conn)
    assert label(0) == "Alles läuft noch"
    assert label(1.5) == "Netze wackeln"
    assert label(3.0) == "Strom fällt"
    assert label(4.0) == "Das Netz stirbt"
    assert label(10) == "Stille"
    assert label(0.99) != label(1.0)


def test_world_infra_timeline(conn):
    def wi(day):
        _set_tick(conn, int(day * DAY))
        return decay_world.world_infra(conn)
    w0 = wi(0)
    json.dumps(w0)  # serialisierbar
    assert set(w0) == {"day", "power", "water", "cellular", "internet", "phase"}
    assert w0["cellular"] == 0.6 and w0["internet"] == 0.9 and w0["power"] == 1.0
    assert wi(2)["internet"] == pytest.approx(0.6)
    assert wi(3)["internet"] == pytest.approx(0.35)
    assert wi(4)["internet"] == pytest.approx(0.15)
    assert wi(5)["internet"] == pytest.approx(0.02)
    assert wi(5)["cellular"] <= 0.02
    prev = (1.0, 1.0)
    for i in range(0, 20 * 10):
        w = wi(i / 10)
        assert w["cellular"] <= prev[0] + 1e-9 and w["internet"] <= prev[1] + 1e-9
        prev = (w["cellular"], w["internet"])


def test_advance_is_lazy(conn):
    insert_location(conn, loc_id=1)                       # unentdeckt, kein Inventar
    insert_location(conn, loc_id=2)
    conn.execute("UPDATE locations SET discovery_status='discovered' WHERE id=2;")
    insert_location(conn, loc_id=3)                       # unentdeckt, aber mit Inventar
    conn.execute("INSERT INTO location_inventory (location_id,item_id,quantity) "
                 "VALUES (3,'canned_beans',1.0);")
    conn.commit()
    _set_tick(conn, DAY)
    with conn:
        n = decay_world.advance(conn, 10)
    ids = {r["location_id"] for r in conn.execute("SELECT location_id FROM location_infra;")}
    assert ids == {2, 3} and n == 2
    assert conn.execute("SELECT updated_tick FROM location_infra WHERE location_id=2;"
                        ).fetchone()["updated_tick"] == DAY


# --- Leitungswasser-Gate (#7) -------------------------------------------------

def _thirst(conn):
    return conn.execute("SELECT thirst FROM characters WHERE id=1;").fetchone()["thirst"]


def test_tap_water_day0_but_not_after_pressure_loss(conn):
    _discovered_loc(conn)
    conn.execute("UPDATE characters SET thirst=0.2 WHERE id=1;")
    conn.commit()
    with conn:
        auto_provision(conn, 10, 0)
    assert _thirst(conn) > 0.2  # Tag 0: Hahn läuft
    assert conn.execute("SELECT COUNT(*) c FROM group_inventory;").fetchone()["c"] == 0

    conn.execute("UPDATE characters SET thirst=0.2 WHERE id=1;")
    _set_tick(conn, 4 * DAY)
    with conn:
        auto_provision(conn, 10, 4 * DAY)
    assert _thirst(conn) == pytest.approx(0.2)  # Druck weg -> kein Wasser aus dem Nichts


def test_tap_needs_discovered_location(conn):
    insert_location(conn, loc_id=LOC)  # unentdeckt
    conn.execute("UPDATE characters SET lat=49.0, lon=11.0, thirst=0.2 WHERE id=1;")
    conn.commit()
    with conn:
        auto_provision(conn, 10, 0)
    assert _thirst(conn) == pytest.approx(0.2)


def test_inventory_water_preferred_over_tap(conn):
    _discovered_loc(conn)
    _add_group(conn, "water_1l", 5.0)
    conn.execute("UPDATE characters SET thirst=0.2 WHERE id=1;")
    conn.commit()
    with conn:
        auto_provision(conn, 10, 0)
    q = conn.execute("SELECT SUM(quantity) q FROM group_inventory WHERE item_id='water_1l';"
                     ).fetchone()["q"]
    assert q < 5.0


# --- Verderb ohne Kühlung (#7) ---------------------------------------------------

def _quality_after(conn, now, anchor):
    with conn:
        resources.apply_decay(conn, now)
    r = conn.execute("SELECT quality FROM group_inventory WHERE item_id='bread_loaf';").fetchone()
    return r["quality"] if r else 0.0


def test_faster_spoilage_without_cooling(conn):
    lost = decay_world.cooling_lost_tick(1337, LOC)
    anchor, now = lost - 100, lost + 700
    # A: Gruppe an Ort mit ausgefallener Kühlung; B: gleiche Lage, aber noch gekühlt (Anker/Jetzt vorher)
    _discovered_loc(conn)
    _add_group(conn, "bread_loaf", 3.0, tick=anchor)
    q_warm = _quality_after(conn, now, anchor)

    base = resources.quality_at(anchor, now, 4320)
    assert q_warm < base - 1e-3  # schneller als Katalog-Halbwertszeit
    expected = resources.quality_cooled(anchor, now, 4320, lost)
    assert q_warm == pytest.approx(round(expected, 4), abs=1e-4)


def test_normal_spoilage_while_cooled(conn):
    lost = decay_world.cooling_lost_tick(1337, LOC)
    _discovered_loc(conn)
    _add_group(conn, "bread_loaf", 3.0, tick=0)
    q = _quality_after(conn, lost - 10, 0)
    assert q == pytest.approx(round(resources.quality_at(0, lost - 10, 4320), 4), abs=1e-4)


def test_spoilage_without_location_unchanged(conn):
    _add_group(conn, "bread_loaf", 3.0, tick=0)
    q = _quality_after(conn, 3 * DAY, 0)
    assert q == pytest.approx(round(resources.quality_at(0, 3 * DAY, 4320), 4), abs=1e-4)


def test_location_inventory_spoils_faster_without_power(conn):
    _discovered_loc(conn, at_player=False)
    lost = decay_world.cooling_lost_tick(1337, LOC)
    conn.execute("INSERT INTO location_inventory (location_id,item_id,quantity,quality,"
                 "produced_tick) VALUES (?,?,2.0,1.0,?);", (LOC, "bread_loaf", lost - 100))
    ledger.add(conn, "bread_loaf", 2.0)
    conn.commit()
    with conn:
        resources.apply_decay(conn, lost + 700)
    q = conn.execute("SELECT quality FROM location_inventory;").fetchone()["quality"]
    assert q < resources.quality_at(lost - 100, lost + 700, 4320) - 1e-3


def test_quality_never_rises(conn):
    _discovered_loc(conn)
    lost = decay_world.cooling_lost_tick(1337, LOC)
    _add_group(conn, "bread_loaf", 3.0, tick=lost - 100)
    q1 = _quality_after(conn, lost + 600, 0)
    q2 = _quality_after(conn, lost + 600, 0)
    assert q2 <= q1 + 1e-9


# --- Bilanz & Tick-Reihenfolge --------------------------------------------------

def test_ledger_clean_over_ticks_with_decay_and_tap(conn):
    _discovered_loc(conn)
    _add_group(conn, "bread_loaf", 4.0)
    _add_group(conn, "milk_1l", 4.0)
    conn.execute("UPDATE characters SET thirst=0.1, hunger=0.1 WHERE id=1;")
    conn.commit()
    # Rund um den Stromausfall und Wasserdruckverlust ticken
    for start_day in (0, 2.4, 4):
        _set_tick(conn, int(start_day * DAY))
        for _ in range(30):
            advance_tick(conn, constants.TICK_MINUTES)
    flagged = conn.execute("SELECT COALESCE(SUM(flagged),0) f FROM resource_audit;"
                           ).fetchone()["f"]
    assert flagged == 0


def test_tick_writes_infra_for_discovered(conn):
    _discovered_loc(conn, at_player=False)
    advance_tick(conn, constants.TICK_MINUTES)
    row = conn.execute("SELECT updated_tick FROM location_infra WHERE location_id=?;",
                       (LOC,)).fetchone()
    assert row is not None
    assert row["updated_tick"] == conn.execute("SELECT tick FROM world;").fetchone()["tick"]


def test_tick_deterministic_with_infra():
    snaps = []
    for _ in range(2):
        c = make_conn()
        set_seed(c, 7)
        _discovered_loc(c)
        _add_group(c, "bread_loaf", 3.0)
        _set_tick(c, int(2.5 * DAY))
        for _ in range(20):
            advance_tick(c, constants.TICK_MINUTES)
        snaps.append((
            [tuple(r) for r in c.execute("SELECT * FROM location_infra;")],
            [tuple(r) for r in c.execute(
                "SELECT item_id, quantity, quality FROM group_inventory ORDER BY id;")],
        ))
        c.close()
    assert snaps[0] == snaps[1]
