"""Lokaler Straßengraph für Fuß-Routing.

Holt das Straßennetz (``highway=*``) einmal via Overpass (gecacht wie die
Gebäude), baut daraus einen ungerichteten Graph aus OSM-Knoten und findet per
Dijkstra den kürzesten Fußweg. Offline nach dem ersten Fetch, deterministisch,
und später für NPCs/Fahrzeuge wiederverwendbar.

Der Graph wird pro Prozess einmal gebaut und gecacht (``get_graph``).
"""
from __future__ import annotations

import heapq
import math
from typing import Any

from .. import config
from . import overpass

# Fußgänger ignorieren in Schritt 1 keine Wege; nur klar nicht-begehbares ausschließen.
_EXCLUDE_HIGHWAY = {"motorway", "motorway_link", "trunk", "trunk_link"}

_M_PER_DEG_LAT = 111_320.0


def build_roads_query(lat: float, lon: float, radius_m: int) -> str:
    return f"""
[out:json][timeout:{config.OVERPASS_TIMEOUT_S}];
way["highway"](around:{radius_m},{lat},{lon});
out geom;
""".strip()


def fetch_roads(lat: float, lon: float, radius_m: int, *, force: bool = False) -> dict[str, Any]:
    return overpass.fetch_query(
        build_roads_query(lat, lon, radius_m), lat, lon, radius_m, tag="roads", force=force
    )


def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distanz in Metern (lokale äquirektanguläre Näherung)."""
    lat1, lon1 = a
    lat2, lon2 = b
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    dx = (lon2 - lon1) * m_per_deg_lon
    dy = (lat2 - lat1) * _M_PER_DEG_LAT
    return math.hypot(dx, dy)


class RoadGraph:
    """Ungerichteter Graph: node_id -> (lat, lon); Adjazenz node_id -> {nachbar: dist}."""

    def __init__(self) -> None:
        self.coords: dict[int, tuple[float, float]] = {}
        self.adj: dict[int, dict[int, float]] = {}

    def _add_edge(self, a: int, b: int) -> None:
        d = _dist_m(self.coords[a], self.coords[b])
        self.adj.setdefault(a, {})[b] = d
        self.adj.setdefault(b, {})[a] = d

    @property
    def node_count(self) -> int:
        return len(self.coords)

    def nearest_node(self, lat: float, lon: float) -> int | None:
        best, best_d = None, float("inf")
        for nid, (nlat, nlon) in self.coords.items():
            d = _dist_m((lat, lon), (nlat, nlon))
            if d < best_d:
                best, best_d = nid, d
        return best

    def shortest_path(self, start: int, goal: int) -> tuple[list[tuple[float, float]], float]:
        """Dijkstra. Liefert (Wegpunkte als (lat,lon)-Liste, Gesamtdistanz_m).
        Leere Liste + inf, wenn kein Weg existiert."""
        if start == goal:
            return [self.coords[start]], 0.0
        dist = {start: 0.0}
        prev: dict[int, int] = {}
        pq: list[tuple[float, int]] = [(0.0, start)]
        visited: set[int] = set()
        while pq:
            d, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)
            if u == goal:
                break
            for v, w in self.adj.get(u, {}).items():
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if goal not in dist:
            return [], float("inf")
        # Pfad rückwärts rekonstruieren.
        path_ids = [goal]
        while path_ids[-1] != start:
            path_ids.append(prev[path_ids[-1]])
        path_ids.reverse()
        return [self.coords[nid] for nid in path_ids], dist[goal]


def build_graph(data: dict[str, Any]) -> RoadGraph:
    """Baut den Graph aus einer Overpass-``out geom``-Antwort (Ways mit
    ``nodes`` + ``geometry``)."""
    g = RoadGraph()
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        tags = el.get("tags", {}) or {}
        if tags.get("highway") in _EXCLUDE_HIGHWAY:
            continue
        node_ids = el.get("nodes") or []
        geom = el.get("geometry") or []
        if len(node_ids) != len(geom) or len(node_ids) < 2:
            continue
        for nid, pt in zip(node_ids, geom):
            g.coords[nid] = (pt["lat"], pt["lon"])
        for a, b in zip(node_ids, node_ids[1:]):
            g._add_edge(a, b)
    return g


# --- Prozess-Cache ------------------------------------------------------
_graph: RoadGraph | None = None


def get_graph(
    lat: float | None = None,
    lon: float | None = None,
    radius_m: int | None = None,
    *,
    force: bool = False,
) -> RoadGraph:
    """Lazy: baut den Graph einmal aus dem (gecachten) Straßennetz um das
    konfigurierte Viertel und hält ihn im Prozess."""
    global _graph
    if _graph is not None and not force:
        return _graph
    lat = config.CENTER_LAT if lat is None else lat
    lon = config.CENTER_LON if lon is None else lon
    # etwas größerer Radius als das Viertel, damit Randwege verbunden bleiben
    radius_m = (config.RADIUS_M + 200) if radius_m is None else radius_m
    _graph = build_graph(fetch_roads(lat, lon, radius_m, force=force))
    return _graph
