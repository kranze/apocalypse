"""Claude-Backend (Anthropic SDK) — offene Intention -> Vorschlags-Objekt.

Aktiv, sobald ``ANTHROPIC_API_KEY`` gesetzt ist. Adjudikator v2: echte
Tool-Use-Schleife (``run_adjudication``) — Claude liest über Lese-Werkzeuge,
schlägt Effekte per ``propose_*`` vor, bekommt vom Sim-Kern ok/Ablehnungsgrund
zurück, plant ggf. um und beendet mit ``finish``. Die Werkzeug-Schemas sind
``strict`` (garantiert schema-konforme Eingaben). Der System-Text (Prompt-Datei
+ Welt-Bibel) ist konstant -> Prompt-Caching; Veränderliches (Tag, Uhrzeit,
Kontext) steht in der User-Nachricht. Bei API-Fehlern Fallback auf den
deterministischen Stub (gleiches Protokoll). ``interpret`` bleibt als
Einzelschuss-Notpfad.

Eisernes Prinzip: liefert nur einen Vorschlag (Effekte + Narration) — der
Sim-Kern validiert/führt aus und berechnet Folgen selbst.
"""
from __future__ import annotations

import json
from typing import Any

from . import lore
from .base import EFFECT_OPS, FEASIBILITY, LLMBackend, proposal
from .stub import RuleBackend

INTERPRET_MODEL = "claude-sonnet-5-5"

# Einzelschuss-Notpfad (``interpret``); die Tool-Use-Schleife nutzt lore.system_prompt().
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
                system=[{"type": "text", "text": _SYSTEM + "\n\n" + lore.world_bible(),
                         "cache_control": {"type": "ephemeral"}}],
                tools=[_TOOL],
                tool_choice={"type": "auto"},  # erzwungenes tool_choice: 400 auf Sonnet 5.5
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


    # --- Tool-Use-Schleife (Adjudikator v2) -----------------------------
    def run_adjudication(self, text, context, toolbox, max_iterations=8) -> dict[str, Any]:
        """Mehrschritt-Schleife gegen die Toolbox des Sim-Kerns. Bei API-Fehlern
        (nichts geschrieben, nur Staging im Speicher) -> Stub-Schleife."""
        try:
            return self._loop(text, context, toolbox)
        except Exception:
            toolbox.reset()
            return self._fallback.run_adjudication(text, context, toolbox, max_iterations)

    def _loop(self, text, context, toolbox) -> dict[str, Any]:
        tools = toolbox.specs()
        system = [{"type": "text", "text": lore.system_prompt(),
                   "cache_control": {"type": "ephemeral"}}]
        messages = _history_messages(context)
        user_msg = (
            f"Spieler-Eingabe: {text!r}\n\nKontext (nur reale Optionen):\n"
            + json.dumps(_compact(context), ensure_ascii=False)
        )
        if messages and messages[-1]["role"] == "user":
            messages[-1] = {"role": "user",
                            "content": messages[-1]["content"] + "\n\n" + user_msg}
        else:
            messages.append({"role": "user", "content": user_msg})

        while toolbox.begin_turn():
            resp = self._client.messages.create(
                model=INTERPRET_MODEL, max_tokens=4096, system=system, tools=tools,
                tool_choice={"type": "auto"},  # forced tool_choice: 400 auf Sonnet 5.5
                output_config={"effort": "medium"},
                messages=messages,
            )
            if resp.stop_reason in ("refusal", "max_tokens"):
                raise RuntimeError(f"adjudication stopped: {resp.stop_reason}")
            # Antwort UNVERÄNDERT zurückgeben (Thinking-Blöcke bleiben gültig).
            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                # Nur Text, kein finish: Text als Narration übernehmen.
                narr = " ".join(b.text for b in resp.content if b.type == "text").strip()
                toolbox.call("finish", {"understanding": "", "feasibility": "feasible",
                                        "reason": "", "narration": narr})
                return {"stop": "finish"}
            others = [b for b in uses if b.name != "finish"]
            results = []
            for b in others:
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(toolbox.call(b.name, dict(b.input)),
                                                      ensure_ascii=False)})
            for b in uses:
                if b.name != "finish":
                    continue
                if others:  # finish muss allein stehen: erst Ergebnisse sehen lassen
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "is_error": True,
                                    "content": json.dumps({"ok": False,
                                                           "reason": "finish_must_be_alone"})})
                else:
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(toolbox.call("finish", dict(b.input)),
                                                          ensure_ascii=False)})
            if toolbox.finished is not None:
                return {"stop": "finish"}
            messages.append({"role": "user", "content": results})
        return {"stop": "limit"}

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


    def match_items(self, query, location, candidates, profile=None, history=None) -> dict[str, Any]:
        sys = (
            "Der Spieler durchsucht einen Ort einer Endzeit-Welt und sucht nach "
            "etwas. Der Ort hat bereits ein festes Inventar (Kandidatenliste). "
            "Wähle daraus die Items, die zur Suche passen (item_ids, NUR IDs aus "
            "der Liste; kein passendes Item -> leere Liste) und schreibe eine "
            "knappe deutsche Narration. Erfinde keine Items und nenne keine "
            "Mengen. Antworte nur über das Werkzeug."
        )
        history_block = _history_compact(history)
        user = (history_block + "\n\n" if history_block else "") + json.dumps({
            "suche": query,
            "ort": {"art": location.get("label") or location.get("type"),
                    "name": location.get("name")},
            "kandidaten": [{"item_id": c["item_id"], "name": c["name"],
                            "kategorie": c.get("category")} for c in candidates],
            "person": profile,
        }, ensure_ascii=False)
        try:
            resp = self._client.messages.create(
                model=INTERPRET_MODEL, max_tokens=400,
                system=[{"type": "text", "text": sys, "cache_control": {"type": "ephemeral"}}],
                tools=[_MATCH_TOOL],
                tool_choice={"type": "auto"},  # erzwungenes tool_choice: 400 auf Sonnet 5.5
                messages=[{"role": "user", "content": user}],
            )
            out = next((b.input for b in resp.content if b.type == "tool_use"), None)
        except Exception:
            return self._fallback.match_items(query, location, candidates, profile)
        if not out:
            return self._fallback.match_items(query, location, candidates, profile)
        return {"item_ids": [i for i in (out.get("item_ids") or []) if isinstance(i, str)],
                "narration": out.get("narration", "")}


_MATCH_TOOL = {
    "name": "report_matches",
    "description": "Welche vorhandenen Items des Ortes passen zur Suche?",
    "input_schema": {
        "type": "object",
        "properties": {
            "item_ids": {"type": "array", "items": {"type": "string"}},
            "narration": {"type": "string"},
        },
        "required": ["item_ids", "narration"],
    },
}


def _history_messages(context: dict[str, Any]) -> list[dict]:
    """Gesprächsverlauf als alternierende messages (player -> user, narrator ->
    assistant, system -> user-Notiz); gleiche Rollen in Folge werden verschmolzen."""
    out: list[dict] = []
    for turn in (context.get("history") or []):
        t = turn.get("text", "")
        if not t:
            continue
        role = turn.get("role", "")
        if role == "player":
            msg = {"role": "user", "content": t}
        elif role == "narrator":
            msg = {"role": "assistant", "content": t}
        elif role == "system":
            msg = {"role": "user", "content": f"[System: {t}]"}
        else:
            continue
        if out and out[-1]["role"] == msg["role"]:
            out[-1] = {"role": msg["role"], "content": out[-1]["content"] + "\n" + t}
        else:
            out.append(msg)
    while out and out[0]["role"] != "user":  # API: erste Nachricht muss user sein
        out.pop(0)
    return out


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
        "world_state": context.get("world_state"),
        "locations": [
            {"id": l.get("id"), "type": l.get("type"), "name": l.get("name"),
             "dist_m": l.get("dist_m"), "status": l.get("discovery_status"),
             "searched": l.get("searched")}
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
