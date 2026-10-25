"""CLI-Backend — echtes Claude über die lokale ``claude``-CLI (kein API-Key).

Nutzt die Print-Mode-CLI (``claude -p "<prompt>"``) per Subprozess und parst die
Antwort. Praktisch, um ohne ``ANTHROPIC_API_KEY`` über das Claude-Abo zu spielen.
Langsamer als die API und ohne erzwungenes Tool-Use → wir bitten um reines JSON
und extrahieren es robust; bei Fehlern Fallback auf den deterministischen Stub.

Adjudikator v2: ``run_adjudication`` emuliert die Tool-Use-Schleife per
JSON-Rundenprotokoll (siehe dort); unbrauchbare Antworten -> Stub-Schleife.

Eisernes Prinzip unverändert: liefert nur Vorschläge/Urteile als dict — der
Sim-Kern validiert/klemmt und schreibt.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import lore, status
from .base import COMPANION_INTENTS, EFFECT_OPS, FEASIBILITY, LLMBackend, proposal
from .stub import RuleBackend

_CLI = os.environ.get("WASTELAND_CLAUDE_CLI", "claude")
_TIMEOUT_S = int(os.environ.get("WASTELAND_CLI_TIMEOUT_S", "120"))
_CATEGORIES = ("food", "water", "tool", "material", "fuel", "medical", "misc")


def _extract_json(text: str) -> dict | None:
    """Erstes balanciertes JSON-Objekt aus einem CLI-Text ziehen."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


class CliBackend(LLMBackend):
    name = "cli"

    def __init__(self) -> None:
        self._fallback = RuleBackend()
        self._exe = shutil.which(_CLI) or _CLI

    def _run(self, prompt: str) -> str:
        proc = subprocess.run(
            [self._exe, "-p", prompt],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "claude CLI failed")
        return proc.stdout

    def _json(self, prompt: str) -> dict | None:
        self._err = None
        try:
            out = _extract_json(self._run(prompt))
        except Exception as e:
            self._err = e
            return None
        if out is None:
            self._err = "kein JSON in der CLI-Antwort"
        return out

    def _why(self) -> Any:
        return getattr(self, "_err", None) or "unbrauchbare Antwort"

    # --- interpret -----------------------------------------------------
    def interpret(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        history_block = _history_compact(context.get("history"))
        prompt = (
            "Du bist der Adjudikator einer Endzeit-Survival-Sim. Wandle die "
            "Spieler-Eingabe in einen Vorschlag um. Antworte AUSSCHLIESSLICH mit "
            "einem JSON-Objekt, keine weiteren Worte.\n"
            f"Erlaubte Effekt-Ops: {', '.join(EFFECT_OPS)}.\n"
            "Schema: {\"understanding\": str, \"feasibility\": "
            "\"feasible|risky|too_complex|impossible\", \"reason\": str, "
            "\"effects\": [{\"op\": str, \"target\"?: str, \"query\"?: str, "
            "\"ctype\"?: str, \"params\"?: object, \"minutes\"?: number}], "
            "\"narration\": str (kurz, deutsch)}.\n"
            "search{query} = an Ort gezielt suchen; transfer = Gefundenes nehmen; "
            "maßlos Überkomplexes -> too_complex. Erfinde keine Ergebnisse.\n\n"
            + (history_block + "\n\n" if history_block else "")
            + f"Eingabe: {text!r}\nKontext: {json.dumps(_compact(context), ensure_ascii=False)}"
        )
        out = self._json(prompt)
        if not out:
            status.record_fallback("interpret", self._why())
            return self._fallback.interpret(text, context)
        status.record_ok("interpret")
        feas = out.get("feasibility") if out.get("feasibility") in FEASIBILITY else "feasible"
        effects = [e for e in (out.get("effects") or []) if isinstance(e, dict) and e.get("op") in EFFECT_OPS]
        return proposal(
            understanding=out.get("understanding", ""), feasibility=feas,
            reason=out.get("reason", ""), effects=effects,
            narration=out.get("narration", ""),
        )

    # --- Tool-Use-Schleife (Adjudikator v2): JSON-Rundenprotokoll -------------
    # Die CLI kennt kein natives Tool-Use. Emulation: pro Runde ein frischer
    # ``claude -p``-Aufruf mit Werkzeugliste + bisherigem Protokoll; das Modell
    # antwortet mit {"calls": [{"tool": name, "input": {...}}, ...]}. Jeder Call
    # läuft gegen dieselbe Toolbox (gleiche gestagte Validierung, Ablehnung geht
    # im nächsten Rundenprompt zurück). Kein erzwungenes Schema -> die Toolbox
    # prüft/normalisiert alles; unbrauchbare Antwort -> Stub-Schleife.
    def run_adjudication(self, text, context, toolbox, max_iterations=8) -> dict[str, Any]:
        try:
            res = self._loop(text, context, toolbox)
            status.record_ok("run_adjudication")
            return res
        except Exception as e:
            status.record_fallback("run_adjudication", getattr(self, "_err", None) or e)
            toolbox.reset()
            return self._fallback.run_adjudication(text, context, toolbox, max_iterations)

    def _loop(self, text, context, toolbox) -> dict[str, Any]:
        tool_lines = "\n".join(
            f"- {s['name']}: {s['description']} Eingabe: "
            f"{json.dumps(list(s['input_schema']['properties']), ensure_ascii=False)}"
            for s in toolbox.specs()
        )
        header = (
            lore.system_prompt()
            + "\n\n# Protokoll (CLI)\nDu hast keine echten Werkzeuge, sondern antwortest je Runde "
            "AUSSCHLIESSLICH mit einem JSON-Objekt {\"calls\": [{\"tool\": str, \"input\": "
            "object}, ...]}. Die Ergebnisse siehst du in der nächsten Runde. `finish` "
            "gehört allein in eine eigene Runde. Optionale Felder: null oder weglassen. "
            "transfer.items = [{\"item\": str, \"qty\": number}].\n\n# Werkzeuge\n"
            + tool_lines
            + f"\n\nEingabe: {text!r}\nKontext: "
            + json.dumps(_compact(context), ensure_ascii=False)
        )
        history_block = _history_compact(context.get("history"))
        if history_block:
            header += "\n\n" + history_block
        transcript: list[dict[str, Any]] = []
        while toolbox.begin_turn():
            prompt = header
            if transcript:
                prompt += "\n\n# Bisheriger Verlauf dieser Adjudikation\n" + json.dumps(
                    transcript, ensure_ascii=False)
            out = self._json(prompt)
            calls = out.get("calls") if isinstance(out, dict) else None
            if not isinstance(calls, list) or not calls:
                raise RuntimeError("unbrauchbare CLI-Antwort")
            others = [c for c in calls if isinstance(c, dict) and c.get("tool") != "finish"]
            round_log = []
            for c in others[:8]:
                res = toolbox.call(str(c.get("tool")), c.get("input") or {})
                round_log.append({"tool": c.get("tool"), "input": c.get("input"), "result": res})
            fin = next((c for c in calls if isinstance(c, dict) and c.get("tool") == "finish"), None)
            if fin is not None:
                if others:
                    round_log.append({"tool": "finish",
                                      "result": {"ok": False, "reason": "finish_must_be_alone"}})
                else:
                    toolbox.call("finish", fin.get("input") or {})
                    return {"stop": "finish"}
            transcript.append({"round": len(transcript) + 1, "calls": round_log})
        return {"stop": "limit"}

    # --- Companion (Slice M4): reines JSON, Fallback Stub ----------------------
    def companion_reply(self, persona, memory, context, player_text) -> dict[str, Any]:
        prompt = (
            lore.companion_system()
            + "\n\nAntworte AUSSCHLIESSLICH mit einem JSON-Objekt: {\"speech\": str, "
            "\"intents\": [{\"intent\": str, \"item_id\"?: str, \"qty\"?: number, "
            "\"topic\"?: str}]}. Erlaubte intent-Werte: "
            + ", ".join(COMPANION_INTENTS) + ".\n\n"
            + json.dumps({"persona": persona, "memory": memory, "context": context,
                          "spieler_sagt": player_text}, ensure_ascii=False)
        )
        out = self._json(prompt)
        if not out or not isinstance(out.get("speech"), str):
            status.record_fallback("companion_reply", self._why())
            return self._fallback.companion_reply(persona, memory, context, player_text)
        status.record_ok("companion_reply")
        intents = [i for i in (out.get("intents") or []) if isinstance(i, dict)]
        return {"speech": out["speech"], "intents": intents}

    def describe_survivor(self, identity, context, history=None) -> dict[str, Any]:
        prompt = (
            "Erfinde Persönlichkeit und Kurzbiografie (je 1-2 Sätze, deutsch) für einen "
            "Überlebenden einer Endzeit-Welt, passend zur Identität. Antworte NUR mit JSON: "
            "{\"personality\": str, \"short_bio\": str, \"demeanor\": str (ein Adjektiv)}.\n"
            + json.dumps({"identitaet": identity, "welt": context}, ensure_ascii=False)
        )
        out = self._json(prompt)
        if not out or not out.get("personality"):
            status.record_fallback("describe_survivor", self._why())
            return self._fallback.describe_survivor(identity, context)
        status.record_ok("describe_survivor")
        return out

    def summarize_day(self, persona, entries) -> str:
        lines = "\n".join(f"[{e.get('role')}] {e.get('text')}" for e in entries if e.get("text"))
        prompt = ("Fasse dieses Gespräch in höchstens 3 knappen deutschen Sätzen zusammen "
                  "(nur Gesagtes, keine neuen Fakten). Nur den Text.\n" + lines)
        try:
            txt = self._run(prompt).strip()
            if txt:
                status.record_ok("summarize_day")
                return txt
            status.record_fallback("summarize_day", "leere Antwort")
            return self._fallback.summarize_day(persona, entries)
        except Exception as e:
            status.record_fallback("summarize_day", e)
            return self._fallback.summarize_day(persona, entries)

    # --- match_items ---------------------------------------------------
    def match_items(self, query, location, candidates, profile=None, history=None) -> dict[str, Any]:
        history_block = _history_compact(history)
        prompt = (
            "Der Spieler durchsucht einen Ort einer Endzeit-Welt. Der Ort hat ein "
            "festes Inventar (Kandidaten). Wähle daraus die zur Suche passenden "
            "Items (NUR item_id aus der Liste; keins passt -> leere Liste), "
            "erfinde nichts und nenne keine Mengen. Antworte NUR mit JSON:\n"
            "{\"item_ids\": [str], \"narration\": str(kurz, deutsch)}\n\n"
            + (history_block + "\n\n" if history_block else "")
            + f"Suche: {query!r}\n"
            f"Ort: {json.dumps({'art': location.get('label') or location.get('type'), 'name': location.get('name')}, ensure_ascii=False)}\n"
            f"Kandidaten: {json.dumps([{'item_id': c['item_id'], 'name': c['name']} for c in candidates], ensure_ascii=False)}\n"
            f"Person: {json.dumps(profile, ensure_ascii=False)}"
        )
        out = self._json(prompt)
        if not out:
            status.record_fallback("match_items", self._why())
            return self._fallback.match_items(query, location, candidates, profile)
        status.record_ok("match_items")
        return {"item_ids": [i for i in (out.get("item_ids") or []) if isinstance(i, str)],
                "narration": out.get("narration", "")}

    # --- narrate_location ----------------------------------------------
    def narrate_location(self, location, profile=None, history=None) -> str:
        history_block = _history_compact(history)
        prompt = (
            "Beschreibe knapp und atmosphärisch (2–3 Sätze, Deutsch, 2. Person) "
            "den ersten Eindruck dieses Ortes in einer verlassenen Endzeit-Welt. "
            "Keine Mengen, keine Spielmechanik. Nur den Text, keine Anführungszeichen.\n"
            + (history_block + "\n" if history_block else "")
            + f"Ort: {location.get('label') or location.get('type')}"
            + (f" namens {location.get('name')}" if location.get('name') else "")
        )
        try:
            txt = self._run(prompt).strip()
            if txt:
                status.record_ok("narrate_location")
                return txt
            status.record_fallback("narrate_location", "leere Antwort")
            return self._fallback.narrate_location(location, profile)
        except Exception as e:
            status.record_fallback("narrate_location", e)
            return self._fallback.narrate_location(location, profile)

    # --- Epilog (Slice M5) ----------------------------------------------
    def narrate_epilogue(self, evaluation, profile=None, companion=None, highlights=None) -> str:
        sys = (Path(__file__).parent / "prompts" / "epilogue.md").read_text(encoding="utf-8")
        prompt = (sys + "\nAntworte nur mit dem Epilog-Text.\n\n" + json.dumps(
            {"evaluation": evaluation, "profile": profile, "companion": companion,
             "highlights": highlights}, ensure_ascii=False))
        try:
            txt = self._run(prompt).strip()
            if txt:
                status.record_ok("narrate_epilogue")
                return txt
            status.record_fallback("narrate_epilogue", "leere Antwort")
            return self._fallback.narrate_epilogue(evaluation, profile, companion, highlights)
        except Exception as e:
            status.record_fallback("narrate_epilogue", e)
            return self._fallback.narrate_epilogue(evaluation, profile, companion, highlights)


def _history_compact(history: list[dict] | None) -> str:
    """Liefert einen 'Bisher:'-Block aus dem Gesprächsverlauf oder leer."""
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
             "dist_m": l.get("dist_m"), "searched": l.get("searched")}
            for l in context.get("locations", [])[:15]
        ],
        "inventory": [i.get("item_id") for i in context.get("inventory", [])],
        "capabilities": context.get("capabilities", []),
        "profile": context.get("profile"),
    }
