"""Geräte mit Akku und Netzqualität (Slice M2 Teil B, Issue #46).

Die Geräte selbst (``smartphone``, ``laptop``, ``powerbank``) sind normale
Items in ``group_inventory`` — Loot ist ein Transfer und bilanzneutral. Der
LADEZUSTAND ist kein Ledger-Gut, sondern lebt in ``device_state`` (je Gruppe und
Item): ``charge`` = Summe der Ladungen aller Einheiten (je 0..1), ``units`` =
zuletzt bekannte Stückzahl. Leerlaufverbrauch wird lazy beim Zugriff
nachgerechnet (``sync``), Online-Verbrauch und Laden buchen die Aktionen
explizit — beides ausschließlich über Funktionen dieses Moduls (Eisernes
Prinzip: kein inline-Schreiben, kein LLM).

Netzqualität (Regel, siehe ``network_quality``):
    quality = internet_at(tick) * max(cellular_at(tick), 1.0 falls WLAN am Ort)
WLAN gibt es nur an einem Ort (Location in Reichweite) mit lokalem Strom über
``WIFI_POWER_MIN``. Das Internet-Backbone (``internet_at``) ist die Obergrenze:
ohne Backbone hilft weder Mobilfunk noch WLAN. Unter ``ONLINE_MIN_QUALITY``
geht nichts mehr online.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from . import constants, decay_world

ONLINE_DEVICES = ("smartphone", "laptop")      # nutzbar für Online-Aktionen
DEVICES = ("smartphone", "laptop", "powerbank")


def _tick(conn) -> int:
    return conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]


def _owned(conn, gid: int) -> dict[str, float]:
    return {
        r["item_id"]: r["q"] for r in conn.execute(
            "SELECT item_id, SUM(quantity) AS q FROM group_inventory "
            "WHERE group_id = ? AND item_id IN ('smartphone','laptop','powerbank') "
            "GROUP BY item_id HAVING SUM(quantity) > 0;", (gid,),
        ).fetchall()
    }


def grant(conn, gid: int, item_id: str, charge_per_unit: float, units: float = 1.0) -> None:
    """Setzt den Ladezustand für ``units`` Einheiten (Spielstart). Das Item selbst
    muss der Aufrufer über eine ledgergebuchte Sim-Funktion ins Inventar legen."""
    now = _tick(conn)
    conn.execute(
        "INSERT INTO device_state (group_id, item_id, units, charge, updated_tick) "
        "VALUES (?,?,?,?,?) ON CONFLICT(group_id, item_id) DO UPDATE SET "
        "units=excluded.units, charge=excluded.charge, updated_tick=excluded.updated_tick;",
        (gid, item_id, units, charge_per_unit * units, now),
    )


def sync(conn, gid: int) -> dict[str, dict[str, float]]:
    """Gleicht device_state mit dem Inventar ab (neue Einheiten kommen mit
    ``DEVICE_FOUND_CHARGE``, verlorene nehmen ihre Ladung mit) und wendet den
    Leerlaufverbrauch seit ``updated_tick`` an. Liefert {item_id: {units, charge}}."""
    now = _tick(conn)
    owned = _owned(conn, gid)
    out: dict[str, dict[str, float]] = {}
    with conn:
        for item_id in DEVICES:
            qty = owned.get(item_id, 0.0)
            row = conn.execute(
                "SELECT units, charge, updated_tick FROM device_state "
                "WHERE group_id = ? AND item_id = ?;", (gid, item_id),
            ).fetchone()
            if qty <= 0:
                if row is not None:
                    conn.execute(
                        "DELETE FROM device_state WHERE group_id = ? AND item_id = ?;",
                        (gid, item_id))
                continue
            if row is None:
                units, charge, last = qty, qty * constants.DEVICE_FOUND_CHARGE, now
            else:
                units, charge, last = row["units"], row["charge"], row["updated_tick"]
                if qty > units:
                    charge += (qty - units) * constants.DEVICE_FOUND_CHARGE
                units = qty
                charge = min(charge, units)
            hours = max(0, now - last) / 60.0
            idle = constants.DEVICE_DRAIN_IDLE_PER_H.get(item_id, 0.0)
            charge = max(0.0, charge - idle * hours * units)
            conn.execute(
                "INSERT INTO device_state (group_id, item_id, units, charge, updated_tick) "
                "VALUES (?,?,?,?,?) ON CONFLICT(group_id, item_id) DO UPDATE SET "
                "units=excluded.units, charge=excluded.charge, "
                "updated_tick=excluded.updated_tick;",
                (gid, item_id, units, charge, now),
            )
            out[item_id] = {"units": units, "charge": charge}
    return out


def status(conn, gid: int) -> list[dict[str, Any]]:
    """Geräteliste für HUD/API: pct = mittlere Ladung je Einheit in Prozent."""
    names = dict(conn.execute(
        "SELECT id, name FROM item_catalog WHERE category = 'device';").fetchall())
    st = sync(conn, gid)
    return [
        {"item_id": i, "name": names.get(i, i), "units": v["units"],
         "charge": round(v["charge"], 4),
         "pct": round(100.0 * v["charge"] / v["units"], 1) if v["units"] else 0.0}
        for i, v in st.items()
    ]


def best_online_device(conn, gid: int) -> tuple[str, float] | None:
    """Gerät (smartphone/laptop) mit der meisten Restladung (Summe) oder None, wenn
    keines über ``DEVICE_MIN_CHARGE`` liegt. Liefert (item_id, charge)."""
    st = sync(conn, gid)
    best = None
    for i in ONLINE_DEVICES:
        v = st.get(i)
        if v and v["charge"] > constants.DEVICE_MIN_CHARGE:
            if best is None or v["charge"] > best[1]:
                best = (i, v["charge"])
    return best


def has_online_device(conn, gid: int) -> bool:
    return any(i in _owned(conn, gid) for i in ONLINE_DEVICES)


def online_minutes_available(conn, gid: int) -> int:
    """Wie viele Minuten Online-Betrieb der Akku des besten Geräts noch trägt."""
    best = best_online_device(conn, gid)
    if best is None:
        return 0
    item, charge = best
    rate = constants.DEVICE_DRAIN_ONLINE_PER_H[item]
    return int(math.floor(charge / rate * 60.0 + 1e-9))


def drain_online(conn, gid: int, minutes: int) -> str | None:
    """Zieht ``minutes`` Online-Verbrauch vom besten Gerät ab (nie unter 0).
    Liefert die item_id des genutzten Geräts."""
    best = best_online_device(conn, gid)
    if best is None:
        return None
    item, charge = best
    used = constants.DEVICE_DRAIN_ONLINE_PER_H[item] * minutes / 60.0
    with conn:
        conn.execute(
            "UPDATE device_state SET charge = ? WHERE group_id = ? AND item_id = ?;",
            (max(0.0, charge - used), gid, item),
        )
    return item


# --- Netzqualität --------------------------------------------------------
def network_quality(conn, location_id: int | None) -> dict[str, Any]:
    """Qualität 0..1 am Standort (siehe Moduldoku). ``location_id`` = Ort in
    Reichweite oder None (draußen: nur Mobilfunk)."""
    tick = _tick(conn)
    internet = decay_world.internet_at(tick)
    cellular = decay_world.cellular_at(tick)
    wifi = False
    if location_id is not None:
        wifi = decay_world.infra_for(conn, location_id)["power"] > constants.WIFI_POWER_MIN
    access = max(cellular, 1.0 if wifi else 0.0)
    q = round(internet * access, 4)
    return {
        "quality": q, "internet": internet, "cellular": cellular, "wifi": wifi,
        "online": q >= constants.ONLINE_MIN_QUALITY,
    }


# --- Laden ---------------------------------------------------------------
def _deficits(conn, gid: int) -> dict[str, dict[str, float]]:
    st = sync(conn, gid)
    return {i: {**v, "deficit": v["units"] - v["charge"]} for i, v in st.items()}


def grid_charge_plan(conn, gid: int, location_id: int | None) -> dict[str, Any] | None:
    """Laden am Stromnetz: möglich bei lokalem Strom > CHARGE_POWER_MIN. Liefert
    {minutes, gains{item: delta}} oder None (nichts zu laden / kein Strom)."""
    if location_id is None:
        return None
    if decay_world.infra_for(conn, location_id)["power"] <= constants.CHARGE_POWER_MIN:
        return None
    d = _deficits(conn, gid)
    need = {i: v for i, v in d.items() if v["deficit"] > 0.02}
    if not need:
        return None
    hours = max(
        v["deficit"] / v["units"] / constants.DEVICE_CHARGE_PER_H[i] for i, v in need.items())
    minutes = int(min(constants.CHARGE_MAX_MINUTES,
                      max(constants.CHARGE_MIN_MINUTES, math.ceil(hours * 60.0))))
    gains = {
        i: min(v["deficit"],
               constants.DEVICE_CHARGE_PER_H[i] * minutes / 60.0 * v["units"])
        for i, v in need.items()
    }
    return {"minutes": minutes, "gains": gains, "source": "grid"}


def powerbank_charge_plan(conn, gid: int) -> dict[str, Any] | None:
    """Laden aus der Powerbank (überall): Ladung wandert vom Akku der Powerbank
    (``POWERBANK_CAPACITY_PHONES`` Smartphone-Ladungen je volle Einheit) in
    Smartphone/Laptop. Liefert {minutes, gains, pb_cost} oder None."""
    d = _deficits(conn, gid)
    pb = d.get("powerbank")
    if pb is None or pb["charge"] <= constants.DEVICE_MIN_CHARGE:
        return None
    targets = {i: d[i]["deficit"] for i in ONLINE_DEVICES if i in d and d[i]["deficit"] > 0.02}
    if not targets:
        return None
    energy = pb["charge"] * constants.POWERBANK_CAPACITY_PHONES
    total = min(sum(targets.values()), energy)
    gains: dict[str, float] = {}
    left = total
    for i in ONLINE_DEVICES:  # Smartphone zuerst
        if i in targets and left > 0:
            g = min(targets[i], left)
            gains[i] = g
            left -= g
    minutes = max(15, int(math.ceil(total / constants.POWERBANK_OUTPUT_PER_H * 60.0)))
    return {"minutes": minutes, "gains": gains,
            "pb_cost": total / constants.POWERBANK_CAPACITY_PHONES, "source": "powerbank"}


def charge_plan(conn, gid: int, location_id: int | None,
                source: str | None = None) -> dict[str, Any] | None:
    """Gemeinsame Ladeplan-Wahl (Aktion ``charge`` UND Effekt ``charge_device``):
    erst Stromnetz (nur bei lokalem Strom > CHARGE_POWER_MIN), dann Powerbank.
    ``source`` = "grid" | "powerbank" | None (beides erlaubt)."""
    plan = None
    if source in (None, "grid"):
        plan = grid_charge_plan(conn, gid, location_id)
    if plan is None and source in (None, "powerbank"):
        plan = powerbank_charge_plan(conn, gid)
    return plan


def charge_block_reason(conn, gid: int) -> str:
    """Warum es keinen Ladeplan gibt: ``already_full`` oder ``no_power``."""
    if all(v["units"] - v["charge"] <= 0.02 for v in sync(conn, gid).values()):
        return "already_full"
    return "no_power"


def apply_charge(conn, gid: int, plan: dict[str, Any], minutes: int | None = None) -> dict[str, float]:
    """Bucht einen Ladeplan (ggf. anteilig, wenn die Aktion unterbrochen wurde)."""
    frac = 1.0 if minutes is None else max(0.0, min(1.0, minutes / plan["minutes"]))
    now = _tick(conn)
    done: dict[str, float] = {}
    with conn:
        for item, gain in plan["gains"].items():
            g = gain * frac
            conn.execute(
                "UPDATE device_state SET charge = MIN(units, charge + ?), updated_tick = ? "
                "WHERE group_id = ? AND item_id = ?;", (g, now, gid, item))
            done[item] = round(g, 4)
        if plan.get("pb_cost"):
            conn.execute(
                "UPDATE device_state SET charge = MAX(0.0, charge - ?) "
                "WHERE group_id = ? AND item_id = 'powerbank';",
                (plan["pb_cost"] * frac, gid))
    return done
