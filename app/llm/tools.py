"""Werkzeug-Definitionen des Adjudikators v2 (Tool-Use-Schleife).

Reine Daten + Argument-Normalisierung, keine DB. Das Modell bekommt

* **Lese-Werkzeuge** (``READ_TOOLS``): Orte in der Nähe, eigenes Inventar,
  Inhalt eines bereits durchsuchten Ortes, KB-Abfrage, aktive Capabilities,
  Anforderungs-Lieferanten, Gerätestatus, Weltzustand,
* **Vorschlags-Werkzeuge** (``propose_<op>`` je Effekt aus ``EFFECT_OPS``) und
* ``finish`` (beendet die Schleife mit Verständnis, Machbarkeit, Narration).

Alle Schemas sind strict-tauglich (``additionalProperties: false``, alle
Felder ``required``, Optionales als ``null``-fähig), damit die API die Eingaben
garantiert schema-konform liefert. Die Ausführung der Werkzeuge liegt NICHT hier,
sondern im Sim-Kern (``app/sim/toolbox.py``): jeder Vorschlag wird dort sofort
validiert, die Antwort (ok/Ablehnungsgrund) geht ans Modell zurück.
"""
from __future__ import annotations

from typing import Any

from .base import EFFECT_OPS, FEASIBILITY

_S = {"type": "string"}
_NS = {"type": ["string", "null"]}
_NN = {"type": ["number", "null"]}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


_ITEM_QTY = _obj({"item": _S, "qty": {"type": "number"}})

# --- Lese-Werkzeuge ------------------------------------------------------
READ_TOOLS: dict[str, dict[str, Any]] = {
    "get_nearby_locations": {
        "description": "Orte in Reichweite des Spielers (Typ, Name, Entfernung, "
                       "Entdeckungsstatus, ob durchsucht).",
        "input_schema": _obj({}),
    },
    "get_inventory": {
        "description": "Das Gruppen-Inventar des Spielers (Item, Menge, Kategorie).",
        "input_schema": _obj({}),
    },
    "get_location_inventory": {
        "description": "Inhalt eines Ortes in Reichweite — nur wenn er bereits "
                       "durchsucht wurde, sonst ist er unbekannt.",
        "input_schema": _obj({"location_id": {"type": "integer"}}),
    },
    "kb_lookup": {
        "description": "Verbindliche Fakten aus der Wissensbasis, z.B. topic "
                       "'provides:heat' (welche Items Hitze liefern) oder "
                       "'capability_recipe:ssid_beacon'. key=null listet das ganze Topic.",
        "input_schema": _obj({"topic": _S, "key": _NS}),
    },
    "get_capabilities": {
        "description": "Aktive Capabilities der Gruppe (dauerhaft Aufgebautes).",
        "input_schema": _obj({}),
    },
    "get_requirement_providers": {
        "description": "Welche Items eine Anforderung (heat, power, transmitter, ...) "
                       "erfüllen und ob die Gruppe eines davon besitzt. "
                       "requirement=null: alle bekannten Anforderungen.",
        "input_schema": _obj({"requirement": _NS}),
    },
    "get_device_status": {
        "description": "Akkustand der Geräte (Smartphone/Laptop/Powerbank) und "
                       "Netzqualität am Standort (Mobilfunk/WLAN/Internet).",
        "input_schema": _obj({}),
    },
    "get_world_state": {
        "description": "Tag, Uhrzeit und Infrastruktur-Lage (soweit bekannt).",
        "input_schema": _obj({}),
    },
}

# --- Vorschlags-Werkzeuge (je Effekt eines) --------------------------------
_PROPOSE_SPECS: dict[str, tuple[str, dict[str, Any]]] = {
    "move_to": ("Zu einem Ort gehen (target = Ortstyp oder Name).",
                _obj({"target": _S})),
    "discover": ("Ein nahes Gebäude betreten/erkunden.", _obj({"target": _S})),
    "transfer": ("Bereits Gefundenes eines nahen Ortes einsammeln. items=null: alles; "
                 "sonst Liste {item, qty}.",
                 _obj({"target": _S, "items": {"type": ["array", "null"], "items": _ITEM_QTY}})),
    "consume_food": ("Etwas Essbares essen (item=null: das nahrhafteste).",
                     _obj({"item": _NS})),
    "prepare": ("Ein rohes Lebensmittel zubereiten (braucht Hitze + Wasser).",
                _obj({"item": _NS})),
    "search": ("Am aktuellen Ort gezielt nach etwas suchen; was es dort gibt, "
               "klärt der Sim-Kern.", _obj({"query": _S})),
    "advance_time": ("Zeit verstreichen lassen (Minuten, wird vom Kern geklemmt).",
                     _obj({"minutes": {"type": "number"}})),
    "transform": ("Etwas herstellen/umbauen: verbraucht consume, braucht requires "
                  "(Anforderungen), erzeugt produce (nur Katalog-Items, nicht mehr "
                  "Menge als verbraucht).",
                  _obj({"consume": {"type": "array", "items": _ITEM_QTY},
                        "produce": {"type": "array", "items": _ITEM_QTY},
                        "requires": {"type": "array", "items": _S}})),
    "establish_capability": ("Etwas Dauerhaftes aufbauen; nur ctypes mit KB-Rezept.",
                             _obj({"ctype": _S, "info": _NS, "target": _NS})),
    "narrate": ("Rein erzählerisch, kein Weltzustand betroffen.", _obj({})),
    "fill_water": ("Wasser am aktuellen (durchsuchten) Ort abfüllen/mitnehmen; "
                   "liters=null: so viel wie sinnvoll.", _obj({"liters": _NN})),
    "barricade": ("Den Ort, an dem man steht, verbarrikadieren (braucht Material "
                  "laut KB-Rezept).", _obj({"target": _NS})),
    "charge_device": ("Smartphone/Laptop laden (Stromnetz nur bei Strom am Ort, "
                      "sonst Speicher/Powerbank). source: 'grid', 'storage', 'powerbank' oder null = "
                      "was gerade geht.", _obj({"source": _NS})),
    "drink": ("Einen Liter Wasser trinken (Inventar, sonst Leitungswasser — nur "
              "solange am Ort Wasserdruck herrscht).", _obj({})),
    "leave_note": ("Eine kurze Nachricht/einen Zettel am aktuellen Ort hinterlassen.",
                   _obj({"text": _S})),
    "declare_item": ("Spielerwissen: der Spieler sagt, was in SEINER Wohnung oder bei "
                     "Nachbarn steht (z.B. ein 100-kWh-Akku im Keller). Beurteile die "
                     "Plausibilität (plausible=false + reason bei Absurdem wie einem A380 "
                     "im Keller) und liefere realistische Eigenschaften: Gewicht je Stück, "
                     "Menge, provides/requires nur aus heat|power|transmitter, bei "
                     "Energiespeichern capacity_kwh/charge_kwh. where='neighbour', wenn der "
                     "Gegenstand bei einem Nachbarn steht, sonst 'here'. Der Kern klemmt "
                     "alles und prüft den Ort.",
                     _obj({"name": _S, "category": {"type": "string", "enum": [
                               "food", "water", "tool", "material", "fuel", "medical", "misc"]},
                           "weight_kg": _NN, "quantity": _NN,
                           "provides": {"type": ["array", "null"], "items": _S},
                           "requires": {"type": ["array", "null"], "items": _S},
                           "capacity_kwh": _NN, "charge_kwh": _NN,
                           "plausible": {"type": "boolean"}, "reason": _S,
                           "where": {"type": "string", "enum": ["here", "neighbour"]}})),
}

FINISH_TOOL = "finish"
_FINISH_SPEC = {
    "description": "Beendet die Adjudikation. Erst aufrufen, wenn alle Vorschläge "
                   "beantwortet sind — allein in einem eigenen Schritt. "
                   "Narration beschreibt NUR, was der Kern akzeptiert hat.",
    "input_schema": _obj({
        "understanding": _S,
        "feasibility": {"type": "string", "enum": list(FEASIBILITY)},
        "reason": _S,
        "narration": _S,
    }),
}

PROPOSE_PREFIX = "propose_"


def propose_name(op: str) -> str:
    return PROPOSE_PREFIX + op


def op_of(tool_name: str) -> str | None:
    """Effekt-Op eines Vorschlags-Werkzeugs (None, wenn kein solches)."""
    if tool_name.startswith(PROPOSE_PREFIX):
        op = tool_name[len(PROPOSE_PREFIX):]
        if op in EFFECT_OPS:
            return op
    return None


# Die API erlaubt höchstens so viele Werkzeuge mit ``strict: true`` je Request.
MAX_STRICT_TOOLS = 20


def tool_specs(*, strict: bool = True) -> list[dict[str, Any]]:
    """Alle Werkzeuge im Anthropic-Tool-Format, in stabiler Reihenfolge
    (wichtig fürs Prompt-Caching).

    ``strict`` gilt nur für Vorschläge und ``finish`` — deren Eingaben verarbeitet
    der Sim-Kern weiter. Lese-Werkzeuge bleiben locker (die Toolbox prüft ihre
    Argumente selbst); so bleibt die Zahl strikter Werkzeuge unter dem API-Limit."""
    out: list[dict[str, Any]] = []
    n_strict = 0

    def add(name: str, desc: str, schema: dict[str, Any], *, want_strict: bool) -> None:
        nonlocal n_strict
        spec: dict[str, Any] = {"name": name, "description": desc, "input_schema": schema}
        if strict and want_strict and n_strict < MAX_STRICT_TOOLS:
            spec["strict"] = True
            n_strict += 1
        out.append(spec)

    for name, d in READ_TOOLS.items():
        add(name, d["description"], d["input_schema"], want_strict=False)
    # finish zuerst als strikt reservieren, damit es nie am Limit scheitert
    proposals = [(op, _PROPOSE_SPECS[op]) for op in EFFECT_OPS if op in _PROPOSE_SPECS]
    if strict and len(proposals) + 1 > MAX_STRICT_TOOLS:
        proposals_strict = MAX_STRICT_TOOLS - 1
    else:
        proposals_strict = len(proposals)
    for i, (op, (desc, schema)) in enumerate(proposals):
        add(propose_name(op), "Vorschlag: " + desc, schema, want_strict=i < proposals_strict)
    add(FINISH_TOOL, _FINISH_SPEC["description"], _FINISH_SPEC["input_schema"], want_strict=True)
    return out


def to_effect(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Normalisiert Werkzeug-Argumente zu einem Effekt-dict (``null`` entfällt,
    Item-Listen werden zu Mengen-Maps, ``info`` zu ``params``)."""
    eff: dict[str, Any] = {"op": op}
    for k, v in (args or {}).items():
        if v is None:
            continue
        if k == "items" and isinstance(v, list):
            m = {i["item"]: float(i.get("qty", 1)) for i in v
                 if isinstance(i, dict) and i.get("item")}
            if m:  # leere Liste = „alles"
                eff["items"] = m
        elif k == "info":
            eff["params"] = {"info": v}
        else:
            eff[k] = v
    return eff
