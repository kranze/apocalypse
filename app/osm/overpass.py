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

# Mirror-Fallback: der Haupt-Server (overpass-api.de) liefert bei schwereren
# Queries gern 504/429. Bei Fehlern werden der Reihe nach Mirror probiert.
_MIRRORS = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
)


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


def _cache_key(lat: float, lon: float, radius_m: int, tag: str = "") -> str:
    raw = f"{QUERY_VERSION}|{lat:.6f}|{lon:.6f}|{radius_m}"
    if tag:
        raw += f"|{tag}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_file(lat: float, lon: float, radius_m: int, tag: str = "") -> Path:
    return config.OSM_CACHE_DIR / f"{_cache_key(lat, lon, radius_m, tag)}.json"


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


def fetch(lat: float, lon: float, radius_m: int, *, force: bool = False) -> dict[str, Any]:
    """Liefert die POI/Gebäude-Overpass-Antwort als dict (Disk-Cache, tag=\"\")."""
    return fetch_query(build_query(lat, lon, radius_m), lat, lon, radius_m, force=force)
