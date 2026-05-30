"""Deterministischer Regel-Stub (kein Netz, kein Key).

Grobe Keyword-Heuristik, die eine offene Eingabe auf einen Effekt + ein
Machbarkeitsurteil abbildet. Bewusst simpel — die sprachliche/urteilende Tiefe
liefert das Claude-Backend. Format identisch zu ClaudeBackend, damit der
Adjudikator backend-agnostisch bleibt. Für Tests deterministisch.
"""
from __future__ import annotations

from typing import Any

from .base import LLMBackend, proposal

_LOCATION_SYNONYMS: dict[str, str] = {
    "supermarkt": "supermarket", "markt": "supermarket", "laden": "supermarket",
    "tankstelle": "fuel_station", "tanke": "fuel_station",
    "apotheke": "pharmacy", "krankenhaus": "hospital", "klinik": "hospital",
    "baumarkt": "hardware", "haus": "house", "gebäude": "building", "gebaeude": "building",
}

# Reihenfolge = Priorität.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("too_complex", ("mobilfunk", "handynetz", "netz wieder", "netz reaktiv",
                     "internet wieder", "ganze stadt", "alle masten")),
    ("establish_capability", ("ssid", "beacon", "funksignal", "signal senden",
                              "sende ein", "antenne", "funkmast", "über den mast",
                              "ueber den mast", "leuchtturm")),
    ("prepare", ("koch", "zubereit", "brat", "gar ", "gare")),
    ("consume_food", ("iss", "essen", "ess ", "futter", "mampf", "verzehr")),
    ("transfer", ("plünder", "pluender", "durchsuch", "nimm", "raff", "räum", "loot")),
    ("discover", ("betr", "geh rein", "geh in", "hinein", "rein", "öffne", "oeffne",
                  "erkund", "untersuch")),
    ("look", ("schau", "status", "umschau", "umsehen", "sieh", "betracht", "was ist hier")),
    ("advance_time", ("wart", "warte", "raste", "ausruh", "ruh")),
    ("move_to", ("geh", "lauf", "beweg", "move", "spazier", "marschier",
                 "zum ", "zur ", "richtung")),
]


class RuleBackend(LLMBackend):
    name = "stub"

    def interpret(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        t = (text or "").lower().strip()
        if not t:
            return proposal(feasibility="impossible", reason="leer",
                            narration="Du tust nichts Bestimmtes.")

        match = next((kind for kind, kws in _RULES if any(k in t for k in kws)), None)

        if match is None:
            return proposal(feasibility="impossible", reason="unverstanden",
                            narration="Das ergibt für dich gerade keinen Sinn.")
        if match == "too_complex":
            return proposal(feasibility="too_complex", reason="zu_komplex",
                            understanding="ein ganzes Netz reaktivieren",
                            narration="Das übersteigt deine Mittel bei Weitem.")
        if match == "look":
            return proposal(effects=[{"op": "narrate"}], narration="Du siehst dich um.")
        if match == "advance_time":
            return proposal(effects=[{"op": "advance_time"}],
                            narration="Du lässt etwas Zeit verstreichen.")
        if match == "consume_food":
            return proposal(effects=[{"op": "consume_food"}], narration="Du isst etwas.")
        if match == "prepare":
            return proposal(effects=[{"op": "prepare"}], narration="Du bereitest etwas zu.")
        if match == "establish_capability":
            return proposal(
                feasibility="risky",
                understanding="ein SSID-Funksignal aussenden",
                effects=[{"op": "establish_capability", "ctype": "ssid_beacon",
                          "params": {"info": "KOMM_NACH_HIER"}, "target": "tower"}],
                narration="Du versuchst, über einen Sender ein Funksignal abzusetzen.",
            )
        # move_to / discover / transfer: Ziel aus Synonymen/Namen ziehen
        target = self._target(t, context)
        return proposal(effects=[{"op": match, "target": target}],
                        narration="Du machst dich daran.")

    def _target(self, t: str, context: dict[str, Any]) -> str | None:
        for word, loc_type in _LOCATION_SYNONYMS.items():
            if word in t:
                return loc_type
        for loc in context.get("locations", []):
            name = (loc.get("name") or "").lower()
            if name and len(name) >= 4 and name in t:
                return loc.get("name")
        return None
