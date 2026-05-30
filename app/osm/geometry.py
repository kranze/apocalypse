"""Leichtgewichtige Geometrie auf lat/lon — ohne externe Geo-Libs.

Koordinaten sind Listen von ``(lat, lon)``-Tupeln (Polygon-Ring, erster Punkt
kann gleich dem letzten sein). Flächen werden über eine lokale
äquirektanguläre Projektion um den Centroid in m² berechnet — für
Gebäude-Footprints (wenige zehn bis hundert Meter) ausreichend genau.
"""
from __future__ import annotations

import math

Coord = tuple[float, float]  # (lat, lon)

_M_PER_DEG_LAT = 111_320.0  # Meter pro Breitengrad (nahezu konstant)


def centroid(coords: list[Coord]) -> Coord:
    """Arithmetischer Schwerpunkt der Ringpunkte (ohne doppelten Schlusspunkt)."""
    pts = _strip_closing(coords)
    n = len(pts)
    lat = sum(p[0] for p in pts) / n
    lon = sum(p[1] for p in pts) / n
    return (lat, lon)


def area_m2(coords: list[Coord]) -> float:
    """Polygonfläche in m² via Shoelace auf lokaler Projektion. 0.0 bei < 3 Punkten."""
    pts = _strip_closing(coords)
    if len(pts) < 3:
        return 0.0

    lat0, _ = centroid(pts)
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians(lat0))

    # Projektion auf lokale Meter-Ebene relativ zum ersten Punkt.
    ref_lat, ref_lon = pts[0]
    xy = [
        ((lon - ref_lon) * m_per_deg_lon, (lat - ref_lat) * _M_PER_DEG_LAT)
        for lat, lon in pts
    ]

    s = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_in_polygon(lat: float, lon: float, coords: list[Coord]) -> bool:
    """Ray-Casting: liegt (lat, lon) innerhalb des Polygons?"""
    pts = _strip_closing(coords)
    if len(pts) < 3:
        return False

    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        yi, xi = pts[i]  # lat, lon
        yj, xj = pts[j]
        intersects = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _strip_closing(coords: list[Coord]) -> list[Coord]:
    """Entfernt einen doppelten Schlusspunkt (geschlossener Ring)."""
    if len(coords) >= 2 and coords[0] == coords[-1]:
        return coords[:-1]
    return coords
