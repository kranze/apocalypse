"""Overpass-Abfrage mit Disk-Cache.

Eine einmalige Live-Abfrage pro (lat, lon, radius, QUERY_VERSION) wird als
Roh-JSON nach ``data/osm_cache/`` geschrieben; danach immer aus dem Cache
gelesen. Das hält Re-Läufe reproduzierbar und schont den öffentlichen
Overpass-Server.

Throttle: aufeinanderfolgende echte Netz-Requests werden auf mindestens
``config.OVERPASS_MIN_INTERVAL_S`` gedrosselt. Cache-Hits gehen sofort
durch (kein sleep).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import requests

from .. import config

# Erhöhen, wenn sich die Query-Struktur ändert -> invalidiert alte Caches.
QUERY_VERSION = 1

# Overpass weist den Default-python-requests-User-Agent ab (406). Eigener UA + Kontakt.
_HEADERS = {"User-Agent": "Wasteland-Sim/0.1 (single-player survival sim; OSM loader)"}

# Mirror-Fallback: der Haupt-Server (overpass-api.de) liefert bei schwereren
# Queries gern 504/429. Bei Fehlern werden der Reihe nach Mirror probiert.
_MIRRORS = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
)

# Throttle-State: Zeitstempel des letzten echten Netz-Requests (Modul-Global).
_last_net_request_ts: float = 0.0


def _endpoints() -> list[str]:
    seen, out = set(), []
    for url in (config.OVERPASS_URL, *_MIRRORS):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def build_query(lat: float, lon: float, radius_m: int) -> str:
    """Overpass-QL: Gebäude (mit Geometrie) + relevante POI-Nodes im Umkreis."""
    return f"""
[out:json][timeout:{config.OVERPASS_TIMEOUT_S}];
(
  way["building"](around:{radius_m},{lat},{lon});
  node["shop"](around:{radius_m},{lat},{lon});
  node["amenity"~"fuel|pharmacy|hospital|doctors"](around:{radius_m},{lat},{lon});
);
out geom;
""".strip()


def build_bbox_query(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> str:
    """Overpass-QL: Gebäude + relevante POI-Nodes in einer Bounding-Box.

    Bbox-Filter ist effizienter als ``around:`` für kachelbasierte Abfragen,
    weil der Overpass-Server intern direkt den Spatial-Index nutzt.
    """
    bbox = f"{min_lat:.6f},{min_lon:.6f},{max_lat:.6f},{max_lon:.6f}"
    return f"""
[out:json][timeout:{config.OVERPASS_TIMEOUT_S}];
(
  way["building"]({bbox});
  node["shop"]({bbox});
  node["amenity"~"fuel|pharmacy|hospital|doctors"]({bbox});
);
out geom;
""".strip()


def _cache_key(lat: float, lon: float, radius_m: int, tag: str = "") -> str:
    raw = f"{QUERY_VERSION}|{lat:.6f}|{lon:.6f}|{radius_m}"
    if tag:
        raw += f"|{tag}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _bbox_cache_key(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> str:
    """Stabiler Cache-Key für eine Bbox-Abfrage (je Chunk eindeutig).

    Format: ``bbox_v{VERSION}_{min_lat:.6f}_{min_lon:.6f}_{max_lat:.6f}_{max_lon:.6f}``.
    Das stellt sicher, dass Chunk-Grenzen immer dieselbe Cache-Datei treffen.
    """
    raw = (
        f"bbox|{QUERY_VERSION}"
        f"|{min_lat:.6f}|{min_lon:.6f}|{max_lat:.6f}|{max_lon:.6f}"
    )
    return "bbox_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_file(lat: float, lon: float, radius_m: int, tag: str = "") -> Path:
    return config.OSM_CACHE_DIR / f"{_cache_key(lat, lon, radius_m, tag)}.json"


def _bbox_cache_file(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> Path:
    return config.OSM_CACHE_DIR / f"{_bbox_cache_key(min_lat, min_lon, max_lat, max_lon)}.json"


def _throttle() -> None:
    """Wartet, bis seit dem letzten Netz-Request mind. OVERPASS_MIN_INTERVAL_S
    vergangen sind. Wird NUR vor echten Fetches aufgerufen; Cache-Hits nicht."""
    global _last_net_request_ts
    elapsed = time.monotonic() - _last_net_request_ts
    wait = config.OVERPASS_MIN_INTERVAL_S - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_net_request_ts = time.monotonic()


def _do_net_fetch(query: str, cache_file: Path) -> dict[str, Any]:
    """Führt einen throttled Netz-Request mit Mirror-Fallback aus und cached."""
    _throttle()
    last_err: Exception | None = None
    for url in _endpoints():
        try:
            resp = requests.post(
                url, data={"data": query}, headers=_HEADERS,
                timeout=config.OVERPASS_TIMEOUT_S + 10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # 504/429/Timeout -> nächster Mirror
            last_err = e
            continue
        config.OSM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data

    raise last_err if last_err else RuntimeError("Overpass: kein Endpoint erreichbar")


def fetch_query(
    query: str,
    lat: float,
    lon: float,
    radius_m: int,
    *,
    tag: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Führt eine beliebige Overpass-Query aus und cached sie unter
    (coords, radius, tag). ``tag`` trennt verschiedene Query-Arten im Cache
    (z.B. POIs vs. Straßen)."""
    cache_file = _cache_file(lat, lon, radius_m, tag)
    if cache_file.exists() and not force:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    return _do_net_fetch(query, cache_file)


def fetch(lat: float, lon: float, radius_m: int, *, force: bool = False) -> dict[str, Any]:
    """Liefert die POI/Gebäude-Overpass-Antwort als dict (Disk-Cache, tag=\"\")."""
    return fetch_query(build_query(lat, lon, radius_m), lat, lon, radius_m, force=force)


def fetch_bbox(
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Liefert POI/Gebäude-Overpass-Antwort für eine Bbox (Disk-Cache je Chunk).

    Cache-Hit ist sofort (kein Throttle). Echter Netz-Request wird gedrosselt
    (mind. OVERPASS_MIN_INTERVAL_S seit dem letzten echten Request).
    """
    cache_file = _bbox_cache_file(min_lat, min_lon, max_lat, max_lon)
    if cache_file.exists() and not force:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    query = build_bbox_query(min_lat, min_lon, max_lat, max_lon)
    return _do_net_fetch(query, cache_file)


def build_bbox_query_combined(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> str:
    """Overpass-QL: Gebäude UND Straßen in einer einzigen Bbox-Abfrage (Union).

    Liefert way["building"] und way["highway"] in einer Antwort (out geom;),
    sodass parser.parse die Gebäude und roads.merge_ways die Straßen extrahieren
    können — jeweils anhand ihrer eigenen Tag-Filter.
    """
    bbox = f"{min_lat:.6f},{min_lon:.6f},{max_lat:.6f},{max_lon:.6f}"
    return f"""
[out:json][timeout:{config.OVERPASS_TIMEOUT_S}];
(
  way["building"]({bbox});
  node["shop"]({bbox});
  node["amenity"~"fuel|pharmacy|hospital|doctors"]({bbox});
  way["highway"]({bbox});
);
out geom;
""".strip()


def _bbox_combined_cache_key(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> str:
    """Stabiler Cache-Key für die kombinierte Bbox-Abfrage (Gebäude + Straßen).

    Separater Key vom reinen Gebäude-Cache, damit beide Varianten unabhängig
    gecacht werden können.
    """
    raw = (
        f"bbox_combined|{QUERY_VERSION}"
        f"|{min_lat:.6f}|{min_lon:.6f}|{max_lat:.6f}|{max_lon:.6f}"
    )
    return "bbox_combined_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _bbox_combined_cache_file(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> Path:
    return config.OSM_CACHE_DIR / f"{_bbox_combined_cache_key(min_lat, min_lon, max_lat, max_lon)}.json"


def fetch_bbox_combined(
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Liefert Gebäude + Straßen in EINER Overpass-Antwort für eine Bbox.

    Eigener Cache-Key (``bbox_combined``), damit Gebäude-Only- und kombinierte
    Queries unabhängig gecacht werden. Throttle und Mirror-Fallback werden
    wiederverwendet (via ``_do_net_fetch``).

    Die Antwort enthält way["building"]-, node["shop"/"amenity"]- und
    way["highway"]-Elemente gemischt. Aufrufer (parser.parse bzw.
    roads.merge_ways) filtern jeweils nach ihren eigenen Tag-Bedingungen.
    """
    cache_file = _bbox_combined_cache_file(min_lat, min_lon, max_lat, max_lon)
    if cache_file.exists() and not force:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    query = build_bbox_query_combined(min_lat, min_lon, max_lat, max_lon)
    return _do_net_fetch(query, cache_file)
