"""Werkzeugkasten des Adjudikators v2 — die Sim-Kern-Seite der Tool-Use-Schleife.

Das LLM-Backend (``app/llm``) ruft nur ``AdjudicationToolbox.call(name, args)``.
Hier — im Sim-Kern — passiert alles Wirksame:

* **Lese-Werkzeuge** liefern read-only Auskünfte (Orte, Inventar, Inhalt eines
  bereits durchsuchten Ortes, KB, Capabilities, Anforderungs-Lieferanten,
  Weltzustand).
* **Vorschlags-Werkzeuge** (``propose_<op>``) werden SOFORT per
  ``effects.validate_one`` gegen DB + **gestagten Plan** (``effects.Staged``)
  geprüft. Akzeptierte Vorschläge landen in ``plan`` (und im Staging), abgelehnte
  in ``rejections``; das Modell bekommt ok / Grund zurück und darf umplanen.
  Es wird NICHTS geschrieben.
* ``finish`` beendet die Schleife (Verständnis, Machbarkeit, Narration).

Angewendet wird der Plan erst nach der Schleife durch den Adjudikator
(``effects.apply_atomic``). Eisernes Prinzip: das Modell sieht nur Auskünfte und
Validierungsergebnisse; es kann nichts schreiben.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..llm import tools as llm_tools
from ..llm.base import FEASIBILITY
from . import capabilities, effects, generation, kb, requirements

# Obergrenze der Werkzeugaufrufe je erlaubter Modell-Runde (Schutz gegen
# Endlos-Aufrufe innerhalb einer Runde, z.B. extrem viele Parallel-Calls).
_CALLS_PER_TURN = 8


def hint_for(reason: str | None) -> str | None:
    """Kurzer Spieler-Hinweis zu einem Ablehnungsgrund."""
    if not reason:
        return None
    if reason.startswith("missing:"):
        return f"Dir fehlt: {reason.split(':', 1)[1]}."
    return {
        "no_target": "Davon ist nichts in der Nähe.",
        "no_food": "Nichts direkt Essbares dabei.",
        "no_water": "Nicht genug Wasser.",
        "no_heat": "Keine Hitzequelle.",
        "no_recipe": "So etwas kannst du (noch) nicht aufbauen.",
        "nothing_to_prepare": "Nichts zuzubereiten.",
        "nothing_to_take": "Da liegt nichts (mehr), was du nehmen könntest.",
        "no_water_source": "Hier gibt es kein Wasser zum Abfüllen.",
        "not_searched": "Das hast du hier noch nicht durchsucht.",
        "no_location_here": "Dafür musst du an einem Ort sein.",
        "produce_exceeds_input": "Aus so wenig wird nicht so viel.",
        "iteration_limit": "Das ist zu verwickelt — versuch es einfacher.",
    }.get(reason)


class AdjudicationToolbox:
    """Zustand und Werkzeuge EINER Adjudikation (wird nach der Schleife verworfen)."""

    def __init__(self, conn: sqlite3.Connection, character_id: int,
                 context: dict[str, Any], max_iterations: int = 8) -> None:
        self.conn = conn
        self.character_id = character_id
        self.context = context
        self.max_iterations = max_iterations
        self.allow_partial = True  # False: jede Ablehnung verwirft den ganzen Plan
        self.reset()

    # --- Zustand ------------------------------------------------------
    def reset(self) -> None:
        self.staged = effects.new_staged()
        self.plan: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []
        self.finished: dict[str, Any] | None = None
        self.log: list[dict[str, Any]] = []
        self.turns = 0
        self.calls = 0
        self.limit_hit = False
        self.last_rejected = False

    def specs(self) -> list[dict[str, Any]]:
        return llm_tools.tool_specs()

    def begin_turn(self) -> bool:
        """Jede Modell-Runde meldet sich hier an; False = Iterationslimit."""
        self.turns += 1
        if self.turns > self.max_iterations:
            self.limit_hit = True
            return False
        return True

    # --- Dispatch -----------------------------------------------------
    def call(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        """Führt ein Werkzeug aus. Wirft nie; Fehler kommen als ``ok: False``."""
        args = args if isinstance(args, dict) else {}
        self.calls += 1
        if self.calls > self.max_iterations * _CALLS_PER_TURN:
            self.limit_hit = True
            res: dict[str, Any] = {"ok": False, "reason": "iteration_limit"}
        elif self.finished is not None:
            res = {"ok": False, "reason": "already_finished"}
        else:
            try:
                res = self._dispatch(name, args)
            except Exception as exc:  # defensiv: Modell-Eingaben dürfen nichts sprengen
                res = {"ok": False, "reason": f"tool_error:{type(exc).__name__}"}
        self.log.append({"tool": name, "args": args, "result": res})
        return res

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == llm_tools.FINISH_TOOL:
            return self._finish(args)
        op = llm_tools.op_of(name)
        if op is not None:
            return self._propose(op, args)
        reader = getattr(self, f"_r_{name}", None) if name in llm_tools.READ_TOOLS else None
        if reader is None:
            return {"ok": False, "reason": f"unknown_tool:{name}"}
        return {"ok": True, **reader(args)}

    # --- Vorschlag / Finish ---------------------------------------------
    def _propose(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        eff = llm_tools.to_effect(op, args)
        ok, reason = effects.validate_one(
            self.conn, self.character_id, eff, self.context, self.staged
        )
        if ok:
            self.plan.append(eff)
            self.last_rejected = False
            return {"ok": True, "accepted": True, "plan_size": len(self.plan)}
        self.rejections.append({"op": op, "reason": reason, "effect": eff})
        self.last_rejected = True
        return {"ok": False, "accepted": False, "reason": reason, "hint": hint_for(reason)}

    def _finish(self, args: dict[str, Any]) -> dict[str, Any]:
        feas = args.get("feasibility")
        self.finished = {
            "understanding": str(args.get("understanding") or ""),
            "feasibility": feas if feas in FEASIBILITY else "feasible",
            "reason": str(args.get("reason") or ""),
            "narration": str(args.get("narration") or ""),
        }
        return {"ok": True, "finished": True}

    # --- Lese-Werkzeuge -------------------------------------------------
    def _gid(self) -> int:
        p = self.context.get("player") or {}
        return p.get("group_id") or 1

    def _r_get_nearby_locations(self, args) -> dict[str, Any]:
        locs = []
        for loc in self.context.get("locations", []):
            row = dict(loc)
            row["searched"] = generation.is_searched(self.conn, loc["id"])
            locs.append(row)
        return {"locations": locs}

    def _r_get_inventory(self, args) -> dict[str, Any]:
        staged = {k: round(v, 3) for k, v in self.staged.delta.items() if abs(v) > 1e-9}
        return {"inventory": self.context.get("inventory", []), "staged_changes": staged}

    def _r_get_location_inventory(self, args) -> dict[str, Any]:
        try:
            lid = int(args.get("location_id"))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "bad_location_id"}
        near = {l["id"] for l in self.context.get("locations", [])}
        reach_ok = lid in near and next(
            l["dist_m"] for l in self.context["locations"] if l["id"] == lid
        ) <= effects._REACH_M
        if not reach_ok:
            return {"ok": False, "reason": "out_of_reach"}
        if not generation.is_searched(self.conn, lid):
            return {"ok": False, "reason": "not_searched"}
        rows = self.conn.execute(
            "SELECT li.item_id, ic.name, ic.category, SUM(li.quantity) AS quantity "
            "FROM location_inventory li JOIN item_catalog ic ON ic.id = li.item_id "
            "WHERE li.location_id = ? AND li.quantity > 0 GROUP BY li.item_id "
            "ORDER BY li.item_id;", (lid,),
        ).fetchall()
        taken = {i: q for (l, i), q in self.staged.loc_taken.items() if l == lid}
        return {"location_id": lid, "items": [
            {"item_id": r["item_id"], "name": r["name"], "category": r["category"],
             "quantity": round(r["quantity"] - taken.get(r["item_id"], 0.0), 3)}
            for r in rows
        ]}

    def _r_kb_lookup(self, args) -> dict[str, Any]:
        topic, key = args.get("topic"), args.get("key")
        if not topic:
            return {"ok": False, "reason": "no_topic"}
        if key:
            fact = kb.lookup(self.conn, str(topic), str(key))
            return {"facts": [{"key": key, **fact}] if fact else []}
        return {"facts": kb.list_topic(self.conn, str(topic))}

    def _r_get_capabilities(self, args) -> dict[str, Any]:
        return {"capabilities": [
            {"ctype": c["ctype"], "params": c["params"]}
            for c in capabilities.list_active(self.conn, self._gid())
        ]}

    def _r_get_requirement_providers(self, args) -> dict[str, Any]:
        req = args.get("requirement")
        reqs = [req] if req else list(requirements.KNOWN)
        out = {}
        for r in reqs:
            out[r] = {
                "providers": requirements.providers(self.conn, r),
                "satisfied_by": (requirements.satisfy(
                    self.conn, self._gid(), r,
                    owned_fn=lambda i: self.staged.owned(self.conn, self._gid(), i))),
            }
        return {"requirements": out}

    def _r_get_world_state(self, args) -> dict[str, Any]:
        return {"world_state": self.context.get("world_state") or {}}
