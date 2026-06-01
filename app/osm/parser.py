"""Overpass-JSON -> Location-Records.

Gebäude (ways mit ``geometry``) werden zu Locations mit Centroid + Footprint.
Relevante POI-Nodes (Shop/Amenity) verfeinern den Typ des sie enthaltenden
Gebäudes; liegt ein POI in keinem Gebäude, wird er als eigenständige Location
ohne Footprint aufgenommen.
"""
from __future__ import annotations

import json
from typing import Any

from . import geometry, tagmap


def _name(tags: dict[str, str]) -> str | None:
    return tags.get("name")


def _way_coords(element: dict[str, Any]) -> list[geometry.Coord]:
    return [(pt["lat"], pt["lon"]) for pt in element.get("geometry", [])]


def parse(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Wandelt eine Overpass-Antwort in deduplizierte Location-Records um.

    Record-Felder: osm_id, type, name, lat, lon, footprint_m2 (oder None).
    """
    elements = data.get("elements", [])

    # 1) Gebäude (ways mit Geometrie).
    buildings: dict[str, dict[str, Any]] = {}
    building_geoms: dict[str, list[geometry.Coord]] = {}
    for el in elements:
        if el.get("type") != "way":
            continue
        tags = el.get("tags", {}) or {}
        if "building" not in tags and tagmap.classify(tags) is None:
            continue
        coords = _way_coords(el)
        if len(coords) < 3:
            continue

        osm_id = f"way/{el['id']}"
        lat, lon = geometry.centroid(coords)
        loc_type = tagmap.classify(tags) or "house"
        buildings[osm_id] = {
            "osm_id": osm_id,
            "type": loc_type,
            "label": tagmap.label(tags) or "Gebäude",
            "name": _name(tags),
            "lat": lat,
            "lon": lon,
            "footprint_m2": round(geometry.area_m2(coords), 1),
            "footprint_json": json.dumps([[la, lo] for la, lo in coords]),
        }
        building_geoms[osm_id] = coords

    # 2) POI-Nodes: in enthaltendes Gebäude einordnen oder eigenständig anlegen.
    standalone: dict[str, dict[str, Any]] = {}
    for el in elements:
        if el.get("type") != "node":
            continue
        tags = el.get("tags", {}) or {}
        poi_type = tagmap.classify(tags)
        if poi_type is None:
            continue
        lat, lon = el.get("lat"), el.get("lon")
        if lat is None or lon is None:
            continue

        container = None
        for osm_id, coords in building_geoms.items():
            if geometry.point_in_polygon(lat, lon, coords):
                container = osm_id
                break

        if container is not None:
            # Typ + Label des Gebäudes präzisieren (POI ist spezifischer).
            buildings[container]["type"] = poi_type
            buildings[container]["label"] = tagmap.label(tags) or buildings[container]["label"]
            if not buildings[container].get("name"):
                buildings[container]["name"] = _name(tags)
        else:
            osm_id = f"node/{el['id']}"
            standalone[osm_id] = {
                "osm_id": osm_id,
                "type": poi_type,
                "label": tagmap.label(tags) or "Ort",
                "name": _name(tags),
                "lat": lat,
                "lon": lon,
                "footprint_m2": None,
                "footprint_json": None,
            }

    return list(buildings.values()) + list(standalone.values())
