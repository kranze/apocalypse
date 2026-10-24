"""Balance-Regressionstests (Issue #49, Fun-Gate aus docs/SLICE_WOCHE1.md §7).

Spielt mit dem Headless-Harness ``scripts/balance_sim.py`` fünf Strategien über
mehrere Seeds durch (nur ``actions.perform`` / ``actions.available``, Stub-LLM,
kein Netz) und prüft die Balance-Ziele aus ``docs/BALANCE.md``:

* Warten ist keine Strategie (passive stirbt/ist fragil),
* Horten, Online und Begleiter führen zu verschiedenen Ausgängen,
* Davonlaufen endet verzweifelt,
* mindestens vier verschiedene Ausgänge, keine dominante Strategie,
* Download-Zeiten (klein/mittel/groß) und Spieldauer eines normalen Durchlaufs.
"""
from __future__ import annotations

import pytest

from app.sim import online
from scripts import balance_sim as bs

SEEDS = (1, 2, 3, 4, 5)


@pytest.fixture(scope="module")
def results():
    rows = bs.run_all(bs.STRATEGIES, SEEDS)
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["strategy"], []).append(r)
    return out


def outcomes(rs):
    return [r["outcome"] for r in rs]


def test_passive_waiting_is_not_a_strategy(results):
    rs = results["passive"]
    assert all(o in ("death", "stay_fragile") for o in outcomes(rs))
    early_deaths = [r for r in rs if r["outcome"] == "death" and r["day"] < 7]
    assert len(early_deaths) > len(rs) / 2


def test_hoarder_secure_or_fragile_depending_on_seed(results):
    o = outcomes(results["hoarder"])
    assert set(o) <= {"stay_secure", "stay_fragile"}
    assert "stay_fragile" in o and "stay_secure" in o


def test_online_can_be_secure_but_cannot_download_everything(results):
    rs = results["online"]
    assert sum(r["outcome"] == "stay_secure" for r in rs) > len(rs) / 2
    assert all(r["packages"] < len(online.packages()) for r in rs)
    assert all(r["packages"] >= 5 for r in rs)  # Wissen ist erreichbar


def test_companion_mostly_go_hopeful(results):
    rs = results["companion"]
    assert sum(r["outcome"] == "go_hopeful" for r in rs) > len(rs) / 2
    assert all(r["companion"] in ("joined", "met") for r in rs)


def test_reckless_ends_desperate(results):
    o = outcomes(results["reckless"])
    assert not any(x.startswith("stay") or x == "go_hopeful" for x in o)
    assert o.count("go_desperate") >= len(o) - 1  # höchstens ein Tod


def test_at_least_four_distinct_outcomes(results):
    seen = {r["outcome"] for rs in results.values() for r in rs}
    assert len(seen) >= 4, seen


def test_no_strategy_dominates_all_seeds(results):
    best = []
    for i, seed in enumerate(SEEDS):
        scored = {s: rs[i]["score"] for s, rs in results.items()}
        best.append(max(scored, key=scored.get))
    assert len(set(best)) >= 2, best


def test_normal_run_takes_about_an_hour_or_more(results):
    for name in ("hoarder", "companion"):
        avg = sum(r["minutes"] for r in results[name]) / len(results[name])
        assert 55 <= avg <= 95, (name, avg)


def test_download_times_at_day_zero_with_wifi():
    # Tag 0, WLAN + Strom: Netzqualität = Internet(0) * 1.0 = 0.9
    q = 0.9
    assert 20 <= online.minutes_needed(50, q) <= 40       # klein
    assert 60 <= online.minutes_needed(150, q) <= 90      # mittel
    assert 180 <= online.minutes_needed(450, q) <= 240    # groß
    sizes = {p["size_mb"] for p in online.packages()}
    assert sizes == {50, 150, 450}


def test_run_is_deterministic():
    a = bs.run("hoarder", 1)
    b = bs.run("hoarder", 1)
    assert a == b
