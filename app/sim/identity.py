"""Prozedurale, deterministische Basis-Identitaet fuer Ueberlebende.

Eisernes Prinzip: vollstaendig deterministisch aus (world_seed, survivor_id)
und (lat, lon). Kein LLM, kein datetime.now(), keine Zufaelligkeit ausserhalb
des geseedeten RNG.

Die Funktion `generate_identity` ist eine reine Funktion: gleiche Eingaben
liefern immer dasselbe Ergebnis.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from .locale import age_weights, name_pool, professions, region_for


def _weighted_choice(rng: random.Random, items: list, weights: list[float]):
    """Waehlt ein Element aus `items` proportional zu `weights` (deterministisch)."""
    total = sum(weights)
    r = rng.uniform(0.0, total)
    running = 0.0
    for item, w in zip(items, weights):
        running += w
        if r <= running:
            return item
    return items[-1]  # Sicherheitsnetz (Floating-Point-Toleranz)


def generate_identity(
    survivor_id: int,
    lat: float,
    lon: float,
    world_seed: int,
    start_datetime: str,
) -> dict:
    """Erzeugt eine deterministische Basis-Identitaet fuer einen Ueberlebenden.

    Parameters
    ----------
    survivor_id:
        Primaerschluessel des survivors-Eintrags. Zusammen mit world_seed
        eindeutiger Seed-String fuer den RNG.
    lat, lon:
        Position des Ueberlebenden (bestimmt die Kulturregion).
    world_seed:
        Globaler Welt-Seed aus ``world.world_seed``.
    start_datetime:
        ISO-Datetimestring aus ``world.start_datetime`` (z.B. '2026-09-01T06:00:00').
        Wird fuer die Altersberechnung verwendet (KEIN datetime.now()).

    Returns
    -------
    dict mit Keys:
        name (str)       - "Vorname Nachname"
        sex (str)        - 'm', 'f' oder 'x'
        birthdate (str)  - ISO-Datum, z.B. '1985-03-14'
        profession (str) - Berufsbezeichnung
    """
    # --- Deterministischer RNG aus (world_seed, survivor_id) ------------------
    # Gleiche Eingaben -> identisches Ergebnis, unabhaengig von Aufruf-Reihenfolge.
    rng = random.Random(f"{world_seed}:{survivor_id}")

    # --- Geschlecht -----------------------------------------------------------
    sex = rng.choice(["m", "f"])

    # --- Alter und Geburtsdatum -----------------------------------------------
    brackets = age_weights()         # [((lo, hi), weight), ...]
    bracket_items = [b[0] for b in brackets]
    bracket_weights = [b[1] for b in brackets]

    lo, hi = _weighted_choice(rng, bracket_items, bracket_weights)
    age_years = rng.randint(lo, hi)  # gleichverteilt innerhalb der Altersgruppe

    start_dt = datetime.fromisoformat(start_datetime)
    birth_dt = start_dt - timedelta(days=age_years * 365 + rng.randint(0, 364))
    birthdate = birth_dt.date().isoformat()

    # --- Beruf ----------------------------------------------------------------
    prof_data = professions()        # [(name, weight), ...]
    prof_names = [p[0] for p in prof_data]
    prof_weights = [p[1] for p in prof_data]
    profession = _weighted_choice(rng, prof_names, prof_weights)

    # --- Name (regional) ------------------------------------------------------
    region = region_for(lat, lon)
    pool = name_pool(region)         # {"male": [...], "female": [...], "surnames": [...]}

    if sex == "m":
        forename_list = pool["male"]
    elif sex == "f":
        forename_list = pool["female"]
    else:
        # 'x': aus beiden Listen waehlen
        forename_list = pool["male"] + pool["female"]

    forename = rng.choice(forename_list)
    surname = rng.choice(pool["surnames"])
    name = f"{forename} {surname}"

    return {
        "name": name,
        "sex": sex,
        "birthdate": birthdate,
        "profession": profession,
    }
