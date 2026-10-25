"""LLM-Status: Zählung echter Aufrufe vs. Rückfälle (offline, Fake-Clients)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.llm import reset_backend, status
from app.llm.claude import ClaudeBackend


class _FailingClient:
    def __init__(self):
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        raise RuntimeError("API kaputt")


class _TextClient:
    def __init__(self, text="Ein stiller Ort."):
        self._text = text
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)])


def _backend(client) -> ClaudeBackend:
    b = ClaudeBackend.__new__(ClaudeBackend)  # ohne anthropic-Client
    from app.llm.stub import RuleBackend
    b._client = client
    b._fallback = RuleBackend()
    return b


@pytest.fixture(autouse=True)
def _clean():
    status.reset()
    yield
    status.reset()
    reset_backend()


def test_failure_counts_fallback_and_last_error():
    b = _backend(_FailingClient())
    txt = b.narrate_location({"type": "house", "label": "Haus", "name": None})
    assert txt  # Stub-Text
    s = status.snapshot("claude")
    assert s["fallbacks"] == 1 and s["calls_ok"] == 0
    assert "narrate_location" in s["last_error"] and "API kaputt" in s["last_error"]
    assert s["last_error_at"]
    assert s["by_method"]["narrate_location"]["fallbacks"] == 1
    assert status.thread_fallbacks() == 1


def test_success_counts_ok():
    b = _backend(_TextClient())
    assert b.narrate_location({"type": "house", "label": "Haus", "name": None}) == "Ein stiller Ort."
    s = status.snapshot("claude")
    assert s["calls_ok"] == 1 and s["fallbacks"] == 0 and s["last_error"] is None


def test_empty_answer_is_fallback():
    b = _backend(_TextClient(text="  "))
    b.narrate_location({"type": "house", "label": "Haus", "name": None})
    assert status.snapshot("claude")["fallbacks"] == 1


def test_endpoint_shape_and_stub(monkeypatch):
    monkeypatch.setenv("WASTELAND_LLM_BACKEND", "stub")
    reset_backend()
    from app.main import app
    c = TestClient(app)
    j = c.get("/llm/status").json()
    assert {"backend", "calls_ok", "fallbacks", "last_error", "last_error_at"} <= set(j)
    assert j["backend"] == "stub" and j["fallbacks"] == 0 and j["calls_ok"] == 0
    assert j["last_error"] is None
