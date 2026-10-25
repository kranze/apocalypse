"""Verfall-Simulator: Infrastruktur-Zustand (Strom, Wasser, Kühlung, ...) je Ort.

Phase 1 des Ticks (Welt/Physik). Alles ist eine REINE Funktion aus
(Tag seit Kollaps, world_seed, location_id) — kein RNG außer ``rng.roll``.
Infrastruktur ist kein Ledger-Gut; dieses Modul fasst Inventar/Ledger nie an.

Tag = fraktional ``tick / MINUTES_PER_DAY`` (Tick = Spielminuten seit Kollaps).
Höhendaten gibt es nicht; „höher gelegene Orte früher“ wird durch den
Orts-Jitter abgebildet.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import constants, rng

_COLS = ("power", "water", "refrigeration", "structural", "overgrowth")


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _day(tick: int) -> float:
    return tick / constants.MINUTES_PER_DAY


def _jit(seed: int, loc_id: int, what: str) -> float:
    return rng.roll(seed, "infra", what, loc_id)


def _power_off_day(seed: int, loc_id: int) -> float:
    lo, hi = constants.INFRA_POWER_OFF_DAY_MIN, constants.INFRA_POWER_OFF_DAY_MAX
    return lo + _jit(seed, loc_id, "power") * (hi - lo)


def _water_off_day(seed: int, loc_id: int) -> float:
    lo, hi = constants.INFRA_WATER_OFF_DAY_MIN, constants.INFRA_WATER_OFF_DAY_MAX
    return lo + _jit(seed, loc_id, "water") * (hi - lo)


def _power_at(day: float, off: float) -> float:
    return _clamp01((off - day) / constants.INFRA_POWER_FALL_DAYS)


def _water_at(day: float, off: float) -> float:
    hold = constants.INFRA_WATER_HOLD_DAY
    return _clamp01((off - day) / (off - hold))


def values_at(seed: int, loc_id: int, tick: int) -> dict[str, float]:
    """Infrastruktur-Werte (0..1) eines Orts zum Tick. Rein, ohne DB."""
    d = _day(tick)
    power = _power_at(d, _power_off_day(seed, loc_id))
    js = 0.7 + 0.6 * _jit(seed, loc_id, "struct")
    jo = 0.7 + 0.6 * _jit(seed, loc_id, "overgrowth")
    structural = 1.0 - _clamp01(
        (d - constants.INFRA_STRUCT_HOLD_DAYS) / (constants.INFRA_STRUCT_DECAY_DAYS * js))
    overgrowth = _clamp01(
        (d - constants.INFRA_OVERGROWTH_START_DAY) / (constants.INFRA_OVERGROWTH_FULL_DAYS * jo))
    return {
        "power": round(power, 4),
        "water": round(_water_at(d, _water_off_day(seed, loc_id)), 4),
        "refrigeration": round(power, 4),  # Kühlung folgt Strom am Ort
        "structural": round(structural, 4),
        "overgrowth": round(overgrowth, 4),
    }


def infra_loc(conn: sqlite3.Connection, lat, lon, reach: float,
              character_id: int = 1) -> int | None:
    """Ort, dessen Infrastruktur (Strom/Wasser) an (lat, lon) gilt: nächste Location
    in ``reach`` (entdeckt oder nicht — jedes Gebäude hat Hahn/Steckdose), sonst None.
    Das Zuhause ist eine echte Location (Typ ``home``, siehe ``home.py``) und wird
    daher wie jeder andere Ort gefunden. Rein lesend. ``character_id`` bleibt für
    Aufrufer-Kompatibilität erhalten."""
    if lat is None or lon is None:
        return None
    import math

    def dist(a_lat, a_lon):
        mlon = 111_320.0 * math.cos(math.radians((lat + a_lat) / 2))
        return math.hypot((a_lon - lon) * mlon, (a_lat - lat) * 111_320.0)

    best, best_d = None, reach
    for r in conn.execute("SELECT id, lat, lon FROM locations;").fetchall():
        d = dist(r["lat"], r["lon"])
        if d <= best_d:
            best, best_d = r["id"], d
    return best


def cooling_lost_tick(seed: int, loc_id: int) -> int:
    """Tick, ab dem die Kühlung am Ort ausgefallen ist (refrigeration < Schwelle).
    Reine Funktion; Grundlage für den schnelleren Verderb (resources.py)."""
    off = _power_off_day(seed, loc_id)
    day = off - constants.INFRA_POWER_FALL_DAYS * constants.INFRA_REFRIGERATION_WARM_BELOW
    return int(round(day * constants.MINUTES_PER_DAY))


def _world(conn: sqlite3.Connection):
    return conn.execute("SELECT tick, world_seed FROM world WHERE id = 1;").fetchone()


def _store(conn: sqlite3.Connection, loc_id: int, v: dict[str, float], tick: int) -> None:
    conn.execute(
        "INSERT INTO location_infra (location_id, power, water, refrigeration, structural, "
        "overgrowth, updated_tick) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(location_id) DO UPDATE SET power=excluded.power, water=excluded.water, "
        "refrigeration=excluded.refrigeration, structural=excluded.structural, "
        "overgrowth=excluded.overgrowth, updated_tick=excluded.updated_tick;",
        (loc_id, *(v[c] for c in _COLS), tick),
    )


def infra_for(conn: sqlite3.Connection, location_id: int) -> dict[str, Any]:
    """Aktueller Infrastruktur-Zustand eines Orts, REIN berechnet (kein Schreiben —
    lesende Endpunkte dürfen die DB nicht ändern, Issue #52). Persistiert wird nur
    im Tick (``advance``). Gibt power/water/refrigeration/structural/overgrowth/
    updated_tick zurück."""
    w = _world(conn)
    v = values_at(w["world_seed"], location_id, w["tick"])
    return {**v, "updated_tick": w["tick"]}


def advance(conn: sqlite3.Connection, minutes: int) -> int:
    """Phase 1: schreibt den Zustand für bereits entdeckte Orte (oder Orte mit
    Inventar) fort. Nicht entdeckte Orte bleiben lazy. Liefert Anzahl Orte.
    ``minutes`` ist informativ — der Zustand ist eine reine Funktion des Ticks."""
    w = _world(conn)
    tick, seed = w["tick"], w["world_seed"]
    n = 0
    for r in conn.execute(
        "SELECT l.id FROM locations l WHERE l.discovery_status != 'undiscovered' "
        "OR EXISTS (SELECT 1 FROM location_inventory li WHERE li.location_id = l.id);"
    ).fetchall():
        _store(conn, r["id"], values_at(seed, r["id"], tick), tick)
        n += 1
    return n


def _interp(points, day: float) -> float:
    if day <= points[0][0]:
        return points[0][1]
    for (d0, v0), (d1, v1) in zip(points, points[1:]):
        if day <= d1:
            return v0 + (v1 - v0) * (day - d0) / (d1 - d0)
    return points[-1][1]


def cellular_at(tick: int) -> float:
    return round(_interp(constants.INFRA_CELLULAR_POINTS, _day(tick)), 4)


def internet_at(tick: int) -> float:
    return round(_interp(constants.INFRA_INTERNET_POINTS, _day(tick)), 4)


def phase_at(tick: int) -> str:
    d = _day(tick)
    for limit, label in constants.INFRA_PHASES:
        if d < limit:
            return label
    return constants.INFRA_PHASES[-1][1]


def global_phase(conn: sqlite3.Connection) -> str:
    """Grobe Welt-Verfallsphase als deutsches Label (Narration/HUD)."""
    return phase_at(_world(conn)["tick"])


def world_infra(conn: sqlite3.Connection) -> dict[str, Any]:
    """Weltweite Lage (JSON-serialisierbar). power/water = typischer Ort
    (Median-Jitter 0.5); cellular/internet = Netz-Kurven der Timeline."""
    tick = _world(conn)["tick"]
    d = _day(tick)
    lo, hi = constants.INFRA_POWER_OFF_DAY_MIN, constants.INFRA_POWER_OFF_DAY_MAX
    wlo, whi = constants.INFRA_WATER_OFF_DAY_MIN, constants.INFRA_WATER_OFF_DAY_MAX
    return {
        "day": round(d, 3),
        "power": round(_power_at(d, (lo + hi) / 2), 4),
        "water": round(_water_at(d, (wlo + whi) / 2), 4),
        "cellular": cellular_at(tick),
        "internet": internet_at(tick),
        "phase": phase_at(tick),
    }
