"""LLM-Abstraktionsschicht.

Eisernes Prinzip (CLAUDE.md / DESIGN.md §2): Diese Schicht bekommt nur einen
read-only Kontext und liefert *Intentionen* bzw. *Urteile* als dict zurück. Sie
importiert KEINE DB-/Schreibfunktion und kann nichts am Weltzustand ändern. Nur
der Sim-Kern (Adjudikator → validierte Sim-Funktionen) schreibt.

``get_backend()`` wählt automatisch: echtes Claude, wenn ``ANTHROPIC_API_KEY``
gesetzt ist, sonst der deterministische Regel-Stub. Override per
``WASTELAND_LLM_BACKEND=stub|claude|auto``.
"""
from __future__ import annotations

import os

from .base import EFFECT_OPS, LLMBackend
from .stub import RuleBackend

_backend: LLMBackend | None = None


def get_backend() -> LLMBackend:
    """Liefert das (prozessweit gecachte) LLM-Backend gemäß Konfiguration."""
    global _backend
    if _backend is not None:
        return _backend

    choice = os.environ.get("WASTELAND_LLM_BACKEND", "auto").lower()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    if choice == "stub" or (choice == "auto" and not has_key):
        _backend = RuleBackend()
    else:
        # Lazy importieren, damit eine Stub-only-Installation kein anthropic braucht.
        from .claude import ClaudeBackend

        _backend = ClaudeBackend()
    return _backend


def reset_backend() -> None:
    """Cache zurücksetzen (Tests/Backend-Wechsel)."""
    global _backend
    _backend = None


__all__ = ["LLMBackend", "RuleBackend", "EFFECT_OPS", "get_backend", "reset_backend"]
