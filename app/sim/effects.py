"""Geschlossenes, validiertes Effekt-Vokabular des Adjudikators.

Der Spieler adressiert diese Ops nie direkt — der Adjudikator (bzw. das LLM)
komponiert sie aus einer offenen Absicht. Jeder Effekt hat einen Validator
(read-only Prüfung gegen die DB) und einen Applier (Ausführung über bestehende,
ledger-geprüfte Sim-Funktionen).

Adjudikator v2 (Tool-Use-Schleife): Vorschläge werden EINZELN und sofort gegen
einen **gestageten Plan** (``Staged``) validiert — eine reine Überlagerung im
Speicher (Bestandsänderungen früherer, schon akzeptierter Vorschläge), es wird
nichts geschrieben. Erst ``apply_atomic`` schreibt den fertigen Plan
(alles-oder-nichts, Snapshot-Rollback).

Grenzen des Stagings (bewusst einfach): Positionswechsel (``move_to``) wirken
nicht auf spätere Vorschläge derselben Adjudikation (Reichweiten werden gegen
die Position zu Beginn geprüft); Fundstücke einer noch nicht durchsuchten
Location sind vor dem Anwenden unbekannt (``search`` -> ``transfer`` im selben
Plan wird daher nur gegen die vorab bekannte Lage geprüft); Essens-Auswahl
(``consume_food`` ohne Item) ist eine Näherung des Appliers.

Eisernes Prinzip: Effekte sind Vorschläge; hier wird hart gegen DB/Ressourcen/
Vorbedingungen geprüft. Halluzinierte Ziele/Mengen werden abgelehnt.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from ..llm import get_backend

from . import (
    capabilities,
    constants,
    generation,
    kb,
    looting,
    movement,
    requirements,
    resources,
    tick,
)
from .events import HALTING, emit_event

OPS = (
    "move_to", "discover", "transfer", "consume_food", "prepare", "search",
    "advance_time", "transform", "establish_capability", "narrate",
    "fill_water", "barricade", "leave_note",
)

_FILL_MAX_L = 10.0       # so viel Wasser füllt man in einem Zug höchstens ab
_NOTE_MAX_CHARS = 280

_SEARCH_REACH_M = 40.0  # man durchsucht den Ort, an dem man steht

_M_PER_DEG_LAT = 111_320.0
_LOCATION_TYPES = {
    "house", "building", "supermarket", "fuel_station", "hardware",
    "pharmacy", "hospital",
}
_REACH_M = 400.0  # Nähe für discover/transfer


# --- Geo / Ziel-Auflösung ----------------------------------------------
def _dist(lat1, lon1, lat2, lon2) -> float:
    mlon = _M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * mlon, (lat2 - lat1) * _M_PER_DEG_LAT)


def resolve_location(conn, player, target, *, reach=None):
    """Löst eine Zielbeschreibung (Typ oder Name) deterministisch gegen die DB
    auf. None, wenn nichts (in Reichweite) passt — Anti-Halluzination."""
    if player is None or player["lat"] is None:
        return None
    lat, lon = player["lat"], player["lon"]
    loc_type, name = None, None
    if target:
        t = str(target).lower()
        if t in _LOCATION_TYPES:
            loc_type = t
        else:
            name = target
    q = "SELECT id, type, name, lat, lon, discovery_status FROM locations WHERE 1=1"
    params: list[Any] = []
    if loc_type:
        q += " AND type = ?"
        params.append(loc_type)
    if name:
        q += " AND lower(name) LIKE ?"
        params.append(f"%{str(name).lower()}%")
    best, best_d = None, float("inf")
    for r in conn.execute(q, params).fetchall():
        d = _dist(lat, lon, r["lat"], r["lon"])
        if d < best_d and (reach is None or d <= reach):
            best, best_d = r, d
    return best


# --- Inventar-Helfer ----------------------------------------------------
def _owned(conn, group_id, item_id) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(quantity),0.0) AS q FROM group_inventory "
        "WHERE group_id = ? AND item_id = ?;",
        (group_id, item_id),
    ).fetchone()["q"] or 0.0


def _produce(conn, group_id, item_id, qty, tick_now):
    conn.execute(
        "INSERT INTO group_inventory (group_id, item_id, quantity, quality, "
        "acquired_tick) VALUES (?, ?, ?, 1.0, ?) "
        "ON CONFLICT(group_id, item_id, quality) DO UPDATE SET "
        "quantity = quantity + excluded.quantity;",
        (group_id, item_id, qty, tick_now),
    )


def _group_of(conn, character_id) -> int | None:
    row = conn.execute(
        "SELECT group_id FROM characters WHERE id = ?;", (character_id,)
    ).fetchone()
    return row["group_id"] if row else None


def _fail(reason: str) -> dict:
    return {"ok": False, "reason": reason}


# --- Staging (Überlagerung, schreibt nichts) ----------------------------
class Staged:
    """Gestagter Plan: Bestandsänderungen bereits akzeptierter Vorschläge.

    ``delta`` = Gruppen-Inventar (+Zugang / -Verbrauch), ``loc_taken`` = was
    aus einem Location-Inventar schon vorgemerkt ist. Reine Speicher-Struktur;
    die DB bleibt unberührt, bis ``apply_atomic`` läuft."""

    def __init__(self) -> None:
        self.delta: dict[str, float] = {}
        self.loc_taken: dict[tuple[int, str], float] = {}

    def owned(self, conn, gid, item_id) -> float:
        return max(0.0, _owned(conn, gid, item_id) + self.delta.get(item_id, 0.0))

    def add(self, item_id: str, qty: float) -> None:
        self.delta[item_id] = self.delta.get(item_id, 0.0) + qty

    def take(self, item_id: str, qty: float) -> None:
        self.add(item_id, -qty)


def _satisfy(conn, gid, req, st: Staged, extra: dict[str, float] | None = None):
    """``requirements.satisfy`` gegen den gestageten Bestand (minus ``extra``)."""
    ex = extra or {}
    return requirements.satisfy(
        conn, gid, req, owned_fn=lambda i: st.owned(conn, gid, i) - ex.get(i, 0.0)
    )


def _catalog(conn, item_id):
    """Katalogzeile eines Items (None, wenn unbekannt)."""
    return conn.execute(
        "SELECT ic.id, ic.category, ic.kcal_per_unit, ic.needs_preparation, "
        "ic.requires_water_l, ic.prepared_into FROM world w "
        "JOIN item_catalog ic ON ic.id = ? WHERE w.id = 1;",
        (item_id,),
    ).fetchone()


def _candidate_items(conn, gid, st: Staged) -> list[str]:
    """Alle Items, die gerade (real oder gestaged) im Besitz sein könnten."""
    ids = {r["item_id"] for r in conn.execute(
        "SELECT DISTINCT item_id FROM group_inventory WHERE group_id = ?;", (gid,)
    ).fetchall()}
    ids |= {i for i, v in st.delta.items() if v > 0}
    return sorted(ids)


def _here(conn, ctx):
    """Nächste Location in Reichweite (egal welcher Status) oder None."""
    return resolve_location(conn, ctx["player"], None, reach=_REACH_M)


# --- Validatoren (read-only; bei Erfolg wird in ``st`` gestaged) --------
def _v_move(conn, char, eff, ctx, st):
    return (True, None) if resolve_location(conn, ctx["player"], eff.get("target")) \
        else (False, "no_target")


def _v_near(conn, char, eff, ctx, st):
    return (True, None) if resolve_location(conn, ctx["player"], eff.get("target"), reach=_REACH_M) \
        else (False, "no_target")


def _loc_gain(conn, loc_id, requested, st: Staged, only_category=None):
    """Was ein Transfer aus dem (durchsuchten) Location-Inventar bringen würde:
    {item: menge}, abzüglich schon Vorgemerktem. Unbekannt (nicht durchsucht) -> {}."""
    if not generation.is_searched(conn, loc_id):
        return {}
    sql = ("SELECT li.item_id, SUM(li.quantity) AS q, ic.category FROM location_inventory li "
           "JOIN item_catalog ic ON ic.id = li.item_id WHERE li.location_id = ? "
           "AND li.quantity > 0 GROUP BY li.item_id ORDER BY li.item_id;")
    gain: dict[str, float] = {}
    for r in conn.execute(sql, (loc_id,)).fetchall():
        if only_category and r["category"] != only_category:
            continue
        avail = r["q"] - st.loc_taken.get((loc_id, r["item_id"]), 0.0)
        want = avail if requested is None else float(requested.get(r["item_id"], 0.0) or 0.0)
        take = min(avail, max(0.0, want))
        if take > 1e-9:
            gain[r["item_id"]] = take
    return gain


def _v_transfer(conn, char, eff, ctx, st):
    loc = resolve_location(conn, ctx["player"], eff.get("target"), reach=_REACH_M)
    if not loc:
        return (False, "no_target")
    req = eff.get("items") if isinstance(eff.get("items"), dict) else None
    if generation.is_searched(conn, loc["id"]):
        gain = _loc_gain(conn, loc["id"], req, st)
        if not gain:
            return (False, "nothing_to_take")
        for item, q in gain.items():
            st.add(item, q)
            st.loc_taken[(loc["id"], item)] = st.loc_taken.get((loc["id"], item), 0.0) + q
    return (True, None)


def _v_consume_food(conn, char, eff, ctx, st):
    gid = _group_of(conn, char)
    only = eff.get("item")
    best, best_score = None, -1.0
    for item in _candidate_items(conn, gid, st):
        if only and item != only:
            continue
        cat = _catalog(conn, item)
        if cat is None or cat["category"] != "food" or cat["needs_preparation"]:
            continue
        q = st.owned(conn, gid, item)
        if q <= 1e-9:
            continue
        qual = conn.execute(
            "SELECT COALESCE(MAX(quality), 1.0) AS ql FROM group_inventory "
            "WHERE group_id = ? AND item_id = ?;", (gid, item)
        ).fetchone()["ql"]
        score = (cat["kcal_per_unit"] or 0.0) * qual
        if score > best_score:
            best, best_score = (item, q), score
    if best is None:
        return (False, "no_food")
    st.take(best[0], min(1.0, best[1]))
    return (True, None)


def _v_prepare(conn, char, eff, ctx, st):
    gid = _group_of(conn, char)
    only = eff.get("item")
    raw = None
    for item in _candidate_items(conn, gid, st):
        if only and item != only:
            continue
        cat = _catalog(conn, item)
        if (cat is None or not cat["needs_preparation"] or not cat["prepared_into"]
                or st.owned(conn, gid, item) < 1.0):
            continue
        raw = cat
        break
    if raw is None:
        return (False, "nothing_to_prepare")
    need_water = raw["requires_water_l"] or 0.0
    if st.owned(conn, gid, "water_1l") < need_water:
        return (False, "no_water")
    src = _satisfy(conn, gid, "heat", st)
    if src is None:
        return (False, "no_heat")
    st.take(raw["id"], 1.0)
    if need_water > 0:
        st.take("water_1l", need_water)
    if src["consume"] > 0:
        st.take(src["item"], src["consume"])
    st.add(raw["prepared_into"], 1.0)
    return (True, None)


def _v_advance_time(conn, char, eff, ctx, st):
    return (True, None)


def _v_transform(conn, char, eff, ctx, st):
    gid = _group_of(conn, char)
    need: dict[str, float] = {}
    for c in eff.get("consume", []):
        need[c["item"]] = need.get(c["item"], 0.0) + float(c.get("qty", 1))
    for item, q in need.items():
        if st.owned(conn, gid, item) < q:
            return (False, f"missing:{item}")
    used = dict(need)
    for req in eff.get("requires", []):
        src = _satisfy(conn, gid, req, st, used)
        if src is None:
            return (False, f"missing:{req}")
        if src["consume"] > 0:
            used[src["item"]] = used.get(src["item"], 0.0) + src["consume"]
    produce = eff.get("produce") or []
    if not produce:
        return (False, "nothing_produced")
    # Kein Items-Erfinden: nur Katalog-Items, und nicht mehr Masse heraus als hinein.
    out_total = 0.0
    for p in produce:
        if _catalog(conn, p["item"]) is None:
            return (False, f"unknown_item:{p['item']}")
        out_total += float(p.get("qty", 1))
    if out_total > sum(used.values()) + 1e-9:
        return (False, "produce_exceeds_input")
    for item, q in used.items():
        st.take(item, q)
    for p in produce:
        st.add(p["item"], float(p.get("qty", 1)))
    return (True, None)


def _v_establish(conn, char, eff, ctx, st):
    gid = _group_of(conn, char)
    ctype = eff.get("ctype")
    recipe = kb.lookup(conn, f"capability_recipe:{ctype}", ctype)
    if recipe is None or not isinstance(recipe["value"], dict):
        return (False, "no_recipe")  # z.B. "Mobilfunknetz" -> kein Rezept
    used: dict[str, float] = {}
    for req in recipe["value"].get("requires", []):
        src = _satisfy(conn, gid, req, st, used)
        if src is None:
            return (False, f"missing:{req}")
        if src["consume"] > 0:
            used[src["item"]] = used.get(src["item"], 0.0) + src["consume"]
    for item, q in used.items():
        st.take(item, q)
    return (True, None)


def _v_barricade(conn, char, eff, ctx, st):
    if _here(conn, ctx) is None:
        return (False, "no_target")
    return _v_establish(conn, char, {"ctype": "barricade"}, ctx, st)


def _v_narrate(conn, char, eff, ctx, st):
    return (True, None)


def _v_leave_note(conn, char, eff, ctx, st):
    text = str(eff.get("text") or "").strip()
    if not text:
        return (False, "no_text")
    if len(text) > _NOTE_MAX_CHARS:
        return (False, "text_too_long")
    if _here(conn, ctx) is None:
        return (False, "no_target")
    return (True, None)


def _v_fill_water(conn, char, eff, ctx, st):
    """Wasser abfüllen — nur gegen Wasser-Items im Inventar einer bereits
    durchsuchten Location (ein Wasserquellen-Konzept gibt es noch nicht)."""
    loc = _discovered_here(conn, ctx["player"])
    if loc is None:
        return (False, "no_location_here")
    if not generation.is_searched(conn, loc["id"]):
        return (False, "not_searched")
    try:
        liters = float(eff.get("liters") or _FILL_MAX_L)
    except (TypeError, ValueError):
        return (False, "bad_amount")
    liters = min(max(liters, 0.0), _FILL_MAX_L)
    if liters <= 0:
        return (False, "bad_amount")
    gain = _loc_gain(conn, loc["id"], None, st, only_category="water")
    if not gain:
        return (False, "no_water_source")
    left = liters
    for item, q in gain.items():
        take = min(q, left)
        if take <= 1e-9:
            continue
        st.add(item, take)
        st.loc_taken[(loc["id"], item)] = st.loc_taken.get((loc["id"], item), 0.0) + take
        left -= take
    return (True, None)


# --- Applier ------------------------------------------------------------
def _a_move(conn, char, eff, ctx):
    loc = resolve_location(conn, ctx["player"], eff.get("target"))
    r = movement.set_destination(conn, char, loc["lat"], loc["lon"])
    r["target_name"] = loc["name"] or loc["type"]
    return r


def _a_discover(conn, char, eff, ctx):
    loc = resolve_location(conn, ctx["player"], eff.get("target"), reach=_REACH_M)
    r = generation.discover(conn, loc["id"])
    r["target_name"] = loc["name"] or loc["type"]
    return r


def _a_transfer(conn, char, eff, ctx):
    loc = resolve_location(conn, ctx["player"], eff.get("target"), reach=_REACH_M)
    gid = _group_of(conn, char)
    r = looting.loot(conn, loc["id"], gid, eff.get("items"))
    r["target_name"] = loc["name"] or loc["type"]
    return r


def _a_consume_food(conn, char, eff, ctx):
    return resources.eat(conn, char, eff.get("item"))


def _a_prepare(conn, char, eff, ctx):
    return resources.prepare(conn, char, eff.get("item"))


def _a_advance_time(conn, char, eff, ctx):
    """Zeit verstreichen lassen: geklemmt auf FREETEXT_MAX_MINUTES, in
    TICK_MINUTES-Schritten (Rest als letzter Teilschritt). Bricht bei Tod oder
    einem haltenden Interrupt ab. Ohne Minutenangabe: ein einzelner Tick."""
    try:
        requested = int(eff.get("minutes", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    if requested <= 0:
        r = tick.advance_tick(conn)
        return {"ok": True, "tick": r["tick"], "minutes": constants.TICK_MINUTES,
                "interrupts": r["interrupts"]}
    minutes = min(requested, constants.FREETEXT_MAX_MINUTES)
    spent, interrupts, now = 0, [], None
    while spent < minutes:
        step = min(constants.TICK_MINUTES, minutes - spent)
        r = tick.advance_tick(conn, step)
        spent += step
        now = r["tick"]
        interrupts += r["interrupts"]
        alive = conn.execute(
            "SELECT is_alive FROM characters WHERE id = ?;", (char,)
        ).fetchone()
        if alive is None or not alive["is_alive"]:
            break
        if any(i["severity"] in HALTING for i in r["interrupts"]):
            break
    return {"ok": True, "tick": now, "minutes": spent, "clamped": minutes != requested,
            "interrupts": interrupts}


def _a_transform(conn, char, eff, ctx):
    gid = _group_of(conn, char)
    now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
    with conn:
        for c in eff.get("consume", []):
            resources._consume_from_group(conn, gid, c["item"], float(c.get("qty", 1)))
            ledger_add(conn, c["item"], -float(c.get("qty", 1)))
        for req in eff.get("requires", []):
            src = requirements.satisfy(conn, gid, req)
            if src and src["consume"] > 0:
                resources._consume_from_group(conn, gid, src["item"], src["consume"])
                ledger_add(conn, src["item"], -src["consume"])
        for p in eff.get("produce", []):
            _produce(conn, gid, p["item"], float(p.get("qty", 1)), now)
            ledger_add(conn, p["item"], float(p.get("qty", 1)))
    return {"ok": True, "produced": eff.get("produce")}


def _a_establish(conn, char, eff, ctx):
    gid = _group_of(conn, char)
    ctype = eff.get("ctype")
    recipe = kb.lookup(conn, f"capability_recipe:{ctype}", ctype)["value"]
    loc = resolve_location(conn, ctx["player"], eff.get("target"), reach=_REACH_M)
    now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
    params = dict(eff.get("params") or {})
    if "range_km" in recipe:
        params.setdefault("range_km", recipe["range_km"])
    with conn:
        for req in recipe.get("requires", []):
            src = requirements.satisfy(conn, gid, req)
            if src and src["consume"] > 0:
                resources._consume_from_group(conn, gid, src["item"], src["consume"])
                ledger_add(conn, src["item"], -src["consume"])
        cap_id = capabilities.create(
            conn, ctype, gid,
            location_id=(loc["id"] if loc else None),
            params=params, upkeep=recipe.get("upkeep"), tick=now,
        )
    return {"ok": True, "capability_id": cap_id, "ctype": ctype, "params": params}


def _a_narrate(conn, char, eff, ctx):
    return {"ok": True}


def _a_barricade(conn, char, eff, ctx):
    """Ort verbarrikadieren = Capability ``barricade`` über das KB-Rezept
    (``capability_recipe:barricade``), am nächsten Ort in Reichweite."""
    return _a_establish(conn, char, {"ctype": "barricade", "params": eff.get("params") or {},
                                     "target": eff.get("target")}, ctx)


def _a_leave_note(conn, char, eff, ctx):
    """Nachricht hinterlassen: ein Event am Ort (Kategorie ``note``). Keine
    Ressourcen betroffen; geschrieben über die Sim-Funktion ``emit_event``."""
    loc = _here(conn, ctx)
    text = str(eff.get("text") or "").strip()
    now = conn.execute("SELECT tick FROM world WHERE id=1;").fetchone()["tick"]
    with conn:
        emit_event(
            conn, now, "note", text, subject_type="location",
            subject_id=loc["id"], payload={"character_id": char, "text": text},
        )
    return {"ok": True, "location_id": loc["id"],
            "narration": "Du hinterlässt eine Nachricht."}


def _a_fill_water(conn, char, eff, ctx):
    """Wasser abfüllen = Transfer der Wasser-Items der Location in die Gruppe
    (bilanzneutral, über ``looting.loot``)."""
    loc = _discovered_here(conn, ctx["player"])
    gid = _group_of(conn, char)
    try:
        left = min(max(float(eff.get("liters") or _FILL_MAX_L), 0.0), _FILL_MAX_L)
    except (TypeError, ValueError):
        left = _FILL_MAX_L
    wanted: dict[str, float] = {}
    rows = conn.execute(
        "SELECT li.item_id, SUM(li.quantity) AS q FROM location_inventory li "
        "JOIN item_catalog ic ON ic.id = li.item_id WHERE li.location_id = ? "
        "AND ic.category = 'water' AND li.quantity > 0 "
        "GROUP BY li.item_id ORDER BY li.item_id;",
        (loc["id"],),
    ).fetchall()
    for r in rows:
        take = min(r["q"], left)
        if take > 1e-9:
            wanted[r["item_id"]] = take
            left -= take
    if not wanted:
        return {"ok": False, "reason": "no_water_source"}
    r = looting.loot(conn, loc["id"], gid, wanted)
    r["filled_l"] = sum(r.get("transferred", {}).values())
    r["narration"] = f"Du füllst {r['filled_l']:g} L Wasser ab."
    return r


def ledger_add(conn, item_id, delta):
    from . import ledger
    ledger.add(conn, item_id, delta)


# --- Suche: Abgleich gegen das generierte Inventar ------------------------
def _discovered_here(conn, player):
    """Nächste ENTDECKTE Location, an der der Spieler steht (≤ Reichweite)."""
    if not player or player.get("lat") is None:
        return None
    lat, lon = player["lat"], player["lon"]
    best, best_d = None, _SEARCH_REACH_M
    for r in conn.execute(
        "SELECT id, type, label, name, lat, lon FROM locations "
        "WHERE discovery_status != 'undiscovered';"
    ).fetchall():
        d = _dist(lat, lon, r["lat"], r["lon"])
        if d <= best_d:
            best, best_d = r, d
    return best


def _v_search(conn, char, eff, ctx, st):
    if not eff.get("query"):
        return (False, "no_query")
    if _discovered_here(conn, ctx["player"]) is None:
        return (False, "no_location_here")
    return (True, None)


def _a_search(conn, char, eff, ctx):
    """Freitext-Suche: löst dieselbe deterministische Generierung aus wie die
    Klickaktion „Durchsuchen" und gleicht die Anfrage NUR gegen das vorhandene
    Inventar des Ortes ab. Das LLM wählt aus der Kandidatenliste und erzählt;
    es kann weder Items noch Mengen anlegen (Eisernes Prinzip)."""
    loc = _discovered_here(conn, ctx["player"])
    query = str(eff.get("query", "")).strip()
    generation.ensure_generated(conn, loc["id"])
    cands = [
        dict(r) for r in conn.execute(
            "SELECT li.item_id, ic.name, ic.category, SUM(li.quantity) AS quantity "
            "FROM location_inventory li JOIN item_catalog ic ON ic.id = li.item_id "
            "WHERE li.location_id = ? AND li.quantity > 0 "
            "GROUP BY li.item_id ORDER BY li.item_id;",
            (loc["id"],),
        ).fetchall()
    ]
    where = loc["name"] or loc["label"] or loc["type"]
    if not cands:
        return {"ok": True, "found": False, "items": [], "location_id": loc["id"],
                "narration": f"In {where} ist nichts Brauchbares (mehr) zu finden."}

    res = get_backend().match_items(
        query, {"type": loc["type"], "label": loc["label"], "name": loc["name"]},
        cands, ctx.get("profile"),
    )
    by_id = {c["item_id"]: c for c in cands}
    ids: list[str] = []
    for i in res.get("item_ids") or []:  # nur Kandidaten zählen; Rest wird verworfen
        if isinstance(i, str) and i in by_id and i not in ids:
            ids.append(i)
    narration = (res.get("narration") or "").strip()
    if not ids:
        return {"ok": True, "found": False, "items": [], "location_id": loc["id"],
                "narration": narration or "Danach suchst du hier vergeblich."}
    items = [{"item": i, "name": by_id[i]["name"], "quantity": by_id[i]["quantity"]}
             for i in ids]
    listing = ", ".join(f"{it['quantity']:g}x {it['name']}" for it in items)
    return {"ok": True, "found": True, "items": items, "location_id": loc["id"],
            "narration": (narration + " " if narration else "") + f"Vor Ort liegt: {listing}."}


_VALIDATORS = {
    "move_to": _v_move, "discover": _v_near, "transfer": _v_transfer,
    "consume_food": _v_consume_food, "prepare": _v_prepare, "search": _v_search,
    "advance_time": _v_advance_time, "transform": _v_transform,
    "establish_capability": _v_establish, "narrate": _v_narrate,
    "fill_water": _v_fill_water, "barricade": _v_barricade, "leave_note": _v_leave_note,
}
_APPLIERS = {
    "move_to": _a_move, "discover": _a_discover, "transfer": _a_transfer,
    "consume_food": _a_consume_food, "prepare": _a_prepare, "search": _a_search,
    "advance_time": _a_advance_time, "transform": _a_transform,
    "establish_capability": _a_establish, "narrate": _a_narrate,
    "fill_water": _a_fill_water, "barricade": _a_barricade, "leave_note": _a_leave_note,
}


# --- öffentliche Schnittstelle -----------------------------------------
def new_staged() -> Staged:
    return Staged()


def validate_one(conn, character_id, eff, ctx, staged: Staged) -> tuple[bool, str | None]:
    """Validiert EINEN Vorschlag gegen DB + gestagten Plan. Bei Erfolg wird der
    Bestandseffekt in ``staged`` vorgemerkt (sonst bleibt ``staged`` unverändert).
    Schreibt nie in die DB."""
    op = eff.get("op") if isinstance(eff, dict) else None
    if op not in _VALIDATORS:
        return (False, f"unknown_op:{op}")
    try:
        return _VALIDATORS[op](conn, character_id, eff, ctx, staged)
    except (KeyError, TypeError, ValueError):
        return (False, f"bad_params:{op}")


def validate_all(conn, character_id, effects, ctx) -> tuple[bool, str | None]:
    """Prüft alle Effekte nacheinander (read-only) gegen einen gemeinsamen
    gestagten Plan (spätere Effekte sehen die Verbräuche früherer)."""
    st = Staged()
    for eff in effects:
        ok, reason = validate_one(conn, character_id, eff, ctx, st)
        if not ok:
            return (False, reason)
    return (True, None)


def apply_all(conn, character_id, effects, ctx) -> list[dict]:
    """Wendet alle Effekte der Reihe nach an (nach erfolgreicher Validierung).
    Nicht atomar — für den Adjudikator gilt ``apply_atomic``."""
    results = []
    for eff in effects:
        res = _APPLIERS[eff["op"]](conn, character_id, eff, ctx)
        results.append({"op": eff["op"], "result": res})
    return results


def apply_atomic(conn, character_id, effects, ctx) -> tuple[bool, list[dict], str | None]:
    """Wendet einen validierten Plan alles-oder-nichts an.

    Die Applier committen einzeln (``with conn``); echte Atomarität über mehrere
    Effekte entsteht daher per **Snapshot**: vor einem Mehr-Effekt-Plan wird die
    DB in einen In-Memory-Snapshot kopiert und bei Ausnahme oder weichem
    Fehlschlag (``ok: False``) eines Appliers vollständig zurückgespielt.
    Einzel-Effekt-Pläne brauchen keinen Snapshot (der Applier ist selbst atomar);
    ihr weicher Fehlschlag wird unverändert gemeldet.

    Liefert (ok, applied, reason). Bei ``ok=False`` mit Rollback ist ``applied``
    leer. Kosten: ein Memory-Backup der DB je Mehr-Effekt-Plan (bewusst
    akzeptiert: Freitext-Aktionen sind selten und teuer genug)."""
    if len(effects) <= 1:
        results = []
        try:
            results = apply_all(conn, character_id, effects, ctx)
        except Exception as exc:  # Applier-Fehler: Applier rollen selbst zurück
            return (False, [], f"apply_error:{type(exc).__name__}")
        return (True, results, None)

    conn.commit()
    snap = sqlite3.connect(":memory:")
    try:
        conn.backup(snap)
        results = []
        reason = None
        try:
            for eff in effects:
                res = _APPLIERS[eff["op"]](conn, character_id, eff, ctx)
                results.append({"op": eff["op"], "result": res})
                if isinstance(res, dict) and res.get("ok") is False:
                    reason = res.get("reason") or "apply_failed"
                    break
        except Exception as exc:
            reason = f"apply_error:{type(exc).__name__}"
        if reason is not None:
            conn.rollback()
            snap.backup(conn)  # vollständig zurückspielen
            return (False, [], reason)
        return (True, results, None)
    finally:
        snap.close()
