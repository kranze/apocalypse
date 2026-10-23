"""Schalter SURVIVOR_SIM_ENABLED: automatischer Survivor-Tagesschritt in advance_tick.

Aus (Default): step_day wird nicht aufgerufen, der Tagesmarker läuft aber mit.
An: step_day pro überschrittenem Tag. Deterministisch, ohne Netz.
"""
from __future__ import annotations

import app.config as config
from app.sim import constants, survivor_sim
from app.sim.tick import advance_tick
from tests.conftest import make_conn, set_seed


def _make():
    conn = make_conn()
    set_seed(conn, 1337)
    return conn


def _day(conn) -> int:
    return conn.execute("SELECT survivor_sim_day FROM world WHERE id = 1;").fetchone()[0]


def _spy(monkeypatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(survivor_sim, "step_day", lambda c, d: calls.append(d))
    return calls


def test_default_is_disabled():
    assert config.SURVIVOR_SIM_ENABLED is False


def test_disabled_does_not_call_step_day(monkeypatch):
    monkeypatch.setattr(config, "SURVIVOR_SIM_ENABLED", False)
    calls = _spy(monkeypatch)
    conn = _make()
    conn.execute("UPDATE world SET tick = 1430, survivor_sim_day = 0 WHERE id = 1;")
    conn.commit()
    advance_tick(conn, minutes=constants.TICK_MINUTES)
    assert calls == []


def test_disabled_advances_day_marker(monkeypatch):
    monkeypatch.setattr(config, "SURVIVOR_SIM_ENABLED", False)
    _spy(monkeypatch)
    conn = _make()
    conn.execute("UPDATE world SET tick = 1430, survivor_sim_day = 0 WHERE id = 1;")
    conn.commit()
    advance_tick(conn, minutes=constants.TICK_MINUTES)
    assert _day(conn) == 1


def test_enable_later_has_no_catchup_burst(monkeypatch):
    monkeypatch.setattr(config, "SURVIVOR_SIM_ENABLED", False)
    calls = _spy(monkeypatch)
    conn = _make()
    # Zeit springt weit voran (5 Tage), Globalsim aus
    conn.execute(
        "UPDATE world SET tick = ?, survivor_sim_day = 0 WHERE id = 1;",
        (5 * constants.MINUTES_PER_DAY - 10,),
    )
    conn.commit()
    advance_tick(conn, minutes=constants.TICK_MINUTES)
    assert _day(conn) == 5 and calls == []
    # Jetzt einschalten: nur der nächste Tag, nicht Tage 1..5
    monkeypatch.setattr(config, "SURVIVOR_SIM_ENABLED", True)
    conn.execute(
        "UPDATE world SET tick = ? WHERE id = 1;", (6 * constants.MINUTES_PER_DAY - 10,)
    )
    conn.commit()
    advance_tick(conn, minutes=constants.TICK_MINUTES)
    assert calls == [6]


def test_enabled_calls_step_day_per_crossed_day(monkeypatch):
    monkeypatch.setattr(config, "SURVIVOR_SIM_ENABLED", True)
    calls = _spy(monkeypatch)
    conn = _make()
    conn.execute("UPDATE world SET tick = 1430, survivor_sim_day = 0 WHERE id = 1;")
    conn.commit()
    advance_tick(conn, minutes=constants.TICK_MINUTES)
    assert calls == [1]


def test_enabled_no_call_without_day_change(monkeypatch):
    monkeypatch.setattr(config, "SURVIVOR_SIM_ENABLED", True)
    calls = _spy(monkeypatch)
    conn = _make()
    conn.execute("UPDATE world SET tick = 100, survivor_sim_day = 0 WHERE id = 1;")
    conn.commit()
    advance_tick(conn, minutes=constants.TICK_MINUTES)
    assert calls == []
