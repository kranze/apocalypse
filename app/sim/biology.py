"""Biologie: Bedürfnisse, Performance, Sterbe-Check.

Phase 3 des Ticks (CLAUDE.md). In Schritt 1 ist nur die Hunger-Achse aktiv;
die übrigen Achsen bleiben bei 1.0 und gehen neutral (Faktor 1.0) in die
Performance ein. Kein Binär-Tod: Performance degradiert, erst unter der
kritischen Schwelle folgt ein (deterministischer) Sterbe-Wurf (DESIGN.md §5).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import constants
from .events import emit_event
from .movement import carried_weight
from .rng import roll


def _penalty(value: float) -> float:
    """1.0 ab Komfortschwelle, linear bis 0.0 bei 0 — für jede Bedürfnis-Achse."""
    return max(0.0, min(1.0, value / constants.PERF_COMFORT_HUNGER))


def compute_targets(sex, weight_kg, height_cm, age) -> tuple[float, float]:
    """Tagesbedarf aus Profil: kcal via Mifflin-St-Jeor (× Grundaktivität),
    Wasser via ml/kg. Robuste Defaults, falls Felder fehlen."""
    w = weight_kg or 75.0
    h = height_cm or 175.0
    a = age if age is not None else 35
    s = {"m": 5.0, "f": -161.0}.get((sex or "").lower(), -78.0)  # x/unbekannt: Mittel
    bmr = 10.0 * w + 6.25 * h - 5.0 * a + s
    daily_kcal = round(max(1200.0, bmr * 1.4), 0)
    daily_water_l = round(w * constants.WATER_ML_PER_KG / 1000.0, 2)
    return daily_kcal, daily_water_l


def apply_hunger(
    conn: sqlite3.Connection,
    minutes: int,
    now_tick: int,
    distances: dict | None = None,
) -> list[dict[str, Any]]:
    """Senkt die Sättigung aller Lebenden über die verstrichene Zeit.

    Grundverbrauch (Zeit) plus Aktivitäts-Verbrauch: gelaufene Distanz mal
    Gesamtgewicht (Körper + Rucksack). ``distances`` = {char_id: meter} aus der
    Bewegungsphase. Emittiert einen Interrupt beim *Überschreiten* einer
    Schwelle (einmalig).
    """
    distances = distances or {}
    base_loss = minutes / constants.MINUTES_PER_DAY * constants.HUNGER_LOSS_PER_DAY
    interrupts = []
    for row in conn.execute(
        "SELECT id, name, hunger, weight_kg, group_id, daily_kcal "
        "FROM characters WHERE is_alive = 1;"
    ).fetchall():
        old = row["hunger"]
        loss = base_loss
        dist_m = distances.get(row["id"], 0.0)
        if dist_m > 0:
            total_kg = (row["weight_kg"] or 0.0) + carried_weight(conn, row["group_id"])
            kcal = constants.K_WALK_KCAL_PER_KG_KM * total_kg * (dist_m / 1000.0)
            loss += kcal / row["daily_kcal"]
        new = max(0.0, old - loss)
        conn.execute(
            "UPDATE characters SET hunger = ? WHERE id = ?;",
            (round(new, 6), row["id"]),
        )
        if old >= constants.HUNGER_CRIT > new:
            interrupts.append(
                emit_event(
                    conn, now_tick, "need",
                    f"{row['name']} ist kritisch hungrig.",
                    severity="soft", subject_type="character", subject_id=row["id"],
                    payload={"hunger": round(new, 4)},
                )
            )
        elif old >= constants.HUNGER_SOFT > new:
            interrupts.append(
                emit_event(
                    conn, now_tick, "need",
                    f"{row['name']} wird hungrig.",
                    severity="soft", subject_type="character", subject_id=row["id"],
                    payload={"hunger": round(new, 4)},
                )
            )
    return interrupts


def apply_thirst(conn, minutes, now_tick, distances=None) -> list[dict[str, Any]]:
    """Senkt den Durst über Zeit + Aktivität (Schwitzen beim Laufen)."""
    distances = distances or {}
    base = minutes / constants.MINUTES_PER_DAY * constants.THIRST_LOSS_PER_DAY
    interrupts = []
    for row in conn.execute(
        "SELECT id, name, thirst FROM characters WHERE is_alive = 1;"
    ).fetchall():
        old = row["thirst"]
        loss = base + constants.THIRST_ACTIVITY_PER_KM * (distances.get(row["id"], 0.0) / 1000.0)
        new = max(0.0, old - loss)
        conn.execute("UPDATE characters SET thirst = ? WHERE id = ?;", (round(new, 6), row["id"]))
        if old >= constants.HUNGER_CRIT > new:
            interrupts.append(emit_event(conn, now_tick, "need", f"{row['name']} ist kritisch durstig.",
                              severity="soft", subject_type="character", subject_id=row["id"]))
        elif old >= constants.HUNGER_SOFT > new:
            interrupts.append(emit_event(conn, now_tick, "need", f"{row['name']} wird durstig.",
                              severity="soft", subject_type="character", subject_id=row["id"]))
    return interrupts


def apply_sleep(conn, minutes, now_tick) -> list[dict[str, Any]]:
    """Schlafdruck im Wachzustand (Erholung passiert in der Versorgung/Ruhe)."""
    loss = minutes / constants.MINUTES_PER_DAY * constants.SLEEP_LOSS_PER_DAY
    interrupts = []
    for row in conn.execute(
        "SELECT id, name, sleep FROM characters WHERE is_alive = 1;"
    ).fetchall():
        old = row["sleep"]
        new = max(0.0, old - loss)
        conn.execute("UPDATE characters SET sleep = ? WHERE id = ?;", (round(new, 6), row["id"]))
        if old >= constants.HUNGER_CRIT > new:
            interrupts.append(emit_event(conn, now_tick, "need", f"{row['name']} ist völlig erschöpft.",
                              severity="soft", subject_type="character", subject_id=row["id"]))
        elif old >= constants.HUNGER_SOFT > new:
            interrupts.append(emit_event(conn, now_tick, "need", f"{row['name']} wird müde.",
                              severity="soft", subject_type="character", subject_id=row["id"]))
    return interrupts


def recompute_satisfaction(conn: sqlite3.Connection, minutes: int) -> None:
    """Zufriedenheit nähert sich gedämpft einem Ziel aus Bedürfnis-Deckung
    (schwächste Achse stärker gewichtet) + Geborgenheit (in einem Gebäude)
    − Isolation (alle tot)."""
    rate = min(1.0, minutes / constants.MINUTES_PER_DAY * constants.SATISFACTION_ADJUST_PER_DAY)
    isolation = minutes / constants.MINUTES_PER_DAY * constants.SATISFACTION_ISOLATION_PER_DAY
    for row in conn.execute(
        "SELECT id, hunger, thirst, sleep, satisfaction, lat, lon FROM characters "
        "WHERE is_alive = 1;"
    ).fetchall():
        needs = [row["hunger"], row["thirst"], row["sleep"]]
        w = constants.SATISFACTION_MIN_WEIGHT
        target = w * min(needs) + (1 - w) * (sum(needs) / len(needs))
        if row["lat"] is not None and _at_shelter(conn, row["lat"], row["lon"]):
            target += constants.SATISFACTION_SHELTER_BONUS
        target = max(0.0, min(1.0, target - isolation))
        new = row["satisfaction"] + (target - row["satisfaction"]) * rate
        conn.execute("UPDATE characters SET satisfaction = ? WHERE id = ?;",
                     (round(max(0.0, min(1.0, new)), 6), row["id"]))


def _at_shelter(conn, lat, lon) -> bool:
    for r in conn.execute(
        "SELECT lat, lon FROM locations WHERE discovery_status != 'undiscovered';"
    ).fetchall():
        if abs(r["lat"] - lat) < 0.0005 and abs(r["lon"] - lon) < 0.0005:
            return True
    return False


def recompute_performance(conn: sqlite3.Connection) -> None:
    """Abgeleitete Performance je Lebendem, multiplikativ aus Hunger × Durst × Schlaf."""
    for row in conn.execute(
        "SELECT id, hunger, thirst, sleep FROM characters WHERE is_alive = 1;"
    ).fetchall():
        perf = _penalty(row["hunger"]) * _penalty(row["thirst"]) * _penalty(row["sleep"])
        conn.execute(
            "UPDATE characters SET performance = ? WHERE id = ?;",
            (round(perf, 4), row["id"]),
        )


def death_check(
    conn: sqlite3.Connection, now_tick: int, minutes: int, seed: int
) -> list[dict[str, Any]]:
    """Sterbe-Wurf nur unter kritischer Performance; deterministisch geseedet."""
    interrupts = []
    for row in conn.execute(
        "SELECT id, name, performance FROM characters WHERE is_alive = 1;"
    ).fetchall():
        perf = row["performance"]
        if perf >= constants.CRIT_PERFORMANCE:
            continue
        p_death = (
            (constants.CRIT_PERFORMANCE - perf)
            * constants.DEATH_K
            * (minutes / constants.MINUTES_PER_DAY)
        )
        if roll(seed, "death", row["id"], now_tick) < p_death:
            conn.execute(
                "UPDATE characters SET is_alive = 0, performance = 0 WHERE id = ?;",
                (row["id"],),
            )
            interrupts.append(
                emit_event(
                    conn, now_tick, "need",
                    f"{row['name']} ist gestorben (Entkräftung).",
                    severity="decision", subject_type="character", subject_id=row["id"],
                    payload={"performance": round(perf, 4), "p_death": round(p_death, 5)},
                )
            )
    return interrupts
