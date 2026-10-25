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

**Einzige kontrollierte Ausnahme — Spielerwissen (#54):** Der Spieler darf
plausible Gegenstände in seinem eigenen Haus und bei Nachbarn (Umkreis
`DECLARE_RADIUS_M`) deklarieren („im Keller steht ein 100-kWh-Akku“). Das LLM
beurteilt nur Plausibilität und schlägt Eigenschaften vor; der Sim-Kern klemmt,
legt an und bucht im Ledger als eigene Quelle `player_declared`. Kein anderer
Pfad darf Items erzeugen — die Suche erfindet weiterhin nichts.

## Tech-Stack
- Backend: Python + FastAPI
- DB: SQLite (Schema in `schema.sql`)
- Frontend: Browser, OSM via Leaflet (`app/web/`)
- LLM: Claude API hinter Abstraktionsschicht `app/llm/` (Claude, CLI, Stub)

## Stand
Umgesetzt: deterministischer Sim-Kern (Tick, Hunger/Durst/Schlaf, Lazy-Inventar,
Plündern, Decay, Bilanz), OSM-Laden mit Chunk-Streaming und Straßengraph, erste
Adjudikator-Schleife mit Knowledge Base und Capabilities, prozedurale
Überlebenden-Identität und eine globale Überlebenden-Sim mit Debug-Weltkarte.

## AKTUELLE PHASE: Vertical Slice „Die erste Woche"
Plan: `docs/SLICE_WOCHE1.md`. Meilensteine M0–M5 = Issues #44–#49 (Label
`slice-woche1`). Ziel: ein in 60–90 Minuten spielbarer Durchlauf, der beweist,
dass das Konzept Spaß macht — erst Tiefe, dann Breite.

In Scope:
- Aktionsbasierte Zeit: jede Aktion kostet Spielzeit, dann rückt die Welt vor
- Hybrid-Interaktion: Routine per Klick, Kreatives per Freitext an den Adjudikator
- Deterministische Verfalls-Timeline (Strom, Wasser, Mobilfunk, Internet)
- Online-Phase: Geräte mit Akku, Wissens-Downloads aus kuratiertem Katalog
- Adjudikator v2 als Tool-Use-Schleife (jeder Vorschlag vom Sim-Kern validiert)
- Genau ein Companion mit Dialog, Gedächtnis und eigenen Bedürfnissen
- Tag 7: „bleiben oder gehen", Bewertung durch Sim-Kern, Epilog durch LLM

NICHT in Scope (eingefroren bzw. später):
- Ausbau der globalen Überlebenden-Sim (bleibt bestehen, liefert den Companion;
  der automatische Tagesschritt ist per `config.SURVIVOR_SIM_ENABLED` bzw.
  `WASTELAND_SURVIVOR_SIM_ENABLED` standardmäßig aus)
- Ausbau von Debug-Weltkarte und Chunk-Streaming
- Grid-Sicht, Kampf, Tierökologie, mehrere Companions, Phasen nach Woche 1, Civ

## Sim-Kern Phasen-Reihenfolge pro Tick (einhalten)
1. Physik/Welt fortschreiben (Zeit, Bewegung, Wetter, Verfall)
2. Ressourcen fortschreiben (Verbrauch, Verderb, Lagerkapazität)
3. Biologie (Bedürfnisse, Performance, Sterbe-Check)
4. Agenten-Ticks (Companion, ab M4)
5. Interrupts sammeln, Bilanz-Prüfung
Niemals Agenten/LLM parallel zur Welt-Berechnung — erst Welt fertig, dann reagieren.

## ARBEITSWEISE: Orchestrieren, nicht selbst implementieren (Pflicht)
Der Haupt-Claude (Opus) **orchestriert nur** und schont seinen Kontext. Echte
Implementierungsarbeit wird **delegiert**:
1. Jede nicht-triviale Aufgabe zuerst als **GitHub-Issue** anlegen (`gh issue
   create`), klar abgegrenzt, mit Akzeptanzkriterien und betroffenen Dateien.
2. Umsetzung an **dedizierte Sub-Agenten** geben — Modell nach Aufwand:
   **Haiku** für mechanische/kleine Änderungen, **Sonnet** für normale Features
   und Tests. Opus implementiert selbst nur in Ausnahmefällen.
3. Der Haupt-Claude liest **nicht** ganze Dateien zum Selbst-Coden ein, sondern
   gibt Agenten präzise Aufträge (Issue-Nummer, Dateien, Vertrag) und prüft nur
   deren **Ergebnis** (Diff-Zusammenfassung, Tests grün, Issue schließen).
4. Test-Agenten fassen ausschließlich `tests/` an, Implementierungs-Agenten nur
   die im Auftrag genannten Dateien. Eisernes Prinzip gilt für alle.
5. Parallele, unabhängige Aufträge in **einer** Nachricht starten (nebenläufig).

## Konventionen
- Jede Ressourcen-Änderung läuft durch eine Sim-Kern-Funktion, nie inline.
- Crafting/Transfer = DB-Transaktion (atomar, commit-or-rollback).
- Nach jedem Tick: Bilanz-Prüfung (Gesamt-Ressourcen vs. Quellen/Senken) als
  Sicherheitsnetz gegen Erzeugung aus dem Nichts.
- LLM-Aufrufe immer hinter `llm/` Abstraktionsschicht, nie verstreut.
- Tests für den Sim-Kern: deterministisch, reproduzierbar über fixen Seed.

## Validierungs-Gate
Jeder Meilenstein endet spielbar. Der Slice gilt erst als Erfolg, wenn das
Fun-Gate in `docs/SLICE_WOCHE1.md` §7 erreicht ist. Fällt es durch, wird am Kern
nachgebessert — keine neuen Systeme in die Breite, bis es besteht.
