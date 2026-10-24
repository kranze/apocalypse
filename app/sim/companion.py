"""Der Companion (Slice M4, Issue #48; Profil-Anreicherung #16).

Genau ein Überlebender wird zum Begleiter des Spielers. Dieses Modul ist reiner
Sim-Kern: Zustandsmaschine, Vertrauensregeln, Validierung und Anwendung der
Intentionen. Das LLM (``llm/``) liefert nur freie Rede + *Intentionen*; jede
Intention läuft durch ``_validate`` -> ``_apply`` und ändert nur über Sim-Funktionen
(``looting.transfer_between_groups``, ``_add_trust`` ...) etwas. Vertrauen
(``trust``) ändert sich ausschließlich durch Sim-Regeln, nie durch eine Behauptung
des LLM.

Phasen-Einordnung: ``advance`` ist Phase 4 („Agenten-Ticks“) von
``tick.advance_tick`` — nach Welt, Ressourcen und Biologie, deterministisch und
OHNE LLM. Bewegung läuft über ``movement`` (``route_to`` + Tick-Bewegung).

Stages: contacted -> approaching -> met -> joined | left | dead.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ..llm import get_backend
from ..llm.base import COMPANION_INTENTS
from . import chatlog, constants, effects, looting, movement, provision, rng, survivors
from .events import DECISION, SOFT, emit_event

INTENTS = tuple(COMPANION_INTENTS)
PLAYER_ID = 1
STAGES = ("contacted", "approaching", "met", "joined", "left", "dead")
TOPICS = ("profession", "places")

# Treffpunkt-Vorlieben (kleiner = besser): Supermarkt, Kirche, Schule, Rest.
_PREF_LABELS = {"Kirche": 1, "Kapelle": 1, "Schule": 2}
_PLACE_RADIUS_M = 300.0     # „Orte, die er gesehen hat“ (share_info places)
_SEEN_WINDOW_MIN = 180      # Ereignisse in Sichtweite: so weit zurück
_MAX_INTENTS = 6


# --- kleine Helfer -----------------------------------------------------------
def _world(conn):
    return conn.execute(
        "SELECT tick, world_seed, start_datetime FROM world WHERE id = 1;").fetchone()


def _day_of(conn, tick: int) -> int:
    start = datetime.fromisoformat(_world(conn)["start_datetime"])
    return (start + timedelta(minutes=tick)).date().toordinal() - start.date().toordinal()


def _char(conn, character_id: int):
    return conn.execute("SELECT * FROM characters WHERE id = ?;", (character_id,)).fetchone()


def _dist(a, b) -> float:
    return effects._dist(a["lat"], a["lon"], b["lat"], b["lon"])


def _loc_name(loc) -> str:
    return loc["name"] or loc["label"] or loc["type"]


def _item_name(conn, item_id: str) -> str:
    r = conn.execute("SELECT name FROM item_catalog WHERE id = ?;", (item_id,)).fetchone()
    return r["name"] if r else item_id


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def get(conn) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM companion_state WHERE id = 1;").fetchone()
    return dict(r) if r else None


def traits(conn, character_id: int) -> dict[str, float]:
    """Persönlichkeitswerte 0..1, deterministisch aus Welt-Seed + Charakter."""
    seed = _world(conn)["world_seed"]
    return {k: round(rng.roll(seed, "trait", character_id, k), 4)
            for k in ("caution", "sociability", "optimism")}


def trait_text(tr: dict[str, float]) -> str:
    """Kurzer Persönlichkeits-Default aus den Werten (bis das Profil angereichert ist)."""
    parts = ["vorsichtig" if tr["caution"] >= 0.6 else "unbekümmert" if tr["caution"] <= 0.4 else "bedacht",
             "gesellig" if tr["sociability"] >= 0.6 else "eher für sich" if tr["sociability"] <= 0.4 else "umgänglich",
             "zuversichtlich" if tr["optimism"] >= 0.6 else "pessimistisch" if tr["optimism"] <= 0.4 else "nüchtern"]
    return ", ".join(parts).capitalize() + "."


def positive_scale(tr: dict[str, float]) -> float:
    """Gesellige Charaktere fassen schneller Vertrauen (Faktor auf positive Deltas)."""
    return 0.8 + 0.4 * tr["sociability"]


def leave_threshold(tr: dict[str, float]) -> float:
    """Optimisten halten länger durch: niedrigere Schwelle."""
    return constants.COMPANION_LEAVE_TRUST + 0.1 * (0.5 - tr["optimism"])


def compute_opinion(trust: float, char, tr: dict[str, float]) -> str:
    """Haltung zu „bleiben oder gehen“ — rein deterministisch aus Persönlichkeit,
    Bedürfnislage und Vertrauen. Das LLM formuliert sie nur."""
    need = min(char["hunger"], char["thirst"])
    go = 0.4 * (1.0 - need) + 0.3 * tr["sociability"] + 0.3 * trust
    stay = 0.5 * tr["caution"] + 0.3 * need + 0.2 * (1.0 - tr["sociability"])
    diff = go - stay
    return "go" if diff > 0.08 else "stay" if diff < -0.08 else "undecided"


def _add_trust(conn, delta: float, *, tick: int, reason: str | None = None,
               log: bool = True) -> float:
    """Einzige Schreibstelle für ``trust`` (0..1). Positive Deltas werden mit dem
    Geselligkeitsfaktor skaliert. Gibt den tatsächlichen Wert nach der Änderung."""
    row = get(conn)
    tr = traits(conn, row["character_id"])
    d = delta * positive_scale(tr) if delta > 0 else delta
    new = round(_clamp01(row["trust"] + d), 6)
    conn.execute("UPDATE companion_state SET trust = ? WHERE id = 1;", (new,))
    if log and reason and abs(d) > 1e-9:
        emit_event(conn, tick, "companion", f"Vertrauen {d:+.3f} ({reason}).",
                   subject_type="character", subject_id=row["character_id"],
                   payload={"trust": new, "delta": round(d, 6), "reason": reason})
    return new


def _say(conn, character_id: int, role: str, text: str) -> None:
    chatlog.append(conn, character_id, role, text)


# --- Darstellung (GET /companion) -------------------------------------------
def view(conn) -> dict[str, Any] | None:
    """Das Objekt des Companion-Vertrags oder None, solange es keinen Kontakt gibt."""
    row = get(conn)
    if row is None:
        return None
    c = _char(conn, row["character_id"])
    prof = survivors.get_profile(conn, row["character_id"])
    tr = traits(conn, row["character_id"])
    age = survivors._survivor_age(conn, c["birthdate"])
    loc = conn.execute(
        "SELECT id, name, label, type, lat, lon FROM locations WHERE id = ?;",
        (row["meeting_location_id"],)).fetchone() if row["meeting_location_id"] else None
    return {
        "character_id": row["character_id"], "stage": row["stage"],
        "name": c["name"], "age": age, "profession": c["profession"],
        "personality": (prof or {}).get("personality") or trait_text(tr),
        "short_bio": (prof or {}).get("short_bio") or "",
        "lat": c["lat"], "lon": c["lon"],
        "needs": {"hunger": c["hunger"], "thirst": c["thirst"], "sleep": c["sleep"]},
        "trust": row["trust"], "opinion": row["opinion"],
        "meeting_point": ({"location_id": loc["id"], "name": _loc_name(loc),
                           "lat": loc["lat"], "lon": loc["lon"]} if loc else None),
    }


def chat(conn, limit: int = 40) -> list[dict[str, Any]]:
    """Companion-Chat: [{role, text, tick}] chronologisch (player|companion|narrator)."""
    row = get(conn)
    if row is None:
        return []
    rows = conn.execute(
        "SELECT role, text, created_tick AS tick FROM chat_log WHERE character_id = ? "
        "ORDER BY turn DESC LIMIT ?;", (row["character_id"], max(1, int(limit)))).fetchall()
    return [dict(r) for r in reversed(rows)]


def here(conn, player_id: int = PLAYER_ID) -> dict[str, Any] | None:
    """Companion-Zustand, wenn er sprechbereit ist (met|joined, lebt, <= 30 m), sonst None."""
    row = get(conn)
    if row is None or row["stage"] not in ("met", "joined"):
        return None
    c, p = _char(conn, row["character_id"]), _char(conn, player_id)
    if c is None or not c["is_alive"] or p is None or p["lat"] is None or c["lat"] is None:
        return None
    return row if _dist(c, p) <= constants.COMPANION_REACH_M else None


# --- Kontakt -----------------------------------------------------------------
def _meeting_pref(loc) -> int:
    if loc["type"] == "supermarket":
        return 0
    return _PREF_LABELS.get(loc["label"] or "", 3)


def _pick_meeting(conn, player, comp):
    """Realer Ort „zwischen“ beiden: kleinster Umweg, bevorzugt Supermarkt/Kirche/
    Schule. Deterministisch (Tie-Break über die Orts-ID)."""
    d_pc = _dist(player, comp)
    best, fallback = None, None
    mid = {"lat": (player["lat"] + comp["lat"]) / 2, "lon": (player["lon"] + comp["lon"]) / 2}
    for r in conn.execute(
            "SELECT id, type, label, name, lat, lon FROM locations;").fetchall():
        dp = _dist(player, r)
        if dp > constants.COMPANION_MEET_RADIUS_M:
            continue
        dm = _dist(mid, r)
        if fallback is None or (dm, r["id"]) < fallback[0]:
            fallback = ((dm, r["id"]), r)
        detour = dp + _dist(comp, r) - d_pc
        if detour > max(300.0, 0.5 * d_pc):
            continue
        key = (_meeting_pref(r), round(detour), r["id"])
        if best is None or key < best[0]:
            best = (key, r)
    pick = best or fallback
    return pick[1] if pick else None


def _nearest_survivor_char(conn, lat, lon, radius_m):
    best = None
    for r in conn.execute(
            "SELECT id, lat, lon FROM characters WHERE type = 'survivor' AND is_alive = 1 "
            "AND lat IS NOT NULL;").fetchall():
        d = effects._dist(lat, lon, r["lat"], r["lon"])
        if d <= radius_m and (best is None or (d, r["id"]) < best[0]):
            best = ((d, r["id"]), r["id"])
    return best[1] if best else None


def _spawn_local_survivor(conn, lat, lon, seed) -> None:
    """Gibt es im Umkreis gar keinen Überlebenden, entsteht deterministisch einer
    (Survivor-Zeile, danach die übliche Materialisierung). Kein Ressourcen-Gut."""
    ang = 2 * math.pi * rng.roll(seed, "companion_spawn_angle", lat, lon)
    dist = 900.0 + 900.0 * rng.roll(seed, "companion_spawn_dist", lat, lon)
    s_lat = lat + (dist * math.cos(ang)) / 111_320.0
    s_lon = lon + (dist * math.sin(ang)) / (111_320.0 * math.cos(math.radians(lat)))
    sex = "m" if rng.roll(seed, "companion_spawn_sex", lat, lon) < 0.5 else "f"
    age = 20 + int(rng.roll(seed, "companion_spawn_age", lat, lon) * 41)
    conn.execute(
        "INSERT INTO survivors (lat, lon, sex, birth_tick, home_lat, home_lon) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        (s_lat, s_lon, sex, -(age * 525_600), s_lat, s_lon))
    eps = 1e-7
    survivors.materialize_in_bbox(conn, s_lat - eps, s_lon - eps, s_lat + eps, s_lon + eps)


def _pick_survivor(conn, anchor, seed, radius_m) -> int | None:
    """Nächster materialisierter Survivor-Charakter in Reichweite; sonst per
    ``materialize_in_bbox`` erzeugen; sonst (keine Überlebenden) einen anlegen."""
    lat, lon = anchor
    cid = _nearest_survivor_char(conn, lat, lon, radius_m)
    if cid is not None:
        return cid
    dlat = radius_m / 111_320.0
    dlon = dlat / max(0.1, math.cos(math.radians(lat)))
    survivors.materialize_in_bbox(conn, lat - dlat, lon - dlon, lat + dlat, lon + dlon)
    cid = _nearest_survivor_char(conn, lat, lon, radius_m)
    if cid is None:
        _spawn_local_survivor(conn, lat, lon, seed)
        cid = _nearest_survivor_char(conn, lat, lon, radius_m * 2)
    return cid


_SOURCE_TEXT = {
    "beacon": "Ein Funksignal antwortet auf deinen Beacon.",
    "note": "Jemand hat deine Notiz gefunden.",
}


def ensure_contact(conn, source: str, *, text: str | None = None,
                   near: tuple[float, float] | None = None) -> list[dict[str, Any]]:
    """Erstkontakt (reply | beacon | note): macht GENAU EINEN Überlebenden zum
    Companion, wählt den Treffpunkt und liefert den Vertrags-Interrupt. Gibt es
    schon einen Companion, passiert nichts. Ohne passenden Ort/Survivor bleibt der
    Kontakt offen ([]; der Agenten-Tick versucht es später erneut). Schreibt ohne
    eigenes Commit (Aufrufer/Tick hält die Transaktion)."""
    if get(conn) is not None:
        return []
    player = _char(conn, PLAYER_ID)
    if player is None or player["lat"] is None:
        return []
    w = _world(conn)
    anchor = near or (player["lat"], player["lon"])
    cid = _pick_survivor(conn, anchor, w["world_seed"], constants.COMPANION_CONTACT_RADIUS_M)
    if cid is None:
        return []
    comp = _char(conn, cid)
    loc = _pick_meeting(conn, player, comp)
    if loc is None:
        return []

    gid = conn.execute(
        "INSERT INTO groups (name, is_player_group, leader_id) VALUES ('Begleiter', 0, ?);",
        (cid,)).lastrowid
    conn.execute("UPDATE characters SET group_id = ? WHERE id = ?;", (gid, cid))
    tr = traits(conn, cid)
    trust0 = round(constants.COMPANION_TRUST_START + 0.1 * tr["sociability"], 6)
    conn.execute(
        "INSERT INTO companion_state (id, character_id, group_id, stage, mode, trust, opinion, "
        "meeting_location_id, contact_source, contact_tick, last_tick) "
        "VALUES (1, ?, ?, 'contacted', 'wait', ?, ?, ?, ?, ?, ?);",
        (cid, gid, trust0, compute_opinion(trust0, comp, tr), loc["id"], source,
         w["tick"], w["tick"]))
    conn.execute("UPDATE online_contacts SET handled = 1 "
                 "WHERE kind IN ('reply', 'beacon') AND handled = 0;")
    msg = (f"Jemand hat geantwortet: {text or _SOURCE_TEXT.get(source, 'Eine Nachricht kommt an.')} "
           f"Treffpunkt: {_loc_name(loc)}.")
    _say(conn, cid, "narrator", msg)
    return [emit_event(conn, w["tick"], "companion", msg, severity=DECISION,
                       subject_type="character", subject_id=cid,
                       payload={"source": source, "meeting_location_id": loc["id"]})]


def on_contact(conn, source: str, *, text: str | None = None) -> list[dict[str, Any]]:
    """Aktions-Haken (z.B. nach ``online.record_search``): ``ensure_contact`` in eigener
    Transaktion."""
    with conn:
        return ensure_contact(conn, source, text=text)


def _scan_contacts(conn, now_tick: int) -> list[dict[str, Any]]:
    """Ohne Companion: offene Antworten/Beacon-Kontakte und von Survivors gefundene
    Notizen in Kontakt umsetzen (deterministisch, Tick-Phase 4)."""
    pend = conn.execute(
        "SELECT id, kind, text FROM online_contacts WHERE kind IN ('reply', 'beacon') "
        "AND handled = 0 ORDER BY id LIMIT 1;").fetchone()
    if pend is not None:
        return ensure_contact(conn, pend["kind"], text=pend["text"])
    notes = conn.execute(
        "SELECT e.id, e.tick, l.lat, l.lon FROM events e JOIN locations l ON l.id = e.subject_id "
        "WHERE e.category = 'note' AND e.subject_type = 'location' AND e.tick <= ? "
        "AND NOT EXISTS (SELECT 1 FROM online_contacts oc WHERE oc.kind = 'note_seen' "
        "AND oc.text = CAST(e.id AS TEXT)) ORDER BY e.id;",
        (now_tick - constants.COMPANION_NOTE_DELAY_MIN,)).fetchall()
    r = constants.COMPANION_NOTE_RADIUS_M
    for n in notes:
        if survivors.count_near(conn, n["lat"], n["lon"], r) == 0:
            continue
        out = ensure_contact(conn, "note", near=(n["lat"], n["lon"]))
        if out:
            conn.execute("INSERT INTO online_contacts (tick, kind, text) VALUES (?, 'note_seen', ?);",
                         (now_tick, str(n["id"])))
            return out
    return []


# --- Agenten-Tick (Phase 4) ---------------------------------------------------
def _clear_path(conn, character_id: int) -> None:
    conn.execute("UPDATE characters SET dest_lat = NULL, dest_lon = NULL, path_json = NULL "
                 "WHERE id = ?;", (character_id,))


def _die(conn, row, now_tick: int) -> dict[str, Any]:
    c = _char(conn, row["character_id"])
    _clear_path(conn, row["character_id"])
    conn.execute("UPDATE companion_state SET stage = 'dead', mode = 'wait' WHERE id = 1;")
    msg = f"{c['name']} ist gestorben."
    _say(conn, row["character_id"], "narrator", msg)
    return emit_event(conn, now_tick, "companion", msg, severity=DECISION,
                      subject_type="character", subject_id=row["character_id"])


def _new_group(conn, character_id: int) -> int:
    return conn.execute(
        "INSERT INTO groups (name, is_player_group, leader_id) VALUES ('Begleiter', 0, ?);",
        (character_id,)).lastrowid


def _leave(conn, row, now_tick: int, why: str) -> dict[str, Any]:
    """Companion geht: Stage ``left``, eigene Gruppe (nimmt nichts mit)."""
    cid = row["character_id"]
    c = _char(conn, cid)
    gid = row["group_id"]
    if row["stage"] == "joined":
        gid = _new_group(conn, cid)
        conn.execute("UPDATE characters SET group_id = ? WHERE id = ?;", (gid, cid))
    _clear_path(conn, cid)
    conn.execute("UPDATE companion_state SET stage = 'left', mode = 'wait', group_id = ?, "
                 "left_tick = ? WHERE id = 1;", (gid, now_tick))
    msg = f"{c['name']} will gehen."
    _say(conn, cid, "narrator", f"{c['name']} geht. ({why})")
    return emit_event(conn, now_tick, "companion", msg, severity=DECISION,
                      subject_type="character", subject_id=cid, payload={"why": why})


def _join(conn, row, now_tick: int) -> None:
    """Anschluss: gleiche ``group_id`` wie der Spieler; sein bisheriger Besitz wandert
    (bilanzneutral) ins gemeinsame Inventar."""
    cid, old = row["character_id"], row["group_id"]
    player_gid = _char(conn, PLAYER_ID)["group_id"]
    if old is not None and old != player_gid:
        for r in conn.execute("SELECT DISTINCT item_id FROM group_inventory WHERE group_id = ?;",
                              (old,)).fetchall():
            looting.transfer_between_groups(conn, old, player_gid, r["item_id"])
    conn.execute("UPDATE characters SET group_id = ? WHERE id = ?;", (player_gid, cid))
    if old is not None and old != player_gid:
        conn.execute("DELETE FROM groups WHERE id = ?;", (old,))
    conn.execute("UPDATE companion_state SET stage = 'joined', mode = 'follow', group_id = ?, "
                 "joined_tick = ? WHERE id = 1;", (player_gid, now_tick))
    c = _char(conn, cid)
    _say(conn, cid, "narrator", f"{c['name']} schließt sich dir an. Ihr teilt ab jetzt euren Besitz.")
    emit_event(conn, now_tick, "companion", f"{c['name']} schließt sich dir an.",
               subject_type="character", subject_id=cid)


def _route(conn, cid: int, lat: float, lon: float) -> bool:
    return bool(movement.route_to(conn, cid, lat, lon).get("ok"))


def advance(conn, minutes: int, now_tick: int, seed: int) -> list[dict[str, Any]]:
    """Phase 4: ein deterministischer Agenten-Schritt (kein LLM). Läuft in der
    Tick-Transaktion; liefert Interrupts."""
    row = get(conn)
    if row is None:
        return _scan_contacts(conn, now_tick)
    if row["stage"] in ("left", "dead"):
        return []
    cid = row["character_id"]
    c = _char(conn, cid)
    if c is None or not c["is_alive"]:
        return [_die(conn, row, now_tick)]

    out: list[dict[str, Any]] = []
    tr = traits(conn, cid)
    dt = minutes / constants.MINUTES_PER_DAY
    stage = row["stage"]

    # Zeitbasierte Vertrauensregeln (leise, ohne Event je Tick).
    if stage in ("met", "joined"):
        if stage == "joined":
            _add_trust(conn, constants.COMPANION_TRUST_JOINED_PER_DAY * dt,
                       tick=now_tick, log=False)
        if min(c["hunger"], c["thirst"]) < constants.COMPANION_HUNGRY:
            _add_trust(conn, -constants.COMPANION_TRUST_HUNGER_PER_DAY * dt
                       * (1.3 - 0.6 * tr["optimism"]), tick=now_tick, log=False)
        if row["request_item"] and now_tick - (row["request_tick"] or now_tick) \
                >= constants.COMPANION_REQUEST_TIMEOUT_MIN:
            if min(c["hunger"], c["thirst"]) < constants.COMPANION_HUNGRY:
                _add_trust(conn, -constants.COMPANION_TRUST_IGNORED_REQUEST,
                           tick=now_tick, reason="Bitte blieb unerfüllt")
            conn.execute("UPDATE companion_state SET request_item = NULL, request_tick = NULL "
                         "WHERE id = 1;")
    row = get(conn)
    opinion = compute_opinion(row["trust"], c, tr)
    conn.execute("UPDATE companion_state SET opinion = ?, last_tick = ? WHERE id = 1;",
                 (opinion, now_tick))

    # Gehen: sehr niedriges Vertrauen UND Not.
    if stage in ("met", "joined") and row["trust"] < leave_threshold(tr) \
            and min(c["hunger"], c["thirst"]) < constants.COMPANION_HUNGRY:
        return [_leave(conn, row, now_tick, "kein Vertrauen, in Not")]

    player = _char(conn, PLAYER_ID)
    loc = conn.execute("SELECT id, name, label, type, lat, lon FROM locations WHERE id = ?;",
                       (row["meeting_location_id"],)).fetchone()
    if stage == "contacted":
        lo, hi = constants.COMPANION_CONTACT_DELAY_MIN
        delay = lo + int(rng.roll(seed, "contact_delay", cid) * (hi - lo + 1))
        if now_tick - row["contact_tick"] >= delay and loc is not None \
                and _route(conn, cid, loc["lat"], loc["lon"]):
            conn.execute("UPDATE companion_state SET stage = 'approaching' WHERE id = 1;")
    elif stage == "approaching" and loc is not None:
        c = _char(conn, cid)
        at_loc = _dist(c, loc) <= constants.COMPANION_REACH_M
        if not at_loc and c["path_json"] is None:
            _route(conn, cid, loc["lat"], loc["lon"])  # Route verloren: neu planen
        if at_loc and player is not None and player["lat"] is not None \
                and _dist(player, loc) <= constants.COMPANION_REACH_M:
            _clear_path(conn, cid)
            conn.execute("UPDATE companion_state SET stage = 'met', met_tick = ?, mode = 'wait' "
                         "WHERE id = 1;", (now_tick,))
            _add_trust(conn, constants.COMPANION_TRUST_MET, tick=now_tick, reason="Treffen")
            try:
                survivors.enrich_on_encounter(conn, cid)
            except Exception:
                pass  # Profil ist Dekor; ohne Anreicherung geht der Tag weiter
            msg = f"{c['name']} ist am Treffpunkt."
            _say(conn, cid, "narrator", msg)
            out.append(emit_event(conn, now_tick, "companion", msg, severity=SOFT,
                                  subject_type="character", subject_id=cid))
    elif stage in ("met", "joined") and row["mode"] == "follow":
        c = _char(conn, cid)
        if player is not None and player["lat"] is not None \
                and _dist(c, player) > constants.COMPANION_FOLLOW_DIST_M:
            stale = c["dest_lat"] is None or effects._dist(
                c["dest_lat"], c["dest_lon"], player["lat"], player["lon"]
            ) > constants.COMPANION_FOLLOW_DIST_M
            if stale:
                _route(conn, cid, player["lat"], player["lon"])
    return out


# --- Gespräch -------------------------------------------------------------------
def _known_places(conn, c) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT id, type, label, name, lat, lon FROM locations;").fetchall():
        d = effects._dist(c["lat"], c["lon"], r["lat"], r["lon"])
        if d <= _PLACE_RADIUS_M:
            out.append((d, r["id"], {"name": _loc_name(r), "type": r["type"],
                                     "dist_m": round(d)}))
    return [p for _, _, p in sorted(out, key=lambda x: (x[0], x[1]))[:5]]


def _inventory(conn, gid) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT gi.item_id, ic.name, ic.category, SUM(gi.quantity) AS qty FROM group_inventory gi "
        "JOIN item_catalog ic ON ic.id = gi.item_id WHERE gi.group_id = ? AND gi.quantity > 0 "
        "GROUP BY gi.item_id ORDER BY gi.item_id;", (gid,)).fetchall()]


def _persona(conn, row) -> dict[str, Any]:
    c = _char(conn, row["character_id"])
    prof = survivors.get_profile(conn, row["character_id"]) or {}
    tr = traits(conn, row["character_id"])
    return {
        "name": c["name"], "age": survivors._survivor_age(conn, c["birthdate"]),
        "profession": c["profession"],
        "personality": prof.get("personality") or trait_text(tr),
        "short_bio": prof.get("short_bio") or "", "demeanor": prof.get("demeanor") or "",
    }


def _context(conn, row, history) -> dict[str, Any]:
    """Read-only Kontext für das LLM: nur Eigenes, Gesehenes, Gesagtes."""
    cid = row["character_id"]
    c = _char(conn, cid)
    tick = _world(conn)["tick"]
    seen = [r["message"] for r in conn.execute(
        "SELECT message FROM events WHERE tick >= ? AND category IN ('need', 'companion', 'world') "
        "AND subject_type = 'character' AND subject_id IN (?, ?) AND severity != 'info' "
        "ORDER BY id DESC LIMIT 6;", (tick - _SEEN_WINDOW_MIN, cid, PLAYER_ID)).fetchall()]
    shared = row["stage"] == "joined"
    return {
        "stage": row["stage"], "trust": row["trust"], "opinion": row["opinion"],
        "join_trust": constants.COMPANION_JOIN_TRUST,
        "state": {"hunger": c["hunger"], "thirst": c["thirst"], "sleep": c["sleep"]},
        "shared_inventory": shared,
        "inventory": [] if shared else _inventory(conn, row["group_id"]),
        "seen": list(reversed(seen)), "known_places": _known_places(conn, c),
        "history": history,
    }


def _summarize_previous_days(conn, row, today: int, backend) -> None:
    """Einmal je Spieltag (beim ersten Gespräch des Tages): vergangene Tage mit
    Gesprächseinträgen, die noch keine Zusammenfassung haben (max. 3 jüngste), vom
    Backend zusammenfassen lassen. Der Kern klemmt und speichert nur Text."""
    if row["last_summary_day"] >= today:
        return
    cid = row["character_id"]
    by_day: dict[int, list[dict]] = {}
    for e in conn.execute(
            "SELECT role, text, created_tick FROM chat_log WHERE character_id = ? "
            "AND role IN ('player', 'companion') ORDER BY turn;", (cid,)).fetchall():
        d = _day_of(conn, e["created_tick"] or 0)
        if d < today:
            by_day.setdefault(d, []).append({"role": e["role"], "text": e["text"]})
    done = {r["day"] for r in conn.execute("SELECT day FROM companion_memory;").fetchall()}
    persona = _persona(conn, row)
    for d in sorted(set(by_day) - done)[-3:]:
        try:
            text = backend.summarize_day(persona, by_day[d]) or ""
        except Exception:
            text = ""
        text = " ".join(str(text).split())[:constants.COMPANION_SUMMARY_MAX].strip()
        if text:
            conn.execute("INSERT OR REPLACE INTO companion_memory (day, text) VALUES (?, ?);",
                         (d, text))
    conn.execute("UPDATE companion_state SET last_summary_day = ? WHERE id = 1;", (today,))


def _memory(conn) -> list[str]:
    return [r["text"] for r in conn.execute(
        "SELECT text FROM (SELECT day, text FROM companion_memory ORDER BY day DESC LIMIT 3) "
        "ORDER BY day;").fetchall()]


def _validate(conn, row, intent, staged, sim) -> tuple[bool, str | None, dict[str, Any]]:
    """Prüft EINE Intention hart gegen den Sim-Zustand (+ bereits akzeptierte, gestagte
    Intentionen). Liefert (ok, grund, normalisierte Felder). Schreibt nichts."""
    if not isinstance(intent, dict):
        return False, "bad_intent", {}
    name = intent.get("intent")
    if name not in INTENTS:
        return False, "unknown_intent", {"intent": name if isinstance(name, str) else None}
    cid, gid, stage = row["character_id"], row["group_id"], sim["stage"]
    norm: dict[str, Any] = {"intent": name}
    if stage not in ("met", "joined"):
        return False, "companion_not_here", norm

    def _item(required_owned: bool, qty_default: float | None = 1.0):
        item_id = intent.get("item_id")
        cat = conn.execute("SELECT id FROM item_catalog WHERE id = ?;", (item_id,)).fetchone() \
            if isinstance(item_id, str) else None
        if cat is None:
            return None, "unknown_item"
        norm["item_id"], norm["item_name"] = item_id, _item_name(conn, item_id)
        if qty_default is None:
            return item_id, None
        try:
            qty = float(intent.get("qty") if intent.get("qty") is not None else qty_default)
        except (TypeError, ValueError):
            return None, "bad_qty"
        if not (0 < qty <= 50):
            return None, "bad_qty"
        norm["qty"] = qty
        if required_owned and staged.owned(conn, gid, item_id) + 1e-9 < qty:
            return None, "not_owned"
        return item_id, None

    if name in ("follow",):
        if stage == "met" and row["trust"] < constants.COMPANION_FOLLOW_TRUST:
            return False, "trust_too_low", norm
        return True, None, norm
    if name == "wait_here" or name == "refuse":
        return True, None, norm
    if name in ("give_item", "ask_item", "share_food") and stage == "joined":
        return False, "shared_inventory", norm
    if name == "give_item":
        item_id, why = _item(True)
        if why:
            return False, why, norm
        w = conn.execute("SELECT weight_kg FROM item_catalog WHERE id = ?;", (item_id,)).fetchone()
        pgid = _char(conn, PLAYER_ID)["group_id"]
        free = looting.carry_capacity(conn, pgid) - movement.carried_weight(conn, pgid) \
            - sim["incoming_kg"]
        need = (w["weight_kg"] or 0.0) * norm["qty"]
        if need > free + 1e-9:
            return False, "too_heavy", norm
        staged.take(item_id, norm["qty"])
        sim["incoming_kg"] += need
        return True, None, norm
    if name == "ask_item":
        item_id, why = _item(False, None)
        return (False, why, norm) if why else (True, None, norm)
    if name == "share_food":
        best = provision._best_ready_food(conn, gid, None)
        if best is None or staged.owned(conn, gid, best[0]) < 1.0 - 1e-9:
            return False, "no_food", norm
        item_id = best[0]
        norm["item_id"], norm["item_name"], norm["qty"] = item_id, _item_name(conn, item_id), 1.0
        staged.take(item_id, 1.0)
        return True, None, norm
    if name == "agree_join":
        if stage != "met":
            return False, "already_joined", norm
        if row["trust"] < constants.COMPANION_JOIN_TRUST:
            return False, "trust_too_low", norm
        if row["opinion"] == "stay":
            return False, "prefers_to_stay", norm
        sim["stage"] = "joined"
        return True, None, norm
    if name == "leave":
        if row["opinion"] != "go" and row["trust"] >= constants.COMPANION_FOLLOW_TRUST:
            return False, "wants_to_stay", norm
        sim["stage"] = "left"
        return True, None, norm
    if name == "share_info":
        topic = intent.get("topic")
        if topic not in TOPICS:
            return False, "unknown_topic", norm
        norm["topic"] = topic
        c = _char(conn, cid)
        if topic == "places" and not _known_places(conn, c):
            return False, "nothing_known", norm
        if topic == "profession" and not c["profession"]:
            return False, "nothing_known", norm
        return True, None, norm
    return False, "unknown_intent", norm


def _apply(conn, row, norm, now_tick: int, events: list[dict]) -> None:
    """Wendet eine validierte Intention an — nur über Sim-Funktionen. Läuft in der
    Transaktion von ``converse`` (alles-oder-nichts)."""
    name, cid, gid = norm["intent"], row["character_id"], row["group_id"]
    c = _char(conn, cid)
    pgid = _char(conn, PLAYER_ID)["group_id"]
    if name == "follow":
        conn.execute("UPDATE companion_state SET mode = 'follow' WHERE id = 1;")
    elif name == "wait_here":
        conn.execute("UPDATE companion_state SET mode = 'wait' WHERE id = 1;")
        _clear_path(conn, cid)
    elif name in ("give_item", "share_food"):
        moved = looting.transfer_between_groups(conn, gid, pgid, norm["item_id"], norm["qty"])
        _say(conn, cid, "narrator",
             f"{c['name']} gibt dir {moved:g}x {norm['item_name']}.")
        _add_trust(conn, constants.COMPANION_TRUST_SHARED, tick=now_tick,
                   reason="teilt Besitz")
    elif name == "ask_item":
        conn.execute("UPDATE companion_state SET request_item = ?, request_tick = ? WHERE id = 1;",
                     (norm["item_id"], now_tick))
        _say(conn, cid, "narrator", f"{c['name']} bittet dich um {norm['item_name']}.")
    elif name == "agree_join":
        _join(conn, row, now_tick)
    elif name == "leave":
        events.append(_leave(conn, get(conn), now_tick, "auf eigenen Wunsch"))
    elif name == "share_info":
        if norm["topic"] == "profession":
            info = f"{c['name']} war früher: {c['profession']}."
        else:
            places = _known_places(conn, c)
            info = (f"{c['name']} kennt hier in der Nähe: "
                    + ", ".join(f"{p['name']} ({p['dist_m']} m)" for p in places) + ".")
        _say(conn, cid, "narrator", info)
        norm["info"] = info


def converse(conn, player_text: str, backend=None) -> dict[str, Any]:
    """Ein Gesprächszug. Voraussetzung: ``here(conn)`` ist wahr (der Aufrufer prüft).
    Ablauf: Tageszusammenfassung -> Chat -> Backend (Rede + Intentionen) -> jede
    Intention validieren -> akzeptierte atomar anwenden -> Rede speichern.
    Liefert {speech, intents_applied, intents_rejected, companion, events}."""
    backend = backend or get_backend()
    row = get(conn)
    cid = row["character_id"]
    tick = _world(conn)["tick"]
    today = _day_of(conn, tick)
    text = " ".join(str(player_text or "").split())[:500]

    with conn:
        _summarize_previous_days(conn, row, today, backend)
        row = get(conn)
        history = chatlog.recent(conn, cid, 12)
        _say(conn, cid, "player", text)
        # Sim-Regel: ein Gespräch festigt die Beziehung minimal (Tagescap).
        if row["talk_day"] != today:
            conn.execute("UPDATE companion_state SET talk_day = ?, talk_trust_today = 0.0 "
                         "WHERE id = 1;", (today,))
            row = get(conn)
        room = constants.COMPANION_TRUST_TALK_CAP - row["talk_trust_today"]
        gain = min(constants.COMPANION_TRUST_TALK, max(0.0, room))
        if gain > 1e-9:
            _add_trust(conn, gain, tick=tick, log=False)
            conn.execute("UPDATE companion_state SET talk_trust_today = talk_trust_today + ? "
                         "WHERE id = 1;", (gain,))
        row = get(conn)

    ctx = _context(conn, row, history)
    try:
        reply = backend.companion_reply(_persona(conn, row), _memory(conn), ctx, text) or {}
    except Exception:
        reply = {}
    speech = " ".join(str(reply.get("speech") or "").split())[:constants.COMPANION_SPEECH_MAX].strip()
    raw = reply.get("intents")
    raw = raw if isinstance(raw, list) else []

    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    events: list[dict] = []
    staged = effects.new_staged()
    sim = {"stage": row["stage"], "incoming_kg": 0.0}
    plan: list[dict[str, Any]] = []
    for it in raw[:_MAX_INTENTS]:
        ok, why, norm = _validate(conn, row, it, staged, sim)
        if ok:
            plan.append(norm)
        else:
            rejected.append({**{k: v for k, v in norm.items() if k != "info"}, "reason": why})
    for it in raw[_MAX_INTENTS:]:
        rejected.append({"intent": it.get("intent") if isinstance(it, dict) else None,
                         "reason": "too_many_intents"})

    try:
        with conn:  # alles-oder-nichts über alle akzeptierten Intentionen
            for norm in plan:
                _apply(conn, row, norm, tick, events)
                row = get(conn)
            if speech:
                _say(conn, cid, "companion", speech)
        applied = [{k: v for k, v in n.items() if k != "info"} for n in plan]
    except Exception:
        events = []
        rejected += [{**{k: v for k, v in n.items()}, "reason": "apply_error"} for n in plan]
        with conn:
            if speech:
                _say(conn, cid, "companion", speech)
    return {"speech": speech, "intents_applied": applied, "intents_rejected": rejected,
            "companion": view(conn), "events": events}


def give_to_companion(conn, item_id: str, qty: float = 1.0) -> dict[str, Any]:
    """Spieler gibt dem (nicht angeschlossenen) Companion etwas aus dem Gruppen-Inventar:
    bilanzneutraler Transfer + Vertrauensregel (mehr bei Bedürftigkeit)."""
    row = here(conn)
    if row is None:
        return {"ok": False, "reason": "companion_not_here"}
    if row["stage"] == "joined":
        return {"ok": False, "reason": "shared_inventory"}
    cat = conn.execute("SELECT id, name, category FROM item_catalog WHERE id = ?;",
                       (item_id,)).fetchone() if item_id else None
    if cat is None:
        return {"ok": False, "reason": "unknown_item"}
    pgid = _char(conn, PLAYER_ID)["group_id"]
    tick = _world(conn)["tick"]
    c = _char(conn, row["character_id"])
    with conn:
        moved = looting.transfer_between_groups(conn, pgid, row["group_id"], item_id, qty)
        if moved <= 0:
            return {"ok": False, "reason": "not_owned"}
        needy = (cat["category"] == "food" and c["hunger"] < 0.6) \
            or (cat["category"] == "water" and c["thirst"] < 0.6)
        _add_trust(conn, constants.COMPANION_TRUST_GIVE_HUNGRY if needy
                   else constants.COMPANION_TRUST_GIVE, tick=tick, reason="Gabe")
        if row["request_item"] == item_id:
            conn.execute("UPDATE companion_state SET request_item = NULL, request_tick = NULL "
                         "WHERE id = 1;")
        _say(conn, row["character_id"], "narrator",
             f"Du gibst {c['name']} {moved:g}x {cat['name']}.")
    return {"ok": True, "item_id": item_id, "item_name": cat["name"], "qty": moved}


# --- Neues Spiel --------------------------------------------------------------
def reset(conn) -> None:
    """Neues Spiel: Companion-Zustand, Profile, Gedächtnis, Chat und alle
    Survivor-Charaktere der alten Welt entfernen (der Spieler bleibt)."""
    with conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM characters WHERE type = 'survivor';").fetchall()]
        for cid in ids:
            conn.execute("DELETE FROM chat_log WHERE character_id = ?;", (cid,))
        conn.execute("DELETE FROM companion_state;")
        conn.execute("DELETE FROM companion_memory;")
        conn.execute("DELETE FROM survivor_profile;")
        conn.execute("DELETE FROM characters WHERE type = 'survivor';")
        conn.execute("DELETE FROM groups WHERE name = 'Begleiter' AND is_player_group = 0;")
