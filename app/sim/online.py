"""Online-Phase (Slice M2 Teil B, Issue #46): Nachrichten, Überlebenden-Suche,
Wissens-Downloads.

Kuratierte Daten liegen in ``app/data/news_fragments.json`` und
``app/data/knowledge_packages.json``. Dieses Modul enthält nur die
deterministische Sim-Logik (Auswahl, Fortschritt, KB-Schreiben); Zeitkosten und
Akku-Abzug erledigen die Aktionen in ``actions.py`` über ``devices``.

Eisernes Prinzip: Wissen entsteht ausschließlich durch einen vollständigen,
validierten Download und wird per ``kb.add(..., provenance="curated")`` in die KB
geschrieben. Das LLM ist nirgends beteiligt.

Haken für #48 (Companion): ``pending_contacts`` liefert offene Antworten auf
``search_survivors`` (``online_contacts.kind = 'reply'``, ``handled = 0``);
wer sie verbraucht, ruft ``mark_contact_handled`` auf.
"""
from __future__ import annotations

import json
import math
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import constants, decay_world, devices, kb, rng

_DATA = Path(__file__).resolve().parent.parent / "data"


@lru_cache(maxsize=1)
def news_fragments() -> tuple[dict, ...]:
    raw = json.loads((_DATA / "news_fragments.json").read_text(encoding="utf-8"))
    return tuple(raw["fragments"])


@lru_cache(maxsize=1)
def packages() -> tuple[dict, ...]:
    raw = json.loads((_DATA / "knowledge_packages.json").read_text(encoding="utf-8"))
    return tuple(raw["packages"])


def package(package_id: str) -> dict | None:
    return next((p for p in packages() if p["id"] == package_id), None)


def _world(conn):
    return conn.execute("SELECT tick, world_seed FROM world WHERE id = 1;").fetchone()


# --- Nachrichten ---------------------------------------------------------
def pick_news(conn, day: int) -> dict | None:
    """Deterministisch (Seed, Tag, Anzahl bereits gelesener) ein ungelesenes
    Fragment, das am Tag ``day`` schon veröffentlicht ist. Aktuelle Fragmente
    (day_max >= day) haben Vorrang vor Archiv. None, wenn nichts Neues da ist."""
    read = {r["fragment_id"] for r in conn.execute("SELECT fragment_id FROM news_read;")}
    avail = [f for f in news_fragments() if f["id"] not in read and f["day_min"] <= day]
    if not avail:
        return None
    current = [f for f in avail if f["day_max"] >= day] or avail
    seed = _world(conn)["world_seed"]
    idx = int(rng.roll(seed, "news", day, len(read)) * len(current)) % len(current)
    return current[idx]


def news_minutes(conn, day: int) -> int:
    lo, hi = constants.READ_NEWS_MINUTES
    seed = _world(conn)["world_seed"]
    n = conn.execute("SELECT COUNT(*) AS n FROM news_read;").fetchone()["n"]
    return lo + int(rng.roll(seed, "news_min", day, n) * (hi - lo + 1)) % (hi - lo + 1)


def mark_news_read(conn, fragment_id: str, tick: int) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO news_read (fragment_id, read_tick) VALUES (?, ?);",
            (fragment_id, tick))


# --- Überlebenden-Suche ---------------------------------------------------
# Genau EINE Person antwortet (es kommt genau ein Companion) — also Ich-Form/Singular.
# (neutraler Text, Variante mit Bezug auf den Beruf der antwortenden Person); die
# Berufsvariante setzt der Erstkontakt ein (``personalize_reply``), sobald feststeht,
# wer geantwortet hat. ``{beruf}`` = deutsche Berufsbezeichnung.
_REPLY_TEMPLATES = (
    ("Eine kurze Nachricht: „Ich bin allein und suche Wasser. Seid ihr noch da?“",
     "Eine kurze Nachricht: „Ich war {beruf} und suche jetzt Wasser. Seid ihr noch da?“"),
    ("Eine Sprachnachricht, stark verrauscht: Jemand nennt einen Treffpunkt am Stadtrand.",
     "Eine Sprachnachricht, stark verrauscht: Eine Person, früher {beruf}, nennt einen "
     "Treffpunkt am Stadtrand."),
    ("Eine Nachricht ohne Absender: „Ich lebe. Ich bleibe, wo ich bin. Meldet euch.“",
     "Eine Nachricht ohne Absender: „Ich lebe, früher {beruf}. Ich bleibe, wo ich bin. "
     "Meldet euch.“"),
)
_REPLIES = tuple(t[0] for t in _REPLY_TEMPLATES)


def personalize_reply(text: str | None, profession_de: str | None) -> str | None:
    """Gibt zu einem gespeicherten Antworttext die Variante mit Bezug auf den Beruf der
    antwortenden Person zurück (nur wenn der Text eine bekannte Vorlage ist und ein
    Beruf bekannt ist; sonst unverändert). Rein, ohne DB."""
    if not text or not profession_de:
        return text
    for plain, personal in _REPLY_TEMPLATES:
        if text == plain:
            return personal.format(beruf=profession_de)
    return text


def search_tries(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM online_contacts WHERE kind = 'attempt';").fetchone()["n"]


def contact_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM online_contacts WHERE kind = 'reply';").fetchone()["n"]


def search_chance(tries: int, quality: float) -> float:
    base = min(constants.SEARCH_SURVIVORS_CAP,
               constants.SEARCH_SURVIVORS_BASE + constants.SEARCH_SURVIVORS_STEP * tries)
    return round(base * (0.5 + 0.5 * quality), 4)


def record_search(conn, quality: float, tick: int) -> dict[str, Any]:
    """Protokolliert einen Suchversuch und würfelt deterministisch (Seed, Versuchs-
    nummer) über eine mit den Versuchen steigende, qualitätsabhängige Chance. Bei
    Erfolg wird zusätzlich eine Antwort gespeichert. Gibt {tries, chance, reply}
    zurück (reply = Text oder None)."""
    seed = _world(conn)["world_seed"]
    tries = search_tries(conn)
    chance = search_chance(tries, quality)
    hit = rng.roll(seed, "search_survivors", tries) < chance
    reply = None
    with conn:
        conn.execute(
            "INSERT INTO online_contacts (tick, kind, text) VALUES (?, 'attempt', NULL);",
            (tick,))
        if hit:
            reply = _REPLIES[int(rng.roll(seed, "search_reply", tries) * len(_REPLIES))
                             % len(_REPLIES)]
            conn.execute(
                "INSERT INTO online_contacts (tick, kind, text) VALUES (?, 'reply', ?);",
                (tick, reply))
    return {"tries": tries + 1, "chance": chance, "reply": reply}


def pending_contacts(conn) -> list[dict[str, Any]]:
    """Offene (noch nicht verwendete) Antworten — Haken für #48."""
    return [dict(r) for r in conn.execute(
        "SELECT id, tick, text FROM online_contacts WHERE kind = 'reply' AND handled = 0 "
        "ORDER BY id;")]


def mark_contact_handled(conn, contact_id: int) -> None:
    with conn:
        conn.execute("UPDATE online_contacts SET handled = 1 WHERE id = ?;", (contact_id,))


# --- Downloads -------------------------------------------------------------
def progress(conn, package_id: str) -> dict[str, Any]:
    p = package(package_id)
    row = conn.execute(
        "SELECT downloaded_mb, done_tick FROM package_progress WHERE package_id = ?;",
        (package_id,)).fetchone()
    mb = min(p["size_mb"], row["downloaded_mb"]) if row else 0.0
    done = bool(row and row["done_tick"] is not None)
    return {"downloaded_mb": mb, "remaining_mb": 0.0 if done else p["size_mb"] - mb,
            "done": done, "fraction": 1.0 if done else mb / p["size_mb"]}


def bandwidth_mb_per_min(quality: float) -> float:
    return constants.DOWNLOAD_MB_PER_MIN * quality


def minutes_needed(remaining_mb: float, quality: float) -> int:
    bw = bandwidth_mb_per_min(quality)
    return int(math.ceil(remaining_mb / bw)) if bw > 0 else 10**9


def add_progress(conn, package_id: str, mb: float, tick: int) -> dict[str, Any]:
    """Schreibt Teilfortschritt; bei Abschluss die Freischaltungen in die KB
    (curated). Atomar (eine Transaktion). Liefert {downloaded_mb, done}."""
    p = package(package_id)
    cur = progress(conn, package_id)
    total = min(float(p["size_mb"]), cur["downloaded_mb"] + max(0.0, mb))
    done = total >= p["size_mb"] - 1e-9
    with conn:
        conn.execute(
            "INSERT INTO package_progress (package_id, downloaded_mb, done_tick) "
            "VALUES (?,?,?) ON CONFLICT(package_id) DO UPDATE SET "
            "downloaded_mb=excluded.downloaded_mb, done_tick=excluded.done_tick;",
            (package_id, total, tick if done else None))
        if done:
            kb.add(conn, f"knowledge:{package_id}", package_id,
                   {"title": p["title"], "summary": p.get("summary"), "tick": tick},
                   provenance="curated", tick=tick)
            for topic, key, value in p.get("kb", []):
                kb.add(conn, topic, key, value, provenance="curated", tick=tick)
    return {"downloaded_mb": total, "done": done}


def has_knowledge(conn, package_id: str) -> bool:
    return kb.lookup(conn, f"knowledge:{package_id}", package_id) is not None


def reset_online_state(conn) -> None:
    """Neues Spiel: Online-Zustand und freigeschaltetes Wissen zurücksetzen."""
    topics = [f"knowledge:{p['id']}" for p in packages()]
    recipes = [(t, k) for p in packages() for t, k, _ in p.get("kb", [])]
    with conn:
        for stmt in ("DELETE FROM device_state;", "DELETE FROM news_read;",
                     "DELETE FROM package_progress;", "DELETE FROM online_contacts;"):
            conn.execute(stmt)
        for t in topics:
            conn.execute("DELETE FROM knowledge_base WHERE topic = ?;", (t,))
        for t, k in recipes:
            conn.execute("DELETE FROM knowledge_base WHERE topic = ? AND key = ?;", (t, k))


# --- HUD ------------------------------------------------------------------
def hud_infra(conn, group_id: int, location_id: int | None) -> dict[str, Any]:
    """Infrastruktur-Block für /world/state: Strom/Wasser am Standort (falls ein Ort
    in Reichweite liegt, sonst typischer Wert der Welt), Netz, Akkus."""
    w = decay_world.world_infra(conn)
    if location_id is not None:
        loc = decay_world.infra_for(conn, location_id)
        power, water, scope = loc["power"], loc["water"], "here"
    else:
        power, water, scope = w["power"], w["water"], "world"
    net = devices.network_quality(conn, location_id)
    return {
        "phase": w["phase"], "day": w["day"], "scope": scope,
        "power": power, "water": water,
        "cellular": net["cellular"], "internet": net["internet"],
        "quality": net["quality"], "wifi": net["wifi"], "online": net["online"],
        "devices": devices.status(conn, group_id),
    }
