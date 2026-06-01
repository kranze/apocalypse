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


# Deutsche Anzeige-Labels (Display), feiner als der mechanische `type`.
_SHOP_LABEL = {
    "supermarket": "Supermarkt", "convenience": "Kiosk",
    "doityourself": "Baumarkt", "hardware": "Baumarkt", "trade": "Baumarkt",
    "bakery": "Bäckerei", "butcher": "Metzgerei", "kiosk": "Kiosk",
}
_AMENITY_LABEL = {
    "fuel": "Tankstelle", "pharmacy": "Apotheke", "hospital": "Krankenhaus",
    "doctors": "Arztpraxis", "clinic": "Klinik",
}
_BUILDING_LABEL = {
    "house": "Einfamilienhaus", "detached": "Einfamilienhaus",
    "residential": "Wohnhaus", "apartments": "Mehrfamilienhaus",
    "terrace": "Reihenhaus", "semidetached_house": "Doppelhaus",
    "bungalow": "Bungalow", "commercial": "Geschäftshaus", "retail": "Geschäftshaus",
    "industrial": "Industriegebäude", "warehouse": "Lagerhalle",
    "garage": "Garage", "garages": "Garagen", "hut": "Hütte", "cabin": "Hütte",
    "church": "Kirche", "chapel": "Kapelle", "school": "Schule",
    "kindergarten": "Kindergarten", "hospital": "Krankenhaus", "yes": "Gebäude",
}


def label(tags: dict[str, str]) -> str | None:
    """Deutsches Anzeige-Label aus OSM-Tags (z.B. 'Supermarkt', 'Einfamilienhaus').

    Reihenfolge wie ``classify``: shop > amenity > building. Liefert None, wenn
    nichts passt."""
    shop = tags.get("shop")
    if shop:
        return _SHOP_LABEL.get(shop, "Geschäft")
    amenity = tags.get("amenity")
    if amenity and amenity in _AMENITY_LABEL:
        return _AMENITY_LABEL[amenity]
    building = tags.get("building")
    if building:
        return _BUILDING_LABEL.get(building, "Gebäude")
    return None


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
