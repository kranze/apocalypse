"""Headless Balance-Durchlauf (Issue #49, Fun-Gate).

Spielt ausschließlich über ``actions.perform`` / ``actions.available`` (Stub-LLM,
kein Netz, Luftlinien-Routing) eine komplette Woche in einem synthetischen
Viertel durch — mit fünf Strategien:

  passive, hoarder, online, companion, reckless

Aufruf::

    python scripts/balance_sim.py                # alle Strategien, Seeds 1..5
    python scripts/balance_sim.py --seeds 8 --strategy hoarder -v

Importierbar: ``run(strategy, seed)`` liefert ein Ergebnis-Dict, ``run_all(...)``
die Tabelle (für ``tests/test_balance.py``).
"""
from __future__ import annotations

import argparse
import contextlib
import math
import os
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("WASTELAND_LLM_BACKEND", "stub")

import app.config as config  # noqa: E402
import app.llm as llm_mod  # noqa: E402
import app.osm.roads as roads_module  # noqa: E402
from app.sim import (actions, companion, constants, ending, game, generation, online,  # noqa: E402
                     survivors)

HOME = (49.0, 11.0)
PLAYER = 1
SEC_PER_CLICK = 25.0
SEC_PER_TEXT = 45.0
MAX_ACTIONS = 900          # Sicherheitsnetz gegen Endlosschleifen
STRATEGIES = ("passive", "hoarder", "online", "companion", "reckless")

# Freitext-Zeilen, die der Stub versteht (Spieler probieren zwischendurch etwas aus)
FREETEXT_LINES = ("Ich schaue mich um.", "Ich sehe mich im Raum um.", "Ich schaue aus dem Fenster.",
                  "Ich ruhe mich kurz aus.")

# Download-Reihenfolge je nach Nutzen fürs Bleiben (Online-Strategie)
PACKAGE_PRIORITY = ("water_treatment", "disease_control", "first_aid", "food_preservation",
                    "fire_cooking", "shelter_warmth", "wild_plants", "solar_batteries")


# --- Welt -------------------------------------------------------------------
class _NoGraph:
    def nearest_node(self, lat, lon):
        return None


@contextlib.contextmanager
def offline():
    """Stub-LLM, Luftlinien-Routing, kein Netz."""
    old = (roads_module.get_graph, roads_module.ensure_roads_for_chunk,
           os.environ.get("WASTELAND_LLM_BACKEND"))
    os.environ["WASTELAND_LLM_BACKEND"] = "stub"
    llm_mod.reset_backend()
    roads_module.get_graph = lambda *a, **k: _NoGraph()
    roads_module.ensure_roads_for_chunk = lambda *a, **k: None
    try:
        yield
    finally:
        roads_module.get_graph, roads_module.ensure_roads_for_chunk = old[0], old[1]
        if old[2] is None:
            os.environ.pop("WASTELAND_LLM_BACKEND", None)
        else:
            os.environ["WASTELAND_LLM_BACKEND"] = old[2]
        llm_mod.reset_backend()


def _offset(dist_m: float, angle: float) -> tuple[float, float]:
    dlat = dist_m * math.cos(angle) / 111_320.0
    dlon = dist_m * math.sin(angle) / (111_320.0 * math.cos(math.radians(HOME[0])))
    return HOME[0] + dlat, HOME[1] + dlon


def layout(seed: int) -> list[dict[str, Any]]:
    """Synthetisches Viertel: Zuhause, 10 Häuser, 2 Supermärkte, Tankstelle,
    Apotheke, Baumarkt in 100 m bis 1,5 km Entfernung (deterministisch je Seed)."""
    r = random.Random(seed * 7919 + 13)
    spec: list[tuple[str, str, float, float, float]] = [("house", "Zuhause", 0.0, 0.0, 120.0)]
    for i in range(10):
        spec.append(("house", f"Haus {i + 1}", r.uniform(100, 1500), r.uniform(0, 2 * math.pi),
                     r.uniform(80, 220)))
    spec += [
        ("supermarket", "Supermarkt Nord", r.uniform(300, 700), r.uniform(0, 2 * math.pi), 450.0),
        ("supermarket", "Supermarkt Süd", r.uniform(800, 1400), r.uniform(0, 2 * math.pi), 600.0),
        ("fuel_station", "Tankstelle", r.uniform(500, 1200), r.uniform(0, 2 * math.pi), 150.0),
        ("pharmacy", "Apotheke", r.uniform(300, 900), r.uniform(0, 2 * math.pi), 120.0),
        ("hardware", "Baumarkt", r.uniform(700, 1500), r.uniform(0, 2 * math.pi), 400.0),
    ]
    out = []
    for i, (typ, name, d, a, fp) in enumerate(spec, start=1):
        lat, lon = _offset(d, a)
        out.append({"id": i, "type": typ, "name": name, "lat": lat, "lon": lon,
                    "footprint": fp, "gen_seed": 1000 * seed + i})
    return out


def build_world(seed: int):
    """Neues Spiel (ohne Netz) im synthetischen Viertel. Gibt die Connection zurück."""
    from tests.conftest import make_conn, set_seed  # nur Test-Helfer für die DB
    conn = make_conn()
    set_seed(conn, 1000 + seed)
    # Die weltweite Überlebenden-Verteilung (3 s) ist für den Durchlauf unnötig: der Companion
    # erzeugt bei Bedarf einen lokalen Überlebenden (companion._spawn_local_survivor).
    real_spawn = survivors.spawn_survivors
    survivors.spawn_survivors = lambda *a, **k: None
    try:
        res = game.new_game(conn, {"name": "Test", "lat": HOME[0], "lon": HOME[1],
                                   "weight_kg": 75.0, "height_cm": 175.0, "sex": "f",
                                   "birthdate": "1990-01-01"})
    finally:
        survivors.spawn_survivors = real_spawn
    assert res["ok"], res
    for loc in layout(seed):
        conn.execute(
            "INSERT INTO locations (id, osm_id, type, name, lat, lon, footprint_m2, "
            "discovery_status, generation_seed) VALUES (?,?,?,?,?,?,?,'undiscovered',?);",
            (loc["id"], f"syn_{loc['id']}", loc["type"], loc["name"], loc["lat"], loc["lon"],
             loc["footprint"], loc["gen_seed"]))
    conn.commit()
    return conn


# --- Bot --------------------------------------------------------------------
class Bot:
    """Spielt ausschließlich über perform/available. Zählt Klick- und Textaktionen."""

    def __init__(self, conn, verbose: bool = False, pace: str = "normal"):
        self.conn = conn
        self.pace = pace
        self.idles = 0
        self.clicks = 0
        self.texts = 0
        self.verbose = verbose
        self.survivor_searches = (-1, 0)

    # -- Grundfunktionen
    def do(self, action: dict) -> dict:
        r = actions.perform(self.conn, PLAYER, action)
        if action.get("kind") in ("talk", "freetext"):
            self.texts += 1
        else:
            self.clicks += 1
        if self.verbose:
            print(f"   d{self.day()} {action.get('kind'):16s} ok={r.get('ok')} "
                  f"{r.get('reason') or ''} min={r.get('minutes_spent')}")
        return r

    @property
    def n_actions(self) -> int:
        return self.clicks + self.texts

    def done(self) -> bool:
        return ending.is_ended(self.conn) or self.n_actions >= MAX_ACTIONS

    def day(self) -> int:
        return ending.current_day(self.conn)

    def char(self):
        return self.conn.execute("SELECT * FROM characters WHERE id=1;").fetchone()

    def av(self) -> list[dict]:
        return actions.available(self.conn, PLAYER)["actions"]

    def find(self, kind: str, **match) -> dict | None:
        for a in self.av():
            if a["kind"] == kind and all(a.get(k) == v for k, v in match.items()):
                return a
        return None

    def locs(self, *types: str) -> list[dict]:
        c = self.char()
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, type, lat, lon FROM locations;") if not types or r["type"] in types]
        for r in rows:
            r["dist"] = math.hypot((r["lat"] - c["lat"]) * 111_320.0,
                                   (r["lon"] - c["lon"]) * 111_320.0 * math.cos(math.radians(c["lat"])))
        return sorted(rows, key=lambda r: r["dist"])

    def inv(self, item_id: str) -> float:
        return self.conn.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM group_inventory WHERE group_id=? AND item_id=?;",
            (self.char()["group_id"], item_id)).fetchone()[0]

    def dist_to(self, loc_id: int) -> float:
        return next(r["dist"] for r in self.locs() if r["id"] == loc_id)

    # -- zusammengesetzte Handlungen
    def goto(self, loc_id: int) -> bool:
        for _ in range(8):
            r = self.do({"kind": "walk", "location_id": loc_id})
            if not r.get("ok") and not r.get("minutes_spent"):
                return False
            if r.get("stopped") == "arrived":
                return True
            if self.done() or not self.char()["is_alive"]:
                return False
        return False

    def visit(self, loc_id: int) -> bool:
        """Hingehen, durchsuchen, plündern. True, wenn etwas getan wurde."""
        if not self.goto(loc_id):
            return False
        if self.find("search", location_id=loc_id):
            self.do({"kind": "search", "location_id": loc_id})
        if self.find("loot", location_id=loc_id):
            self.do({"kind": "loot", "location_id": loc_id})
        return True

    def free_capacity(self) -> float:
        from app.sim import looting, movement
        gid = self.char()["group_id"]
        return looting.carry_capacity(self.conn, gid) - movement.carried_weight(self.conn, gid)

    def refill_target(self, min_free_kg: float = 5.0) -> int | None:
        """Nächster durchsuchter Ort mit Restbestand, sobald im Rucksack wieder Platz ist."""
        if self.free_capacity() < min_free_kg:
            return None
        for r in self.locs():
            left = self.conn.execute(
                "SELECT COUNT(*) FROM location_inventory WHERE location_id=?;", (r["id"],)
            ).fetchone()[0]
            if left and generation.is_searched(self.conn, r["id"]):
                return r["id"]
        return None

    def maintain(self) -> bool:
        """Essen und Schlafen — wie ein vernünftiger Spieler."""
        c = self.char()
        if c["hunger"] < 0.55:
            a = self.find("eat")
            if a and a.get("options"):
                self.do({"kind": "eat", "item_id": a["options"][0]["item_id"]})
                return True
        if c["sleep"] < 0.35 and self.find("sleep"):
            self.do({"kind": "sleep"})
            return True
        return False

    def idle(self) -> None:
        """Zeit vergehen lassen. ``efficient``: Warten bis morgen früh (Untergrenze der Klicks).
        ``normal``: ein gewöhnlicher Spieler — tagsüber ~2 h am Stück, jede dritte Pause
        ein Freitext („Ich schaue mich um“), nachts schlafen."""
        if self.pace == "efficient":
            for a in self.av():
                if a["kind"] == "wait" and a["minutes"] and a["minutes"] > 60:
                    self.do({"kind": "wait", "minutes": a["minutes"]})
                    return
            self.do({"kind": "wait", "minutes": 60})
            return
        hour = actions.world_clock(self.conn)["datetime"].hour
        night = hour >= 22 or hour < 6
        if night:
            if self.find("sleep") and self.char()["sleep"] < 0.9:
                self.do({"kind": "sleep"})
                return
            self.do({"kind": "wait", "minutes": max(60, actions.minutes_until_morning(self.conn))})
            return
        self.idles += 1
        if self.idles % 3 == 0:
            self.do({"kind": "freetext", "text": FREETEXT_LINES[self.idles // 3 % len(FREETEXT_LINES)]})
            return
        self.do({"kind": "wait", "minutes": 120})

    def idle_short(self) -> None:
        self.do({"kind": "wait", "minutes": 60})

    def online_block(self, budget_min: int, packages=PACKAGE_PRIORITY, news: int = 2,
                     survivors: bool = False) -> int:
        """Online-Zeit am aktuellen Ort verbrauchen (Nachrichten, Downloads, Laden).
        Gibt die verbrauchten Minuten zurück."""
        used = 0
        read = 0
        while used < budget_min and not self.done():
            if self.maintain():
                continue
            if survivors and self.find("search_survivors"):
                day, n = self.survivor_searches
                if day != self.day():
                    day, n = self.day(), 0
                if n < 4:
                    self.survivor_searches = (day, n + 1)
                    r = self.do({"kind": "search_survivors"})
                    used += r.get("minutes_spent") or 0
                    if companion.get(self.conn):
                        return used
                    continue
            if survivors and not self.find("search_survivors") and self.find("charge")                     and self.survivor_searches[1] < 4:
                r = self.do({"kind": "charge"})
                used += r.get("minutes_spent") or 0
                if not r["ok"]:
                    break
                continue
            if read < news and self.find("read_news"):
                r = self.do({"kind": "read_news"})
                used += r.get("minutes_spent") or 0
                read += 1
                continue
            pending = [p for p in packages if not online.progress(self.conn, p)["done"]]
            if not pending:
                break
            r = self.do({"kind": "download", "package_id": pending[0]})
            if r["ok"]:
                used += r["minutes_spent"]
                continue
            if r.get("reason") == "no_battery":
                c = self.do({"kind": "charge"})
                if not c["ok"]:
                    break
                used += c["minutes_spent"]
                continue
            break  # kein Netz / sonstiges
        return used


# --- Strategien --------------------------------------------------------------
class Strategy:
    name = "base"

    def __init__(self, bot: Bot):
        self.bot = bot

    def step(self) -> None:
        raise NotImplementedError

    def decision(self) -> dict:
        return {"kind": "decide", "choice": "stay"}

    def early_decision(self) -> dict | None:
        return None

    def loot_targets(self, houses: int = 99) -> list[int]:
        """Supermarkt zuerst, dann Häuser, zweiter Supermarkt, Apotheke/Baumarkt/Tankstelle."""
        b = self.bot
        sm = [r["id"] for r in b.locs("supermarket")]
        hs = [r["id"] for r in b.locs("house") if r["id"] != 1][:houses]
        rest = [r["id"] for r in b.locs("pharmacy", "hardware", "fuel_station")]
        return sm[:1] + hs[:3] + sm[1:] + hs[3:] + rest

    def next_unsearched(self, targets: list[int]) -> int | None:
        for t in targets:
            if not generation.is_searched(self.bot.conn, t):
                return t
        return None

    def stay_home(self) -> None:
        b = self.bot
        if b.dist_to(1) > constants.LOOT_REACH_M:
            b.goto(1)
            return
        b.idle()


class Passive(Strategy):
    name = "passive"

    def step(self) -> None:
        b = self.bot
        if b.maintain():
            return
        if not generation.is_searched(b.conn, 1):
            b.visit(1)
            return
        b.idle()


class Hoarder(Strategy):
    name = "hoarder"

    def step(self) -> None:
        b = self.bot
        if b.maintain():
            return
        t = self.next_unsearched(self.loot_targets()) if b.day() < 3 else None
        if t is not None:
            b.visit(t)
            return
        t = b.refill_target() if b.day() <= 4 else None
        if t is not None:
            b.visit(t)
            return
        self.stay_home()


class OnlineStrat(Strategy):
    name = "online"

    def __init__(self, bot):
        super().__init__(bot)
        self.blocks: set[tuple[int, str]] = set()

    def step(self) -> None:
        b = self.bot
        if b.maintain():
            return
        d = b.day()
        if d <= 3 and (d, "main") not in self.blocks:
            if b.dist_to(1) > constants.LOOT_REACH_M:
                b.goto(1)
                return
            if not generation.is_searched(b.conn, 1):
                b.visit(1)
                return
            self.blocks.add((d, "main"))
            b.online_block(budget_min=330 if d == 0 else 300)
            return
        if d <= 2:
            t = self.next_unsearched(self.loot_targets(houses=2)[:4])
            if t is not None:
                b.visit(t)
                return
        if d <= 3 and (d, "late") not in self.blocks:
            self.blocks.add((d, "late"))
            if b.dist_to(1) > constants.LOOT_REACH_M:
                b.goto(1)
            b.online_block(budget_min=240, news=1)
            return
        self.stay_home()


class CompanionStrat(Strategy):
    name = "companion"

    def __init__(self, bot):
        super().__init__(bot)
        self.gives = 0
        self.talk_day = -1

    def supplies(self) -> float:
        b = self.bot
        return b.inv("water_1l") + sum(
            b.inv(i) for i in ("canned_beans", "canned_meat", "canned_soup", "bread_loaf",
                               "crackers", "chocolate", "pasta_500g"))

    def loot_more(self, limit_day: int = 5) -> bool:
        b = self.bot
        if b.day() >= limit_day:
            return False
        t = self.next_unsearched(self.loot_targets()) or b.refill_target()
        if t is None:
            return False
        b.visit(t)
        return True

    def step(self) -> None:
        b = self.bot
        if b.maintain():
            return
        row = companion.get(b.conn)
        stage = row["stage"] if row else None
        if stage in ("contacted", "approaching"):
            if self.supplies() < 8 and self.loot_more():
                return
            w = b.find("walk")
            if w and w.get("location_id")                     and b.dist_to(w["location_id"]) > constants.COMPANION_REACH_M * 0.5:
                b.do({"kind": "walk", "location_id": w["location_id"]})
                return
            b.idle_short()  # am Treffpunkt: auf den Companion warten
            return
        if stage == "met":
            self.social()
            return
        if stage == "joined":
            if self.loot_more():
                return
            self.stay_home()
            return
        if row is None and b.day() <= 3:
            if not generation.is_searched(b.conn, 1):
                b.visit(1)
                return
            if b.dist_to(1) > constants.LOOT_REACH_M:
                b.goto(1)
            b.online_block(budget_min=200, packages=(), news=0, survivors=True)
            if companion.get(b.conn) is None and self.loot_more():
                return
            return
        if self.loot_more():
            return
        self.stay_home()

    def social(self) -> None:
        b = self.bot
        if companion.here(b.conn) is None:
            mp = companion.view(b.conn)["meeting_point"]
            if mp:
                b.goto(mp["location_id"])
            else:
                b.idle_short()
            return
        v = companion.view(b.conn)
        if v["trust"] >= constants.COMPANION_JOIN_TRUST and v["opinion"] != "stay":
            b.do({"kind": "talk", "text": "Komm mit mir, wir bleiben zusammen."})
            return
        give = b.find("give")
        if give and give.get("options") and self.gives < 7:
            self.gives += 1
            opts = give["options"]
            water = next((o for o in opts if o["item_id"] == "water_1l"), None)
            food = next((o for o in opts if o["item_id"] != "water_1l"), None)
            thirsty = v["needs"]["thirst"] < v["needs"]["hunger"]
            pick = (water if thirsty else food) or water or food
            b.do({"kind": "give", "item_id": pick["item_id"]})
            return
        if self.talk_day != b.day():
            self.talk_day = b.day()
            b.do({"kind": "talk", "text": "Wie geht es dir? Vertraust du mir?"})
            return
        if self.loot_more():
            return
        b.idle()

    def decision(self) -> dict:
        b = self.bot
        row = companion.get(b.conn)
        opts = ending.decision_options(b.conn)
        contacts = [d["id"] for d in opts["destinations"] if d["id"].startswith("contact:")]
        if row and row["stage"] == "joined" and contacts:
            bike = b.inv("bicycle") >= 1
            return {"kind": "decide", "choice": "go", "transport": "bike" if bike else "foot",
                    "destination": contacts[0]}
        return {"kind": "decide", "choice": "stay"}


class Reckless(Strategy):
    name = "reckless"

    def step(self) -> None:
        b = self.bot
        if b.maintain():
            return
        hs = [r["id"] for r in b.locs("house") if r["id"] != 1][:2]
        for t in [1] + hs:
            if not generation.is_searched(b.conn, t) and b.day() < 4:
                b.visit(t)
                return
        b.idle()

    def early_decision(self):
        if ending.decision_open(self.bot.conn):
            return {"kind": "decide", "choice": "go", "transport": "foot", "destination": "north"}
        return None

    def decision(self):
        return {"kind": "decide", "choice": "go", "transport": "foot", "destination": "north"}


CLASSES = {"passive": Passive, "hoarder": Hoarder, "online": OnlineStrat,
           "companion": CompanionStrat, "reckless": Reckless}


# --- Durchlauf ----------------------------------------------------------------
def run(strategy: str, seed: int, verbose: bool = False, pace: str = "normal") -> dict[str, Any]:
    """Ein kompletter Durchlauf. Liefert outcome/score/Tod/Tag/Kennzahlen/Dauer."""
    with offline():
        conn = build_world(seed)
        bot = Bot(conn, verbose=verbose, pace=pace)
        strat = CLASSES[strategy](bot)
        while not bot.done():
            if not bot.char()["is_alive"]:
                break
            early = strat.early_decision()
            if ending.deadline_reached(conn) or early:
                r = bot.do(early or strat.decision())
                if not r["ok"]:  # Fallback: bleiben
                    bot.do({"kind": "decide", "choice": "stay"})
                break
            n0, t0 = bot.n_actions, conn.execute("SELECT tick FROM world").fetchone()[0]
            strat.step()
            if bot.n_actions == n0 or conn.execute("SELECT tick FROM world").fetchone()[0] == t0:
                bot.idle()  # Sicherheitsnetz: die Zeit muss weiterlaufen
        end = ending.get_ending(conn)
        if end is None:  # Abbruch durch Sicherheitsnetz
            end = {"outcome": "timeout", "score": 0.0, "day": bot.day(), "cause_of_death": None,
                   "stats": ending.stats(conn)}
        st = end["stats"]
        secs = bot.clicks * SEC_PER_CLICK + bot.texts * SEC_PER_TEXT
        res = {
            "strategy": strategy, "seed": seed, "outcome": end["outcome"],
            "score": end["score"], "cause": end.get("cause_of_death"), "day": end["day"],
            "actions": bot.n_actions, "packages": len(st["packages"]),
            "companion": st["companion"], "km": st["km_walked"],
            "minutes": round(secs / 60.0, 1),
            "components": {c["key"]: c["value"] for c in end.get("components", [])},
        }
        conn.close()
        return res


def run_all(strategies=STRATEGIES, seeds=range(1, 6), pace: str = "normal") -> list[dict[str, Any]]:
    return [run(s, seed, pace=pace) for s in strategies for seed in seeds]


def summarize(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for s in dict.fromkeys(r["strategy"] for r in results):
        rs = [r for r in results if r["strategy"] == s]
        dist: dict[str, int] = {}
        for r in rs:
            dist[r["outcome"]] = dist.get(r["outcome"], 0) + 1
        out[s] = {"n": len(rs), "outcomes": dist,
                  "avg_score": round(sum(r["score"] for r in rs) / len(rs), 3),
                  "avg_minutes": round(sum(r["minutes"] for r in rs) / len(rs), 1),
                  "avg_actions": round(sum(r["actions"] for r in rs) / len(rs), 1)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--strategy", choices=STRATEGIES, action="append")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--pace", choices=("normal", "efficient"), default="normal",
                    help="normal = gewöhnlicher Spieler (Standard); efficient = Untergrenze der Klicks")
    ap.add_argument("-c", "--components", action="store_true", help="Score-Komponenten ausgeben")
    args = ap.parse_args(argv)
    strategies = args.strategy or STRATEGIES
    results = []
    print(f"{'strategy':10s} {'seed':>4s} {'outcome':13s} {'score':>6s} {'death':11s} {'day':>3s} "
          f"{'acts':>5s} {'pkg':>3s} {'comp':10s} {'km':>5s} {'min':>6s}")
    for s in strategies:
        for seed in range(1, args.seeds + 1):
            r = run(s, seed, verbose=args.verbose, pace=args.pace)
            results.append(r)
            print(f"{r['strategy']:10s} {r['seed']:4d} {r['outcome']:13s} {r['score']:6.3f} "
                  f"{(r['cause'] or '-'):11s} {r['day']:3d} {r['actions']:5d} {r['packages']:3d} "
                  f"{r['companion']:10s} {r['km']:5.1f} {r['minutes']:6.1f}")
            if args.components:
                print("      ", r["components"])
    print()
    for s, v in summarize(results).items():
        print(f"{s:10s} {v['outcomes']}  score~{v['avg_score']}  ~{v['avg_minutes']} min "
              f"({v['avg_actions']} Aktionen)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
