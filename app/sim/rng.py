"""Deterministischer Zufall für den Sim-Kern.

Kein ``random`` — jeder "Wurf" ist eine reine Funktion von (world_seed, Kontext).
So bleibt jeder Tick über fixen Seed exakt reproduzierbar (CLAUDE.md: Tests
deterministisch). Gleiche Eingaben -> gleicher Wurf.
"""
from __future__ import annotations

import hashlib


def roll(seed: int, *parts: object) -> float:
    """Liefert einen reproduzierbaren Wurf in [0.0, 1.0).

    ``parts`` ist der Kontext (z.B. "death", character_id, tick), der den Wurf
    eindeutig und wiederholbar macht.
    """
    raw = "|".join([str(seed), *(str(p) for p in parts)]).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64
