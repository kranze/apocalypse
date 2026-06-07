"""
Population grid loader.

Loads app/data/pop_grid.csv — a compact 1-degree lat/lon grid derived from
GHSL GHS-POP (EC JRC & CIESIN, CC-BY-4.0, ghsl.jrc.ec.europa.eu).

See app/data/POP_GRID_SOURCE.md for full attribution and methodology.
"""

from __future__ import annotations

import csv
import functools
from pathlib import Path

# Resolve CSV path relative to this file's location (app/sim/ -> app/data/)
_DATA_CSV = Path(__file__).parent.parent / "data" / "pop_grid.csv"


@functools.lru_cache(maxsize=1)
def load_grid() -> list[tuple[float, float, float]]:
    """Return the population grid as a list of (lat, lon, weight) tuples.

    Each tuple represents the centre of a 1-degree lat/lon cell with its
    estimated population (weight).  The list is cached process-wide after the
    first call.

    Returns
    -------
    list[tuple[float, float, float]]
        Sorted by (lat, lon).  Cells with population < 1 are excluded.

    Raises
    ------
    FileNotFoundError
        If app/data/pop_grid.csv is missing.  Re-generate it by running
        ``python data/process_pop_grid.py`` from the repository root.
    """
    if not _DATA_CSV.exists():
        raise FileNotFoundError(
            f"Population grid CSV not found: {_DATA_CSV}\n"
            "Re-generate it by running:\n"
            "  python data/process_pop_grid.py\n"
            "from the repository root (requires rasterio, pyproj, numpy and\n"
            "the GHS-POP source raster in C:/Users/marti/Downloads/)."
        )

    result: list[tuple[float, float, float]] = []
    with open(_DATA_CSV, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            result.append((float(row["lat"]), float(row["lon"]), float(row["pop"])))
    return result
