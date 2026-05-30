# CLAUDE.md

Operativer Anker für Claude Code. Vollständiges Konzept in `DESIGN.md` — dort
nachschlagen, hier nicht duplizieren.

## Projekt
Top-Down-Survival-Sim auf OSM-Basis. Single-Player. Ein Virus tötet fast alle
Menschen; der Spieler überlebt, baut über Phasen eine Gemeinschaft auf.

## EISERNES PRINZIP (niemals brechen)
Drei Schichten, strikt getrennt:
1. **Sim-Kern** = einzige Quelle der Wahrheit. Nur er schreibt die DB.
2. **Datenbank** = persistenter State.
3. **LLM** = schlägt nur vor, schreibt NIE direkt. Output wird auf *Intentionen*
   geparst, nie auf behauptete *Zustände*.

→ Folge: Kein Akteur kann Ressourcen/Fakten "herzaubern". Halluzinationen sind
wirkungslos, weil nur validierter Sim-Kern-Code die DB ändert. Wenn du irgendwo
versucht bist, LLM-Output direkt in die DB zu schreiben: STOPP. Das bricht das
Prinzip und ist nie erlaubt.

## Tech-Stack
- Backend: Python + FastAPI
- DB: SQLite (Schema in `schema.sql`)
- Frontend: Browser, OSM via Leaflet/MapLibre (später; nicht Teil von Schritt 1)
- LLM: Claude API hinter Abstraktionsschicht (erst ab Schritt 2 relevant)

## AKTUELLE PHASE: Schritt 1 — deterministischer Sim-Kern, KEIN LLM
In Scope:
- Ein OSM-Viertel laden, Locations als Footprint + Typ ablegen
- Zeit-Tick (Sim-Kern), Phasen-Schleife
- Lazy Generation: Inventar wird erst bei Entdeckung erzeugt (deterministischer
  Seed pro Location). Vorher existieren keine Inventar-Zeilen.
- Eine Bedürfnis-Achse: Hunger (Rest als Spalten vorbereitet, aber nicht aktiv)
- Plündern = Transfer Location → Spieler-Gruppe (bilanzneutral)
- Decay für Nahrung

NICHT in Scope (später):
- LLM, Adjudikator, Agenten, NPCs, Companions, Civ — alles weglassen
- Frontend-Politur — minimaler Renderer reicht

## Sim-Kern Phasen-Reihenfolge pro Tick (einhalten)
1. Physik/Welt fortschreiben (Zeit, Wetter, Decay)
2. Ressourcen fortschreiben (Verbrauch, Verderb, Lagerkapazität)
3. Biologie (Bedürfnisse, Performance, Sterbe-Check)
4. (Schritt 2+: Agenten-Ticks)
5. Interrupts sammeln
Niemals Agenten/LLM parallel zur Welt-Berechnung — erst Welt fertig, dann reagieren.

## Konventionen
- Jede Ressourcen-Änderung läuft durch eine Sim-Kern-Funktion, nie inline.
- Crafting/Transfer = DB-Transaktion (atomar, commit-or-rollback).
- Nach jedem Tick: Bilanz-Prüfung (Gesamt-Ressourcen vs. Quellen/Senken) als
  Sicherheitsnetz gegen Erzeugung aus dem Nichts.
- LLM-Aufrufe immer hinter `llm/` Abstraktionsschicht, nie verstreut.
- Tests für den Sim-Kern: deterministisch, reproduzierbar über fixen Seed.

## Validierungs-Gate
Schritt 1 gilt als erfolgreich, wenn man im Viertel laufen, ein Gebäude betreten,
Inventar (lazy generiert) plündern, die Items behalten und über Zeit hungrig
werden kann — und ein erneuter Besuch denselben (ggf. geplünderten) Zustand zeigt.
Erst danach Schritt 2 (Adjudikator).
