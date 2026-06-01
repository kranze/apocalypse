"""Vertrag der LLM-Backends — offene Intention statt Verb-Klassifikation.

``interpret`` nimmt Free-Text + read-only Kontext und liefert ein
**Vorschlags-Objekt**: Verständnis, Machbarkeitsurteil, vorgeschlagene Effekte
(aus dem geschlossenen Ausführungs-Vokabular) und Narration. Das LLM *schlägt
vor*; der Sim-Kern validiert jeden Effekt hart und führt ihn aus.

Proposal-Format:
{ understanding: str, feasibility: 'feasible'|'risky'|'too_complex'|'impossible',
  reason: str, effects: [ {op, ...params} ], narration: str }
"""
from __future__ import annotations

from typing import Any

# Geschlossenes Ausführungs-Vokabular (muss zu sim/effects.OPS passen).
EFFECT_OPS = (
    "move_to", "discover", "transfer", "consume_food", "prepare", "search",
    "advance_time", "transform", "establish_capability", "narrate",
)

FEASIBILITY = ("feasible", "risky", "too_complex", "impossible")


class LLMBackend:
    name = "base"

    def interpret(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def narrate_location(
        self,
        location: dict[str, Any],
        profile: dict | None = None,
        history: list[dict] | None = None,
    ) -> str:
        """Kurze, atmosphärische Beschreibung eines erreichten Ortes (Flavor,
        keine Mechanik). ``history`` ist der jüngste Gesprächsverlauf
        (aus ``chatlog.recent``); Default: None (rückwärtskompatibel)."""
        return ""

    def search_item(
        self,
        query: str,
        location: dict[str, Any],
        profile: dict | None = None,
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Beurteilt, ob ``query`` an diesem Ort plausibel zu finden ist, und
        erfindet ggf. ein konkretes Item. Liefert {found, narration, item?},
        item = {name, category, weight_kg, kcal_per_unit?, decay_halflife_min?,
        quantity}. ``history`` ist optional (rückwärtskompatibel, Default None).
        Default: nichts gefunden."""
        return {"found": False, "narration": "", "item": None}


def proposal(
    *, understanding="", feasibility="feasible", reason="", effects=None, narration=""
) -> dict[str, Any]:
    return {
        "understanding": understanding,
        "feasibility": feasibility,
        "reason": reason,
        "effects": effects or [],
        "narration": narration,
    }
