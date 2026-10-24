"""Lore- und Prompt-Dateien für den Adjudikator (Issue #6).

Welt-Bibel (``app/data/world_bible.md``) und System-Prompt
(``app/llm/prompts/adjudicator.md``) sind editierbare Dateien statt Code. Beide
werden einmal pro Prozess gelesen und gecacht (stabiler Prompt-Präfix ->
Prompt-Caching im Claude-Backend); fehlt eine Datei, greift ein knapper Default.
``reload()`` verwirft den Cache (Tests / Hot-Edit).

Reines Lesen von Textdateien — keine DB, kein Schreiben.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_LLM_DIR = Path(__file__).resolve().parent
BIBLE_PATH = _LLM_DIR.parent / "data" / "world_bible.md"
PROMPT_PATH = _LLM_DIR / "prompts" / "adjudicator.md"
COMPANION_PROMPT_PATH = _LLM_DIR / "prompts" / "companion.md"

_DEFAULT_BIBLE = (
    "Ein Virus hat fast alle Menschen getötet; weltweit leben nur noch rund "
    "100.000 Überlebende. Die Welt ist still und leer; Infrastruktur verfällt."
)
_DEFAULT_SYSTEM = (
    "Du bist der Adjudikator einer Endzeit-Survival-Simulation. Nutze die "
    "Werkzeuge: lies den Zustand, schlage Effekte per propose_* vor, plane bei "
    "Ablehnung um und beende mit finish. Erfinde nichts; der Sim-Kern entscheidet."
)


def _read(path: Path, default: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return text or default


@lru_cache(maxsize=1)
def world_bible() -> str:
    return _read(BIBLE_PATH, _DEFAULT_BIBLE)


@lru_cache(maxsize=1)
def adjudicator_system() -> str:
    return _read(PROMPT_PATH, _DEFAULT_SYSTEM)


_DEFAULT_COMPANION = (
    "Du spielst einen Überlebenden und sprichst knapp, deutsch, in der Ich-Form. "
    "Erfinde keine Fakten und behaupte keine Zustände; nenne nur Intentionen."
)


@lru_cache(maxsize=1)
def companion_system() -> str:
    """System-Text des Companions: Prompt + Welt-Bibel (konstant -> cachebar)."""
    return _read(COMPANION_PROMPT_PATH, _DEFAULT_COMPANION) + "\n\n# Welt-Bibel\n\n" + world_bible()


def system_prompt() -> str:
    """Konstanter System-Text: Prompt + Welt-Bibel (cachebar, enthält nichts
    Veränderliches wie Tag/Uhrzeit — das steht im Nutzer-Kontext)."""
    return adjudicator_system() + "\n\n# Welt-Bibel\n\n" + world_bible()


def reload() -> None:
    world_bible.cache_clear()
    adjudicator_system.cache_clear()
    companion_system.cache_clear()
