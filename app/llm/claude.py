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
    "- transfer{target}: an einem Ort bereits Gefundenes einsammeln/mitnehmen\n"
    "- search{query}: an dem Ort, an dem man steht, gezielt nach etwas suchen "
    "(z.B. 'Fernseher', 'Wasser', 'Werkzeug') — was es dort gibt, klärt der Kern\n"
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
                        "query": {"type": ["string", "null"]},
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
        # Gesprächsverlauf als echte messages-Vorgeschichte voranstellen:
        # player-Turns → role "user", narrator-Turns → role "assistant".
        # system-Einträge werden als kurze user-Notiz eingebettet.
        # Der System-Prompt (_SYSTEM) bleibt konstant für Prompt-Caching.
        history_msgs: list[dict] = []
        for turn in (context.get("history") or []):
            role = turn.get("role", "")
            t = turn.get("text", "")
            if not t:
                continue
            if role == "player":
                history_msgs.append({"role": "user", "content": t})
            elif role == "narrator":
                history_msgs.append({"role": "assistant", "content": t})
            elif role == "system":
                history_msgs.append({"role": "user", "content": f"[System: {t}]"})
        # Anthropic erwartet alternierend user/assistant; bei mehreren gleichen
        # Rollen in Folge das letzte behalten (kann durch system-Einbettung entstehen).
        merged: list[dict] = []
        for msg in history_msgs:
            if merged and merged[-1]["role"] == msg["role"]:
                merged[-1] = {"role": msg["role"],
                               "content": merged[-1]["content"] + "\n" + msg["content"]}
            else:
                merged.append(msg)

        user_msg = (
            f"Spieler-Eingabe: {text!r}\n\nKontext (nur reale Optionen):\n"
            + json.dumps(_compact(context), ensure_ascii=False)
        )
        # Stellt sicher, dass die letzte Nachricht role=user ist.
        if merged and merged[-1]["role"] == "user":
            merged[-1] = {"role": "user",
                           "content": merged[-1]["content"] + "\n\n" + user_msg}
            messages = merged
        else:
            messages = merged + [{"role": "user", "content": user_msg}]

        try:
            resp = self._client.messages.create(
                model=INTERPRET_MODEL,
                max_tokens=700,
                system=[{"type": "text", "text": _SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": _TOOL["name"]},
                messages=messages,
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


    def narrate_location(self, location, profile=None, history=None) -> str:
        sys = (
            "Du erzählst knapp und atmosphärisch (2–3 Sätze) den ersten Eindruck "
            "eines Ortes in einer verlassenen Endzeit-Welt, in der fast alle "
            "Menschen friedlich gestorben sind. Keine Spielmechanik, keine Mengen, "
            "kein Aufzählen von Gegenständen. Deutsch, zweite Person."
        )
        history_block = _history_compact(history)
        user = (history_block + "\n\n" if history_block else "") + json.dumps({
            "ort": {"art": location.get("label") or location.get("type"),
                    "name": location.get("name")},
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


    def search_item(self, query, location, profile=None, history=None) -> dict[str, Any]:
        sys = (
            "Der Spieler durchsucht einen konkreten Ort einer Endzeit-Welt nach "
            "etwas. Beurteile realistisch, ob sich das DORT finden ließe "
            "(Fernseher im Einfamilienhaus: ja; im Schwimmbad: nein). Wenn ja, "
            "erfinde EIN konkretes, plausibles Objekt (Marke/Modell wo sinnvoll), "
            "passende Kategorie, Gewicht in kg, bei Lebensmitteln kcal je Einheit, "
            "und eine realistische Stückzahl. Schreibe eine knappe deutsche "
            "Narration. Findet sich nichts, found=false mit kurzer Begründung. "
            "Antworte nur über das Werkzeug."
        )
        history_block = _history_compact(history)
        user = (history_block + "\n\n" if history_block else "") + json.dumps({
            "suche": query,
            "ort": {"art": location.get("label") or location.get("type"),
                    "name": location.get("name")},
            "person": profile,
        }, ensure_ascii=False)
        try:
            resp = self._client.messages.create(
                model=INTERPRET_MODEL, max_tokens=400,
                system=[{"type": "text", "text": sys, "cache_control": {"type": "ephemeral"}}],
                tools=[_FIND_TOOL],
                tool_choice={"type": "tool", "name": _FIND_TOOL["name"]},
                messages=[{"role": "user", "content": user}],
            )
            out = next((b.input for b in resp.content if b.type == "tool_use"), None)
        except Exception:
            return self._fallback.search_item(query, location, profile)
        if not out:
            return self._fallback.search_item(query, location, profile)
        if not out.get("found"):
            return {"found": False, "narration": out.get("narration", ""), "item": None}
        it = out.get("item") or {}
        return {"found": True, "narration": out.get("narration", ""), "item": {
            "name": it.get("name"), "category": it.get("category"),
            "weight_kg": it.get("weight_kg"), "kcal_per_unit": it.get("kcal_per_unit"),
            "decay_halflife_min": it.get("decay_halflife_min"),
            "quantity": it.get("quantity", 1),
        }}


_FIND_TOOL = {
    "name": "report_find",
    "description": "Plausibilitäts-Urteil + erfundenes Item für eine Suche.",
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "narration": {"type": "string"},
            "item": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string",
                                 "enum": ["food", "water", "tool", "material", "fuel", "medical", "misc"]},
                    "weight_kg": {"type": "number"},
                    "kcal_per_unit": {"type": ["number", "null"]},
                    "decay_halflife_min": {"type": ["number", "null"]},
                    "quantity": {"type": "number"},
                },
                "required": ["name", "category", "weight_kg", "quantity"],
            },
        },
        "required": ["found", "narration"],
    },
}


def _history_compact(history: list[dict] | None) -> str:
    """Kompakte Zusammenfassung der Chat-History für User-Blöcke (search/narrate).

    Liefert einen lesbaren „Bisher:"-Abschnitt oder einen leeren String, wenn
    keine History übergeben wurde.
    """
    if not history:
        return ""
    lines = [f"- [{h['role']}] {h['text']}" for h in history if h.get("text")]
    if not lines:
        return ""
    return "Bisher (älteste zuerst):\n" + "\n".join(lines)


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
