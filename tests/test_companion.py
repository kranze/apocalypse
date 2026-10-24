"""Tests für den Companion (Slice M4, Issue #48; Profil-Anreicherung #16/#17).

Deterministisch (fixer Seed, Stub-LLM bzw. Fake-Backend, kein Netz,
Luftlinien-Routing). Prüft Kontakt, Zustandsmaschine, Gespräch,
Intentions-Validierung, Vertrauensregeln, Anschluss/Gehen/Tod, Profil und Gedächtnis.
"""
from __future__ import annotations

import os

import pytest

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
import app.osm.roads as roads_module
from app.db import _MIGRATIONS
from app.llm.stub import RuleBackend
from app.sim import (actions, audit, companion, constants, effects, game, looting, online,
                     survivors, tick)
from tests.conftest import insert_location, make_conn, set_seed
from tests.test_actions import HOME, add_group_item, build_world
from tests.test_devices_online import give_device, world_with_phone

llm_mod.reset_backend()
DAY = constants.MINUTES_PER_DAY


class _NoGraph:
    def nearest_node(self, lat, lon):
        return None


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    os.environ["WASTELAND_LLM_BACKEND"] = "stub"
    llm_mod.reset_backend()
    monkeypatch.setattr(roads_module, "get_graph", lambda *a, **k: _NoGraph())
    monkeypatch.setattr(roads_module, "ensure_roads_for_chunk", lambda *a, **k: None)
    yield
    llm_mod.reset_backend()


class FakeBackend(RuleBackend):
    """Stub mit festen Antworten und Aufruf-Zählern (kein Netz)."""

    def __init__(self, reply=None):
        self.reply = reply or {"speech": "Hm.", "intents": []}
        self.describe_calls = 0
        self.summary_calls = 0
        self.describe_result = None
        self.summary_text = "Kurz."

    def companion_reply(self, persona, memory, context, player_text):
        self.last_context = context
        self.last_memory = memory
        return self.reply

    def describe_survivor(self, identity, context, history=None):
        self.describe_calls += 1
        if self.describe_result is not None:
            return self.describe_result
        return super().describe_survivor(identity, context, history)

    def summarize_day(self, persona, entries):
        self.summary_calls += 1
        return self.summary_text


@pytest.fixture
def fake(monkeypatch):
    f = FakeBackend()
    monkeypatch.setattr(companion, "get_backend", lambda: f)
    monkeypatch.setattr(survivors, "get_backend", lambda: f, raising=False)
    import app.llm as llm
    monkeypatch.setattr(llm, "get_backend", lambda: f)
    return f


# --- Hilfen -------------------------------------------------------------------
def make_world(conn):
    """Spieler zu Hause; Treffpunkt = Supermarkt am Spielerstandort (Ort 100)."""
    build_world(conn)
    give_device(conn, "smartphone", 1.0)


def contact(conn):
    ev = companion.on_contact(conn, "reply", text="Eine Nachricht.")
    assert ev
    return ev


def run_until(conn, stage, hours=30):
    for _ in range(hours * 6):
        actions.advance_world(conn, 1, 10)
        if companion.get(conn)["stage"] == stage:
            return True
    return False


def meet(conn):
    make_world(conn)
    contact(conn)
    assert run_until(conn, "met")
    return companion.get(conn)


def trust(conn):
    return companion.get(conn)["trust"]


def scale(conn):
    row = companion.get(conn)
    return companion.positive_scale(companion.traits(conn, row["character_id"]))


def say(conn, text="Hallo"):
    return actions.perform(conn, 1, {"kind": "talk", "text": text})


def inv(conn, gid, item):
    return conn.execute(
        "SELECT COALESCE(SUM(quantity),0) AS q FROM group_inventory WHERE group_id=? AND item_id=?;",
        (gid, item)).fetchone()["q"]


def audit_clean(conn):
    t = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
    assert audit.run_audit(conn, t) == []


# --- Kontakt ------------------------------------------------------------------
def test_online_reply_creates_exactly_one_companion(conn):
    world_with_phone(conn, 1.0)
    replies = []
    for _ in range(14):
        conn.execute("UPDATE device_state SET charge = units;")
        conn.commit()
        r = actions.perform(conn, 1, {"kind": "search_survivors"})
        if not r["ok"]:
            break
        if r["result"]["reply"]:
            replies.append(r)
    assert len(replies) >= 2
    first = replies[0]
    assert any(i["severity"] == "decision" and i["message"].startswith("Jemand hat geantwortet")
               and "Treffpunkt: Markt" in i["message"] for i in first["interrupts"])
    rows = conn.execute("SELECT * FROM companion_state;").fetchall()
    assert len(rows) == 1
    assert rows[0]["stage"] in ("contacted", "approaching", "met")
    assert rows[0]["meeting_location_id"] == 100
    assert rows[0]["contact_source"] == "reply"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM characters WHERE type='survivor';").fetchone()["n"] == 1
    v = companion.view(conn)
    assert set(v) == {"character_id", "stage", "name", "age", "profession", "personality",
                      "short_bio", "lat", "lon", "needs", "trust", "opinion", "meeting_point"}
    assert v["meeting_point"]["location_id"] == 100
    assert set(v["needs"]) == {"hunger", "thirst", "sleep"}


def test_no_contact_no_companion(conn):
    make_world(conn)
    assert companion.view(conn) is None
    assert companion.chat(conn) == []
    assert actions.perform(conn, 1, {"kind": "talk", "text": "x"})["reason"] == "companion_not_here"


def test_contact_uses_existing_survivor_and_prefers_church(conn):
    build_world(conn)
    insert_location(conn, loc_id=102, loc_type="house", name="Kirche", lat=HOME[0] + 0.004,
                    lon=HOME[1])
    conn.execute("UPDATE locations SET label='Kirche' WHERE id=102;")
    conn.execute("DELETE FROM locations WHERE id=100;")
    conn.execute("INSERT INTO survivors (lat, lon, sex, birth_tick) VALUES (?, ?, 'f', -10512000);",
                 (HOME[0] + 0.01, HOME[1]))
    conn.commit()
    companion.on_contact(conn, "reply", text="x")
    row = companion.get(conn)
    assert row["meeting_location_id"] == 102
    # genau der vorhandene Survivor wurde materialisiert, es wurde keiner dazuerzeugt
    assert conn.execute("SELECT COUNT(*) AS n FROM survivors;").fetchone()["n"] == 1
    assert conn.execute("SELECT character_id FROM survivors;").fetchone()["character_id"] == \
        row["character_id"]


def test_note_found_by_nearby_survivor(conn):
    build_world(conn)
    conn.execute("INSERT INTO survivors (lat, lon, sex, birth_tick) VALUES (?, ?, 'm', -10512000);",
                 (HOME[0] + 0.003, HOME[1]))
    conn.commit()
    effects.apply_all(conn, 1, [{"op": "leave_note", "text": "Ich bin im Markt."}],
                      {"player": conn.execute("SELECT * FROM characters WHERE id=1;").fetchone()})
    actions.advance_world(conn, 1, 60)
    assert companion.get(conn) is None  # Verzögerung noch nicht erreicht
    actions.advance_world(conn, 1, 120)
    row = companion.get(conn)
    assert row is not None and row["contact_source"] == "note"


def test_beacon_contact_row_creates_companion(conn):
    make_world(conn)
    conn.execute("INSERT INTO online_contacts (tick, kind, text) VALUES (0, 'beacon', 'Signal');")
    conn.commit()
    r = tick.advance_tick(conn, 10)
    assert any(i["message"].startswith("Jemand hat geantwortet") for i in r["interrupts"])
    assert companion.get(conn)["contact_source"] == "beacon"
    assert conn.execute("SELECT COUNT(*) AS n FROM online_contacts "
                        "WHERE kind='beacon' AND handled=0;").fetchone()["n"] == 0


# --- Zustandsmaschine ---------------------------------------------------------
def test_approach_and_meet_via_ticks(conn):
    make_world(conn)
    contact(conn)
    assert companion.get(conn)["stage"] == "contacted"
    assert actions.perform(conn, 1, {"kind": "talk", "text": "hi"})["reason"] == "companion_not_here"
    seen = []
    t_before = trust(conn)
    for _ in range(300):
        r = actions.advance_world(conn, 1, 10)
        seen.append(companion.get(conn)["stage"])
        if seen[-1] == "met":
            events = r["events"]
            break
    assert "approaching" in seen and seen[-1] == "met"
    assert any(e["severity"] == "soft" and e["message"].endswith("ist am Treffpunkt.")
               for e in events)
    row = companion.get(conn)
    assert row["met_tick"] is not None
    assert trust(conn) > t_before
    assert companion.here(conn) is not None
    # Profil wurde einmalig angereichert
    assert conn.execute("SELECT COUNT(*) AS n FROM survivor_profile;").fetchone()["n"] == 1
    v = companion.view(conn)
    assert v["personality"] and v["short_bio"]
    assert v["trust"] == pytest.approx(row["trust"])


def test_available_actions_follow_stage(conn):
    make_world(conn)
    contact(conn)
    acts = actions.available(conn, 1)["actions"]
    walk = [a for a in acts if a["kind"] == "walk" and a["label"] == "Zum Treffpunkt gehen"]
    assert walk and walk[0]["location_id"] == 100
    assert not [a for a in acts if a["kind"] == "talk"]
    assert run_until(conn, "met")
    acts = actions.available(conn, 1)["actions"]
    assert [a for a in acts if a["kind"] == "talk"]
    assert not [a for a in acts if a.get("label") == "Zum Treffpunkt gehen"]


def test_companion_interrupts_survive_player_filter(conn):
    make_world(conn)
    contact(conn)
    msgs = []
    for _ in range(300):
        r = actions.advance_world(conn, 1, 10)
        msgs += [e["message"] for e in r["events"]]
        if companion.get(conn)["stage"] == "met":
            break
    assert any(m.endswith("ist am Treffpunkt.") for m in msgs)
    assert not any("hat das Ziel erreicht" in m for m in msgs)  # NPC-Ankunft bleibt leise


def test_death_sets_stage_dead_and_interrupt(conn):
    meet(conn)
    cid = companion.get(conn)["character_id"]
    conn.execute("UPDATE characters SET is_alive = 0 WHERE id = ?;", (cid,))
    conn.commit()
    r = actions.advance_world(conn, 1, 10)
    assert companion.get(conn)["stage"] == "dead"
    assert any(e["severity"] == "decision" and e["message"].endswith("ist gestorben.")
               for e in r["events"])
    assert companion.here(conn) is None


def test_leave_rule_low_trust_and_need(conn):
    meet(conn)
    cid = companion.get(conn)["character_id"]
    conn.execute("UPDATE companion_state SET trust = 0.0;")
    conn.execute("UPDATE characters SET hunger = 0.25 WHERE id = ?;", (cid,))
    conn.commit()
    r = actions.advance_world(conn, 1, 10)
    assert companion.get(conn)["stage"] == "left"
    assert any(e["severity"] == "decision" and e["message"].endswith("will gehen.")
               for e in r["events"])


def test_no_leave_when_trusted_even_if_hungry(conn):
    meet(conn)
    cid = companion.get(conn)["character_id"]
    conn.execute("UPDATE companion_state SET trust = 0.6;")
    conn.execute("UPDATE characters SET hunger = 0.25 WHERE id = ?;", (cid,))
    conn.commit()
    actions.advance_world(conn, 1, 10)
    assert companion.get(conn)["stage"] == "met"


def test_hunger_erodes_trust_by_rule(conn):
    meet(conn)
    cid = companion.get(conn)["character_id"]
    conn.execute("UPDATE companion_state SET trust = 0.5;")
    conn.execute("UPDATE characters SET hunger = 0.3 WHERE id = ?;", (cid,))
    conn.commit()
    actions.advance_world(conn, 1, 60)
    assert trust(conn) < 0.5


# --- Gespräch -----------------------------------------------------------------
def test_talk_requires_presence_and_text(conn):
    meet(conn)
    cid = companion.get(conn)["character_id"]
    assert say(conn, "")["reason"] == "no_text"
    conn.execute("UPDATE characters SET lat = lat + 0.01 WHERE id = 1;")  # ~1,1 km weg
    conn.commit()
    r = say(conn)
    assert r["ok"] is False and r["reason"] == "companion_not_here" and r["minutes_spent"] == 0
    assert not [a for a in actions.available(conn, 1)["actions"] if a["kind"] == "talk"]
    assert chat_roles(conn) == ["narrator", "narrator"]  # Kontakt + Treffen, kein Gespräch


def chat_roles(conn):
    return [e["role"] for e in companion.chat(conn)]


def test_talk_result_shape_and_chat_log(conn):
    meet(conn)
    t0 = trust(conn)
    r = say(conn, "Hallo, wer bist du?")
    assert r["ok"] and r["minutes_spent"] == constants.COMPANION_TALK_MINUTES
    res = r["result"]
    assert res["speech"] and res["intents_applied"] == [] and res["intents_rejected"] == []
    assert res["companion"]["character_id"] == companion.get(conn)["character_id"]
    chat = companion.chat(conn)
    assert [e["role"] for e in chat][-2:] == ["player", "companion"]
    assert chat[-1]["text"] == res["speech"] and "tick" in chat[-1]
    # nur die Gesprächs-Regel hat Vertrauen bewegt (kein Zustandsbehaupten)
    assert trust(conn) - t0 == pytest.approx(constants.COMPANION_TRUST_TALK * scale(conn), abs=1e-4)


def test_talk_trust_daily_cap(conn):
    meet(conn)
    t0 = trust(conn)
    for _ in range(8):
        say(conn, "Hallo")
    gained = trust(conn) - t0
    assert gained == pytest.approx(constants.COMPANION_TRUST_TALK_CAP * scale(conn), abs=1e-3)


def test_llm_claims_cannot_move_trust(conn, fake):
    meet(conn)
    fake.reply = {"speech": "Ich vertraue dir völlig!", "intents": [],
                  "trust": 1.0, "state": {"trust": 1.0}}
    t0 = trust(conn)
    say(conn, "Vertraust du mir?")
    assert trust(conn) - t0 == pytest.approx(constants.COMPANION_TRUST_TALK * scale(conn), abs=1e-4)


def test_stub_keyword_rules(conn):
    meet(conn)
    r = say(conn, "Komm mit mir")
    assert [i["intent"] for i in r["result"]["intents_rejected"]] == ["refuse"] or \
        [i["intent"] for i in r["result"]["intents_applied"]] == ["refuse"]
    assert companion.get(conn)["stage"] == "met"  # zu wenig Vertrauen
    r = say(conn, "Was war dein Beruf?")
    assert [i["topic"] for i in r["result"]["intents_applied"]] == ["profession"]
    assert any("war früher" in e["text"] for e in companion.chat(conn) if e["role"] == "narrator")


# --- Intentionen --------------------------------------------------------------
def test_intents_validated(conn, fake):
    meet(conn)
    conn.execute("UPDATE companion_state SET trust = 0.5;")
    conn.commit()
    fake.reply = {"speech": "x", "intents": [
        {"intent": "give_item", "item_id": "water_1l", "qty": 1},     # nicht im Besitz
        {"intent": "give_item", "item_id": "nope", "qty": 1},         # unbekannt
        {"intent": "teleport"},                                       # nicht im Vokabular
        {"intent": "share_info", "topic": "weltgeheimnis"},           # unbekanntes Thema
        {"intent": "share_food"},                                     # kein Essen
        {"intent": "follow"},                                         # ok
    ]}
    res = say(conn)["result"]
    reasons = {(i["intent"], i.get("reason")) for i in res["intents_rejected"]}
    assert ("give_item", "not_owned") in reasons and ("give_item", "unknown_item") in reasons
    assert ("teleport", "unknown_intent") in reasons
    assert ("share_info", "unknown_topic") in reasons and ("share_food", "no_food") in reasons
    assert [i["intent"] for i in res["intents_applied"]] == ["follow"]
    assert companion.get(conn)["mode"] == "follow"
    audit_clean(conn)


def test_too_many_intents_are_rejected(conn, fake):
    meet(conn)
    fake.reply = {"speech": "x", "intents": [{"intent": "refuse"}] * 9}
    res = say(conn)["result"]
    assert len(res["intents_applied"]) == 6
    assert [i["reason"] for i in res["intents_rejected"]] == ["too_many_intents"] * 3


def test_follow_needs_trust_before_join(conn, fake):
    meet(conn)
    conn.execute("UPDATE companion_state SET trust = 0.05;")
    conn.commit()
    fake.reply = {"speech": "x", "intents": [{"intent": "follow"}]}
    res = say(conn)["result"]
    assert res["intents_rejected"][0]["reason"] == "trust_too_low"


def test_player_give_and_share_food_ledger_clean(conn, fake):
    meet(conn)
    add_group_item(conn, "canned_beans", 2)
    t0 = trust(conn)
    r = actions.perform(conn, 1, {"kind": "give", "item_id": "canned_beans"})
    assert r["ok"]
    gid = companion.get(conn)["group_id"]
    assert inv(conn, gid, "canned_beans") == 1 and inv(conn, 1, "canned_beans") == 1
    assert trust(conn) - t0 == pytest.approx(constants.COMPANION_TRUST_GIVE * scale(conn), abs=1e-4)
    audit_clean(conn)
    fake.reply = {"speech": "Teilen wir.", "intents": [
        {"intent": "share_food"},
        {"intent": "give_item", "item_id": "canned_beans", "qty": 1}]}   # staged: schon vergeben
    res = say(conn)["result"]
    assert [i["intent"] for i in res["intents_applied"]] == ["share_food"]
    a = res["intents_applied"][0]
    assert a["item_id"] == "canned_beans" and a["item_name"] and a["qty"] == 1.0
    assert res["intents_rejected"] == [
        {"intent": "give_item", "item_id": "canned_beans", "item_name": a["item_name"],
         "qty": 1.0, "reason": "not_owned"}]
    assert inv(conn, 1, "canned_beans") == 2 and inv(conn, gid, "canned_beans") == 0
    audit_clean(conn)


def test_player_give_rejects_unowned_and_unknown(conn):
    meet(conn)
    assert actions.perform(conn, 1, {"kind": "give", "item_id": "canned_beans"})["reason"] == "not_owned"
    assert actions.perform(conn, 1, {"kind": "give", "item_id": "nope"})["reason"] == "unknown_item"


def test_multi_intent_apply_is_atomic(conn, fake, monkeypatch):
    meet(conn)
    gid = companion.get(conn)["group_id"]
    for item in ("canned_beans", "water_1l"):
        add_group_item(conn, item, 1)
        actions.perform(conn, 1, {"kind": "give", "item_id": item})
    before = (inv(conn, 1, "canned_beans"), inv(conn, gid, "canned_beans"),
              inv(conn, 1, "water_1l"), inv(conn, gid, "water_1l"))
    fake.reply = {"speech": "x", "intents": [
        {"intent": "give_item", "item_id": "canned_beans", "qty": 1},
        {"intent": "give_item", "item_id": "water_1l", "qty": 1}]}
    real = looting.transfer_between_groups
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(looting, "transfer_between_groups", flaky)
    res = say(conn)["result"]
    assert res["intents_applied"] == []
    assert {i["reason"] for i in res["intents_rejected"]} == {"apply_error"}
    after = (inv(conn, 1, "canned_beans"), inv(conn, gid, "canned_beans"),
             inv(conn, 1, "water_1l"), inv(conn, gid, "water_1l"))
    assert after == before
    audit_clean(conn)


def test_ask_item_records_request_without_transfer(conn, fake):
    meet(conn)
    add_group_item(conn, "water_1l", 1)
    fake.reply = {"speech": "Hast du Wasser?", "intents": [{"intent": "ask_item", "item_id": "water_1l"}]}
    res = say(conn)["result"]
    assert res["intents_applied"][0]["item_name"]
    assert companion.get(conn)["request_item"] == "water_1l"
    assert inv(conn, 1, "water_1l") == 1
    r = actions.perform(conn, 1, {"kind": "give", "item_id": "water_1l"})
    assert r["ok"] and companion.get(conn)["request_item"] is None


# --- Anschluss ----------------------------------------------------------------
@pytest.fixture
def friendly(monkeypatch):
    monkeypatch.setattr(companion, "traits",
                        lambda conn, cid: {"caution": 0.1, "sociability": 0.9, "optimism": 0.5})


def test_join_shares_group_inventory(conn, fake, friendly):
    meet(conn)
    add_group_item(conn, "canned_beans", 1)
    actions.perform(conn, 1, {"kind": "give", "item_id": "canned_beans"})
    old_gid = companion.get(conn)["group_id"]
    conn.execute("UPDATE companion_state SET trust = 0.8;")
    conn.commit()
    actions.advance_world(conn, 1, 10)  # Haltung wird im Tick berechnet
    assert companion.get(conn)["opinion"] == "go"
    fake.reply = {"speech": "Ich komme mit.", "intents": [{"intent": "agree_join"},
                                                          {"intent": "give_item", "item_id": "canned_beans"}]}
    res = say(conn)["result"]
    assert [i["intent"] for i in res["intents_applied"]] == ["agree_join"], res
    assert res["intents_rejected"][0]["reason"] == "shared_inventory"  # nach dem Anschluss
    row = companion.get(conn)
    assert row["stage"] == "joined" and row["mode"] == "follow" and row["joined_tick"] is not None
    cid = row["character_id"]
    assert conn.execute("SELECT group_id FROM characters WHERE id=?;", (cid,)).fetchone()[0] == 1
    assert inv(conn, 1, "canned_beans") == 1 and inv(conn, old_gid, "canned_beans") == 0
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE id=?;", (old_gid,)).fetchone()[0] == 0
    audit_clean(conn)
    assert actions.perform(conn, 1, {"kind": "give", "item_id": "canned_beans"})["reason"] == \
        "shared_inventory"
    # Tragekapazität der Gruppe wächst um ein lebendes Mitglied
    assert looting.carry_capacity(conn, 1) >= 2 * constants.CARRY_MIN_KG


def test_join_rejected_without_trust_or_when_he_prefers_to_stay(conn, fake, monkeypatch):
    meet(conn)
    fake.reply = {"speech": "ja", "intents": [{"intent": "agree_join"}]}
    conn.execute("UPDATE companion_state SET trust = 0.2;")
    conn.commit()
    assert say(conn)["result"]["intents_rejected"][0]["reason"] == "trust_too_low"
    conn.execute("UPDATE companion_state SET trust = 0.9, opinion = 'stay';")
    conn.commit()
    assert say(conn)["result"]["intents_rejected"][0]["reason"] == "prefers_to_stay"
    assert companion.get(conn)["stage"] == "met"


def test_joined_companion_follows_player(conn, fake, friendly):
    meet(conn)
    conn.execute("UPDATE companion_state SET trust = 0.8;")
    conn.commit()
    actions.advance_world(conn, 1, 10)
    fake.reply = {"speech": "ok", "intents": [{"intent": "agree_join"}]}
    say(conn)
    cid = companion.get(conn)["character_id"]

    def gap():
        return effects._dist(*[conn.execute("SELECT lat, lon FROM characters WHERE id=?;", (i,))
                               .fetchone()[k] for i in (1, cid) for k in (0, 1)][:2],
                             *[conn.execute("SELECT lat, lon FROM characters WHERE id=?;", (cid,))
                               .fetchone()[k] for k in (0, 1)])

    conn.execute("UPDATE characters SET lat = lat + 0.005 WHERE id = 1;")  # ~550 m weg
    conn.commit()
    start = gap()
    assert start > 400
    actions.advance_world(conn, 1, 30)
    assert gap() < start - 100
    # wait_here: er bleibt stehen
    fake.reply = {"speech": "ok", "intents": [{"intent": "wait_here"}]}
    conn.execute("UPDATE characters SET lat = ?, lon = ? WHERE id = 1;",
                 (conn.execute("SELECT lat FROM characters WHERE id=?;", (cid,)).fetchone()[0],
                  conn.execute("SELECT lon FROM characters WHERE id=?;", (cid,)).fetchone()[0]))
    conn.commit()
    say(conn)
    assert companion.get(conn)["mode"] == "wait"


def test_leave_intent_rules(conn, fake):
    meet(conn)
    fake.reply = {"speech": "Ich gehe.", "intents": [{"intent": "leave"}]}
    conn.execute("UPDATE companion_state SET trust = 0.8, opinion = 'stay';")
    conn.commit()
    assert say(conn)["result"]["intents_rejected"][0]["reason"] == "wants_to_stay"
    conn.execute("UPDATE companion_state SET trust = 0.1;")
    conn.commit()
    r = say(conn)
    assert [i["intent"] for i in r["result"]["intents_applied"]] == ["leave"]
    assert companion.get(conn)["stage"] == "left"
    assert any(i["message"].endswith("will gehen.") for i in r["interrupts"])


# --- Profil (#16) und Gedächtnis ---------------------------------------------
def test_profile_enrichment_once_and_clamped(conn, fake):
    fake.describe_result = {"personality": "x" * 5000, "short_bio": "y" * 5000,
                            "demeanor": "z" * 500}
    meet(conn)
    assert fake.describe_calls == 1
    cid = companion.get(conn)["character_id"]
    p = survivors.get_profile(conn, cid)
    assert len(p["personality"]) <= 300 and len(p["short_bio"]) <= 300 and len(p["demeanor"]) <= 40
    assert survivors.enrich_on_encounter(conn, cid)["personality"] == p["personality"]
    assert fake.describe_calls == 1
    assert conn.execute("SELECT COUNT(*) FROM survivor_profile;").fetchone()[0] == 1


def test_profile_fallback_when_backend_gives_nothing(conn, fake):
    fake.describe_result = {}
    meet(conn)
    p = survivors.get_profile(conn, companion.get(conn)["character_id"])
    assert p["personality"] and p["short_bio"] and p["demeanor"]


def test_enrich_unknown_or_non_survivor_returns_none(conn):
    assert survivors.enrich_on_encounter(conn, 1) is None
    assert survivors.enrich_on_encounter(conn, 999) is None


def test_stub_describe_survivor_deterministic():
    ident = {"name": "Anna Weiß", "age": 40, "profession": "Lehrerin", "region": "DE"}
    a = RuleBackend().describe_survivor(ident, {})
    assert a == RuleBackend().describe_survivor(ident, {})
    assert {"personality", "short_bio", "demeanor"} <= set(a)


def test_migrations_idempotent(conn):
    for _ in range(2):
        for stmt in _MIGRATIONS:
            conn.execute(stmt)
    conn.commit()
    for table in ("companion_state", "survivor_profile", "companion_memory"):
        conn.execute(f"SELECT * FROM {table};")


def test_daily_memory_summary_once_per_day(conn, fake):
    fake.summary_text = "s" * 2000
    meet(conn)
    say(conn, "Hallo")
    say(conn, "Noch etwas")
    assert fake.summary_calls == 0                      # heute nichts Vergangenes
    conn.execute("UPDATE world SET tick = tick + ?;", (DAY,))
    conn.commit()
    say(conn, "Guten Morgen")
    say(conn, "Und weiter")
    assert fake.summary_calls == 1                      # genau einmal am neuen Tag
    mem = conn.execute("SELECT day, text FROM companion_memory;").fetchall()
    assert len(mem) == 1 and mem[0]["day"] == 0 and len(mem[0]["text"]) <= constants.COMPANION_SUMMARY_MAX
    assert fake.last_memory == [mem[0]["text"]]
    conn.execute("UPDATE world SET tick = tick + ?;", (DAY,))
    conn.commit()
    say(conn, "Tag drei")
    assert fake.summary_calls == 2


def test_context_has_no_player_inventory(conn, fake):
    meet(conn)
    add_group_item(conn, "canned_beans", 3)
    say(conn)
    blob = str(fake.last_context)
    assert "canned_beans" not in blob and "inventory" in fake.last_context


# --- Neues Spiel, Determinismus ----------------------------------------------
def test_new_game_resets_companion(conn, monkeypatch, fake):
    from tests.test_new_game_reset import _FAKE_LAT, _FAKE_LON, _patch_all_osm
    meet(conn)
    say(conn, "Hallo")
    assert companion.get(conn) is not None
    _patch_all_osm(monkeypatch)
    res = game.new_game(conn, {"lat": _FAKE_LAT, "lon": _FAKE_LON})
    assert res["ok"]
    assert companion.view(conn) is None and companion.chat(conn) == []
    for table in ("companion_state", "survivor_profile", "companion_memory"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM characters WHERE type='survivor';").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE name='Begleiter';").fetchone()[0] == 0


def _scenario(c):
    set_seed(c, 777)
    make_world(c)
    contact(c)
    run_until(c, "met")
    add_group_item(c, "canned_beans", 1)
    actions.perform(c, 1, {"kind": "give", "item_id": "canned_beans"})
    say(c, "Hast du Wasser?")
    say(c, "Was war dein Beruf?")
    actions.advance_world(c, 1, 120)
    v = companion.view(c)
    return v, companion.chat(c), companion.get(c)


def test_same_seed_same_actions_same_companion(conn_seeded):
    a = _scenario(conn_seeded("x"))
    b = _scenario(conn_seeded("y"))
    assert a == b


def test_companion_group_not_player_group_before_join(conn):
    meet(conn)
    row = companion.get(conn)
    assert row["group_id"] != 1
    assert conn.execute("SELECT is_player_group FROM groups WHERE id=?;",
                        (row["group_id"],)).fetchone()[0] == 0


# --- Issue #52: Antworttext, Interrupt-Dopplung, deutsche Berufe -------------
def test_reply_text_fits_single_person_and_profile(conn):
    from app.sim import online
    make_world(conn)
    for plain in online._REPLIES:
        assert "zu zweit" not in plain and "Wir " not in plain
    ev = companion.on_contact(conn, "reply", text=online._REPLIES[0])
    assert len(ev) == 1
    assert "zu zweit" not in ev[0]["message"]
    prof = companion.view(conn)["profession"]
    assert prof in ev[0]["message"]


def test_professions_are_german():
    from app.sim.locale import professions, profession_de
    assert profession_de("Farmer / Agricultural Worker") == "Landwirt/in"
    assert profession_de("Freitext") == "Freitext"
    assert profession_de(None) is None
    # Determinismus: englische Schlüssel + Gewichte unverändert
    assert professions()[0] == ("Farmer / Agricultural Worker", 0.28)


def test_contact_yields_single_specific_interrupt(conn):
    make_world(conn)
    ev = companion.on_contact(conn, "reply", text="Eine Nachricht.")
    assert sum("Jemand hat geantwortet" in e["message"] for e in ev) == 1
