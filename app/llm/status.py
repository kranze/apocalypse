"""Prozessweiter LLM-Status: echte Aufrufe vs. stille Rückfälle auf den Stub.

Reine Beobachtung (Eisernes Prinzip unberührt): kein DB-Zugriff, kein Einfluss
auf Weltzustand. Die Backends melden hier jeden erfolgreichen Aufruf und jeden
Rückfall (Ausnahme / unbrauchbare Antwort); ``GET /llm/status`` zeigt es an.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("wasteland.llm")

_lock = threading.Lock()
_ok: dict[str, int] = {}
_fallbacks: dict[str, int] = {}
_last_error: str | None = None
_last_error_at: str | None = None
# Pro Thread: Rückfälle seit Reset — damit ein Request erkennt, ob sein Text vom Stub kam.
_local = threading.local()


def record_ok(method: str) -> None:
    with _lock:
        _ok[method] = _ok.get(method, 0) + 1


def record_fallback(method: str, reason: Any) -> None:
    """Rückfall auf den Stub festhalten (zählen, loggen, letzten Fehler merken)."""
    global _last_error, _last_error_at
    if isinstance(reason, BaseException):
        text = f"{type(reason).__name__}: {reason}".strip().rstrip(":")
    else:
        text = str(reason)
    text = text[:300]
    with _lock:
        _fallbacks[method] = _fallbacks.get(method, 0) + 1
        _last_error = f"{method}: {text}"
        _last_error_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _local.count = getattr(_local, "count", 0) + 1
    log.warning("LLM-Rückfall auf Stub in %s: %s", method, text)


def thread_fallbacks() -> int:
    """Zähler der Rückfälle im aktuellen Thread (für ``llm_fallback``-Markierung)."""
    return getattr(_local, "count", 0)


def snapshot(backend: str) -> dict[str, Any]:
    with _lock:
        return {
            "backend": backend,
            "calls_ok": sum(_ok.values()),
            "fallbacks": sum(_fallbacks.values()),
            "by_method": {m: {"ok": _ok.get(m, 0), "fallbacks": _fallbacks.get(m, 0)}
                          for m in sorted(set(_ok) | set(_fallbacks))},
            "last_error": _last_error,
            "last_error_at": _last_error_at,
        }


def reset() -> None:
    """Zähler zurücksetzen (Tests)."""
    global _last_error, _last_error_at
    with _lock:
        _ok.clear()
        _fallbacks.clear()
        _last_error = _last_error_at = None
    _local.count = 0
