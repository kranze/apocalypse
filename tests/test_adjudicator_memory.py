"""Tests fuer Adjudikator-Gedaechtnis (Issue #4).

Abgedeckte Faelle:
c) Adjudikator: nach adjudicate() mit Stub stehen player+narrator im chat_log;
   build_context enthaelt danach history mit den erwarteten Eintraegen.
d) History-Fenster: nach >16 Turns liefert build_context["history"] hoechstens 16
   Eintraege, chronologisch aufsteigend.
e) Backend-Signaturen: search_item/narrate_location akzeptieren history=[...] ohne
   Fehler; Stub liefert bei gleicher Eingabe gleiche Ausgabe unabhaengig von history.
f) Determinismus (existierendes Muster): zwei identische Laeufe → gleicher chat_log-Zustand.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["WASTELAND_LLM_BACKEND"] = "stub"
import app.llm as llm_mod
llm_mod.reset_backend()

from tests.conftest import make_conn, set_seed, insert_location
from app.sim import adjudicator, chatlog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def force_stub():
    os.environ["WASTELAND_LLM_BACKEND"] = "stub"
    llm_mod.reset_backend()
    yield
    llm_mod.reset_backend()


@pytest.fixture
def conn():
    c = make_conn()
    set_seed(c, 1337)
    c.execute("UPDATE characters SET lat=49.0, lon=11.0, hunger=0.5 WHERE id=1;")
    c.commit()
    yield c
    c.close()


def _count_log(conn, character_id: int = 1) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM chat_log WHERE character_id=?;",
        (character_id,),
    ).fetchone()["n"]


# ---------------------------------------------------------------------------
# c) Adjudikator schreibt player + narrator nach adjudicate()
# ---------------------------------------------------------------------------

class TestAdjudicatorWritesChatLog:
    def test_adjudicate_writes_player_entry(self, conn):
        """Nach adjudicate() existiert ein chat_log-Eintrag mit role='player'."""
        adjudicator.adjudicate(conn, 1, "schau dich um")
        row = conn.execute(
            "SELECT * FROM chat_log WHERE character_id=1 AND role='player';"
        ).fetchone()
        assert row is not None, "Kein player-Eintrag im chat_log nach adjudicate()"

    def test_adjudicate_writes_narrator_entry(self, conn):
        """Nach adjudicate() existiert ein chat_log-Eintrag mit role='narrator'."""
        adjudicator.adjudicate(conn, 1, "schau dich um")
        row = conn.execute(
            "SELECT * FROM chat_log WHERE character_id=1 AND role='narrator';"
        ).fetchone()
        assert row is not None, "Kein narrator-Eintrag im chat_log nach adjudicate()"

    def test_adjudicate_writes_player_text(self, conn):
        """Der player-Eintrag enthaelt den Originaltext des Spielers."""
        text = "schau dich um"
        adjudicator.adjudicate(conn, 1, text)
        row = conn.execute(
            "SELECT text FROM chat_log WHERE character_id=1 AND role='player';"
        ).fetchone()
        assert row is not None
        assert row["text"] == text

    def test_adjudicate_writes_two_entries_per_call(self, conn):
        """Jeder adjudicate()-Aufruf fuegt genau 2 Eintraege hinzu (player+narrator)."""
        adjudicator.adjudicate(conn, 1, "schau dich um")
        assert _count_log(conn) == 2

    def test_adjudicate_multiple_calls_accumulate(self, conn):
        """Mehrere adjudicate()-Aufrufe akkumulieren Eintraege."""
        adjudicator.adjudicate(conn, 1, "schau dich um")
        adjudicator.adjudicate(conn, 1, "warte")
        assert _count_log(conn) == 4

    def test_build_context_contains_history(self, conn):
        """build_context enthaelt nach adjudicate() das Feld 'history'."""
        adjudicator.adjudicate(conn, 1, "schau dich um")
        ctx = adjudicator.build_context(conn, 1)
        assert "history" in ctx, "build_context fehlt 'history'-Feld"

    def test_build_context_history_has_entries(self, conn):
        """build_context['history'] enthaelt nach adjudicate() mindestens 2 Eintraege."""
        adjudicator.adjudicate(conn, 1, "schau dich um")
        ctx = adjudicator.build_context(conn, 1)
        assert len(ctx["history"]) >= 2

    def test_build_context_history_contains_player_role(self, conn):
        """history in build_context enthaelt einen player-Eintrag."""
        adjudicator.adjudicate(conn, 1, "schau dich um")
        ctx = adjudicator.build_context(conn, 1)
        roles = [e["role"] for e in ctx["history"]]
        assert "player" in roles

    def test_build_context_history_contains_narrator_role(self, conn):
        """history in build_context enthaelt einen narrator-Eintrag."""
        adjudicator.adjudicate(conn, 1, "schau dich um")
        ctx = adjudicator.build_context(conn, 1)
        roles = [e["role"] for e in ctx["history"]]
        assert "narrator" in roles

    def test_build_context_history_ascending_turn_order(self, conn):
        """history-Eintraege sind aufsteigend nach turn sortiert."""
        adjudicator.adjudicate(conn, 1, "schau dich um")
        adjudicator.adjudicate(conn, 1, "warte")
        ctx = adjudicator.build_context(conn, 1)
        turns = [e["turn"] for e in ctx["history"]]
        assert turns == sorted(turns)

    def test_adjudicate_empty_text_no_player_entry(self, conn):
        """Leerer Text → chatlog.append gibt 0 fuer player (kein player-Eintrag),
        aber der Narrator-Text ist nicht leer und wird trotzdem gespeichert."""
        adjudicator.adjudicate(conn, 1, "")
        player_row = conn.execute(
            "SELECT * FROM chat_log WHERE character_id=1 AND role='player';"
        ).fetchone()
        narrator_row = conn.execute(
            "SELECT * FROM chat_log WHERE character_id=1 AND role='narrator';"
        ).fetchone()
        # Leerer Player-Text → kein player-Eintrag
        assert player_row is None, "chatlog.append soll leeren Text nicht speichern"
        # Narrator-Text ist nicht leer → wird gespeichert
        assert narrator_row is not None

    def test_adjudicate_too_complex_writes_log(self, conn):
        """Bei too_complex wird trotzdem player+narrator geschrieben."""
        # Stub: "mobilfunk" → too_complex
        adjudicator.adjudicate(conn, 1, "reaktiviere das mobilfunk-netz")
        assert _count_log(conn) == 2


# ---------------------------------------------------------------------------
# d) History-Fenster: >16 Turns → hoechstens 16, chronologisch
# ---------------------------------------------------------------------------

class TestHistoryWindow:
    def test_build_context_history_max_16(self, conn):
        """Nach >16 Turns liefert build_context['history'] hoechstens 16 Eintraege."""
        # 10 adjudicate()-Aufrufe = 20 chat_log-Eintraege (je player+narrator)
        for _ in range(10):
            adjudicator.adjudicate(conn, 1, "warte")
        ctx = adjudicator.build_context(conn, 1)
        assert len(ctx["history"]) <= 16

    def test_build_context_history_exactly_16_when_more_exist(self, conn):
        """Wenn mehr als 16 Turns existieren, kommen genau 16 zurueck."""
        # 9 Aufrufe = 18 Eintraege > 16
        for _ in range(9):
            adjudicator.adjudicate(conn, 1, "warte")
        total = _count_log(conn)
        assert total == 18
        ctx = adjudicator.build_context(conn, 1)
        assert len(ctx["history"]) == 16

    def test_history_window_is_most_recent(self, conn):
        """Das Fenster zeigt die NEUESTEN 16 Turns (nicht die aeltesten)."""
        # 10 Aufrufe = turns 1..20
        for _ in range(10):
            adjudicator.adjudicate(conn, 1, "warte")
        ctx = adjudicator.build_context(conn, 1)
        # Neueste 16 Turns: 5..20
        assert ctx["history"][0]["turn"] == 5
        assert ctx["history"][-1]["turn"] == 20

    def test_history_window_chronological_with_many_entries(self, conn):
        """Auch bei groessem Log bleibt die Ausgabe aufsteigend geordnet."""
        for _ in range(12):
            adjudicator.adjudicate(conn, 1, "schau dich um")
        ctx = adjudicator.build_context(conn, 1)
        turns = [e["turn"] for e in ctx["history"]]
        assert turns == sorted(turns)


# ---------------------------------------------------------------------------
# e) Backend-Signaturen: history-Parameter akzeptiert, Stub deterministisch
# ---------------------------------------------------------------------------

class TestBackendSignatures:
    def test_search_item_accepts_history_param(self):
        """search_item(history=[...]) laeuft ohne Fehler."""
        backend = llm_mod.get_backend()
        history = [{"turn": 1, "role": "player", "text": "ich suche wasser"}]
        loc = {"type": "house", "name": "Test-Haus"}
        result = backend.search_item("wasser", loc, profile=None, history=history)
        assert isinstance(result, dict)

    def test_search_item_accepts_none_history(self):
        """search_item(history=None) laeuft ohne Fehler."""
        backend = llm_mod.get_backend()
        loc = {"type": "house", "name": "Test-Haus"}
        result = backend.search_item("wasser", loc, profile=None, history=None)
        assert isinstance(result, dict)

    def test_narrate_location_accepts_history_param(self):
        """narrate_location(history=[...]) laeuft ohne Fehler."""
        backend = llm_mod.get_backend()
        history = [{"turn": 1, "role": "narrator", "text": "Du siehst dich um."}]
        loc = {"type": "house", "name": "Test-Haus"}
        result = backend.narrate_location(loc, profile=None, history=history)
        assert isinstance(result, str)

    def test_narrate_location_accepts_none_history(self):
        """narrate_location(history=None) laeuft ohne Fehler."""
        backend = llm_mod.get_backend()
        loc = {"type": "house", "name": "Test-Haus"}
        result = backend.narrate_location(loc, profile=None, history=None)
        assert isinstance(result, str)

    def test_stub_search_item_deterministic_regardless_of_history(self):
        """Stub liefert bei gleicher Eingabe gleiche Ausgabe, egal ob history oder nicht."""
        backend = llm_mod.get_backend()
        loc = {"type": "house", "name": "Test-Haus"}

        result_no_history = backend.search_item("wasser", loc)
        result_with_history = backend.search_item(
            "wasser", loc,
            history=[{"turn": 1, "role": "player", "text": "ich suche wasser"}]
        )
        assert result_no_history["found"] == result_with_history["found"]
        assert result_no_history["narration"] == result_with_history["narration"]

    def test_stub_narrate_location_deterministic_regardless_of_history(self):
        """Stub-Narration ist gleich mit und ohne history."""
        backend = llm_mod.get_backend()
        loc = {"type": "supermarket", "name": "Test-Markt"}

        result_no_history = backend.narrate_location(loc)
        result_with_history = backend.narrate_location(
            loc,
            history=[{"turn": 2, "role": "narrator", "text": "Du siehst dich um."}]
        )
        assert result_no_history == result_with_history

    def test_stub_search_item_returns_found_and_item(self):
        """Stub gibt bei bekanntem Keyword + passendem Ortstyp found=True zurueck."""
        backend = llm_mod.get_backend()
        loc = {"type": "house"}
        result = backend.search_item("wasser", loc)
        assert result["found"] is True
        assert result["item"] is not None

    def test_stub_search_item_unknown_query_not_found(self):
        """Stub gibt bei unbekanntem Keyword found=False zurueck."""
        backend = llm_mod.get_backend()
        loc = {"type": "house"}
        result = backend.search_item("einhorn", loc)
        assert result["found"] is False


# ---------------------------------------------------------------------------
# f) Determinismus: zwei identische Laeufe → gleicher chat_log-Zustand
# ---------------------------------------------------------------------------

class TestChatLogDeterminism:
    def test_two_identical_runs_produce_same_log(self, conn_seeded):
        """Zwei DBs mit gleichem Seed und gleichen Aktionen → identischer chat_log."""
        def _setup(conn):
            conn.execute("UPDATE characters SET lat=49.0, lon=11.0 WHERE id=1;")
            conn.commit()

        c1 = conn_seeded("_clog1")
        c2 = conn_seeded("_clog2")
        _setup(c1)
        _setup(c2)

        actions = ["warte", "schau dich um", "warte"]
        for act in actions:
            adjudicator.adjudicate(c1, 1, act)
            adjudicator.adjudicate(c2, 1, act)

        log1 = c1.execute(
            "SELECT turn, role, text FROM chat_log WHERE character_id=1 ORDER BY turn;"
        ).fetchall()
        log2 = c2.execute(
            "SELECT turn, role, text FROM chat_log WHERE character_id=1 ORDER BY turn;"
        ).fetchall()

        assert len(log1) == len(log2), "Anzahl chat_log-Eintraege unterschiedlich"
        for r1, r2 in zip(log1, log2):
            assert r1["turn"] == r2["turn"]
            assert r1["role"] == r2["role"]
            assert r1["text"] == r2["text"]

        c1.close()
        c2.close()
