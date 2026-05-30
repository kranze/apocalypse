"""Loot-Tabellen je Location-Typ.

Pro Eintrag: ``chance`` (Wahrscheinlichkeit, dass das Item überhaupt vorkommt)
und ``qty_min``/``qty_max`` (Mengenspanne, inklusiv). Alle Items müssen im
``item_catalog`` existieren. Die Generierung zieht daraus deterministisch
(generation_seed), siehe ``generation.py``.

Bewusst grob für Schritt 1 — leicht erweiterbar, sobald der Item-Katalog wächst
(z.B. medizinische Items für pharmacy/hospital).
"""
from __future__ import annotations

# typ -> Liste von (item_id, chance, qty_min, qty_max)
LOOT_TABLES: dict[str, list[dict]] = {
    "supermarket": [
        {"item_id": "canned_beans", "chance": 0.95, "qty_min": 5, "qty_max": 40},
        {"item_id": "canned_meat", "chance": 0.90, "qty_min": 3, "qty_max": 30},
        {"item_id": "bread_loaf", "chance": 0.80, "qty_min": 2, "qty_max": 15},
        {"item_id": "milk_1l", "chance": 0.70, "qty_min": 1, "qty_max": 12},
        {"item_id": "water_1l", "chance": 0.95, "qty_min": 10, "qty_max": 60},
        {"item_id": "pasta_500g", "chance": 0.90, "qty_min": 5, "qty_max": 30},
    ],
    "fuel_station": [
        {"item_id": "water_1l", "chance": 0.80, "qty_min": 2, "qty_max": 20},
        {"item_id": "canned_beans", "chance": 0.60, "qty_min": 1, "qty_max": 10},
        {"item_id": "canned_meat", "chance": 0.50, "qty_min": 1, "qty_max": 8},
        {"item_id": "flashlight", "chance": 0.40, "qty_min": 1, "qty_max": 3},
        {"item_id": "firewood", "chance": 0.30, "qty_min": 1, "qty_max": 5},
    ],
    "pharmacy": [
        {"item_id": "water_1l", "chance": 0.60, "qty_min": 1, "qty_max": 8},
        {"item_id": "flashlight", "chance": 0.50, "qty_min": 1, "qty_max": 2},
    ],
    "hospital": [
        {"item_id": "water_1l", "chance": 0.70, "qty_min": 2, "qty_max": 15},
        {"item_id": "canned_beans", "chance": 0.40, "qty_min": 1, "qty_max": 6},
    ],
    "hardware": [
        {"item_id": "crowbar", "chance": 0.70, "qty_min": 1, "qty_max": 3},
        {"item_id": "flashlight", "chance": 0.60, "qty_min": 1, "qty_max": 4},
        {"item_id": "firewood", "chance": 0.80, "qty_min": 3, "qty_max": 20},
    ],
    "house": [
        {"item_id": "canned_beans", "chance": 0.50, "qty_min": 1, "qty_max": 4},
        {"item_id": "bread_loaf", "chance": 0.40, "qty_min": 1, "qty_max": 2},
        {"item_id": "milk_1l", "chance": 0.30, "qty_min": 1, "qty_max": 2},
        {"item_id": "water_1l", "chance": 0.60, "qty_min": 1, "qty_max": 6},
        {"item_id": "pasta_500g", "chance": 0.40, "qty_min": 1, "qty_max": 3},
        {"item_id": "flashlight", "chance": 0.20, "qty_min": 1, "qty_max": 1},
        {"item_id": "firewood", "chance": 0.30, "qty_min": 1, "qty_max": 6},
    ],
    # generisches Nicht-Wohngebäude: spärlich
    "building": [
        {"item_id": "water_1l", "chance": 0.30, "qty_min": 1, "qty_max": 4},
        {"item_id": "canned_beans", "chance": 0.20, "qty_min": 1, "qty_max": 3},
    ],
}

# Fallback für unbekannte Typen.
DEFAULT_TABLE_KEY = "building"
