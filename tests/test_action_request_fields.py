"""Vertrag HTTP ↔ Sim-Kern: jeder Aktions-Parameter, den ``actions.py`` liest,
muss im Pydantic-Modell ``ActionRequest`` stehen — sonst verwirft FastAPI ihn
stillschweigend (so ging ``package_id`` beim Download verloren)."""
from __future__ import annotations

import re
from pathlib import Path

from app.main import ActionRequest

_ACTIONS = Path(__file__).resolve().parents[1] / "app" / "sim" / "actions.py"


def test_every_action_param_is_a_request_field():
    used = set(re.findall(r'action\.get\("([a-z_]+)"', _ACTIONS.read_text(encoding="utf-8")))
    missing = used - set(ActionRequest.model_fields)
    assert not missing, f"ActionRequest fehlt: {sorted(missing)}"
