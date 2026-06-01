"""Adresse -> Koordinaten via Nominatim, mit Disk-Cache.

Wird beim Spielstart genutzt, um aus der Wohnadresse den Startort (Zuhause) zu
bestimmen. Eine erfolgreiche Auflösung wird gecacht (wie Overpass), damit
Re-Starts offline/reproduzierbar bleiben.

Privacy: die Adresse geht an nominatim.openstreetmap.org. Für ein lokales
Single-Player-Spiel vertretbar; ein manueller Koordinaten-Fallback steht im
Formular zur Verfügung.
"""
from __future__ import annotations

import hashlib
import json

import requests

from .. import config

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_HEADERS = {"User-Agent": "Wasteland-Sim/0.1 (single-player survival sim; geocoder)"}


def _cache_file(address: str):
    key = hashlib.sha1(address.strip().lower().encode("utf-8")).hexdigest()[:16]
    return config.OSM_CACHE_DIR / f"geo_{key}.json"


def geocode(address: str) -> tuple[float, float] | None:
    """Liefert (lat, lon) für eine Adresse oder None. Nutzt Disk-Cache."""
    if not address or not address.strip():
        return None
    cache = _cache_file(address)
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        return (data["lat"], data["lon"]) if data else None

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1},
            headers=_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception:
        return None

    config.OSM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not results:
        cache.write_text(json.dumps(None), encoding="utf-8")
        return None
    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    cache.write_text(json.dumps({"lat": lat, "lon": lon}), encoding="utf-8")
    return (lat, lon)
