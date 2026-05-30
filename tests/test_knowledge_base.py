"""Tests für die Knowledge Base (app/sim/kb.py).

Prüft: lookup/list_topic mit dem Schema-Seed; Provenance-Vorrang (curated > player_verified
> llm_inferred); add überschreibt nur wenn gleiche oder höhere Provenance.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.conftest import make_conn, set_seed
from app.sim import kb


@pytest.fixture
def conn():
    c = make_conn()
    set_seed(c, 1337)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 1. Schema-Seed: provides:heat/firewood ist curated
# ---------------------------------------------------------------------------

class TestSchemaSeed:
    def test_lookup_firewood_exists(self, conn):
        fact = kb.lookup(conn, "provides:heat", "firewood")
        assert fact is not None

    def test_lookup_firewood_provenance_curated(self, conn):
        fact = kb.lookup(conn, "provides:heat", "firewood")
        assert fact["provenance"] == "curated"

    def test_lookup_firewood_value_has_consume(self, conn):
        fact = kb.lookup(conn, "provides:heat", "firewood")
        assert isinstance(fact["value"], dict)
        assert "consume" in fact["value"]

    def test_list_topic_provides_heat(self, conn):
        facts = kb.list_topic(conn, "provides:heat")
        assert len(facts) >= 1

    def test_list_topic_contains_firewood(self, conn):
        facts = kb.list_topic(conn, "provides:heat")
        keys = [f["key"] for f in facts]
        assert "firewood" in keys

    def test_lookup_nonexistent_returns_none(self, conn):
        fact = kb.lookup(conn, "provides:heat", "nonexistent_item")
        assert fact is None

    def test_list_topic_empty_for_unknown(self, conn):
        facts = kb.list_topic(conn, "completely_unknown_topic")
        assert facts == []


# ---------------------------------------------------------------------------
# 2. add — neue Fakten
# ---------------------------------------------------------------------------

class TestAdd:
    def test_add_new_fact_player_verified(self, conn):
        with conn:
            result = kb.add(conn, "provides:heat", "candle", {"consume": 2}, "player_verified", 1)
        assert result is True
        fact = kb.lookup(conn, "provides:heat", "candle")
        assert fact is not None
        assert fact["provenance"] == "player_verified"

    def test_add_new_fact_llm_inferred(self, conn):
        with conn:
            result = kb.add(conn, "provides:heat", "lighter", {"consume": 1}, "llm_inferred", 1)
        assert result is True
        fact = kb.lookup(conn, "provides:heat", "lighter")
        assert fact is not None

    def test_add_new_topic(self, conn):
        with conn:
            kb.add(conn, "weapon_type", "crowbar", {"damage": 5}, "player_verified", 0)
        fact = kb.lookup(conn, "weapon_type", "crowbar")
        assert fact is not None

    def test_add_returns_value_decoded(self, conn):
        with conn:
            kb.add(conn, "provides:heat", "torch", {"consume": 0.5}, "player_verified", 2)
        fact = kb.lookup(conn, "provides:heat", "torch")
        assert isinstance(fact["value"], dict)
        assert fact["value"]["consume"] == 0.5


# ---------------------------------------------------------------------------
# 3. Provenance-Vorrang
# ---------------------------------------------------------------------------

class TestProvenanceRank:
    def test_player_verified_does_not_overwrite_curated(self, conn):
        """player_verified < curated: darf curated nicht überschreiben."""
        with conn:
            result = kb.add(conn, "provides:heat", "firewood", {"consume": 999}, "player_verified", 5)
        assert result is False  # abgelehnt
        fact = kb.lookup(conn, "provides:heat", "firewood")
        assert fact["provenance"] == "curated"
        assert fact["value"]["consume"] != 999

    def test_llm_inferred_does_not_overwrite_curated(self, conn):
        """llm_inferred < curated: darf curated nicht überschreiben."""
        with conn:
            result = kb.add(conn, "provides:heat", "firewood", {"consume": 0}, "llm_inferred", 5)
        assert result is False
        fact = kb.lookup(conn, "provides:heat", "firewood")
        assert fact["provenance"] == "curated"

    def test_player_verified_overwrites_llm_inferred(self, conn):
        """player_verified > llm_inferred: darf llm_inferred überschreiben."""
        with conn:
            kb.add(conn, "provides:heat", "candle", {"consume": 1}, "llm_inferred", 1)
        with conn:
            result = kb.add(conn, "provides:heat", "candle", {"consume": 2}, "player_verified", 2)
        assert result is True
        fact = kb.lookup(conn, "provides:heat", "candle")
        assert fact["provenance"] == "player_verified"
        assert fact["value"]["consume"] == 2

    def test_llm_inferred_does_not_overwrite_player_verified(self, conn):
        """llm_inferred < player_verified: darf player_verified nicht überschreiben."""
        with conn:
            kb.add(conn, "provides:heat", "candle", {"consume": 2}, "player_verified", 1)
        with conn:
            result = kb.add(conn, "provides:heat", "candle", {"consume": 99}, "llm_inferred", 2)
        assert result is False
        fact = kb.lookup(conn, "provides:heat", "candle")
        assert fact["provenance"] == "player_verified"

    def test_player_verified_overwrites_player_verified(self, conn):
        """Gleiche Provenance: Überschreiben erlaubt."""
        with conn:
            kb.add(conn, "provides:heat", "candle", {"consume": 1}, "player_verified", 1)
        with conn:
            result = kb.add(conn, "provides:heat", "candle", {"consume": 3}, "player_verified", 2)
        assert result is True
        fact = kb.lookup(conn, "provides:heat", "candle")
        assert fact["value"]["consume"] == 3

    def test_curated_overwrites_player_verified(self, conn):
        """curated > player_verified: darf überschreiben."""
        with conn:
            kb.add(conn, "provides:heat", "candle", {"consume": 1}, "player_verified", 1)
        with conn:
            result = kb.add(conn, "provides:heat", "candle", {"consume": 5}, "curated", 2)
        assert result is True
        fact = kb.lookup(conn, "provides:heat", "candle")
        assert fact["provenance"] == "curated"


# ---------------------------------------------------------------------------
# 4. list_topic nach add
# ---------------------------------------------------------------------------

class TestListTopicAfterAdd:
    def test_list_topic_includes_added_facts(self, conn):
        with conn:
            kb.add(conn, "provides:heat", "campfire_kit", {"consume": 1}, "player_verified", 0)
        facts = kb.list_topic(conn, "provides:heat")
        keys = [f["key"] for f in facts]
        assert "campfire_kit" in keys
        assert "firewood" in keys

    def test_list_topic_sorted_by_key(self, conn):
        with conn:
            kb.add(conn, "test_topic", "zebra", "z", "player_verified", 0)
            kb.add(conn, "test_topic", "alpha", "a", "player_verified", 0)
        facts = kb.list_topic(conn, "test_topic")
        keys = [f["key"] for f in facts]
        assert keys == sorted(keys)
