"""Vertrag der LLM-Backends — offene Intention statt Verb-Klassifikation.

``interpret`` nimmt Free-Text + read-only Kontext und liefert ein
**Vorschlags-Objekt**: Verständnis, Machbarkeitsurteil, vorgeschlagene Effekte
(aus dem geschlossenen Ausführungs-Vokabular) und Narration. Das LLM *schlägt
vor*; der Sim-Kern validiert jeden Effekt hart und führt ihn aus.

Adjudikator v2: ``run_adjudication`` treibt eine Tool-Use-Schleife gegen eine
**Toolbox** (vom Sim-Kern bereitgestellt; hier nur als Duck-Type benutzt):
``begin_turn() -> bool`` (False = Iterationslimit), ``call(name, args) -> dict``
(führt Lese-/Vorschlags-Werkzeug aus; Vorschläge werden dort sofort validiert
und ggf. mit Grund abgelehnt, geschrieben wird nichts), ``specs()`` (Werkzeuge),
``finished`` (Ergebnis von ``finish`` oder None), ``allow_partial`` (Attribut),
``reset()``. Das Backend schreibt NIE selbst — es ruft nur Werkzeuge.

Proposal-Format (Einzelschuss ``interpret``):
{ understanding: str, feasibility: 'feasible'|'risky'|'too_complex'|'impossible',
  reason: str, effects: [ {op, ...params} ], narration: str }
"""
from __future__ import annotations

from typing import Any

# Geschlossenes Ausführungs-Vokabular (muss zu sim/effects.OPS passen).
EFFECT_OPS = (
    "move_to", "discover", "transfer", "consume_food", "prepare", "search",
    "advance_time", "transform", "establish_capability", "narrate",
    "fill_water", "barricade", "leave_note", "charge_device", "drink",
)

# Geschlossenes Intentions-Vokabular des Companions (muss zu sim/companion.INTENTS passen).
COMPANION_INTENTS = (
    "follow", "wait_here", "give_item", "ask_item", "share_food", "agree_join",
    "refuse", "leave", "share_info",
)

FEASIBILITY = ("feasible", "risky", "too_complex", "impossible")


class LLMBackend:
    name = "base"

    def interpret(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def run_adjudication(
        self, text: str, context: dict[str, Any], toolbox: Any, max_iterations: int = 8
    ) -> dict[str, Any]:
        """Tool-Use-Schleife gegen ``toolbox``. Standard: Einzelschuss über
        ``interpret`` (siehe ``single_shot_adjudication``) — Backends mit echtem
        Tool-Use überschreiben das. Liefert {"stop": "finish"|"limit"|"error"}."""
        return single_shot_adjudication(self, text, context, toolbox)

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

    def describe_survivor(
        self,
        identity: dict[str, Any],
        context: dict[str, Any],
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Persönlichkeit/Kurzbiografie zur prozeduralen Basis-Identität (#16).
        ``identity`` = {name, age, sex, profession, region}. Liefert
        {personality, short_bio, demeanor} (kurze Texte) oder {} (der Sim-Kern
        nimmt dann seinen deterministischen Default). Reiner Vorschlag."""
        return {}

    def companion_reply(
        self,
        persona: dict[str, Any],
        memory: list[str],
        context: dict[str, Any],
        player_text: str,
    ) -> dict[str, Any]:
        """Antwort des Companions auf den Spielertext. Liefert
        {speech: str, intents: [{intent, item_id?, qty?, topic?}]} — Intentionen
        aus ``COMPANION_INTENTS``. Das LLM behauptet keine Zustände; der Sim-Kern
        validiert jede Intention. Default: stumm."""
        return {"speech": "", "intents": []}

    def summarize_day(self, persona: dict[str, Any], entries: list[dict]) -> str:
        """Kompakte Tageszusammenfassung des Gesprächs (Text, kein Zustand).
        Default: leer (der Sim-Kern speichert dann nichts)."""
        return ""

    def match_items(
        self,
        query: str,
        location: dict[str, Any],
        candidates: list[dict[str, Any]],
        profile: dict | None = None,
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Gleicht eine Freitext-Suche gegen die **vorhandenen** Items eines Ortes
        ab. ``candidates`` = [{item_id, name, category, quantity}] aus dem vom
        Sim-Kern generierten Inventar. Liefert {item_ids: [...], narration}:
        ``item_ids`` darf NUR Kandidaten-IDs enthalten (der Sim-Kern verwirft
        alles andere); das LLM erfindet weder Items noch Mengen.
        Default: nichts gefunden."""
        return {"item_ids": [], "narration": ""}


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


def single_shot_adjudication(
    backend: Any, text: str, context: dict[str, Any], toolbox: Any
) -> dict[str, Any]:
    """Degradierter Pfad ohne Mehrschritt-Schleife: ``backend.interpret`` liefert
    einmal ein komplettes Proposal; jeder Effekt läuft trotzdem als Vorschlag
    durch die gestagte Validierung der Toolbox, danach ``finish``. Eine einzige
    Ablehnung verwirft den ganzen Plan (kein Umplanen -> ``allow_partial=False``).
    Gilt auch für Backends, die nur ``interpret`` kennen (Duck-Typing)."""
    toolbox.allow_partial = False
    if not toolbox.begin_turn():
        return {"stop": "limit"}
    p = backend.interpret(text, context)
    for eff in p.get("effects") or []:
        if not isinstance(eff, dict) or eff.get("op") not in EFFECT_OPS:
            continue
        args = {k: v for k, v in eff.items() if k != "op"}
        toolbox.call("propose_" + eff["op"], args)
    toolbox.call("finish", {
        "understanding": p.get("understanding", ""),
        "feasibility": p.get("feasibility", "feasible"),
        "reason": p.get("reason", ""),
        "narration": p.get("narration", ""),
    })
    return {"stop": "finish"}
