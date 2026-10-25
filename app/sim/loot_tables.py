"""Loot-Tabellen je Location-Typ.

Pro Eintrag: ``chance`` (Wahrscheinlichkeit, dass das Item überhaupt vorkommt)
und ``qty_min``/``qty_max`` (Mengenspanne, inklusiv). Alle Items müssen im
``item_catalog`` existieren (Seed in ``schema.sql`` / ``app/db.py``). Die
Generierung zieht daraus deterministisch aus ``generation_seed``, siehe
``generation.py``.

Abgedeckt sind alle Typen, die ``app/osm/tagmap.py`` erzeugt: supermarket
(inkl. Kiosk/Convenience), fuel_station, pharmacy, hospital (inkl. Arztpraxis),
hardware, house (inkl. Wohnhaus/Mehrfamilienhaus/Reihenhaus ...) und das
generische ``building``. Für die erste Spielwoche gilt: Trinkwasser und
Nahrung kommen an realistischen Orten vor, in Wohnhäusern aber nur in
kleinen Mengen.
"""
from __future__ import annotations

# typ -> Liste von Einträgen {item_id, chance, qty_min, qty_max}
LOOT_TABLES: dict[str, list[dict]] = {
    "supermarket": [
        {"item_id": "water_1l", "chance": 0.97, "qty_min": 12, "qty_max": 60},
        {"item_id": "canned_beans", "chance": 0.95, "qty_min": 6, "qty_max": 40},
        {"item_id": "canned_meat", "chance": 0.90, "qty_min": 4, "qty_max": 30},
        {"item_id": "canned_soup", "chance": 0.85, "qty_min": 4, "qty_max": 25},
        {"item_id": "bread_loaf", "chance": 0.80, "qty_min": 2, "qty_max": 15},
        {"item_id": "milk_1l", "chance": 0.70, "qty_min": 1, "qty_max": 12},
        {"item_id": "pasta_500g", "chance": 0.90, "qty_min": 5, "qty_max": 30},
        {"item_id": "crackers", "chance": 0.80, "qty_min": 3, "qty_max": 20},
        {"item_id": "chocolate", "chance": 0.70, "qty_min": 3, "qty_max": 25},
        {"item_id": "bandage", "chance": 0.30, "qty_min": 1, "qty_max": 5},
        {"item_id": "painkillers", "chance": 0.25, "qty_min": 1, "qty_max": 4},
        {"item_id": "powerbank", "chance": 0.25, "qty_min": 1, "qty_max": 2},
    ],
    "fuel_station": [
        {"item_id": "water_1l", "chance": 0.90, "qty_min": 4, "qty_max": 24},
        {"item_id": "chocolate", "chance": 0.80, "qty_min": 2, "qty_max": 15},
        {"item_id": "crackers", "chance": 0.60, "qty_min": 1, "qty_max": 8},
        {"item_id": "canned_soup", "chance": 0.40, "qty_min": 1, "qty_max": 6},
        {"item_id": "bread_loaf", "chance": 0.40, "qty_min": 1, "qty_max": 4},
        {"item_id": "flashlight", "chance": 0.40, "qty_min": 1, "qty_max": 3},
        {"item_id": "duct_tape", "chance": 0.40, "qty_min": 1, "qty_max": 3},
        {"item_id": "gasoline", "chance": 0.50, "qty_min": 1, "qty_max": 4},
        {"item_id": "firewood", "chance": 0.30, "qty_min": 1, "qty_max": 5},
        {"item_id": "powerbank", "chance": 0.30, "qty_min": 1, "qty_max": 1},
    ],
    "pharmacy": [
        {"item_id": "bandage", "chance": 0.95, "qty_min": 5, "qty_max": 30},
        {"item_id": "painkillers", "chance": 0.90, "qty_min": 3, "qty_max": 25},
        {"item_id": "first_aid_kit", "chance": 0.70, "qty_min": 1, "qty_max": 5},
        {"item_id": "water_1l", "chance": 0.60, "qty_min": 1, "qty_max": 8},
        {"item_id": "chocolate", "chance": 0.40, "qty_min": 1, "qty_max": 6},
        {"item_id": "flashlight", "chance": 0.40, "qty_min": 1, "qty_max": 2},
    ],
    "hospital": [
        {"item_id": "bandage", "chance": 0.95, "qty_min": 10, "qty_max": 60},
        {"item_id": "painkillers", "chance": 0.90, "qty_min": 5, "qty_max": 40},
        {"item_id": "first_aid_kit", "chance": 0.80, "qty_min": 2, "qty_max": 10},
        {"item_id": "water_1l", "chance": 0.80, "qty_min": 4, "qty_max": 25},
        {"item_id": "canned_soup", "chance": 0.40, "qty_min": 1, "qty_max": 8},
        {"item_id": "crackers", "chance": 0.40, "qty_min": 1, "qty_max": 8},
        {"item_id": "flashlight", "chance": 0.50, "qty_min": 1, "qty_max": 4},
    ],
    "hardware": [
        {"item_id": "hammer", "chance": 0.85, "qty_min": 1, "qty_max": 6},
        {"item_id": "crowbar", "chance": 0.70, "qty_min": 1, "qty_max": 3},
        {"item_id": "flashlight", "chance": 0.70, "qty_min": 1, "qty_max": 5},
        {"item_id": "rope", "chance": 0.80, "qty_min": 1, "qty_max": 8},
        {"item_id": "duct_tape", "chance": 0.85, "qty_min": 2, "qty_max": 12},
        {"item_id": "firewood", "chance": 0.80, "qty_min": 3, "qty_max": 20},
        {"item_id": "gasoline", "chance": 0.40, "qty_min": 1, "qty_max": 3},
        {"item_id": "generator", "chance": 0.15, "qty_min": 1, "qty_max": 1},
        {"item_id": "bicycle", "chance": 0.35, "qty_min": 1, "qty_max": 2},
        {"item_id": "water_1l", "chance": 0.30, "qty_min": 1, "qty_max": 6},
    ],
    # Wohnhäuser: kleine Vorräte, ein paar Werkzeuge.
    "house": [
        {"item_id": "water_1l", "chance": 0.70, "qty_min": 2, "qty_max": 8},
        {"item_id": "canned_beans", "chance": 0.55, "qty_min": 1, "qty_max": 4},
        {"item_id": "canned_soup", "chance": 0.40, "qty_min": 1, "qty_max": 3},
        {"item_id": "pasta_500g", "chance": 0.50, "qty_min": 1, "qty_max": 3},
        {"item_id": "bread_loaf", "chance": 0.40, "qty_min": 1, "qty_max": 2},
        {"item_id": "crackers", "chance": 0.35, "qty_min": 1, "qty_max": 3},
        {"item_id": "chocolate", "chance": 0.30, "qty_min": 1, "qty_max": 3},
        {"item_id": "milk_1l", "chance": 0.30, "qty_min": 1, "qty_max": 2},
        {"item_id": "bandage", "chance": 0.30, "qty_min": 1, "qty_max": 3},
        {"item_id": "painkillers", "chance": 0.30, "qty_min": 1, "qty_max": 2},
        {"item_id": "flashlight", "chance": 0.25, "qty_min": 1, "qty_max": 1},
        {"item_id": "hammer", "chance": 0.15, "qty_min": 1, "qty_max": 1},
        {"item_id": "duct_tape", "chance": 0.15, "qty_min": 1, "qty_max": 1},
        {"item_id": "firewood", "chance": 0.25, "qty_min": 1, "qty_max": 6},
        {"item_id": "laptop", "chance": 0.15, "qty_min": 1, "qty_max": 1},
        {"item_id": "powerbank", "chance": 0.12, "qty_min": 1, "qty_max": 1},
        {"item_id": "bicycle", "chance": 0.05, "qty_min": 1, "qty_max": 1},
    ],
    # Die eigene Wohnung (Issue #54): kleiner Grundbestand an Alltagsdingen.
    "home": [
        {"item_id": "water_1l", "chance": 0.90, "qty_min": 3, "qty_max": 10},
        {"item_id": "canned_beans", "chance": 0.70, "qty_min": 1, "qty_max": 4},
        {"item_id": "canned_soup", "chance": 0.60, "qty_min": 1, "qty_max": 3},
        {"item_id": "pasta_500g", "chance": 0.75, "qty_min": 1, "qty_max": 3},
        {"item_id": "bread_loaf", "chance": 0.50, "qty_min": 1, "qty_max": 2},
        {"item_id": "crackers", "chance": 0.50, "qty_min": 1, "qty_max": 2},
        {"item_id": "chocolate", "chance": 0.50, "qty_min": 1, "qty_max": 3},
        {"item_id": "bandage", "chance": 0.60, "qty_min": 1, "qty_max": 3},
        {"item_id": "painkillers", "chance": 0.60, "qty_min": 1, "qty_max": 2},
        {"item_id": "flashlight", "chance": 0.55, "qty_min": 1, "qty_max": 1},
        {"item_id": "duct_tape", "chance": 0.35, "qty_min": 1, "qty_max": 1},
        {"item_id": "hammer", "chance": 0.30, "qty_min": 1, "qty_max": 1},
    ],
    # generisches Nicht-Wohngebäude: spärlich
    "building": [
        {"item_id": "water_1l", "chance": 0.35, "qty_min": 1, "qty_max": 4},
        {"item_id": "canned_beans", "chance": 0.20, "qty_min": 1, "qty_max": 3},
        {"item_id": "crackers", "chance": 0.20, "qty_min": 1, "qty_max": 3},
        {"item_id": "bandage", "chance": 0.15, "qty_min": 1, "qty_max": 2},
        {"item_id": "duct_tape", "chance": 0.10, "qty_min": 1, "qty_max": 1},
    ],
}

# Fallback für unbekannte Typen.
DEFAULT_TABLE_KEY = "building"
