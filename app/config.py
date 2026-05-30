"""Zentrale Konfiguration für Wasteland (Schritt 1).

Alle Werte haben sinnvolle Defaults und sind per Umgebungsvariable
überschreibbar (Prefix ``WASTELAND_``). Pfade sind relativ zum Projekt-Root
und werden absolut aufgelöst.
"""
from __future__ import annotations

import os
from pathlib import Path

# Projekt-Root = Verzeichnis über app/
ROOT = Path(__file__).resolve().parent.parent

# .env (falls vorhanden) laden, damit z.B. ANTHROPIC_API_KEY automatisch in der
# Umgebung landet — für CLI-Start und Server gleichermaßen. .env ist gitignored.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass


def _env(name: str, default: str) -> str:
    return os.environ.get(f"WASTELAND_{name}", default)


def _path(name: str, default: str) -> Path:
    raw = _env(name, default)
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p)


# --- Pfade --------------------------------------------------------------
DB_PATH: Path = _path("DB_PATH", "data/wasteland.db")
OSM_CACHE_DIR: Path = _path("OSM_CACHE_DIR", "data/osm_cache")
SCHEMA_PATH: Path = ROOT / "schema.sql"

# --- Welt ---------------------------------------------------------------
# Globaler Seed für alle deterministische Lazy-Generation (DESIGN.md §6).
WORLD_SEED: int = int(_env("WORLD_SEED", "1337"))

# --- Start-Viertel (Mittelpunkt + Radius) -------------------------------
# Platzhalter-Default (Erlangen-Innenstadt). Für ein anderes Viertel
# überschreiben, z.B. WASTELAND_CENTER_LAT / WASTELAND_CENTER_LON / WASTELAND_RADIUS_M.
CENTER_LAT: float = float(_env("CENTER_LAT", "49.5897"))
CENTER_LON: float = float(_env("CENTER_LON", "11.0120"))
RADIUS_M: int = int(_env("RADIUS_M", "400"))

# --- Overpass -----------------------------------------------------------
OVERPASS_URL: str = _env("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OVERPASS_TIMEOUT_S: int = int(_env("OVERPASS_TIMEOUT_S", "90"))
