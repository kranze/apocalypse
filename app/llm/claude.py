"""Claude-Backend (Anthropic SDK) — offene Intention -> Vorschlags-Objekt.

Aktiv, sobald ``ANTHROPIC_API_KEY`` gesetzt ist. Ein Modell-Aufruf je Eingabe:
Claude versteht die freie Absicht, urteilt über Machbarkeit (zu ambitioniert ->
``too_complex``), schlägt Effekte aus dem geschlossenen Vokabular vor und
erzählt das Ergebnis. Der System-Prompt ist konstant (Prompt-Caching). Bei
API-Fehlern Fallback auf den deterministischen Stub.

Eisernes Prinzip: liefert nur einen Vorschlag (Effekte + Narration) — der
Sim-Kern validiert/führt aus und berechnet Folgen selbst.
"""
from __future__ import annotations

import json
from typing import Any

from .base import EFFECT_OPS, FEASIBILITY, LLMBackend, proposal
from .stub import RuleBackend

INTERPRET_MODEL = "claude-sonnet-4-6"

_SYSTEM = (
    "Du bist der Adjudikator einer realistischen Endzeit-Survival-Simulation. "
    "Der Spieler beschreibt frei, was er tun will. Deine Aufgabe:\n"
    "1) Verstehe die Absicht. 2) Urteile über die Machbarkeit mit der Haltung "
    "'erlaube mit Risiko' — kreative Lösungen sind erwünscht, aber maßlos "
    "Überkomplexes (z.B. 'das ganze Mobilfunknetz reaktivieren') ist "
    "'too_complex'. 3) Schlage NUR Effekte aus diesem geschlossenen Vokabular vor:\n"
    "- move_to{target}: zu einem Ort gehen\n"
    "- discover{target}: ein nahes Gebäude betreten/erkunden\n"
    "- transfer{target}: einen nahen Ort durchsuchen/plündern\n"
    "- consume_food: etwas essen\n"
    "- prepare: ein rohes Lebensmittel zubereiten (braucht Hitze+Wasser)\n"
    "- transform{consume[],produce[],requires[]}: etwas herstellen/umbauen\n"
    "- establish_capability{ctype,params,target}: etwas Dauerhaftes aufbauen "
    "(z.B. ctype='ssid_beacon' für ein Funk-Bake-Signal). Nutze nur ctypes, für "
    "die der Kontext ein Rezept nennt.\n"
    "- advance_time{minutes}: Zeit verstreichen lassen\n"
    "- narrate: rein erzählerisch, kein Weltzustand betroffen\n"
    "Berücksichtige das Profil des Charakters (Beruf, Bildung, Hobbys, "
    "Selbstbeschreibung) bei der Machbarkeit: Fachwissen macht manches "
    "plausibler (Elektriker → Strom, Funkamateur → Funk, Arzt → Behandlung), "
    "fehlendes Wissen macht es riskanter.\n"
    "Erfinde keine Ressourcen/Ergebnisse — der Simulationskern prüft jede "
    "Vorbedingung und berechnet Folgen selbst. Behaupte NIE, dass etwas "
    "gefunden/erreicht wurde. Schreibe eine knappe, atmosphärische Narration auf "
    "Deutsch. Antworte nur über das Werkzeug."
)

_TOOL = {
    "name": "emit_proposal",
    "description": "Verständnis, Machbarkeit, vorgeschlagene Effekte, Narration.",
    "input_schema": {
        "type": "object",
        "properties": {
            "understanding": {"type": "string"},
            "feasibility": {"type": "string", "enum": list(FEASIBILITY)},
            "reason": {"type": "string"},
            "narration": {"type": "string"},
            "effects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": list(EFFECT_OPS)},
                        "target": {"type": ["string", "null"]},
                        "item": {"type": ["string", "null"]},
                        "ctype": {"type": ["string", "null"]},
                        "minutes": {"type": ["number", "null"]},
                        "params": {"type": "object"},
                    },
                    "required": ["op"],
                },
            },
        },
        "required": ["understanding", "feasibility", "narration", "effects"],
    },
}


class ClaudeBackend(LLMBackend):
    name = "claude"

    def __init__(self) -> None:
        import anthropic

        self._client = anthropic.Anthropic()
        self._fallback = RuleBackend()

    def interpret(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        user = (
            f"Spieler-Eingabe: {text!r}\n\nKontext (nur reale Optionen):\n"
            + json.dumps(_compact(context), ensure_ascii=False)
        )
        try:
            resp = self._client.messages.create(
                model=INTERPRET_MODEL,
                max_tokens=700,
                system=[{"type": "text", "text": _SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": _TOOL["name"]},
                messages=[{"role": "user", "content": user}],
            )
            out = next((b.input for b in resp.content if b.type == "tool_use"), None)
        except Exception:
            return self._fallback.interpret(text, context)
        if not out:
            return self._fallback.interpret(text, context)

        feas = out.get("feasibility")
        if feas not in FEASIBILITY:
            feas = "feasible"
        effects = [e for e in (out.get("effects") or []) if e.get("op") in EFFECT_OPS]
        return proposal(
            understanding=out.get("understanding", ""),
            feasibility=feas,
            reason=out.get("reason", ""),
            effects=effects,
            narration=out.get("narration", ""),
        )


    def narrate_location(self, location, profile=None) -> str:
        sys = (
            "Du erzählst knapp und atmosphärisch (2–3 Sätze) den ersten Eindruck "
            "eines Ortes in einer verlassenen Endzeit-Welt, in der fast alle "
            "Menschen friedlich gestorben sind. Keine Spielmechanik, keine Mengen, "
            "kein Aufzählen von Gegenständen. Deutsch, zweite Person."
        )
        user = json.dumps({
            "ort": {"typ": location.get("type"), "name": location.get("name")},
            "grober_eindruck": location.get("inventory_summary"),
            "person": profile,
        }, ensure_ascii=False)
        try:
            resp = self._client.messages.create(
                model=INTERPRET_MODEL, max_tokens=300,
                system=[{"type": "text", "text": sys, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
            parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            return " ".join(parts).strip() or self._fallback.narrate_location(location, profile)
        except Exception:
            return self._fallback.narrate_location(location, profile)


def _compact(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "locations": [
            {"type": l.get("type"), "name": l.get("name"), "dist_m": l.get("dist_m"),
             "status": l.get("discovery_status")}
            for l in context.get("locations", [])[:20]
        ],
        "inventory": [
            {"item": i.get("item_id"), "qty": i.get("quantity")}
            for i in context.get("inventory", [])
        ],
        "capabilities": context.get("capabilities", []),
        "known_recipes": context.get("recipes", []),
        "providers": context.get("providers", {}),
        "profile": context.get("profile"),
    }
