# Vertical Slice „Die erste Woche"

> Plan für den ersten wirklich spielbaren Ausschnitt. Ergänzt `DESIGN.md`, ersetzt
> es nicht. Ziel: beweisen, dass das Kernkonzept Spaß macht, **bevor** weiter in
> die Breite gebaut wird.

---

## 1. Das Spielerlebnis in einem Absatz

Tag 0, morgens. Du wachst in deiner echten Wohnung auf, das Handy ist voll mit
Nachrichten, die mitten im Satz abbrechen. Strom, Wasser und Internet laufen noch
— aber du weißt nicht, wie lange. Sieben Tage lang entscheidest du, was du mit der
schwindenden Infrastruktur anfängst: Vorräte bunkern, Wissen herunterladen, im
Netz nach anderen Überlebenden suchen. Irgendwann antwortet jemand. Am Ende der
Woche ist die Stadt kaum noch bewohnbar, und du musst entscheiden: **bleiben oder
gehen** — und womit.

Spieldauer eines Durchlaufs: **60–90 Minuten Echtzeit.**

---

## 2. Festgelegte Kernentscheidungen

| Frage | Entscheidung |
|-------|--------------|
| Interaktion | **Hybrid**: Routine per Klick (gehen, plündern, essen, trinken, schlafen, warten), Freitext an den Adjudikator für alles Kreative |
| Zeitmodell | **Aktionsbasiert**: jede Aktion kostet Spielzeit, danach rückt die Welt um genau diese Spanne vor |
| Begegnung | **Ein Companion** vor Ort, erreichbar ab Tag 2–4, mit LLM-Dialog, Gedächtnis und eigenen Bedürfnissen |
| Ziel | **Bleiben oder gehen**: Tag 7 endet mit der Entscheidung, ihrer Vorbereitung und einem Epilog |

---

## 3. Dramaturgie der Woche

Die Woche ist kein Skript, sondern eine **deterministische Verfalls-Timeline**
(Sim-Kern, fixer Seed + lokaler Jitter). Spannung entsteht, weil Ressourcen und
Möglichkeiten zeitlich begrenzt sind.

| Tag | Welt | Was der Spieler spürt |
|-----|------|-----------------------|
| 0 | Alles läuft noch. Nachrichten verstummen. | Schock, Orientierung. Viel möglich, wenig Wissen. |
| 1 | Erste Stromausfälle in Teilnetzen. Mobilfunk überlastet/lückenhaft. | „Jetzt oder nie" für Online-Aktionen. |
| 2 | Wasserdruck fällt. Netzstrom bricht großflächig zusammen. | Leitungswasser weg, Kühlschränke werden warm. |
| 3 | Mobilfunkmasten auf Notstrom, fallen einzeln aus. Erster Kontakt möglich. | Ein Lebenszeichen — Hoffnung. |
| 4 | Internet nur noch über Reste (Akkus, einzelne Knoten). | Begegnung mit dem Companion wird realistisch. |
| 5 | Netz praktisch tot. Verwesung macht sich bemerkbar. | Gekühlte Nahrung verdorben, Stadt beginnt zu stinken. |
| 6 | Bewohnbarkeit der Innenstadt sinkt deutlich. | Druck zur Entscheidung. |
| 7 | Entscheidungstag. | Bleiben oder gehen, Epilog. |

Konkrete Schwellen liegen als Konstanten in `app/sim/constants.py` (siehe
Issues #5/#7) und werden um **Mobilfunk** und **Internet** erweitert.

---

## 4. Die Kernschleife

```
Spieler wählt Aktion (Klick oder Freitext)
   → Sim-Kern prüft Machbarkeit + bestimmt Zeitkosten
   → Aktion wird atomar angewendet
   → Welt rückt um die Zeitkosten vor (advance_tick in Teilschritten)
   → Interrupts? (Durst, Kontakt, Netzausfall, Geräusch …) → anzeigen, ggf. Entscheidung
   → nächste Aktion
```

„Warten" und „Schlafen" sind Aktionen, die `fast_forward` bis zu einem Ziel oder
dem nächsten Interrupt nutzen. Es gibt keine laufende Uhr, also keinen Zeitdruck
beim Tippen.

### Zeitkosten (erste Kalibrierung)

| Aktion | Kosten |
|--------|--------|
| Gehen | Strecke / Gehtempo (besteht: `WALK_SPEED_M_PER_MIN`) |
| Gebäude durchsuchen | 20–60 min nach Größe/Typ |
| Essen / Trinken | 10–20 min |
| Zubereiten | 20–40 min |
| Nachrichten lesen / Online-Suche | 15–30 min |
| Wissen herunterladen | nach Paketgröße und Netzqualität |
| Gespräch mit Companion | 10 min pro Austausch |
| Schlafen | bis ausgeruht oder Interrupt |
| Freitext-Aktion | vom Adjudikator vorgeschlagen, vom Sim-Kern geklemmt |

---

## 5. Systeme

### 5.1 Was wiederverwendet wird

| Baustein | Wo | Rolle im Slice |
|----------|----|----------------|
| Tick-Schleife, Phasen-Reihenfolge | `app/sim/tick.py` | unverändert, nur anders aufgerufen |
| Hunger, Durst, Schlaf, Performance, Sterbe-Check | `app/sim/biology.py` | Überlebensdruck |
| Auto-Versorgung | `app/sim/provision.py` | kein Micromanagement |
| Lazy-Inventar, Plündern, Decay, Bilanz | `generation`, `looting`, `resources`, `audit`, `ledger` | Ressourcen-Ökonomie |
| OSM-Viertel + Straßengraph | `app/osm/` | die Heimatstadt |
| Adjudikator + Effekte + KB + Capabilities | `app/sim/adjudicator.py`, `effects.py`, `kb.py`, `capabilities.py` | Basis für Freitext |
| Prozedurale Identität | `app/sim/identity.py`, `locale.py` | Companion-Grundgerüst |
| SSID-Beacon-Capability | `app/sim/capabilities.py` | ein Weg, gefunden zu werden |

### 5.2 Was neu gebaut wird

**A. Aktionsbasierte Zeit**
- Ein zentraler Aktions-Endpunkt: Aktion rein, Zeitkosten bestimmen, anwenden,
  Welt vorrücken, Ergebnis + Interrupts zurück.
- Zeitkosten als Konstanten; der Adjudikator darf für Freitext nur einen Wert
  innerhalb fester Grenzen vorschlagen.

**B. Infrastruktur-Verfall** (bestehende Issues #5, #7, erweitert)
- `location_infra` für Strom, Wasser, Kühlung (wie in #5).
- Neu: weltweite Werte für **Mobilfunk** und **Internet** (Qualität 0..1, pro Tag).
- Harte Gates (#7): kein Leitungswasser ohne Druck, schnellerer Verderb ohne
  Kühlung, Geräte brauchen Strom oder Akku.

**C. Die Online-Phase (der eigentliche Aufhänger)**
- Geräte als Items mit Akkustand: Handy, Laptop, Powerbank. Laden nur mit Strom.
- Online-Aktionen, abhängig von Netzqualität und Akku:
  - **Nachrichten lesen**: liefert Lore-Fragmente und Hinweise auf die Lage.
  - **Nach Überlebenden suchen**: kann den ersten Kontakt zum Companion auslösen.
  - **Wissen herunterladen**: Pakete aus einem **kuratierten Katalog** (z. B.
    „Wasser aufbereiten", „Erste Hilfe", „Fahrrad reparieren", „Offline-Karten").
    Ein Download schreibt KB-Einträge bzw. schaltet Rezepte frei.
- Eisernes Prinzip: Das LLM kann keine Downloads erfinden. Was es gibt, steht im
  Katalog. Was heruntergeladen ist, entscheidet der Sim-Kern.
- Kernspannung: Akku und Netzzeit reichen nicht für alles. **Was du vor Tag 5
  herunterlädst, bestimmt, was du am Tag 7 kannst.**

**D. Adjudikator v2 (Tool-Use)**
- Statt einmal Kontext → einmal Effektliste: das Modell bekommt
  **Lese-Werkzeuge** (Orte in der Nähe, Inventar, Gebäudeinhalt, KB-Abfrage,
  Infrastruktur) und **Vorschlags-Werkzeuge** (je ein Werkzeug pro Effekt).
- Jeder Vorschlag wird vom Sim-Kern einzeln validiert; das Modell sieht die
  Ablehnung und kann umplanen. Angewendet wird am Ende atomar.
- Structured Outputs für alle Vorschläge, damit keine Parse-Fehler mehr auftreten.
- Effekt-Vokabular für den Slice erweitern, z. B.: Gerät laden, Wasser abfüllen,
  Tür aufbrechen, Zettel/Nachricht hinterlassen, Ort verbarrikadieren, Fahrzeug
  prüfen.
- Player-Override verallgemeinern: nicht nur `provides:heat`, sondern jede
  `provides:*`-Anforderung.
- Bleibt hinter `app/llm/`; der Stub muss jeden Pfad deterministisch abdecken,
  damit Tests ohne Netz laufen.

**E. Der Companion**
- Ein einzelner Überlebender, materialisiert in der Nähe des Spielers, mit
  prozeduraler Identität und LLM-Profil (#16).
- Erster Kontakt aus der Ferne (Online-Suche, SSID-Bake, Zettel, Geräusch), dann
  Treffen vor Ort.
- Hat eigene Bedürfnisse über die bestehende Biologie. Kann hungrig, krank oder
  misstrauisch sein.
- Dialog: Der Companion **spricht frei**, aber seine **Handlungen** sind
  Intentionen, die durch dieselbe Adjudikator-Pipeline laufen wie die des
  Spielers (folgen, Essen teilen, Gegenstand geben, sich weigern, gehen).
- Gedächtnis: `chat_log` plus eine kompakte, vom Sim-Kern gespeicherte
  Zusammenfassung pro Tag.
- Wissens-Limitierung im Prompt: Er weiß nur, was er gesehen hat oder ihm gesagt
  wurde.
- Hat eine eigene Meinung zu „bleiben oder gehen" — er kann sich der Entscheidung
  anschließen oder nicht.

**F. Bleiben oder gehen + Epilog**
- **Bewohnbarkeit** des Viertels als deterministischer Wert aus Verwesung,
  Wasser, Strom und Vorräten. Sichtbar im HUD.
- Tag 7: Entscheidungsbildschirm.
  - **Bleiben**: Bilanz aus Vorräten, Wasserquelle, Wissen, Schutz.
  - **Gehen**: Wohin (Richtung, Ziel aus Kontakten) und womit (zu Fuß, Fahrrad,
    Auto mit Sprit). Bilanz aus Ausrüstung, Wissen, Gesundheit, Begleitung.
- Epilog: kurzer, vom LLM erzählter Ausblick auf Basis einer **vom Sim-Kern
  berechneten** Bewertung. Das LLM erzählt nur, es bewertet nicht.

**G. Frontend**
- HUD: Tag und Uhrzeit, Bedürfnisse, Akku, Balken für Strom/Wasser/Mobilfunk/
  Internet, Bewohnbarkeit (deckt #8 ab).
- Aktionsleiste mit Klickaktionen je nach Kontext (in Gebäude, auf der Straße,
  mit Gerät).
- Freitextfeld für den Adjudikator, Erzähl-Log mit Ereignissen.
- Companion-Panel: Zustand, Beziehung, Gespräch.

### 5.3 Was eingefroren wird

- **Globale Überlebenden-Sim**: bleibt bestehen und liefert den Companion, wird
  aber nicht weiter ausgebaut. Läuft im Slice nur so weit, wie für die Umgebung
  des Spielers nötig.
- **Debug-Weltkarte**, weiteres **Chunk-Streaming**: kein Ausbau.
- Nicht im Slice: Grid-Sicht, Kampf, Tierökologie, mehrere Companions,
  Phasen-Kompression jenseits von Woche 1, Civ.

---

## 6. Meilensteine (jeder endet spielbar)

| # | Meilenstein | Ergebnis |
|---|-------------|----------|
| M0 (#44) | **Aufräumen** | CLAUDE.md und DESIGN.md auf aktuellen Stand (Phase, Modelle); globale Survivor-Sim hinter Konfig-Schalter |
| M1 (#45) | **Allein überleben** | Aktionsbasierte Zeit, Aktionsleiste, HUD. Man kann 7 Tage allein überleben oder sterben |
| M2 (#46) | **Das Netz stirbt** | Verfalls-Timeline (#5, #7), Geräte mit Akku, Online-Aktionen, Wissens-Katalog |
| M3 (#47) | **Adjudikator v2** | Tool-Use-Schleife, erweitertes Effekt-Vokabular, allgemeiner Override |
| M4 (#48) | **Der Companion** | Kontakt, Treffen, Dialog, gemeinsame Bedürfnisse, Gedächtnis (#16, #17) |
| M5 (#49) | **Bleiben oder gehen** | Bewohnbarkeit, Entscheidungstag, Epilog, Balancing über mehrere Durchläufe |

M3 kann parallel zu M2 laufen, da die Dateien weitgehend getrennt sind
(`app/sim/adjudicator.py`, `effects.py`, `app/llm/` vs. Verfall/Geräte).

---

## 7. Woran wir merken, ob es funktioniert (Fun-Gate)

Nach M5 wird der Slice mehrfach durchgespielt. Er gilt als Erfolg, wenn:

- Ein Durchlauf 60–90 Minuten dauert und sich nicht wie Warten anfühlt.
- Es mindestens **drei spürbar verschiedene Ausgänge** gibt, die von Entscheidungen
  abhängen und nicht vom Zufall.
- Die Wissens-Downloads echte Zielkonflikte erzeugen („Erste Hilfe oder
  Offline-Karten?").
- Der Companion sich wie eine Person anfühlt und nie etwas „herzaubert".
- Freitext öfter überrascht als frustriert. Messbar über eine Quote abgelehnter
  Freitext-Aktionen pro Durchlauf.
- Man ihn ein zweites Mal spielen will.

Fällt das Gate durch, wird am Kern nachgebessert, nicht in die Breite gebaut.

---

## 8. Offene Fragen

- **Startort**: frei wählbar wie bisher, oder für das Balancing zunächst ein
  festes Testviertel?
- **Sprache und Ton** der Erzählung: nüchtern-dokumentarisch oder literarisch?
- **LLM-Budget pro Durchlauf**: Welches Modell für Adjudikator, Companion und
  Epilog? Eine Obergrenze pro Durchlauf festlegen.
- **Inhalt des Wissens-Katalogs**: welche 10–15 Pakete, und was schaltet jedes
  konkret frei?
- **Tod vor Tag 7**: einfach Game Over mit Epilog, oder Neustart vom letzten Tag?
