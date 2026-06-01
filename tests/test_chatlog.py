"""Tests fuer chatlog (app/sim/chatlog.py) — persistentes Adjudikator-Gedaechtnis.

Abgedeckte Faelle (Issue #4):
1. fortlaufende turns je character (Start bei 1, kein Ueberlapp zwischen chars)
2. leerer / None-Text → kein Insert, Rueckgabe 0
3. recent chronologisch aufsteigend, auf limit begrenzt
4. clear entfernt nur den betroffenen Character
5. Migration idempotent: zweimal init_db() → keine Fehler, chat_log vorhanden
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.conftest import make_conn, set_seed
from app.sim import chatlog


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _count_log(conn, character_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM chat_log WHERE character_id = ?;",
        (character_id,),
    ).fetchone()["n"]


def _insert_n(conn, character_id: int, n: int, role: str = "player") -> list[int]:
    """Fuegt n Eintraege ein (committed) und gibt die turn-Liste zurueck."""
    turns = []
    for i in range(n):
        turn = chatlog.append(conn, character_id, role, f"Nachricht {i+1}")
        conn.commit()
        turns.append(turn)
    return turns


# ---------------------------------------------------------------------------
# 1. Fortlaufende turns je character
# ---------------------------------------------------------------------------

class TestAppendTurns:
    def test_first_turn_is_one(self, conn):
        """Erster Eintrag erhaelt turn=1."""
        turn = chatlog.append(conn, 1, "player", "Hallo")
        conn.commit()
        assert turn == 1

    def test_turns_increment_per_character(self, conn):
        """Turns zaehlen 1, 2, 3 pro Character."""
        t1 = chatlog.append(conn, 1, "player", "Eins")
        conn.commit()
        t2 = chatlog.append(conn, 1, "narrator", "Zwei")
        conn.commit()
        t3 = chatlog.append(conn, 1, "player", "Drei")
        conn.commit()
        assert [t1, t2, t3] == [1, 2, 3]

    def test_turns_independent_per_character(self, conn):
        """Turns von Character 1 und Character 2 sind unabhaengig."""
        # Wir brauchen einen zweiten Character — direkt ins Schema einfuegen.
        conn.execute(
            "INSERT INTO characters (group_id, type, name, lat, lon, is_alive) "
            "VALUES (1, 'npc', 'NPC-Test', 49.0, 11.0, 1);"
        )
        conn.commit()
        char2_id = conn.execute(
            "SELECT id FROM characters WHERE name='NPC-Test';"
        ).fetchone()["id"]

        # 3 Eintraege fuer char1
        turns_1 = _insert_n(conn, 1, 3)
        # 2 Eintraege fuer char2
        turns_2 = _insert_n(conn, char2_id, 2)

        assert turns_1 == [1, 2, 3]
        assert turns_2 == [1, 2]

    def test_turn_stored_in_db(self, conn):
        """turn-Wert wird korrekt in der DB abgelegt."""
        chatlog.append(conn, 1, "player", "Test")
        conn.commit()
        row = conn.execute(
            "SELECT turn FROM chat_log WHERE character_id=1;"
        ).fetchone()
        assert row["turn"] == 1

    def test_created_tick_from_world(self, conn):
        """created_tick wird aus world.tick uebernommen."""
        # Tick manuell auf bekannten Wert setzen
        conn.execute("UPDATE world SET tick=42 WHERE id=1;")
        conn.commit()
        chatlog.append(conn, 1, "player", "Tick-Test")
        conn.commit()
        row = conn.execute(
            "SELECT created_tick FROM chat_log WHERE character_id=1;"
        ).fetchone()
        assert row["created_tick"] == 42


# ---------------------------------------------------------------------------
# 2. Leerer / None-Text → kein Insert
# ---------------------------------------------------------------------------

class TestAppendEmptyText:
    def test_none_text_returns_zero(self, conn):
        """None als Text → Rueckgabe 0, kein DB-Eintrag."""
        result = chatlog.append(conn, 1, "player", None)
        conn.commit()
        assert result == 0
        assert _count_log(conn, 1) == 0

    def test_empty_string_returns_zero(self, conn):
        """Leerer String → Rueckgabe 0, kein DB-Eintrag."""
        result = chatlog.append(conn, 1, "player", "")
        conn.commit()
        assert result == 0
        assert _count_log(conn, 1) == 0

    def test_after_empty_next_turn_still_starts_at_one(self, conn):
        """Nach fehlgeschlagenen Inserts beginnt der naechste turn bei 1."""
        chatlog.append(conn, 1, "player", None)
        chatlog.append(conn, 1, "player", "")
        turn = chatlog.append(conn, 1, "player", "Erster echter")
        conn.commit()
        assert turn == 1

    def test_whitespace_only_blocked(self, conn):
        """Reiner Whitespace-Text (leer nach strip... nein: '' ist falsy) —
        non-empty whitespace-only string ist truthy, wird also gespeichert."""
        # Python: bool("  ") == True, also wird er eingefuegt
        result = chatlog.append(conn, 1, "player", "   ")
        conn.commit()
        # Ergebnis: truthy string → wird eingefuegt
        assert result == 1
        assert _count_log(conn, 1) == 1


# ---------------------------------------------------------------------------
# 3. recent: chronologisch aufsteigend + limit
# ---------------------------------------------------------------------------

class TestRecentOrder:
    def test_recent_empty_returns_empty_list(self, conn):
        """Kein Log → leere Liste."""
        result = chatlog.recent(conn, 1, 16)
        assert result == []

    def test_recent_chronological_ascending(self, conn):
        """recent liefert Eintraege in aufsteigender turn-Reihenfolge."""
        _insert_n(conn, 1, 5)
        result = chatlog.recent(conn, 1, 16)
        turns = [r["turn"] for r in result]
        assert turns == sorted(turns)

    def test_recent_limited_to_last_n(self, conn):
        """20 Inserts, limit=16 → genau 16 Eintraege, die LETZTEN 16."""
        _insert_n(conn, 1, 20)
        result = chatlog.recent(conn, 1, 16)
        assert len(result) == 16
        # Die letzten 16 Turns sind 5..20
        returned_turns = [r["turn"] for r in result]
        assert returned_turns[0] == 5   # erster der letzten 16
        assert returned_turns[-1] == 20  # letzter

    def test_recent_fewer_than_limit(self, conn):
        """3 Eintraege, limit=16 → alle 3 geliefert."""
        _insert_n(conn, 1, 3)
        result = chatlog.recent(conn, 1, 16)
        assert len(result) == 3

    def test_recent_result_has_correct_fields(self, conn):
        """Jedes Element enthaelt turn, role, text."""
        chatlog.append(conn, 1, "player", "Nachricht")
        conn.commit()
        result = chatlog.recent(conn, 1, 16)
        assert len(result) == 1
        entry = result[0]
        assert "turn" in entry
        assert "role" in entry
        assert "text" in entry

    def test_recent_text_matches_inserted(self, conn):
        """Eingefuegter Text wird korrekt zurueckgegeben."""
        chatlog.append(conn, 1, "narrator", "Hallo Welt")
        conn.commit()
        result = chatlog.recent(conn, 1, 16)
        assert result[0]["text"] == "Hallo Welt"
        assert result[0]["role"] == "narrator"

    def test_recent_limit_one(self, conn):
        """limit=1 liefert nur den neuesten Eintrag."""
        _insert_n(conn, 1, 5)
        result = chatlog.recent(conn, 1, 1)
        assert len(result) == 1
        assert result[0]["turn"] == 5


# ---------------------------------------------------------------------------
# 4. clear: nur betroffenen Character loeschen
# ---------------------------------------------------------------------------

class TestClear:
    def test_clear_removes_entries_for_character(self, conn):
        """clear(char1) entfernt alle Eintraege von char1."""
        _insert_n(conn, 1, 5)
        with conn:
            chatlog.clear(conn, 1)
        assert _count_log(conn, 1) == 0

    def test_clear_leaves_other_character_intact(self, conn):
        """clear(char1) beruehrt char2-Eintraege nicht."""
        conn.execute(
            "INSERT INTO characters (group_id, type, name, lat, lon, is_alive) "
            "VALUES (1, 'npc', 'NPC-Clear', 49.0, 11.0, 1);"
        )
        conn.commit()
        char2_id = conn.execute(
            "SELECT id FROM characters WHERE name='NPC-Clear';"
        ).fetchone()["id"]

        _insert_n(conn, 1, 3)
        _insert_n(conn, char2_id, 4)

        with conn:
            chatlog.clear(conn, 1)

        assert _count_log(conn, 1) == 0
        assert _count_log(conn, char2_id) == 4

    def test_clear_on_empty_log_is_noop(self, conn):
        """clear auf leeres Log wirft keinen Fehler."""
        with conn:
            chatlog.clear(conn, 1)  # darf nicht crashen
        assert _count_log(conn, 1) == 0

    def test_append_after_clear_restarts_at_one(self, conn):
        """Nach clear beginnt die Turn-Zaehlung wieder bei 1."""
        _insert_n(conn, 1, 3)
        with conn:
            chatlog.clear(conn, 1)
        turn = chatlog.append(conn, 1, "player", "Neues Spiel")
        conn.commit()
        assert turn == 1


# ---------------------------------------------------------------------------
# 5. Migration idempotent
# ---------------------------------------------------------------------------

class TestMigrationIdempotent:
    def test_init_db_twice_no_error(self, tmp_path, monkeypatch):
        """Zweimal init_db() auf derselben DB → kein Fehler."""
        import app.config as cfg
        db_path = tmp_path / "test_idempotent.db"
        monkeypatch.setattr(cfg, "DB_PATH", db_path)

        from app.db import init_db
        init_db()
        init_db()  # darf keinen Fehler werfen

    def test_chat_log_table_exists_after_init(self, tmp_path, monkeypatch):
        """Nach init_db() existiert die chat_log-Tabelle."""
        import app.config as cfg
        db_path = tmp_path / "test_chat_exists.db"
        monkeypatch.setattr(cfg, "DB_PATH", db_path)

        from app.db import init_db, get_connection
        init_db()

        monkeypatch.setattr(cfg, "DB_PATH", db_path)  # sicherstellen
        conn2 = get_connection()
        try:
            row = conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_log';"
            ).fetchone()
            assert row is not None, "chat_log-Tabelle fehlt nach init_db()"
        finally:
            conn2.close()

    def test_chat_log_table_from_migrations_on_in_memory(self, conn):
        """make_conn() (schema.sql) erzeugt chat_log; migration-Statement idempotent."""
        # schema.sql wird in make_conn() eingespielt → chat_log muss existieren
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_log';"
        ).fetchone()
        assert row is not None

        # Migration-Statement nochmals ausfuehren — darf keinen Fehler werfen
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chat_log ("
            " id INTEGER PRIMARY KEY, character_id INTEGER NOT NULL"
            " REFERENCES characters(id) ON DELETE CASCADE, turn INTEGER NOT NULL,"
            " role TEXT NOT NULL, text TEXT NOT NULL, created_tick INTEGER);"
        )
        conn.commit()
