"""Klassifikation von OSM-Tags auf interne ``location.type``-Werte.

Dict-getrieben und leicht erweiterbar. ``classify`` liefert den spezifischsten
Treffer; nicht zugeordnete Gebäude fallen auf ``house`` zurück (über den
Aufrufer), reine POIs ohne Treffer ergeben ``None`` und werden übersprungen.
"""
from __future__ import annotations

# Spezifische Treffer zuerst geprüft (Reihenfolge = Priorität).
_SHOP = {
    "supermarket": "supermarket",
    "convenience": "supermarket",
    "doityourself": "hardware",
    "hardware": "hardware",
    "trade": "hardware",
}

_AMENITY = {
    "fuel": "fuel_station",
    "pharmacy": "pharmacy",
    "hospital": "hospital",
    "doctors": "hospital",
}

# building=<wert> -> Typ. 'yes' und Wohn-Werte werden zu 'house'.
_BUILDING_HOUSE = {
    "house",
    "residential",
    "detached",
    "apartments",
    "terrace",
    "semidetached_house",
    "bungalow",
    "yes",
}


def classify(tags: dict[str, str]) -> str | None:
    """Bestimmt den Location-Typ aus OSM-Tags.

    Returns:
        Konkreter Typ-String, ``"house"`` als Gebäude-Fallback, oder ``None``
        wenn das Objekt uninteressant ist (z.B. POI-Node ohne bekannten Tag).
    """
    shop = tags.get("shop")
    if shop and shop in _SHOP:
        return _SHOP[shop]

    amenity = tags.get("amenity")
    if amenity and amenity in _AMENITY:
        return _AMENITY[amenity]

    building = tags.get("building")
    if building:
        # Wohn-Werte -> house, sonstige bebaute Footprints -> generisch 'building'.
        return "house" if building in _BUILDING_HOUSE else "building"

    return None
