"""Overpass-Abfrage mit Disk-Cache.

Eine einmalige Live-Abfrage pro (lat, lon, radius, QUERY_VERSION) wird als
Roh-JSON nach ``data/osm_cache/`` geschrieben; danach immer aus dem Cache
gelesen. Das hält Re-Läufe reproduzierbar und schont den öffentlichen
Overpass-Server.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import requests

from .. import config

# Erhöhen, wenn sich die Query-Struktur ändert -> invalidiert alte Caches.
QUERY_VERSION = 1

# Overpass weist den Default-python-requests-User-Agent ab (406). Eigener UA + Kontakt.
_HEADERS = {"User-Agent": "Wasteland-Sim/0.1 (single-player survival sim; OSM loader)"}


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


def _cache_key(lat: float, lon: float, radius_m: int) -> str:
    raw = f"{QUERY_VERSION}|{lat:.6f}|{lon:.6f}|{radius_m}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_file(lat: float, lon: float, radius_m: int) -> Path:
    return config.OSM_CACHE_DIR / f"{_cache_key(lat, lon, radius_m)}.json"


def fetch(lat: float, lon: float, radius_m: int, *, force: bool = False) -> dict[str, Any]:
    """Liefert die Overpass-Antwort als dict. Nutzt Disk-Cache, falls vorhanden
    (außer ``force=True``)."""
    cache_file = _cache_file(lat, lon, radius_m)
    if cache_file.exists() and not force:
        return json.loads(cache_file.read_text(encoding="utf-8"))

    query = build_query(lat, lon, radius_m)
    resp = requests.post(
        config.OVERPASS_URL,
        data={"data": query},
        headers=_HEADERS,
        timeout=config.OVERPASS_TIMEOUT_S + 10,
    )
    resp.raise_for_status()
    data = resp.json()

    config.OSM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data
