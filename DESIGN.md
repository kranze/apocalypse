# Projekt "Wasteland" – Design- und Architektur-Dokument

> Lebendes Dokument. Hält die Designentscheidungen fest, die im Konzeptgespräch
> getroffen wurden. Kein fertiges Konzept, sondern eine versionierbare Basis.

---

## 1. Vision

Single-Player, Top-Down, Hardcore-Survival-Simulation in der Tradition von
Dwarf Fortress / RimWorld / Cataclysm: DDA. Ein Virus tötet binnen 24h fast die
gesamte Menschheit. Der Spieler überlebt durch eine genetische Besonderheit.
Weltweit verbleiben **~100.000 Überlebende**, geografisch verstreut.

- Ziel offen: kurzfristig überleben → andere finden → Gemeinschaft aufbauen →
  über Generationen Zivilisation wiederbeleben.
- Spielwelt basiert auf **OpenStreetMap**. Startpunkt frei wählbar.
- Demografische Realität: 100k liegt nahe der Minimum Viable Population.
  Regionale Cluster (50–500) sind nicht selbsterhaltend → Vernetzung wird
  zur biologischen Notwendigkeit, nicht nur sozialem Wunsch.

---

## 2. Architektur-Grundprinzip (nicht verhandelbar)

Drei Schichten, strikt getrennt:

1. **Simulationskern** – deterministischer Code. EINZIGE Quelle der Wahrheit
   für Welt-Zustand, Ressourcen, Zerfall. Schreibt die Datenbank fort.
2. **Datenbank** – der persistente State. Wird ausschließlich vom Sim-Kern
   verändert.
3. **LLM-Schicht** – schlägt nur vor, schreibt NIE direkt. Liefert Intentionen
   (Agenten) oder Bewertungen (Adjudikator), die der Sim-Kern validiert.

**Konsequenz:** Ein Agent kann nichts "herzaubern". Halluzinierte Ressourcen,
Aktionen oder Fakten haben keine Wirkung, weil nur validierter Sim-Kern-Code
die Datenbank ändert. LLM-Output wird auf *Intentionen* geparst, nie auf
behauptete *Zustände*.

---

## 3. Zwei-Sichten-Modell

- **Weltsicht**: OSM-Karte, Top-Down, Symbole für Fahrzeuge/NPCs, Zeit, Wetter.
- **Grid-Sicht**: Tile-Planungsraster, öffnet bei Klick auf ein Objekt.
  - 1 m² Raster für Gebäude/Lager
  - 0,5 m² Raster für Fahrzeuge
  - Einheitlich für alle Innenräume, Fahrzeuge, Eigenbauten
  - Volle Draufsicht, keine Isometrie (Konsistenz mit OSM-Weltsicht)
- Fahrzeug fährt = Symbol in Weltsicht; Grid ist reines Planungsraster ohne
  Umgebung, öffnet nur stationär.
- Taktische Konflikte spielen im Grid. Weltereignisse kommen als Interrupt.

### Grafik
Bewusst minimalistisch (DF/RimWorld-Abstraktion). Heavy Content (Leichen,
Gewalt) abstrahiert zu Tile + Text. Emotionale Last trägt die Text-Narration.

---

## 4. Zeit & Phasen (gestaffelte Kompression)

| Phase | Zeitskala | Fokus |
|-------|-----------|-------|
| 1 (Wochen) | Stunde ≈ Tag | Infrastruktur lebt: Wissen downloaden, Überlebende online suchen, bunkern, raus aus Ballungsraum |
| 2 (Monate) | Stunde ≈ Woche | Kollaps: Mobilität, andere finden, signalisieren, erstes Lager |
| 3 (Jahre) | Stunde ≈ Monat | Stabilisierung: Gruppe, Nahrung, Energie |
| 4+ (Generationen) | Stunde ≈ Jahr | Wissenstransfer, Demografie, Wiederaufbau |
| 5+ (Civ) | variabel | Ministerial-Struktur, Tech-Wiederbelebung, Politik |

- Kontinuierlicher Zeitfluss, Pause + Mehrfachgeschwindigkeit.
- Fast-Forward muss **aggressiv interrupten** (Ereignisse, Schwellen), sonst
  reine Cutscene-Phasen.

---

## 5. Survival-Modell (realistische Biologie)

Fünf Bedürfnis-/Zustandsachsen pro Person: **Durst, Hunger, Schlaf,
Verletzung (akkumulativ), Exposition.**

- Keine binären Tode. Performance degradiert, Penalties stapeln multiplikativ.
- Erst bei kritischer Performance → Sterbe-Wurf.
- Gruppenlogistik: jedes Mitglied verbraucht Ressourcen nach Aktivität/Gewicht.
- In Schock-/Trauma-Phasen dominiert ein emotionales State-Modell (Schock,
  Erschöpfung, Coping) über das strategische.

---

## 6. Ressourcen-Modell (Erhaltung)

Quellen (echte Erzeugung): biologische Produktion (Tiere/Pflanzen),
Wassergewinnung. Alles andere ist Transformation oder Transfer = bilanzneutral.
Senken: Verbrauch, Verderb, Verlust.

- **Lazy Generation**: Orte sind nur OSM-Footprint + Typ, bis sie entdeckt
  werden. Erst bei Betreten wird das Inventar generiert.
- **Deterministischer Seed pro Ort** (`hash(world_seed, location_id)`):
  selbe Entdeckung → selbes Ergebnis. Verhindert Mehrfach-Ausbeutung.
- Decay zum Entdeckungszeitpunkt eingerechnet; danach läuft Decay weiter.
- Plünderung = Transfer Welt-Ort → Gruppe. Wer zuerst kommt, löst Generierung
  aus; danach für alle im aktuellen Zustand.
- **Crafting/Transformation atomar** (DB-Transaktion: Input-Abzug + Output-
  Gutschrift zusammen oder gar nicht).
- **Bilanz-Prüfung** nach jedem Tick als Sicherheitsnetz gegen Erzeugung aus
  dem Nichts.

---

## 7. Agenten-System (Drei Schichten)

| Schicht | Wer | Kosten | Simulation |
|---------|-----|--------|------------|
| Datensatz | Masse der 100k | trivial | statistisch, kein LLM |
| Leichtgewicht | spielernah / Gruppe | gering | LLM nur bei Interaktion |
| Voller Agent | 10–20 aktiv | teuer | eigener Prompt, Memory, Ticks |

### Promotion / Demotion
- **Promotion** bei: struktureller Relevanz (Gruppe ≥ Größe, stabil ≥ Zeit)
  UND (räumlicher Nähe zum Spieler ODER narrativer Bemerkenswertigkeit).
- Bei Promotion: einmaliger LLM-Call (Opus) generiert Backstory → System-Prompt.
- **Demotion** bei Tod, Gruppenauflösung, dauerhafter Spieler-Distanz. State
  wird gespeichert, Agent deaktiviert, bleibt Datensatz; bei Bedarf re-promotbar.
- Budget bleibt konstant (~10–20 aktiv), Besetzung wechselt.
- Prinzip universell: gilt für externe Anführer, Companions, Gruppenmitglieder,
  später Minister.

### Modellwahl
- Routine-Ticks: Haiku 4.5 (günstig, hat in Tests überzeugend Charakter gehalten)
- Komplexe Adjudikation, Spieler-Dialog, Konsistenz-Audits, Backstory: Opus 4.8
- LLM hinter Abstraktionsschicht → Backend (API/lokal) tauschbar.

---

## 8. Der Adjudikator

Bewertet JEDE Intention (Spieler, Agent, NPC) – ein Universum, eine Regel-Schicht.

Reihenfolge: Knowledge Base (verbindlich) → Physik-Check → Ressourcen-Check →
LLM nur für echte Grenzfälle.

- Grundhaltung **"erlaube mit Risiko"** statt "verbiete bei Unsicherheit"
  (D&D-DM-Disziplin). Kreative Lösungen sind Feature, nicht Bug.
- **Player-Override**: Spieler kann Ablehnung mit Begründung anfechten →
  Korrektur wandert in die Knowledge Base (Provenance `player_verified`).
- **Eskalation an den Spieler** in zwei Fällen:
  - Plausibilitäts-Grenzfall (weder klar möglich noch unmöglich)
  - Mandats-Überschreitung (Agent will Entscheidung mit großer Tragweite)

### Agenten-Einschränkung (gegen Halluzination)
- Prompt liefert **kuratiertes Optionen-Set** aus der DB (nur real Verfügbares).
- Routine vs. Ausnahme: nur Ausnahme-Aktionen lösen volle Adjudikation aus.
- Wissens-Limitierung im System-Prompt explizit ("du weißt X NICHT").

---

## 9. Knowledge Base

Kuratierte DB mit verbindlichen Fakten zu Real-World-Komponenten (Fahrzeuge,
Hochvolt, Solar, Medizin). Provenance: `curated` > `player_verified` > `llm_inferred`.

- Bei unbekannten Komponenten: LLM-Websuche, Ergebnis wird gecacht.
- Spieler-Korrekturen reichern die KB an → das Spiel wird über die Spielerschaft
  schlauer ("modding on the fly").

---

## 10. Konfliktquellen (ohne Zombies)

- **Zeit / Infrastrukturzerfall** als Hauptantagonist.
- **Tierökologie**: Tag 1–7 verdurstende Haustiere → Wochen 4–8 Rattenboom +
  lose Hundegruppen → Monat 2–6 Rudel → Jahr 1+ Sukzession. (Tag-18-Realität:
  einzelne Streuner, noch keine koordinierten Rudel.)
- **Verwesungsökologie**: Städte über Wochen unbewohnbar, Krankheitsvektoren.
- **Demografischer Engpass** → Vernetzungsdruck.
- **NPCs kooperativ per Default**; Gewalt aus Stress, nicht Charakter.
- Stammestypen: Wiederaufbau / Opportunisten / Back-to-Roots / Ideologisch.
  Bei 100k random aber: kaum intakte Gruppen am Tag 0 — überwiegend Einzelne,
  die über Wochen Gruppen bilden.

---

## 11. Kommunikations- & Tracking-Tech (Gameplay-Layer)

- **GPS**: bleibt ~6–12 Monate genau, dann driftend; nutzbar Phase 1–3.
- **LoRa-Mesh**: regional (10–40 km/Knoten, mit Mesh 200–400 km Gebiet),
  Text-only, billig, jeder Stamm. Der Alltag.
- **WLAN-Richtfunk**: hochbandbreitig, Sichtlinie nötig, 5–50 km. Strategische
  Investition für Wissens-Pipelines.
- **CB-Funk**: 5–15 km (real, NICHT "weite Strecken").
- **HAM/Kurzwelle**: kontinental, selten, dramatische Fernkontakte.
- **Funkmasten** reaktivierbar → strategische Hochwert-Ziele.
- **WLAN-SSID-Beacon** ("KOMM_NACH_XXX") + Captive-Portal als Informationsbake.

---

## 12. Civ-Brücke (Spätspiel)

Kein separates Spiel, sondern emergente Antwort auf Skalierung großer Gruppen.

- < 20 Personen: Spieler führt direkt.
- 20–50: erste Bereichsverantwortliche (Delegation).
- 100–200: Ministerial-Struktur, Capability-Verwaltung pro Bereich.
- mehrere hundert: echte Politik, Fraktionen.
- Minister sind meist Capability-Sim (kein LLM); werden nur zum Agenten, wenn
  narrativ relevant (Krise, Spieler-Verhandlung, Konflikt).
- Civ-Tiefe = bewahrtes/angewandtes Wissen (Capability-Schicht als Tech-Tree).
- Baut auf derselben Promotions- und Capability-Logik wie alle früheren Phasen.

---

## 13. Tech-Stack

- Frontend: Browser, OSM via Leaflet/MapLibre. PWA (Chrome) als Start;
  Tauri-Wrapper optional später.
- Backend: Python + FastAPI.
- State: SQLite.
- LLM: Claude API (Haiku 4.5 Masse, Opus 4.8 für Anspruchsvolles), hinter
  Abstraktionsschicht. Lokales LLM (4070-tauglich, ~8B) nur für robuste
  Teilaufgaben sinnvoll; qualitativ klar unter Haiku.

---

## 14. Bekannte Risiken

1. LLM-Konsistenz unter gleichem Input — kalibrierbar, nicht eliminierbar.
2. Compute bei Multi-Agent — gestaffelte Komplexität, Sparse-Ticking.
3. OSM-Datenqualität regional schwankend — prozedurale Auffüllung.
4. UX bei Free-Text-Interaktion (Guess-the-Verb) — Affordance-Hinweise, Override.
5. Drama-Hyperinflation bei autonomen Agenten — Initiative-Limits, Anker in
   Alltag, narrative Gravitation beim Spieler.
6. **Scope vs. Ressourcen** — größtes Risiko. Realistische Erwartung: Hobby-
   Projekt erreicht die volle Tiefe wahrscheinlich nie. Baureihenfolge so wählen,
   dass jederzeit ein spielbarer Kern existiert.

---

## 15. Baureihenfolge (riskantestes zuerst)

0. **Design festhalten** (dieses Dokument) ✓
1. **Deterministischer Sim-Kern** (kein LLM): Zeit, Bedürfnisse, ein OSM-Viertel,
   3–4 Gebäude mit Lazy-Inventar, Hunger-Achse. Voll testbar, billig.
2. **Adjudikator-Schleife**: die riskanteste Komponente. Spieler-Intention →
   strukturierter Output → World-State-Delta. Hier zeigt sich, ob das Konzept
   trägt.
3. **Companion** (ein Agent, z.B. Klaus): Tick-Loop, Memory, Wissens-Limitierung.
4. **Externe Anführer + Promotion/Demotion**.
5. **Delegations-Hierarchie** (große Gruppen).
6. **Civ-Phase** (ganz am Ende, evtl. nie).

**Validierungs-Gate nach Schritt 2:** Fühlt sich die Adjudikator-Schleife
konsistent und gut an? Wenn nein, ist das Kernkonzept in Frage gestellt, bevor
viel Zeit investiert wurde.
