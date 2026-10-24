"""Deterministischer Regel-Stub (kein Netz, kein Key).

Grobe Keyword-Heuristik, die eine offene Eingabe auf einen Effekt + ein
Machbarkeitsurteil abbildet. ``run_adjudication`` spielt dasselbe Protokoll wie
das Claude-Backend (Werkzeug-Aufrufe gegen die Toolbox, Ablehnung -> Umplanen),
nur regelbasiert: z.B. ``prepare`` scheitert an ``no_water`` -> ``fill_water``,
dann ``prepare`` erneut. Bewusst simpel — die sprachliche/urteilende Tiefe
liefert das Claude-Backend. Format identisch zu ClaudeBackend, damit der
Adjudikator backend-agnostisch bleibt. Für Tests deterministisch.
"""
from __future__ import annotations

from typing import Any

from .base import LLMBackend, proposal
import hashlib

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
    ("fill_water", ("abfüll", "abfuell", "fülle", "fuelle", "zapf")),
    ("leave_note", ("zettel", "notiz", "nachricht hinterlass", "hinterlasse", "hinterlass")),
    ("barricade", ("barrikad", "verrammel")),
    ("establish_capability", ("ssid", "beacon", "funksignal", "signal senden",
                              "sende ein", "antenne", "funkmast", "über den mast",
                              "ueber den mast", "leuchtturm")),
    ("charge_device", ("aufladen", "auflade", "akku", "lade mein", "lade das", "lade den",
                       "lade dein", "lade ihn", "lade es", "handy laden", "telefon laden",
                       "smartphone laden", "laptop laden", "powerbank")),
    ("search", ("such", "finde", "durchsuch", "schau nach", "gibt es hier", "kram", "stöber", "stoeber")),
    ("prepare", ("koch", "zubereit", "brat", "gar ", "gare")),
    ("consume_food", ("iss", "essen", "ess ", "futter", "mampf", "verzehr")),
    ("drink", ("trink", "wasserhahn", "hahn", "leitungswasser", "durst")),
    ("transfer", ("plünder", "pluender", "nimm", "raff", "räum", "loot", "einsammeln")),
    ("discover", ("betr", "geh rein", "geh in", "hinein", "rein", "öffne", "oeffne",
                  "erkund", "untersuch")),
    ("look", ("schau", "status", "umschau", "umsehen", "sieh", "betracht", "was ist hier")),
    ("advance_time", ("wart", "warte", "raste", "ausruh", "ruh")),
    ("move_to", ("geh", "lauf", "beweg", "move", "spazier", "marschier",
                 "zum ", "zur ", "richtung")),
]


_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "water": ("wasser", "trink", "getränk", "getraenk", "flasche"),
    "food": ("essen", "lebensmittel", "nahrung", "konserve", "futter", "proviant", "hunger"),
    "tool": ("werkzeug",),
    "medical": ("medizin", "medikament", "arznei", "verband", "erste hilfe", "tablette"),
    "fuel": ("brennstoff", "sprit", "treibstoff"),
}
_ITEM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "water_1l": ("wasser", "trink"),
    "canned_beans": ("bohne", "dose"),
    "canned_meat": ("fleisch", "dose"),
    "canned_soup": ("eintopf", "suppe", "dose"),
    "bread_loaf": ("brot",),
    "milk_1l": ("milch",),
    "pasta_500g": ("nudel", "pasta"),
    "crackers": ("keks", "zwieback", "cracker"),
    "chocolate": ("schoko", "süßigkeit", "suessigkeit"),
    "flashlight": ("lampe", "licht", "taschenlampe"),
    "firewood": ("holz", "feuer", "brennholz"),
    "gasoline": ("benzin", "sprit"),
    "crowbar": ("kuhfuß", "kuhfuss", "brecheisen", "brechstange"),
    "hammer": ("hammer",),
    "rope": ("seil", "strick"),
    "duct_tape": ("klebeband", "tape", "gewebeband"),
    "bandage": ("verband", "pflaster"),
    "painkillers": ("schmerz", "tablette"),
    "first_aid_kit": ("erste hilfe", "verbandkasten"),
}


def _note_text(text: str) -> str:
    """Notiztext: Zitat in Anführungszeichen, sonst Teil nach dem Doppelpunkt,
    sonst die ganze Eingabe."""
    for q in ('"', "„", "'"):
        a = text.find(q)
        if a != -1:
            b = text.find("“" if q == "„" else q, a + 1)
            if b > a + 1:
                return text[a + 1:b].strip()
    if ":" in text:
        return text.split(":", 1)[1].strip() or text.strip()
    return text.strip()


def _words(text: str) -> list[str]:
    return "".join(ch if ch.isalnum() else " " for ch in text.lower()).split()


# --- Companion (Slice M4): deterministische Keyword-Regeln ---------------------
_GREETINGS = (
    "Hallo. Schön, dass du da bist.",
    "Ich hatte schon kaum noch gehofft, jemanden zu treffen.",
    "Hm. Ja, ich höre dir zu.",
    "Es ist so still geworden. Was gibt es?",
)
_DEMEANOR_OPEN = {
    "zurückhaltend": "Ich bin vorsichtig. ",
    "freundlich": "",
    "nervös": "Entschuldige, ich bin etwas aufgewühlt. ",
}


def _has(t: str, words: tuple[str, ...]) -> bool:
    return any(w in t for w in words)


def _pick(seq: tuple[str, ...], text: str) -> str:
    h = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    return seq[h % len(seq)]


def _companion_reply(persona, memory, context, player_text) -> dict[str, Any]:
    t = (player_text or "").lower()
    trust = float(context.get("trust") or 0.0)
    opinion = context.get("opinion")
    join_trust = float(context.get("join_trust") or 0.45)
    inv = {i["item_id"]: i for i in (context.get("inventory") or [])}
    prefix = _DEMEANOR_OPEN.get((persona or {}).get("demeanor"), "")

    if _has(t, ("komm mit", "begleit", "schließ dich", "schliess dich", "gemeinsam", "zusammen")):
        if trust >= join_trust and opinion != "stay":
            return {"speech": prefix + "Ja. Ich komme mit dir.", "intents": [{"intent": "agree_join"}]}
        if opinion == "stay":
            return {"speech": prefix + "Nein. Ich bleibe lieber hier.", "intents": [{"intent": "refuse"}]}
        return {"speech": prefix + "Ich muss dir erst besser vertrauen.", "intents": [{"intent": "refuse"}]}
    if _has(t, ("geh weg", "verschwinde", "trennen wir", "lass mich allein")):
        return {"speech": "Wenn du es so willst, gehe ich.", "intents": [{"intent": "leave"}]}
    if _has(t, ("folge mir", "folg mir", "geh mit mir", "lauf mit")):
        return {"speech": "In Ordnung, ich folge dir.", "intents": [{"intent": "follow"}]}
    if _has(t, ("bleib", "warte", "wart hier")):
        return {"speech": "Gut, ich warte hier.", "intents": [{"intent": "wait_here"}]}
    asked = None
    for item_id, kws in _ITEM_KEYWORDS.items():
        if _has(t, kws) and _has(t, ("hast du", "gib", "kannst du mir", "brauche", "habe durst", "habe hunger")):
            asked = item_id
            break
    if asked is not None:
        if asked in inv:
            return {"speech": "Hier, nimm.",
                    "intents": [{"intent": "give_item", "item_id": asked, "qty": 1}]}
        return {"speech": "Ich habe selbst keins. Hast du welches für mich?",
                "intents": [{"intent": "ask_item", "item_id": asked}]}
    if _has(t, ("teilen", "teil dein essen", "hunger")) and _has(t, ("essen", "teil", "hunger")):
        if any(i.get("category") == "food" for i in inv.values()):
            return {"speech": "Wir teilen, was ich habe.", "intents": [{"intent": "share_food"}]}
        return {"speech": "Ich habe nichts zu essen.", "intents": []}
    if _has(t, ("beruf", "was machst du", "was hast du gemacht", "arbeit")):
        return {"speech": "Davon kann ich dir erzählen.",
                "intents": [{"intent": "share_info", "topic": "profession"}]}
    if _has(t, ("gesehen", "gibt es hier", "wo finde", "orte", "in der nähe", "in der naehe")):
        return {"speech": "Ich zeige dir, was ich hier kenne.",
                "intents": [{"intent": "share_info", "topic": "places"}]}
    return {"speech": prefix + _pick(_GREETINGS, t), "intents": []}


def _describe_survivor(identity, context) -> dict[str, Any]:
    name = identity.get("name") or "Die Person"
    prof = identity.get("profession") or "ohne festen Beruf"
    age = identity.get("age")
    demeanor = ("zurückhaltend", "freundlich", "nervös")[
        int(hashlib.sha256(f"{name}|{prof}".encode("utf-8")).hexdigest()[:4], 16) % 3]
    return {
        "personality": f"{demeanor.capitalize()}, praktisch veranlagt.",
        "short_bio": f"{name}, {age if age is not None else 'unbekannten Alters'}, "
                     f"früher {prof}. Hat den Zusammenbruch allein überstanden.",
        "demeanor": demeanor,
    }


def _summarize_day(persona, entries) -> str:
    lines = [e.get("text", "") for e in entries if e.get("text")]
    if not lines:
        return ""
    return f"Gespräche des Tages: {len(lines)} Einträge. Zuletzt: {lines[-1]}"


class _CompanionRules:
    """Regelbasierte Companion-Methoden des Stubs (Mixin für RuleBackend)."""

    def companion_reply(self, persona, memory, context, player_text) -> dict[str, Any]:
        return _companion_reply(persona, memory, context, player_text)

    def describe_survivor(self, identity, context, history=None) -> dict[str, Any]:
        return _describe_survivor(identity, context)

    def summarize_day(self, persona, entries) -> str:
        return _summarize_day(persona, entries)


class RuleBackend(_CompanionRules, LLMBackend):
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
        if match == "charge_device":
            return proposal(effects=[{"op": "charge_device"}],
                            narration="Du lädst deine Geräte.")
        if match == "drink":
            return proposal(effects=[{"op": "drink"}], narration="Du trinkst etwas Wasser.")
        if match == "prepare":
            return proposal(effects=[{"op": "prepare"}], narration="Du bereitest etwas zu.")
        if match == "fill_water":
            return proposal(effects=[{"op": "fill_water"}], narration="Du füllst Wasser ab.")
        if match == "barricade":
            return proposal(effects=[{"op": "barricade"}],
                            narration="Du verbarrikadierst den Ort.")
        if match == "leave_note":
            return proposal(effects=[{"op": "leave_note", "text": _note_text(text)}],
                            narration="Du hinterlässt eine Nachricht.")
        if match == "search":
            return proposal(effects=[{"op": "search", "query": text}],
                            narration="Du durchsuchst die Umgebung.")
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

    def run_adjudication(self, text, context, toolbox, max_iterations=8) -> dict[str, Any]:
        """Deterministische Tool-Use-Schleife (Regeln statt Modell)."""
        toolbox.allow_partial = True
        if not toolbox.begin_turn():
            return {"stop": "limit"}
        p = self.interpret(text, context)
        for eff in p.get("effects") or []:
            op = eff["op"]
            args = {k: v for k, v in eff.items() if k != "op"}
            res = toolbox.call("propose_" + op, args)
            # Umplanen: Zubereitung ohne Wasser -> erst Wasser abfüllen, dann erneut.
            if (not res.get("ok") and op == "prepare" and res.get("reason") == "no_water"
                    and toolbox.begin_turn()):
                if toolbox.call("propose_fill_water", {}).get("ok") and toolbox.begin_turn():
                    toolbox.call("propose_prepare", args)
        toolbox.call("finish", {
            "understanding": p.get("understanding", ""),
            "feasibility": p.get("feasibility", "feasible"),
            "reason": p.get("reason", ""),
            "narration": p.get("narration", ""),
        })
        return {"stop": "finish"}

    def narrate_location(self, location, profile=None, history=None) -> str:
        name = location.get("name") or location.get("label") or location.get("type") or "ein Ort"
        return f"Du erreichst {name}. Es ist still. Du siehst dich um."

    def match_items(self, query, location, candidates, profile=None, history=None) -> dict[str, Any]:
        """Deterministisches Keyword-Matching gegen die vorhandenen Kandidaten
        (kein Netz): Item-Name/-ID-Wörter und Oberbegriffe je Kategorie."""
        q = (query or "").lower()
        ids: list[str] = []
        for c in candidates or []:
            words = [w for w in _words(c.get("name", "")) + _words(c.get("item_id", "")) if len(w) >= 4]
            hit = any(w in q for w in words)
            hit = hit or any(k in q for k in _ITEM_KEYWORDS.get(c.get("item_id", ""), ()))
            hit = hit or any(k in q for k in _CATEGORY_KEYWORDS.get(c.get("category", ""), ()))
            if hit:
                ids.append(c["item_id"])
        if not ids:
            return {"item_ids": [], "narration": "Danach suchst du hier vergeblich."}
        return {"item_ids": ids, "narration": "Du kramst herum und wirst fündig."}

    def _target(self, t: str, context: dict[str, Any]) -> str | None:
        for word, loc_type in _LOCATION_SYNONYMS.items():
            if word in t:
                return loc_type
        for loc in context.get("locations", []):
            name = (loc.get("name") or "").lower()
            if name and len(name) >= 4 and name in t:
                return loc.get("name")
        return None
