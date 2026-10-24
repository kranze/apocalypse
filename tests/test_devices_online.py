"""Tests für Geräte-Akku, Online-Aktionen und Wissens-Downloads (Issue #46, Teil B).

Deterministisch (fixer Seed, kein Netz, Luftlinien-Routing).
"""
from __future__ import annotations

import os

import pytest

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
import app.osm.roads as roads_module
from app.sim import actions, audit, companion, constants, decay_world, devices, ledger, online
from app.sim import kb
from tests.conftest import insert_location
from tests.test_actions import add_group_item, build_world

llm_mod.reset_backend()

DAY = constants.MINUTES_PER_DAY


class _NoGraph:
    def nearest_node(self, lat, lon):
        return None


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(roads_module, "get_graph", lambda *a, **k: _NoGraph())
    monkeypatch.setattr(roads_module, "ensure_roads_for_chunk", lambda *a, **k: None)


def set_tick(conn, tick):
    conn.execute("UPDATE world SET tick = ? WHERE id = 1;", (tick,))
    conn.commit()


def give_device(conn, item_id, charge=1.0, qty=1):
    add_group_item(conn, item_id, qty)
    devices.grant(conn, 1, item_id, charge, units=qty)
    conn.commit()


def world_with_phone(conn, charge=0.8):
    build_world(conn)
    give_device(conn, "smartphone", charge)


def phone_pct(conn):
    return next(d["pct"] for d in devices.status(conn, 1) if d["item_id"] == "smartphone")


def finish_download(conn, pid, limit=60):
    """Lädt ein Paket in Chunks fertig (Akku wird jeweils aufgefüllt). Liefert
    (Antwort der letzten Aktion, Summe der Minuten)."""
    total = 0
    r = None
    for _ in range(limit):
        conn.execute("UPDATE device_state SET charge = units;")
        conn.commit()
        r = actions.perform(conn, 1, {"kind": "download", "package_id": pid})
        if not r["ok"]:
            break
        total += r["minutes_spent"]
        if r["result"]["done"]:
            break
    return r, total


def power_dead_tick(conn, loc_id=100):
    """Ein Tick, an dem am Ort sicher kein Strom mehr da ist (Tag 4)."""
    return 4 * DAY


# --- Akku -----------------------------------------------------------------
def test_catalog_has_devices(conn):
    ids = {r["id"] for r in conn.execute("SELECT id FROM item_catalog;")}
    assert {"smartphone", "laptop", "powerbank"} <= ids


def test_idle_drain_is_slow_and_deterministic(conn):
    world_with_phone(conn, 0.8)
    set_tick(conn, 24 * 60)
    pct = phone_pct(conn)
    expected = 80.0 - 100.0 * constants.DEVICE_DRAIN_IDLE_PER_H["smartphone"] * 24
    assert pct == pytest.approx(expected, abs=0.2)
    assert phone_pct(conn) == pct  # zweiter Zugriff ohne Zeitablauf ändert nichts


def test_online_action_drains_battery_by_duration(conn):
    world_with_phone(conn, 1.0)
    r = actions.perform(conn, 1, {"kind": "read_news"})
    assert r["ok"], r
    used = 100.0 - phone_pct(conn)
    expected = 100.0 * constants.DEVICE_DRAIN_ONLINE_PER_H["smartphone"] * r["minutes_spent"] / 60
    assert used == pytest.approx(expected, abs=1.0)
    assert 15 <= r["minutes_spent"] <= 30


def test_charge_requires_power_at_location(conn):
    world_with_phone(conn, 0.3)
    set_tick(conn, power_dead_tick(conn))
    assert decay_world.infra_for(conn, 100)["power"] <= constants.CHARGE_POWER_MIN
    r = actions.perform(conn, 1, {"kind": "charge"})
    assert not r["ok"] and r["reason"] == "no_power" and r["minutes_spent"] == 0
    assert all(a["kind"] != "charge" for a in actions.available(conn, 1)["actions"])


def test_charge_at_grid_fills_battery(conn):
    world_with_phone(conn, 0.3)
    assert decay_world.infra_for(conn, 100)["power"] > constants.CHARGE_POWER_MIN
    assert any(a["kind"] == "charge" for a in actions.available(conn, 1)["actions"])
    r = actions.perform(conn, 1, {"kind": "charge"})
    assert r["ok"] and r["result"]["source"] == "grid"
    assert phone_pct(conn) == pytest.approx(100.0 * min(
        1.0, 0.3 + constants.DEVICE_CHARGE_PER_H["smartphone"] * r["minutes_spent"] / 60), abs=1.5)
    assert r["minutes_spent"] >= constants.CHARGE_MIN_MINUTES


def test_charge_from_powerbank_transfers_charge(conn):
    world_with_phone(conn, 0.2)
    give_device(conn, "powerbank", 1.0)
    set_tick(conn, power_dead_tick(conn))
    devices.grant(conn, 1, "smartphone", 0.2)
    devices.grant(conn, 1, "powerbank", 1.0)
    conn.commit()
    r = actions.perform(conn, 1, {"kind": "charge"})
    assert r["ok"] and r["result"]["source"] == "powerbank", r
    st = {d["item_id"]: d["pct"] for d in devices.status(conn, 1)}
    assert st["smartphone"] > 95.0          # 0.2 + 0.8 (Defizit voll gefüllt)
    # Powerbank gibt 0.8 Smartphone-Ladungen ab = 0.4 ihrer Kapazität
    assert st["powerbank"] == pytest.approx(60.0, abs=1.0)


def test_empty_battery_blocks_online(conn):
    world_with_phone(conn, 0.0)
    r = actions.perform(conn, 1, {"kind": "read_news"})
    assert not r["ok"] and r["reason"] == "no_battery" and r["minutes_spent"] == 0
    assert all(a["kind"] not in ("read_news", "download") for a in actions.available(conn, 1)["actions"])


def test_no_device_blocks_online(conn):
    build_world(conn)
    r = actions.perform(conn, 1, {"kind": "read_news"})
    assert not r["ok"] and r["reason"] == "no_device"


# --- Netz über die Timeline ------------------------------------------------
def test_online_works_day0_fails_by_day6(conn):
    world_with_phone(conn, 1.0)
    assert any(a["kind"] == "read_news" for a in actions.available(conn, 1)["actions"])
    assert actions.perform(conn, 1, {"kind": "read_news"})["ok"]
    set_tick(conn, 6 * DAY)
    devices.grant(conn, 1, "smartphone", 1.0)
    conn.commit()
    r = actions.perform(conn, 1, {"kind": "read_news"})
    assert not r["ok"] and r["reason"] == "no_network"
    acts = actions.available(conn, 1)["actions"]
    assert all(a["kind"] not in ("read_news", "download", "search_survivors") for a in acts)


def test_network_quality_rule(conn):
    build_world(conn)
    # Tag 0: Internet 0.9; mit WLAN (Strom am Ort) 0.9, draußen nur Mobilfunk 0.6 * 0.9
    n = devices.network_quality(conn, 100)
    assert n["wifi"] and n["quality"] == pytest.approx(0.9)
    out = devices.network_quality(conn, None)
    assert not out["wifi"] and out["quality"] == pytest.approx(0.54)
    set_tick(conn, 4 * DAY)  # kein Strom -> kein WLAN; Mobilfunk 0.3 * Internet 0.15
    n4 = devices.network_quality(conn, 100)
    assert not n4["wifi"] and n4["quality"] == pytest.approx(0.045)
    assert not n4["online"]


# --- Nachrichten -------------------------------------------------------------
def test_news_fragments_curated_data():
    frags = online.news_fragments()
    assert 20 <= len(frags) <= 30
    assert len({f["id"] for f in frags}) == len(frags)
    assert {f["day_min"] for f in frags} >= {0, 1, 2, 3, 4, 5}


def read_all_news(conn, first_tick=0):
    seen = []
    for _ in range(40):
        conn.execute("UPDATE device_state SET charge = units;")
        conn.commit()
        r = actions.perform(conn, 1, {"kind": "read_news"})
        if not r["ok"]:
            break
        seen.append(r["result"]["fragment"]["id"])
    return seen


def test_news_deterministic_and_not_repeated(conn_seeded):
    runs = []
    for tag in ("a", "b"):
        c = conn_seeded(tag)
        world_with_phone(c, 1.0)
        set_tick(c, 2 * DAY)
        devices.grant(c, 1, "smartphone", 1.0)
        c.commit()
        runs.append(read_all_news(c))
    assert runs[0] == runs[1]
    assert len(runs[0]) == len(set(runs[0]))           # nie wiederholt
    day2 = {f["id"] for f in online.news_fragments() if f["day_min"] <= 2}
    assert set(runs[0]) == day2                       # nur bis zum aktuellen Tag veröffentlicht


# --- Überlebenden-Suche ------------------------------------------------------
def test_search_survivors_deterministic_contact(conn_seeded):
    outcomes = []
    for tag in ("a", "b"):
        c = conn_seeded(tag)
        world_with_phone(c, 1.0)
        seq = []
        for _ in range(12):
            c.execute("UPDATE device_state SET charge = units;")
            c.commit()
            r = actions.perform(c, 1, {"kind": "search_survivors"})
            if not r["ok"]:
                seq.append(r["reason"])
                break
            seq.append(bool(r["result"]["reply"]))
            if r["result"]["reply"]:
                assert any(i["severity"] == "decision" and "geantwortet" in i["message"]
                           for i in r["interrupts"])
        outcomes.append(seq)
        assert online.search_tries(c) >= 1
        # Die erste Antwort wird vom Companion (#48) verbraucht (handled = 1).
        consumed = 1 if companion.get(c) else 0
        assert len(online.pending_contacts(c)) == online.contact_count(c) - consumed
    assert outcomes[0] == outcomes[1]
    assert any(x is True for x in outcomes[0])         # steigende Chance -> irgendwann Antwort


def test_search_chance_rises_with_tries():
    assert online.search_chance(0, 0.9) < online.search_chance(3, 0.9) <= \
        constants.SEARCH_SURVIVORS_CAP
    assert online.search_chance(2, 0.1) < online.search_chance(2, 0.9)


# --- Downloads ---------------------------------------------------------------
def test_packages_catalog():
    ps = online.packages()
    assert len(ps) == 12
    assert {p["id"] for p in ps} >= {"water_treatment", "fire_cooking", "first_aid"}
    assert all(p["size_mb"] > 0 and p["title"] for p in ps)


def test_download_time_scales_with_quality(conn):
    build_world(conn)
    q_hi = online.minutes_needed(100.0, 0.9)
    q_lo = online.minutes_needed(100.0, 0.45)
    assert q_lo == pytest.approx(2 * q_hi, abs=2)
    give_device(conn, "smartphone", 1.0)
    r, total = finish_download(conn, "water_treatment")
    assert r["ok"] and r["result"]["done"]
    assert total == online.minutes_needed(50, 0.9) // 1 or abs(
        total - online.minutes_needed(50, 0.9)) <= 2


def test_partial_download_persists_and_completes_writes_kb(conn):
    world_with_phone(conn, 1.0)
    # Qualität drücken: draußen (kein WLAN, nur Mobilfunk) -> langsam, Chunk-Limit greift
    conn.execute("UPDATE characters SET lat = 50.0, lon = 12.0 WHERE id = 1;")  # kein Ort in Reichweite
    devices.grant(conn, 1, "smartphone", 1.0)
    conn.commit()
    r1 = actions.perform(conn, 1, {"kind": "download", "package_id": "first_aid"})
    assert r1["ok"] and not r1["result"]["done"]
    assert r1["stopped"] == "max"
    got = r1["result"]["downloaded_mb"]
    assert 0 < got < 150
    assert online.progress(conn, "first_aid")["downloaded_mb"] == pytest.approx(got, abs=0.01)
    assert not online.has_knowledge(conn, "first_aid")
    # Wiederaufnahme addiert, bis fertig
    r, _ = finish_download(conn, "first_aid", limit=80)
    assert r["ok"] and r["result"]["done"], (
        r["reason"], r["tick"], online.progress(conn, "first_aid"))
    assert online.has_knowledge(conn, "first_aid")
    assert kb.lookup(conn, "knowledge:first_aid", "first_aid")["provenance"] == "curated"
    r = actions.perform(conn, 1, {"kind": "download", "package_id": "first_aid"})
    assert not r["ok"] and r["reason"] == "already_downloaded"


def test_download_interrupted_by_empty_battery_keeps_progress(conn):
    world_with_phone(conn, 0.05)  # ~30 Min Online-Betrieb
    r = actions.perform(conn, 1, {"kind": "download", "package_id": "first_aid"})
    assert r["ok"] and not r["result"]["done"]
    assert r["minutes_spent"] <= 31
    assert online.progress(conn, "first_aid")["downloaded_mb"] > 0
    assert phone_pct(conn) < 1.0
    r2 = actions.perform(conn, 1, {"kind": "download", "package_id": "first_aid"})
    assert not r2["ok"] and r2["reason"] == "no_battery"


def test_download_unlocks_usable_recipes(conn):
    world_with_phone(conn, 1.0)
    assert kb.lookup(conn, "capability_recipe:cooking_fire", "cooking_fire") is None
    for pid in ("fire_cooking", "water_treatment"):
        r, _ = finish_download(conn, pid)
        assert r["ok"] and r["result"]["done"], r
    for ctype in ("cooking_fire", "water_treatment"):
        rec = kb.lookup(conn, f"capability_recipe:{ctype}", ctype)
        assert rec["provenance"] == "curated" and rec["value"]["requires"] == ["heat"]
    online.reset_online_state(conn)
    assert kb.lookup(conn, "capability_recipe:cooking_fire", "cooking_fire") is None
    assert not online.has_knowledge(conn, "fire_cooking")


def test_unknown_package_rejected(conn):
    world_with_phone(conn, 1.0)
    r = actions.perform(conn, 1, {"kind": "download", "package_id": "nope"})
    assert not r["ok"] and r["reason"] == "no_such_package"


def test_download_options_in_action_bar(conn):
    world_with_phone(conn, 1.0)
    dl = next(a for a in actions.available(conn, 1)["actions"] if a["kind"] == "download")
    assert len(dl["options"]) == 12
    o = dl["options"][0]
    assert {"package_id", "title", "size_mb", "minutes_total", "battery_pct"} <= set(o)


# --- Knappheit -------------------------------------------------------------------
def greedy_download(conn, online_budget_min=6 * 60):
    """Spieler holt pro Tag (Tag 0..4) höchstens 6 h Online-Zeit heraus (der Rest ist
    Schlafen, Essen, Plündern; Downloads sind schnell, #49-Balancing), nutzt Stromnetz und Powerbank zum Laden und nimmt
    die kleinsten Pakete zuerst."""
    order = sorted(online.packages(), key=lambda p: p["size_mb"])
    for day in range(0, 5):
        set_tick(conn, max(_tick(conn), day * DAY + 120))
        used = 0
        while used < online_budget_min:
            todo = [p for p in order if not online.progress(conn, p["id"])["done"]]
            if not todo:
                return
            r = actions.perform(conn, 1, {"kind": "download", "package_id": todo[0]["id"]})
            if r["ok"]:
                used += r["minutes_spent"]
                continue
            if r["reason"] == "no_battery":
                c = actions.perform(conn, 1, {"kind": "charge"})
                if not c["ok"]:
                    break
                used += c["minutes_spent"]
                continue
            break  # kein Netz


def _tick(conn):
    return conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]


def test_scarcity_cannot_download_everything_by_day5(conn):
    world_with_phone(conn, 0.8)
    give_device(conn, "powerbank", 1.0)
    greedy_download(conn)
    done = [p["id"] for p in online.packages() if online.progress(conn, p["id"])["done"]]
    assert 3 <= len(done) < len(online.packages())     # etwas geht, aber nicht alles
    total = sum(p["size_mb"] for p in online.packages())
    got = sum(p["size_mb"] for p in online.packages() if p["id"] in done)
    assert got < 0.8 * total


# --- Ledger / Audit --------------------------------------------------------------
def test_loot_of_devices_is_transfer_and_audit_clean(conn):
    build_world(conn)
    conn.execute(
        "INSERT INTO location_inventory (location_id, item_id, quantity, quality, produced_tick) "
        "VALUES (100, 'powerbank', 2, 1.0, 0);")
    ledger.add(conn, "powerbank", 2)
    conn.commit()
    from app.sim import looting
    looting.loot(conn, 100, 1)
    conn.commit()
    flagged = [e for e in audit.run_audit(conn, 1) if "powerbank" in e["message"]]
    assert not flagged
    st = devices.sync(conn, 1)
    assert st["powerbank"]["units"] == 2
    assert st["powerbank"]["charge"] == pytest.approx(2 * constants.DEVICE_FOUND_CHARGE)


def test_charge_is_not_a_ledger_good(conn):
    world_with_phone(conn, 0.2)
    before = ledger.expected_totals(conn)
    actions.perform(conn, 1, {"kind": "charge"})
    actions.perform(conn, 1, {"kind": "read_news"})
    assert ledger.expected_totals(conn) == before
    assert not [e for e in audit.run_audit(conn, _tick(conn)) if "Bilanz" in e["message"]]


def test_devices_in_loot_tables():
    from app.sim.loot_tables import LOOT_TABLES
    assert any(e["item_id"] == "laptop" for e in LOOT_TABLES["house"])
    assert any(e["item_id"] == "powerbank" for e in LOOT_TABLES["house"])
    assert any(e["item_id"] == "powerbank" for e in LOOT_TABLES["supermarket"])
    assert any(e["item_id"] == "powerbank" for e in LOOT_TABLES["fuel_station"])


# --- Spielstart / HUD ---------------------------------------------------------
def test_new_game_gives_charged_smartphone_and_resets_online(conn, monkeypatch):
    from app.sim import game, survivors
    monkeypatch.setattr(survivors, "spawn_survivors", lambda c: None)
    build_world(conn)
    conn.execute("INSERT INTO news_read VALUES ('x', 1);")
    conn.commit()
    r = game.new_game(conn, {"name": "T", "lat": 49.0, "lon": 11.0})
    assert r["ok"]
    gi = conn.execute(
        "SELECT quantity FROM group_inventory WHERE group_id=1 AND item_id='smartphone';"
    ).fetchone()
    assert gi["quantity"] == 1
    assert phone_pct(conn) == pytest.approx(100 * constants.DEVICE_START_CHARGE)
    assert conn.execute("SELECT COUNT(*) AS n FROM news_read;").fetchone()["n"] == 0
    assert not [e for e in audit.run_audit(conn, 0) if "Bilanz" in e["message"]]


def test_hud_infra_block(conn):
    world_with_phone(conn, 0.8)
    h = online.hud_infra(conn, 1, 100)
    assert h["scope"] == "here" and h["wifi"] and h["online"]
    assert {"phase", "power", "water", "cellular", "internet", "devices"} <= set(h)
    assert h["devices"][0]["item_id"] == "smartphone"
    w = online.hud_infra(conn, 1, None)
    assert w["scope"] == "world" and not w["wifi"]
