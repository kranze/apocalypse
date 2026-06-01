"""CLI-Backend — echtes Claude über die lokale ``claude``-CLI (kein API-Key).

Nutzt die Print-Mode-CLI (``claude -p "<prompt>"``) per Subprozess und parst die
Antwort. Praktisch, um ohne ``ANTHROPIC_API_KEY`` über das Claude-Abo zu spielen.
Langsamer als die API und ohne erzwungenes Tool-Use → wir bitten um reines JSON
und extrahieren es robust; bei Fehlern Fallback auf den deterministischen Stub.

Eisernes Prinzip unverändert: liefert nur Vorschläge/Urteile als dict — der
Sim-Kern validiert/klemmt und schreibt.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from .base import EFFECT_OPS, FEASIBILITY, LLMBackend, proposal
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
        try:
            return _extract_json(self._run(prompt))
        except Exception:
            return None

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
            return self._fallback.interpret(text, context)
        feas = out.get("feasibility") if out.get("feasibility") in FEASIBILITY else "feasible"
        effects = [e for e in (out.get("effects") or []) if isinstance(e, dict) and e.get("op") in EFFECT_OPS]
        return proposal(
            understanding=out.get("understanding", ""), feasibility=feas,
            reason=out.get("reason", ""), effects=effects,
            narration=out.get("narration", ""),
        )

    # --- search_item ---------------------------------------------------
    def search_item(self, query, location, profile=None, history=None) -> dict[str, Any]:
        history_block = _history_compact(history)
        prompt = (
            "Der Spieler durchsucht einen Ort einer Endzeit-Welt. Beurteile "
            "realistisch, ob sich das Gesuchte DORT fände; wenn ja, erfinde EIN "
            "konkretes plausibles Objekt. Antworte NUR mit JSON:\n"
            "{\"found\": bool, \"narration\": str(kurz, deutsch), \"item\": "
            "{\"name\": str, \"category\": \"food|water|tool|material|fuel|medical|misc\", "
            "\"weight_kg\": number, \"kcal_per_unit\"?: number, \"quantity\": number}}\n\n"
            + (history_block + "\n\n" if history_block else "")
            + f"Suche: {query!r}\n"
            f"Ort: {json.dumps({'art': location.get('label') or location.get('type'), 'name': location.get('name')}, ensure_ascii=False)}\n"
            f"Person: {json.dumps(profile, ensure_ascii=False)}"
        )
        out = self._json(prompt)
        if not out:
            return self._fallback.search_item(query, location, profile)
        if not out.get("found") or not isinstance(out.get("item"), dict):
            return {"found": False, "narration": out.get("narration", ""), "item": None}
        it = out["item"]
        return {"found": True, "narration": out.get("narration", ""), "item": {
            "name": it.get("name"), "category": it.get("category"),
            "weight_kg": it.get("weight_kg"), "kcal_per_unit": it.get("kcal_per_unit"),
            "decay_halflife_min": it.get("decay_halflife_min"),
            "quantity": it.get("quantity", 1),
        }}

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
            return txt or self._fallback.narrate_location(location, profile)
        except Exception:
            return self._fallback.narrate_location(location, profile)


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
        "locations": [
            {"type": l.get("type"), "name": l.get("name"), "dist_m": l.get("dist_m")}
            for l in context.get("locations", [])[:15]
        ],
        "inventory": [i.get("item_id") for i in context.get("inventory", [])],
        "capabilities": context.get("capabilities", []),
        "profile": context.get("profile"),
    }
