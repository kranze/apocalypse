"""API-Grenze: höchstens ``MAX_STRICT_TOOLS`` Werkzeuge mit ``strict: true`` je
Request (sonst 400 und stiller Rückfall auf den Stub bei jedem Freitext)."""
from __future__ import annotations

from app.llm import tools


def test_strict_tools_within_api_limit():
    specs = tools.tool_specs()
    assert sum(1 for s in specs if s.get("strict")) <= tools.MAX_STRICT_TOOLS


def test_finish_and_proposals_stay_strict():
    specs = {s["name"]: s for s in tools.tool_specs()}
    assert specs[tools.FINISH_TOOL].get("strict")
    assert all(s.get("strict") for n, s in specs.items() if n.startswith(tools.PROPOSE_PREFIX))


def test_non_strict_mode_has_no_strict_flags():
    assert not any(s.get("strict") for s in tools.tool_specs(strict=False))
