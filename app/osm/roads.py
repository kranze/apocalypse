"""Lokaler Straßengraph für Fuß-Routing.

Holt das Straßennetz (``highway=*``) einmal via Overpass (gecacht wie die
Gebäude), baut daraus einen ungerichteten Graph aus OSM-Knoten und findet per
A* den kürzesten Fußweg. Offline nach dem ersten Fetch, deterministisch,
und später für NPCs/Fahrzeuge wiederverwendbar.

Der Graph wächst additiv: jedes geladene Chunk erweitert den prozessweiten
``_graph`` via ``merge_ways``. OSM-Node-IDs sind global eindeutig, Merge ist
idempotent (doppelte Kanten werden einfach überschrieben, Koordinaten sind
unveränderlich).
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

# Gitter-Bucket-Größe für nearest_node-Index (in Grad).
_BUCKET_DEG = 0.01


def build_roads_query(lat: float, lon: float, radius_m: int) -> str:
    return f"""
[out:json][timeout:{config.OVERPASS_TIMEOUT_S}];
way["highway"](around:{radius_m},{lat},{lon});
out geom;
""".strip()


def build_roads_bbox_query(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> str:
    return f"""
[out:json][timeout:{config.OVERPASS_TIMEOUT_S}];
way["highway"]({min_lat},{min_lon},{max_lat},{max_lon});
out geom;
""".strip()


def fetch_roads(lat: float, lon: float, radius_m: int, *, force: bool = False) -> dict[str, Any]:
    return overpass.fetch_query(
        build_roads_query(lat, lon, radius_m), lat, lon, radius_m, tag="roads", force=force
    )


def fetch_roads_bbox(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float, *, force: bool = False
) -> dict[str, Any]:
    """Holt Straßen für eine Bounding-Box (für Chunk-Laden)."""
    # Nutze center+radius-Cache-Schlüssel via overpass.fetch_query nicht direkt
    # möglich; wir konstruieren einen ad-hoc-Schlüssel über fetch_query mit
    # bbox-Parametern. Alternativ direkter Aufruf – hier nutzen wir die
    # bbox-Variante von fetch_query, falls vorhanden, sonst fetch_roads um Center.
    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2
    # Radius als halbe Diagonale der Box in Metern + kleiner Puffer
    dlat = (max_lat - min_lat) * _M_PER_DEG_LAT
    dlon = (max_lon - min_lon) * _M_PER_DEG_LAT * math.cos(math.radians(center_lat))
    radius_m = int(math.hypot(dlat, dlon) / 2) + 100
    return overpass.fetch_query(
        build_roads_query(center_lat, center_lon, radius_m),
        center_lat, center_lon, radius_m,
        tag="roads",
        force=force,
    )


def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distanz in Metern (lokale äquirektanguläre Näherung)."""
    lat1, lon1 = a
    lat2, lon2 = b
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    dx = (lon2 - lon1) * m_per_deg_lon
    dy = (lat2 - lat1) * _M_PER_DEG_LAT
    return math.hypot(dx, dy)


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine-Distanz in Metern (zulässige A*-Heuristik: <= echte Pfadlänge)."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    sin_dlat = math.sin(dlat / 2)
    sin_dlon = math.sin(dlon / 2)
    aval = sin_dlat * sin_dlat + math.cos(lat1) * math.cos(lat2) * sin_dlon * sin_dlon
    return 6_371_000.0 * 2 * math.atan2(math.sqrt(aval), math.sqrt(1 - aval))


def _bucket_key(lat: float, lon: float) -> tuple[int, int]:
    return (math.floor(lat / _BUCKET_DEG), math.floor(lon / _BUCKET_DEG))


class RoadGraph:
    """Ungerichteter Graph: node_id -> (lat, lon); Adjazenz node_id -> {nachbar: dist}.

    Additiv: ``merge_ways`` kann mehrfach aufgerufen werden; bestehende Knoten/
    Kanten werden idempotent überschrieben (OSM-IDs sind global eindeutig,
    Koordinaten unveränderlich). Der Gitter-Hash-Index wird inkrementell gepflegt.
    """

    def __init__(self) -> None:
        self.coords: dict[int, tuple[float, float]] = {}
        self.adj: dict[int, dict[int, float]] = {}
        # Gitter-Hash-Index: bucket_key -> set of node_ids
        self._bucket: dict[tuple[int, int], set[int]] = {}

    def _add_to_index(self, nid: int, lat: float, lon: float) -> None:
        bk = _bucket_key(lat, lon)
        if bk not in self._bucket:
            self._bucket[bk] = set()
        self._bucket[bk].add(nid)

    def _add_edge(self, a: int, b: int) -> None:
        d = _dist_m(self.coords[a], self.coords[b])
        self.adj.setdefault(a, {})[b] = d
        self.adj.setdefault(b, {})[a] = d

    @property
    def node_count(self) -> int:
        return len(self.coords)

    def merge_ways(self, data: dict[str, Any]) -> None:
        """Fügt highway-Ways aus einer Overpass-``out geom``-Antwort additiv ein.

        OSM-Node-IDs sind global eindeutig → Merge ist idempotent:
        - Knoten mit bekannter ID werden nur hinzugefügt, nie überschrieben.
        - Kanten (dist-Berechnung) werden bei jedem Merge neu gesetzt (gleicher Wert).
        - Der Gitter-Index wird für neue Knoten erweitert.
        """
        for el in data.get("elements", []):
            if el.get("type") != "way":
                continue
            tags = el.get("tags", {}) or {}
            # Nur highway-Ways aufnehmen (Pflicht-Filter für kombinierte Queries,
            # bei denen building-Ways und highway-Ways gemischt ankommen).
            if not tags.get("highway"):
                continue
            if tags.get("highway") in _EXCLUDE_HIGHWAY:
                continue
            node_ids = el.get("nodes") or []
            geom = el.get("geometry") or []
            if len(node_ids) != len(geom) or len(node_ids) < 2:
                continue
            for nid, pt in zip(node_ids, geom):
                if nid not in self.coords:
                    lat, lon = pt["lat"], pt["lon"]
                    self.coords[nid] = (lat, lon)
                    self._add_to_index(nid, lat, lon)
            for a, b in zip(node_ids, node_ids[1:]):
                self._add_edge(a, b)

    def nearest_node(self, lat: float, lon: float) -> int | None:
        """Nächster Graph-Knoten via Gitter-Hash-Index (O(1) im Normalfall).

        Sucht im eigenen Bucket + 8 Nachbar-Buckets. Falls leer, wird der
        Suchradius schrittweise vergrößert. Fallback auf lineare Suche wenn
        der Index komplett leer ist.
        """
        if not self.coords:
            return None

        bx, by = _bucket_key(lat, lon)
        best: int | None = None
        best_d = float("inf")

        # Wachsender Suchradius in Bucket-Einheiten.
        max_radius = max(
            math.ceil(max(abs(k[0] - bx) for k in self._bucket.keys()) if self._bucket else 1),
            math.ceil(max(abs(k[1] - by) for k in self._bucket.keys()) if self._bucket else 1),
            2,
        ) if self._bucket else 2

        for radius in range(0, max_radius + 1):
            # Alle Buckets im Quadrat (bx±radius, by±radius)
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    # Nur Rand des Quadrats beim radius > 0 (Schale)
                    if radius > 0 and abs(dx) < radius and abs(dy) < radius:
                        continue
                    bucket = self._bucket.get((bx + dx, by + dy))
                    if bucket is None:
                        continue
                    for nid in bucket:
                        d = _dist_m((lat, lon), self.coords[nid])
                        if d < best_d:
                            best, best_d = nid, d

            if best is not None:
                # Prüfen ob ein weiter entfernter Bucket noch näher liegen könnte.
                # Worst-case: nächster Bucket-Rand hat Mindestabstand (radius)*_BUCKET_DEG*111320
                min_possible_from_next_shell = (radius) * _BUCKET_DEG * _M_PER_DEG_LAT * 0.7
                if best_d <= min_possible_from_next_shell:
                    break
                # Sonst: weiter suchen bis wir sicher sind.
                if radius >= max_radius:
                    break

        return best

    def shortest_path(self, start: int, goal: int) -> tuple[list[tuple[float, float]], float]:
        """A* mit Haversine-Heuristik und deterministischem Tie-Break via node_id.

        Heap-Einträge: (f_score, node_id, g_score) — bei gleichem f_score
        entscheidet die node_id → reproduzierbarer Pfad unabhängig von
        Einfügereihenfolge.

        Liefert (Wegpunkte als (lat,lon)-Liste, Gesamtdistanz_m).
        Leere Liste + inf, wenn kein Weg existiert.
        """
        if start == goal:
            return [self.coords[start]], 0.0

        goal_coord = self.coords[goal]

        def h(nid: int) -> float:
            return _haversine_m(self.coords[nid], goal_coord)

        g_score: dict[int, float] = {start: 0.0}
        prev: dict[int, int] = {}
        # (f_score, node_id, g_score) — deterministischer Tie-Break via node_id
        pq: list[tuple[float, int, float]] = [(h(start), start, 0.0)]
        closed: set[int] = set()

        while pq:
            f, u, g = heapq.heappop(pq)
            if u in closed:
                continue
            closed.add(u)
            if u == goal:
                break
            for v, w in self.adj.get(u, {}).items():
                if v in closed:
                    continue
                ng = g + w
                if ng < g_score.get(v, float("inf")):
                    g_score[v] = ng
                    prev[v] = u
                    heapq.heappush(pq, (ng + h(v), v, ng))

        if goal not in g_score or goal not in closed:
            return [], float("inf")

        # Pfad rückwärts rekonstruieren.
        path_ids = [goal]
        while path_ids[-1] != start:
            path_ids.append(prev[path_ids[-1]])
        path_ids.reverse()
        return [self.coords[nid] for nid in path_ids], g_score[goal]


def build_graph(data: dict[str, Any]) -> RoadGraph:
    """Baut einen neuen Graph aus einer Overpass-``out geom``-Antwort.

    Für Kompatibilität mit bestehenden Tests und new_game.
    Intern nutzt es merge_ways auf einem leeren Graph.
    """
    g = RoadGraph()
    g.merge_ways(data)
    return g


# ---------------------------------------------------------------------------
# Additiver prozessweiter Graph + geladene Chunks
# ---------------------------------------------------------------------------

_graph: RoadGraph = RoadGraph()
_roads_loaded_chunks: set[tuple[int, int]] = set()

# Rückwärts-Kompatibilitäts-Cache (für get_graph-Semantik)
_graph_key: tuple | None = None


def ensure_roads_for_chunk(cx: int, cy: int, *, overlap_deg: float = 0.002) -> None:
    """Lädt Straßen für Chunk (cx, cy) in den prozessweiten additiven Graph.

    Idempotent: bereits geladene Chunks werden übersprungen.
    Der Overlap sorgt dafür, dass Wege an Chunk-Grenzen verbunden bleiben.
    Fehler (Netz, Parsing) werden geloggt, kein Crash.
    """
    if (cx, cy) in _roads_loaded_chunks:
        return

    min_lat = cx * config.CHUNK_DEG - overlap_deg
    min_lon = cy * config.CHUNK_DEG - overlap_deg
    max_lat = min_lat + config.CHUNK_DEG + 2 * overlap_deg
    max_lon = min_lon + config.CHUNK_DEG + 2 * overlap_deg

    try:
        data = fetch_roads_bbox(min_lat, min_lon, max_lat, max_lon)
        _graph.merge_ways(data)
    except Exception:
        # Straßen-Fehler darf Chunk-Load nicht crashen; Chunk bleibt unmarkiert
        # → nächster Versuch beim nächsten ensure-Aufruf.
        return

    _roads_loaded_chunks.add((cx, cy))


def ensure_roads_for_bbox(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> None:
    """Stellt sicher, dass Straßen für alle Chunks in der Bbox geladen sind."""
    import math as _math
    cx_min = _math.floor(min_lat / config.CHUNK_DEG)
    cy_min = _math.floor(min_lon / config.CHUNK_DEG)
    cx_max = _math.floor(max_lat / config.CHUNK_DEG)
    cy_max = _math.floor(max_lon / config.CHUNK_DEG)
    for cx in range(cx_min, cx_max + 1):
        for cy in range(cy_min, cy_max + 1):
            ensure_roads_for_chunk(cx, cy)


def get_graph(
    lat: float | None = None,
    lon: float | None = None,
    radius_m: int | None = None,
    *,
    force: bool = False,
) -> RoadGraph:
    """Rückwärtskompatible Funktion: stellt sicher, dass Straßen für den
    angefragten Bereich im additiven Graph vorhanden sind, und gibt den
    (globalen) Graph zurück.

    Ohne Argumente: Fallback auf Config-Viertel (Tests/CLI).
    ``force=True``: ignoriert den geladenen-Chunk-Cache für diesen Bereich
    (fetcht ggf. erneut, nützlich bei Tests / manuellem Reset).
    """
    global _graph_key

    lat = config.CENTER_LAT if lat is None else lat
    lon = config.CENTER_LON if lon is None else lon
    radius_m = (config.RADIUS_M + 200) if radius_m is None else radius_m

    key = (round(lat, 3), round(lon, 3), radius_m)

    if force:
        # Bei force: gesamte Bbox aus dem loaded-Set entfernen, damit
        # ensure_roads_for_bbox erneut fetcht.
        deg_radius = radius_m / _M_PER_DEG_LAT
        import math as _math
        cx_min = _math.floor((lat - deg_radius) / config.CHUNK_DEG)
        cy_min = _math.floor((lon - deg_radius) / config.CHUNK_DEG)
        cx_max = _math.floor((lat + deg_radius) / config.CHUNK_DEG)
        cy_max = _math.floor((lon + deg_radius) / config.CHUNK_DEG)
        for cx in range(cx_min, cx_max + 1):
            for cy in range(cy_min, cy_max + 1):
                _roads_loaded_chunks.discard((cx, cy))

    deg_radius = radius_m / _M_PER_DEG_LAT
    ensure_roads_for_bbox(
        lat - deg_radius, lon - deg_radius,
        lat + deg_radius, lon + deg_radius,
    )
    _graph_key = key
    return _graph
