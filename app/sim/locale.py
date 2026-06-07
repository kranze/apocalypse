"""
Regional localisation: lat/lon -> culture cluster + identity asset loaders.

Offline, dependency-free (stdlib only).  No GIS libraries, no network calls.

Region lookup uses a curated list of country bounding boxes + representative
centroids mapped to cultural clusters.  The approach is deliberately coarse:
this is name-/flavour-level localisation, not precision geocoding.

Lookup strategy
---------------
1. Try bounding-box table (sorted by area, smallest first) -> first match wins.
2. If no box matched, fall back to nearest-centroid from the same table.
3. Final safety net: "unknown".

See app/data/IDENTITY_DATA_SOURCE.md for data provenance.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Data paths
# ---------------------------------------------------------------------------
_DATA_DIR = Path(__file__).parent.parent / "data"
_NAMES_JSON = _DATA_DIR / "names.json"
_PROFESSIONS_JSON = _DATA_DIR / "professions.json"
_AGE_PYRAMID_JSON = _DATA_DIR / "age_pyramid.json"

# ---------------------------------------------------------------------------
# Cluster definitions
# Each entry: (name, cluster_code, lat_min, lat_max, lon_min, lon_max)
# Sorted roughly small -> large so first-match gives more specific results.
# Bounding boxes are intentionally generous/simplified.
# ---------------------------------------------------------------------------
_BBOX_TABLE: list[tuple[str, str, float, float, float, float]] = [
    # --- British Isles --------------------------------------------------
    ("Ireland",         "british_isles",  51.5,  55.5, -10.5,  -6.0),
    ("United Kingdom",  "british_isles",  49.9,  60.9, -8.2,    1.8),
    # --- Nordics --------------------------------------------------------
    ("Denmark",         "nordics",        54.5,  57.8,  8.0,   15.2),
    ("Norway",          "nordics",        57.9,  71.2,  4.5,   31.1),
    ("Sweden",          "nordics",        55.3,  69.1,  11.1,  24.2),
    ("Finland",         "nordics",        59.8,  70.1,  20.0,  31.6),
    ("Iceland",         "nordics",        63.3,  66.6, -24.5, -13.5),
    # --- Iberia ---------------------------------------------------------
    ("Portugal",        "iberia",         36.9,  42.2, -9.5,  -6.2),
    ("Spain",           "iberia",         36.0,  43.8,  -9.3,   4.3),
    # --- Central Europe -------------------------------------------------
    ("Germany",         "central_europe", 47.3,  55.1,   6.0,  15.1),
    ("Austria",         "central_europe", 46.4,  49.0,   9.5,  17.2),
    ("Switzerland",     "central_europe", 45.8,  47.9,   5.9,  10.5),
    ("Netherlands",     "central_europe", 50.7,  53.6,   3.3,   7.2),
    ("Belgium",         "central_europe", 49.5,  51.5,   2.5,   6.4),
    ("Luxembourg",      "central_europe", 49.4,  50.2,   5.7,   6.5),
    ("France",          "central_europe", 41.3,  51.1,  -5.2,   9.6),
    ("Italy",           "central_europe", 36.6,  47.1,   6.6,  18.5),
    ("Czech Republic",  "central_europe", 48.5,  51.1,  12.1,  18.9),
    ("Slovakia",        "central_europe", 47.7,  49.6,  16.8,  22.6),
    ("Hungary",         "central_europe", 45.7,  48.6,  16.1,  22.9),
    ("Slovenia",        "central_europe", 45.4,  46.9,  13.4,  16.6),
    ("Croatia",         "central_europe", 42.3,  46.6,  13.5,  19.5),
    ("Greece",          "central_europe", 34.8,  41.8,  20.0,  26.6),
    # --- Eastern Europe -------------------------------------------------
    ("Poland",          "eastern_europe", 49.0,  54.9,  14.1,  24.2),
    ("Romania",         "eastern_europe", 43.6,  48.3,  20.3,  29.7),
    ("Bulgaria",        "eastern_europe", 41.2,  44.2,  22.4,  28.6),
    ("Serbia",          "eastern_europe", 41.9,  46.2,  19.0,  23.0),
    ("Ukraine",         "eastern_europe", 44.4,  52.4,  22.1,  40.2),
    ("Belarus",         "eastern_europe", 51.2,  56.2,  23.2,  32.8),
    ("Moldova",         "eastern_europe", 45.5,  48.5,  26.6,  30.1),
    ("Lithuania",       "eastern_europe", 53.9,  56.5,  21.0,  26.8),
    ("Latvia",          "eastern_europe", 55.7,  58.1,  20.9,  28.2),
    ("Estonia",         "eastern_europe", 57.5,  59.7,  21.8,  28.2),
    ("Russia (west)",   "eastern_europe", 47.0,  70.0,  26.0,  60.0),
    ("Russia (east)",   "eastern_europe", 47.0,  77.0,  60.0, 180.0),
    # --- Middle East ----------------------------------------------------
    ("Turkey",          "middle_east",    36.0,  42.1,  26.0,  44.8),
    ("Iran",            "middle_east",    25.1,  39.8,  44.0,  63.4),
    ("Iraq",            "middle_east",    29.1,  37.4,  38.8,  48.6),
    ("Saudi Arabia",    "middle_east",    16.4,  32.2,  34.5,  55.7),
    ("Israel",          "middle_east",    29.5,  33.4,  34.3,  35.9),
    ("Jordan",          "middle_east",    29.2,  33.4,  35.0,  39.3),
    ("Syria",           "middle_east",    32.3,  37.3,  35.7,  42.4),
    ("Lebanon",         "middle_east",    33.0,  34.7,  35.1,  36.7),
    ("Kuwait",          "middle_east",    28.5,  30.1,  46.5,  48.5),
    ("UAE",             "middle_east",    22.6,  26.1,  51.6,  56.4),
    ("Yemen",           "middle_east",    12.6,  19.0,  42.5,  54.1),
    ("Oman",            "middle_east",    16.6,  26.4,  52.0,  60.0),
    ("Afghanistan",     "middle_east",    29.4,  38.5,  60.5,  74.9),
    ("Pakistan",        "south_asia",     23.7,  37.1,  61.0,  77.8),
    # --- South Asia -----------------------------------------------------
    ("India",           "south_asia",     8.1,   37.1,  68.1,  97.4),
    ("Bangladesh",      "south_asia",    20.7,  26.7,  88.0,  92.7),
    ("Sri Lanka",       "south_asia",     5.9,   9.8,  79.7,  81.9),
    ("Nepal",           "south_asia",    26.4,  30.5,  80.0,  88.2),
    # --- East Asia ------------------------------------------------------
    ("China",           "east_asia",     18.2,  53.6,  73.6, 134.8),
    ("Japan",           "east_asia",     24.0,  45.5, 122.9, 153.0),
    ("South Korea",     "east_asia",     33.1,  38.6, 125.9, 129.6),
    ("North Korea",     "east_asia",     37.7,  42.9, 124.2, 130.7),
    ("Taiwan",          "east_asia",     21.9,  25.3, 120.0, 122.0),
    ("Mongolia",        "east_asia",     41.6,  52.2,  87.7, 119.9),
    # --- Southeast Asia -------------------------------------------------
    ("Vietnam",         "southeast_asia",  8.6,  23.4, 102.1, 109.5),
    ("Thailand",        "southeast_asia",  5.6,  20.5,  97.3, 105.7),
    ("Myanmar",         "southeast_asia", 10.0,  28.5,  92.2, 101.2),
    ("Laos",            "southeast_asia", 13.9,  22.5, 100.1, 107.7),
    ("Cambodia",        "southeast_asia", 10.4,  14.7, 102.3, 107.6),
    ("Malaysia",        "southeast_asia",  0.9,   7.4, 100.1, 119.3),
    ("Indonesia",       "southeast_asia", -8.6,   5.9,  95.0, 141.0),
    ("Philippines",     "southeast_asia",  5.0,  20.9, 116.9, 126.7),
    ("Singapore",       "southeast_asia",  1.2,   1.5, 103.6, 104.0),
    # --- Oceania --------------------------------------------------------
    ("Australia",       "oceania",       -43.7,  -10.7, 113.2, 153.6),
    ("New Zealand",     "oceania",       -46.6,  -34.4, 166.4, 178.6),
    ("Papua New Guinea","oceania",        -11.7,   -1.4, 140.8, 150.9),
    # --- North America --------------------------------------------------
    ("Canada",          "north_america",  41.7,  83.1, -141.0, -52.6),
    ("United States",   "north_america",  24.4,  71.4, -168.0, -66.9),
    # --- Latin America --------------------------------------------------
    ("Mexico",          "latin_america",  14.5,  32.7, -117.1, -86.7),
    ("Guatemala",       "latin_america",  13.7,  17.8,  -92.2, -88.2),
    ("Honduras",        "latin_america",  13.0,  16.5,  -89.4, -83.2),
    ("El Salvador",     "latin_america",  13.1,  14.5,  -90.1, -87.7),
    ("Nicaragua",       "latin_america",  10.7,  15.0,  -87.7, -83.2),
    ("Costa Rica",      "latin_america",   8.0,  11.2,  -85.9, -82.6),
    ("Panama",          "latin_america",   7.2,   9.7,  -83.1, -77.2),
    ("Cuba",            "latin_america",  19.8,  23.2,  -85.0, -74.1),
    ("Dominican Rep.",  "latin_america",  17.5,  20.0,  -74.5, -68.3),
    ("Colombia",        "latin_america",  -4.2,  13.4,  -79.0, -66.9),
    ("Venezuela",       "latin_america",   0.7,  12.2,  -73.4, -60.0),
    ("Ecuador",         "latin_america",  -5.0,   1.7,  -81.0, -75.2),
    ("Peru",            "latin_america", -18.4,   0.0,  -81.3, -68.7),
    ("Bolivia",         "latin_america", -22.9,  -9.7,  -69.6, -57.5),
    ("Brazil",          "latin_america", -33.8,   5.3,  -73.9, -34.8),
    ("Chile",           "latin_america", -55.9, -17.5,  -75.7, -66.4),
    ("Argentina",       "latin_america", -55.1, -21.8,  -73.6, -53.7),
    ("Uruguay",         "latin_america", -34.9, -30.1,  -58.4, -53.2),
    ("Paraguay",        "latin_america", -27.6, -19.3,  -62.7, -54.3),
    # --- East Africa ----------------------------------------------------
    ("Kenya",           "east_africa",    -4.7,   4.6,  33.9,  41.9),
    ("Tanzania",        "east_africa",   -11.7,   -0.9,  29.3,  40.4),
    ("Uganda",          "east_africa",    -1.5,   4.2,  29.6,  35.0),
    ("Ethiopia",        "east_africa",     3.4,  14.9,  33.0,  48.0),
    ("Somalia",         "east_africa",    -1.7,  11.5,  41.0,  51.4),
    ("Rwanda",          "east_africa",    -2.8,  -1.1,  28.9,  30.9),
    ("Mozambique",      "east_africa",   -26.9, -10.5,  32.3,  40.8),
    ("Madagascar",      "east_africa",   -25.6, -12.0,  43.2,  50.5),
    ("Zambia",          "east_africa",   -18.1,  -8.2,  22.0,  33.7),
    ("Zimbabwe",        "east_africa",   -22.4, -15.6,  26.0,  33.1),
    ("Sudan",           "east_africa",     8.7,  22.2,  21.9,  38.7),
    # --- West Africa ----------------------------------------------------
    ("Nigeria",         "west_africa",     4.3,  13.9,   3.0,  14.7),
    ("Ghana",           "west_africa",     4.7,  11.2,   -3.3,   1.2),
    ("Cameroon",        "west_africa",     1.7,  13.1,   8.5,  16.2),
    ("Ivory Coast",     "west_africa",     4.3,  10.7,   -8.6,  -2.5),
    ("Senegal",         "west_africa",    11.4,  16.7,  -17.5, -11.4),
    ("Mali",            "west_africa",    10.2,  24.9, -12.2,   4.3),
    ("Burkina Faso",    "west_africa",     9.4,  15.1,  -5.5,   2.4),
    ("Guinea",          "west_africa",     7.2,  12.7,  -15.1,  -7.7),
    ("Niger",           "west_africa",    11.7,  23.5,   0.2,  16.0),
    ("Chad",            "west_africa",     7.5,  23.5,  13.5,  24.0),
    ("Democratic Rep. Congo", "west_africa", -13.5, 5.4, 12.2, 31.3),
    ("Angola",          "west_africa",   -18.0, -4.4,  11.7,  24.1),
]

# Representative centroids (lat, lon) per cluster for nearest-centroid fallback
_CLUSTER_CENTROIDS: list[tuple[str, float, float]] = [
    ("central_europe",  48.5,  10.0),
    ("eastern_europe",  52.0,  30.0),
    ("british_isles",   53.0,  -2.0),
    ("nordics",         63.0,  15.0),
    ("iberia",          40.0,  -4.0),
    ("north_america",   42.0, -95.0),
    ("latin_america",  -10.0, -60.0),
    ("south_asia",      23.0,  78.0),
    ("east_asia",       35.0, 110.0),
    ("southeast_asia",   5.0, 110.0),
    ("east_africa",     -1.0,  37.0),
    ("west_africa",      9.0,   3.0),
    ("middle_east",     30.0,  45.0),
    ("oceania",        -27.0, 133.0),
]


def region_for(lat: float, lon: float) -> str:
    """Return a culture cluster code for the given coordinates.

    Uses a curated bounding-box table (first match on smallest boxes wins)
    with a nearest-centroid fallback.  Fully offline, no external libraries.

    Parameters
    ----------
    lat:
        Latitude in decimal degrees (−90 … +90).
    lon:
        Longitude in decimal degrees (−180 … +180).

    Returns
    -------
    str
        One of the known cluster codes or "unknown" when no match is found.
    """
    # 1. Bounding-box lookup
    for _name, cluster, lat_min, lat_max, lon_min, lon_max in _BBOX_TABLE:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return cluster

    # 2. Nearest-centroid fallback (squared Euclidean distance — good enough)
    best_cluster = "unknown"
    best_dist = float("inf")
    for cluster, clat, clon in _CLUSTER_CENTROIDS:
        dist = (lat - clat) ** 2 + (lon - clon) ** 2
        if dist < best_dist:
            best_dist = dist
            best_cluster = cluster

    # Only accept nearest-centroid when reasonably close (< ~45° Euclidean)
    # This avoids attributing remote ocean cells to the wrong cluster.
    if best_dist > 45 ** 2:
        return "unknown"

    return best_cluster


# ---------------------------------------------------------------------------
# Asset loaders (process-wide LRU cache — pure, no side-effects)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _load_names() -> dict:
    """Load names.json once and cache the result."""
    with open(_NAMES_JSON, encoding="utf-8") as fh:
        return json.load(fh)


@functools.lru_cache(maxsize=32)
def name_pool(region: str) -> dict[str, list[str]]:
    """Return the name pool for *region*.

    Falls back to the "unknown" pool when the cluster is not found.

    Parameters
    ----------
    region:
        Culture cluster code as returned by :func:`region_for`.

    Returns
    -------
    dict with keys "male", "female", "surnames", each a non-empty list of str.
    """
    data = _load_names()
    pool = data.get(region) or data.get("unknown")
    # Strip meta keys (underscore-prefixed)
    return {k: v for k, v in pool.items() if not k.startswith("_")}


@functools.lru_cache(maxsize=1)
def professions() -> list[tuple[str, float]]:
    """Return the global profession list as (name, weight) tuples.

    Weights are relative; they do not sum to exactly 1.0.
    """
    with open(_PROFESSIONS_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    return [(entry[0], entry[1]) for entry in data["professions"]]


@functools.lru_cache(maxsize=1)
def age_weights() -> list[tuple[tuple[int, int], float]]:
    """Return age bracket weights as ((lo, hi), weight) tuples.

    ``lo`` and ``hi`` are inclusive age values in years.
    Weights are relative; they do not sum to exactly 1.0.
    """
    with open(_AGE_PYRAMID_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    return [
        ((entry["range"][0], entry["range"][1]), entry["weight"])
        for entry in data["age_groups"]
    ]
