"""Energiespeicher als Stromquelle (Issue #54).

Ein deklarierter Hausakku / eine Powerstation (siehe ``declare``) ist ein normales
Item (Ledger-gebucht, Quelle ``player_declared``) PLUS eine Zeile in
``energy_state`` mit endlicher Kapazität und Ladestand in kWh. Der Ladestand ist
kein Ledger-Gut (wie ``device_state``). Ein Speicher liefert Strom,

* solange er am Ort liegt, an dem man steht (``location_inventory`` dieses Orts), oder
* überall, wenn man ihn trägt (``group_inventory``, z.B. eine Powerstation).

Genutzt wird er über die bestehenden Nähte: ``requirements.satisfy`` (Anforderung
``power`` für Rezepte) und ``devices.charge_plan`` (Geräte laden). Entnahme nur über
``draw`` (nie unter 0). Dieses Modul schreibt nur den Ladestand, nie Inventar/Ledger.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import constants


def storages_at(conn: sqlite3.Connection, group_id: int,
                location_id: int | None) -> list[dict[str, Any]]:
    """Nutzbare Speicher mit Rest über ``STORAGE_MIN_KWH``: am Ort ODER getragen.
    Reihenfolge deterministisch: erst die am Ort, dann getragene, je nach item_id."""
    loc = -1 if location_id is None else location_id
    rows = conn.execute(
        "SELECT es.item_id, es.capacity_kwh, es.charge_kwh, "
        " COALESCE((SELECT SUM(quantity) FROM location_inventory li "
        "           WHERE li.location_id = ? AND li.item_id = es.item_id), 0.0) AS at_loc, "
        " COALESCE((SELECT SUM(quantity) FROM group_inventory gi "
        "           WHERE gi.group_id = ? AND gi.item_id = es.item_id), 0.0) AS carried "
        "FROM energy_state es WHERE es.charge_kwh > ? ORDER BY es.item_id;",
        (loc, group_id, constants.STORAGE_MIN_KWH),
    ).fetchall()
    out = [
        {"item_id": r["item_id"], "capacity_kwh": r["capacity_kwh"],
         "charge_kwh": r["charge_kwh"], "carried": r["at_loc"] <= 0}
        for r in rows if r["at_loc"] > 0 or r["carried"] > 0
    ]
    out.sort(key=lambda s: (s["carried"], s["item_id"]))
    return out


def available_kwh(conn: sqlite3.Connection, group_id: int, location_id: int | None,
                  used: dict[str, float] | None = None) -> float:
    """Summe der nutzbaren Restenergie (abzüglich bereits vorgemerkter Entnahmen)."""
    used = used or {}
    return sum(max(0.0, s["charge_kwh"] - used.get(s["item_id"], 0.0))
               for s in storages_at(conn, group_id, location_id))


def pick(conn: sqlite3.Connection, group_id: int, location_id: int | None, kwh: float,
         used: dict[str, float] | None = None) -> str | None:
    """Erster Speicher mit mindestens ``kwh`` Rest (nach Abzug von ``used``) oder None."""
    used = used or {}
    for s in storages_at(conn, group_id, location_id):
        if s["charge_kwh"] - used.get(s["item_id"], 0.0) >= kwh - 1e-9:
            return s["item_id"]
    return None


def draw(conn: sqlite3.Connection, item_id: str, kwh: float) -> float:
    """Entnimmt ``kwh`` (höchstens den Rest). Liefert die tatsächlich entnommene
    Menge. Läuft in der Transaktion des Aufrufers."""
    row = conn.execute(
        "SELECT charge_kwh FROM energy_state WHERE item_id = ?;", (item_id,)).fetchone()
    if row is None or kwh <= 0:
        return 0.0
    take = min(row["charge_kwh"], kwh)
    tick = conn.execute("SELECT tick FROM world WHERE id = 1;").fetchone()["tick"]
    conn.execute(
        "UPDATE energy_state SET charge_kwh = MAX(0.0, charge_kwh - ?), updated_tick = ? "
        "WHERE item_id = ?;", (take, tick, item_id))
    return take


def status(conn: sqlite3.Connection, group_id: int,
           location_id: int | None) -> list[dict[str, Any]]:
    """Lesende Übersicht (Name, Rest, Kapazität) für Kontext/HUD."""
    names = dict(conn.execute("SELECT id, name FROM item_catalog;").fetchall())
    return [
        {"item_id": s["item_id"], "name": names.get(s["item_id"], s["item_id"]),
         "charge_kwh": round(s["charge_kwh"], 3), "capacity_kwh": s["capacity_kwh"],
         "carried": s["carried"]}
        for s in storages_at(conn, group_id, location_id)
    ]
