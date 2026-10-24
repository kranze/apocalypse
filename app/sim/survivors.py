"""Globale Überlebenden-Verteilung und lazy NPC-Materialisierung.

Eisernes Prinzip: Nur der Sim-Kern schreibt. Verteilung ist eine rein
deterministische Funktion aus world_seed + Bevölkerungsgitter. Survivor-Zeilen
sind KEIN Ressourcen-Gut: sie berühren weder resource_ledger noch resource_audit.

birth_tick-Konvention (Issue #18):
    Tick 0 = Kollaps-Zeitpunkt. Überlebende werden VOR dem Kollaps geboren,
    daher gilt: birth_tick = -(age_years * 525_600)  (negativ).
    Aktuelles Alter in Jahren = (current_tick - birth_tick) / 525_600.
    525_600 = Minuten pro Jahr (365 * 24 * 60).
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
from typing import Sequence

from ..db import get_world_seed
from .identity import generate_identity
from .locale import age_weights
from .popgrid import load_grid


# Halbe Zellbreite des Bevölkerungsgitters (~0.25°-Zellen, also ±0.125°).
_HALF_CELL = 0.125

# Minuten pro Jahr (365 * 24 * 60); für birth_tick-Berechnung.
_MINUTES_PER_YEAR: int = 525_600

# Maximaldistanz in Metern für Wohnhaus-Suche beim Materialisieren.
_HOUSE_SEARCH_RADIUS_M: float = 150.0

# Location-Typen, die als Wohnhaus gelten (primär) und Fallback-Gebäude.
_HOUSE_TYPES = frozenset({"house"})
_BUILDING_TYPES = frozenset({"house", "building", "apartment", "residential"})


def spawn_survivors(
    conn: sqlite3.Connection,
    total: int = 100_000,
    seed: int | None = None,
    force: bool = False,
) -> int:
    """Verteilt ``total`` Überlebende deterministisch über die Welt.

    Multinomiale Verteilung proportional zur Bevölkerungsdichte des Gitters.
    Innerhalb jeder Zelle wird die Position deterministisch gejittert.

    Regeneriert (Tabelle leeren + neu erzeugen) wenn:
    - ``force`` True, ODER
    - vorhandene Anzahl != total, ODER
    - mindestens eine vorhandene Zeile ``birth_tick IS NULL`` hat
      (Selbstheilung für stale DBs aus Zeiten vor der Spalte).
    Sonst: No-op (Idempotenz).

    Parameters
    ----------
    conn:
        Offene DB-Verbindung (Sim-Kern-Verbindung).
    total:
        Anzahl zu verteilender Überlebender.
    seed:
        Expliziter Seed; falls None wird ``db.get_world_seed(conn)`` genutzt.
    force:
        True erzwingt Tabelle leeren + neu erzeugen unabhängig von der Anzahl.

    Returns
    -------
    int
        Anzahl eingefügter Zeilen (== total).
    """
    # Idempotenz-Check mit Selbstheilung für stale DBs (birth_tick IS NULL
    # oder home_lat IS NULL aus Zeiten vor Issue #29).
    existing = conn.execute("SELECT COUNT(*) FROM survivors;").fetchone()[0]
    if not force and existing == total:
        null_count = conn.execute(
            "SELECT COUNT(*) FROM survivors WHERE birth_tick IS NULL OR home_lat IS NULL;"
        ).fetchone()[0]
        if null_count == 0:
            return total

    # Gitter laden (prozessweit gecacht)
    grid = load_grid()  # list[tuple[lat, lon, weight]]

    if seed is None:
        seed = get_world_seed(conn)

    rng = random.Random(seed)

    lats = [c[0] for c in grid]
    lons = [c[1] for c in grid]
    weights = [c[2] for c in grid]

    total_weight = sum(weights)

    # --- Multinomiale Verteilung via Alias-/Inversion-Sampling ----------------
    # Für 100k Punkte auf ~163k Zellen ist `random.choices` mit kumulativen
    # Gewichten ausreichend schnell und vollständig deterministisch.
    # Wir ziehen alle Zell-Indizes auf einmal.
    cum_weights: list[float] = []
    running = 0.0
    for w in weights:
        running += w
        cum_weights.append(running)

    # Ziehe total Zell-Indizes (deterministisch)
    cell_indices: list[int] = rng.choices(
        range(len(grid)),
        cum_weights=cum_weights,
        k=total,
    )

    # Altersgruppengewichte einmal laden (prozessweit gecacht via lru_cache)
    age_brackets = age_weights()  # [((lo, hi), weight), ...]
    bracket_ranges = [b[0] for b in age_brackets]
    bracket_weights_list = [b[1] for b in age_brackets]
    total_age_weight = sum(bracket_weights_list)
    # Kumulative Gewichte für schnelles Sampling
    cum_age_weights: list[float] = []
    running = 0.0
    for w in bracket_weights_list:
        running += w
        cum_age_weights.append(running)

    # Jitter innerhalb der Zelle: deterministisch aus separatem RNG-State
    # (RNG läuft deterministisch weiter nach den choices-Calls)
    # Gleichzeitig sex und birth_tick pro Survivor deterministisch aus
    # einem survivor-spezifischen RNG (Seed: f"{seed}:{survivor_id}").
    # survivor_id = 1-basiert, da SQLite AUTOINCREMENT bei INSERT ab 1 startet.
    rows: list[tuple[float, float, str, int]] = []
    for i, cell_idx in enumerate(cell_indices):
        cell_lat = lats[cell_idx]
        cell_lon = lons[cell_idx]
        jitter_lat = rng.uniform(-_HALF_CELL, _HALF_CELL)
        jitter_lon = rng.uniform(-_HALF_CELL, _HALF_CELL)
        s_lat = cell_lat + jitter_lat
        s_lon = cell_lon + jitter_lon

        # survivor_id ist 1-basiert (i+1, da Tabelle geleert und frisch befüllt)
        survivor_id = i + 1
        srng = random.Random(f"{seed}:{survivor_id}")

        # Geschlecht: ~50/50
        sex = srng.choice(("m", "f"))

        # Alter: gewichtetes Sampling aus Altersgruppen
        r_age = srng.uniform(0.0, total_age_weight)
        running_w = 0.0
        lo, hi = bracket_ranges[-1]  # Sicherheitsnetz
        for (bracket_lo, bracket_hi), w in zip(bracket_ranges, bracket_weights_list):
            running_w += w
            if r_age <= running_w:
                lo, hi = bracket_lo, bracket_hi
                break
        age_years = srng.randint(lo, hi)

        # birth_tick: negativ (vor Kollaps); Alter = (current_tick - birth_tick) / 525_600
        birth_tick = -(age_years * _MINUTES_PER_YEAR)

        # home_lat/home_lon = initiale Spawn-Position (Heimat-Anker, Issue #29)
        rows.append((s_lat, s_lon, sex, birth_tick, s_lat, s_lon))

    # Tabelle leeren (falls partiell befüllt oder anderer seed);
    # survivor_groups ebenfalls leeren, damit kein Stale-State bleibt.
    conn.execute("DELETE FROM survivor_groups;")
    conn.execute("DELETE FROM survivors;")

    # Bulk-Insert in einer Transaktion (alive=1 DEFAULT, group_id=NULL DEFAULT)
    conn.executemany(
        "INSERT INTO survivors (lat, lon, sex, birth_tick, home_lat, home_lon) VALUES (?, ?, ?, ?, ?, ?);",
        rows,
    )
    conn.commit()
    return total


def materialize_in_bbox(
    conn: sqlite3.Connection,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
) -> list[int]:
    """Materialisiert noch nicht materialisierte Überlebende in der Bbox als NPCs.

    Für jeden Überlebenden mit ``materialized=0`` im angegebenen Rechteck wird
    eine Minimal-NPC-Zeile in ``characters`` angelegt (type='survivor').
    Danach werden ``materialized=1`` und ``character_id`` gesetzt.

    Idempotent: bereits materialisierte Zeilen werden übersprungen.
    Alles in einer Transaktion.

    Returns
    -------
    list[int]
        character_ids der neu angelegten NPCs.
    """
    rows = conn.execute(
        "SELECT id, lat, lon, sex, birth_tick FROM survivors "
        "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND materialized = 0;",
        (min_lat, max_lat, min_lon, max_lon),
    ).fetchall()

    if not rows:
        return []

    # world_seed und start_datetime einmal laden, nicht je Survivor
    world_row = conn.execute(
        "SELECT tick, world_seed, start_datetime FROM world WHERE id = 1;"
    ).fetchone()
    at_tick = world_row["tick"]
    world_seed = world_row["world_seed"]
    start_datetime = world_row["start_datetime"]

    # Alle Gebäude in der erweiterten Bbox laden (für Wohnhaus-Suche).
    # Bbox leicht vergrößern um _HOUSE_SEARCH_RADIUS_M, damit Häuser knapp
    # außerhalb der Survivor-Bbox gefunden werden.
    search_pad_deg = _HOUSE_SEARCH_RADIUS_M / 111_320.0
    house_candidates = conn.execute(
        "SELECT id, type, lat, lon, footprint_json FROM locations "
        "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
        "AND type IN ('house','building','apartment','residential');",
        (
            min_lat - search_pad_deg,
            max_lat + search_pad_deg,
            min_lon - search_pad_deg,
            max_lon + search_pad_deg,
        ),
    ).fetchall()

    def _footprint_centroid(footprint_json: str | None, fallback_lat: float, fallback_lon: float) -> tuple[float, float]:
        """Berechnet den Zentroid eines Footprint-Polygons, sonst Fallback."""
        if not footprint_json:
            return fallback_lat, fallback_lon
        try:
            pts = json.loads(footprint_json)  # [[lat, lon], ...]
            if not pts:
                return fallback_lat, fallback_lon
            avg_lat = sum(p[0] for p in pts) / len(pts)
            avg_lon = sum(p[1] for p in pts) / len(pts)
            return avg_lat, avg_lon
        except Exception:
            return fallback_lat, fallback_lon

    def _nearest_house(s_lat: float, s_lon: float) -> tuple[float, float]:
        """Findet nächstes Wohnhaus; Fallback: nächstes Gebäude; sonst Original."""
        best_house_dist = float("inf")
        best_house_pos: tuple[float, float] | None = None
        best_any_dist = float("inf")
        best_any_pos: tuple[float, float] | None = None

        for loc in house_candidates:
            loc_lat, loc_lon = _footprint_centroid(loc["footprint_json"], loc["lat"], loc["lon"])
            dist = _haversine_m(s_lat, s_lon, loc_lat, loc_lon)
            if loc["type"] in _HOUSE_TYPES:
                if dist < best_house_dist:
                    best_house_dist = dist
                    best_house_pos = (loc_lat, loc_lon)
            if dist < best_any_dist:
                best_any_dist = dist
                best_any_pos = (loc_lat, loc_lon)

        if best_house_pos is not None and best_house_dist <= _HOUSE_SEARCH_RADIUS_M:
            return best_house_pos
        if best_any_pos is not None and best_any_dist <= _HOUSE_SEARCH_RADIUS_M:
            return best_any_pos
        return s_lat, s_lon  # Originalkoordinate als letzter Fallback

    character_ids: list[int] = []

    for row in rows:
        survivor_id = row["id"]
        s_lat = row["lat"]
        s_lon = row["lon"]
        db_sex = row["sex"]
        db_birth_tick = row["birth_tick"]

        # Platzierung im nächsten Wohnhaus
        npc_lat, npc_lon = _nearest_house(s_lat, s_lon)

        identity = generate_identity(
            survivor_id=survivor_id,
            lat=s_lat,
            lon=s_lon,
            world_seed=world_seed,
            start_datetime=start_datetime,
        )

        # Gespeicherte sex/birth_tick aus der DB-Zeile gewinnen (Konsistenz).
        # generate_identity liefert Name/Beruf; sex/Geburtsdatum werden
        # durch die in spawn_survivors festgelegten Werte überschrieben.
        final_sex = db_sex if db_sex is not None else identity["sex"]

        # birth_tick → birthdate (ISO-String) für die characters-Tabelle
        if db_birth_tick is not None:
            from datetime import datetime, timedelta
            start_dt = datetime.fromisoformat(start_datetime)
            birth_dt = start_dt + timedelta(minutes=db_birth_tick)  # db_birth_tick ist negativ
            final_birthdate = birth_dt.date().isoformat()
        else:
            final_birthdate = identity["birthdate"]

        # NPC-Zeile mit echter Identitaet aus generate_identity
        conn.execute(
            "INSERT INTO characters "
            "(name, sex, birthdate, profession, type, lat, lon, "
            "hunger, thirst, sleep, injury, exposure, "
            "satisfaction, performance, is_alive, daily_kcal, daily_water_l) "
            "VALUES (?, ?, ?, ?, 'survivor', ?, ?, 1.0, 1.0, 1.0, 1.0, 1.0, "
            "1.0, 1.0, 1, 2000.0, 2.0);",
            (
                identity["name"],
                final_sex,
                final_birthdate,
                identity["profession"],
                npc_lat,
                npc_lon,
            ),
        )
        char_id = conn.execute("SELECT last_insert_rowid();").fetchone()[0]
        conn.execute(
            "UPDATE survivors SET materialized = 1, character_id = ? WHERE id = ?;",
            (char_id, survivor_id),
        )
        character_ids.append(char_id)

    conn.commit()
    return character_ids


def count_near(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    radius_m: float,
) -> int:
    """Zählt Überlebende im Radius (Haversine nach Bbox-Vorfilter).

    Parameters
    ----------
    conn:
        DB-Verbindung.
    lat, lon:
        Mittelpunkt in Grad.
    radius_m:
        Suchradius in Metern.

    Returns
    -------
    int
        Anzahl survivors in radius_m um (lat, lon).
    """
    # Grobe Bbox-Vorfilterung (equirektangulär)
    d_lat = radius_m / 111_320.0
    d_lon = radius_m / (111_320.0 * math.cos(math.radians(lat))) if lat != 90.0 else radius_m / 111_320.0

    candidates = conn.execute(
        "SELECT lat, lon FROM survivors "
        "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?;",
        (lat - d_lat, lat + d_lat, lon - d_lon, lon + d_lon),
    ).fetchall()

    count = 0
    for row in candidates:
        if _haversine_m(lat, lon, row["lat"], row["lon"]) <= radius_m:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Interne Hilfsfunktion
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine-Distanz in Metern zwischen zwei Koordinaten."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Profil-Anreicherung bei Begegnung (Issue #16)
# ---------------------------------------------------------------------------

_PROFILE_MAX = 300
_DEMEANORS = ("zurückhaltend", "freundlich", "nervös", "gelassen", "misstrauisch")


def _clamp_text(value, limit: int = _PROFILE_MAX) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _survivor_age(conn: sqlite3.Connection, birthdate: str | None) -> int | None:
    from datetime import datetime
    if not birthdate:
        return None
    start = conn.execute("SELECT start_datetime FROM world WHERE id = 1;").fetchone()
    try:
        b, now = datetime.fromisoformat(birthdate), datetime.fromisoformat(start["start_datetime"])
    except (ValueError, TypeError):
        return None
    return max(0, now.year - b.year - ((now.month, now.day) < (b.month, b.day)))


def get_profile(conn: sqlite3.Connection, character_id: int) -> dict | None:
    row = conn.execute(
        "SELECT character_id, personality, short_bio, demeanor, created_tick "
        "FROM survivor_profile WHERE character_id = ?;", (character_id,)).fetchone()
    return dict(row) if row else None


def enrich_on_encounter(conn: sqlite3.Connection, character_id: int) -> dict | None:
    """Begegnung: erzeugt einmalig Persönlichkeit/Kurzbiografie eines
    Survivor-Charakters. Idempotent — existiert schon ein Profil, gibt es KEINEN
    erneuten LLM-Aufruf. Das Backend schlägt nur vor; hier wird geklemmt (Länge)
    und geschrieben, bei leerem/fehlerhaftem Vorschlag greift ein deterministischer
    Default. Schreibt ohne eigenes Commit (Aufrufer hält die Transaktion).
    None, wenn es den Charakter nicht gibt."""
    existing = get_profile(conn, character_id)
    if existing is not None:
        return existing
    char = conn.execute(
        "SELECT id, name, sex, birthdate, profession, lat, lon FROM characters "
        "WHERE id = ? AND type = 'survivor';", (character_id,)).fetchone()
    if char is None:
        return None
    from ..llm import get_backend
    from . import rng
    from .locale import region_for

    world = conn.execute("SELECT tick, world_seed FROM world WHERE id = 1;").fetchone()
    identity = {
        "name": char["name"], "sex": char["sex"], "age": _survivor_age(conn, char["birthdate"]),
        "profession": char["profession"],
        "region": region_for(char["lat"], char["lon"]) if char["lat"] is not None else "unknown",
    }
    try:
        proposal = get_backend().describe_survivor(identity, {"tick": world["tick"]}) or {}
    except Exception:
        proposal = {}
    if not isinstance(proposal, dict):
        proposal = {}
    prof = identity["profession"] or "ohne festen Beruf"
    demeanor = _clamp_text(proposal.get("demeanor"), 40) or _DEMEANORS[
        int(rng.roll(world["world_seed"], "demeanor", character_id) * len(_DEMEANORS))
        % len(_DEMEANORS)]
    personality = _clamp_text(proposal.get("personality")) or f"{demeanor.capitalize()}."
    short_bio = _clamp_text(proposal.get("short_bio")) or (
        f"{identity['name']}, früher {prof}. Hat den Zusammenbruch überlebt.")
    conn.execute(
        "INSERT INTO survivor_profile (character_id, personality, short_bio, demeanor, "
        "created_tick) VALUES (?, ?, ?, ?, ?);",
        (character_id, personality, short_bio, demeanor, world["tick"]))
    return get_profile(conn, character_id)
