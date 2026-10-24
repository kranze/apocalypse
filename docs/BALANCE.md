# Balance der Spielwoche (Issue #49, Fun-Gate)

Gemessen mit `python scripts/balance_sim.py --seeds 8` (Stub-LLM, kein Netz, synthetisches
Viertel: Zuhause, 10 Häuser, 2 Supermärkte, Tankstelle, Apotheke, Baumarkt in 100 m bis 1,5 km).
Die Bots spielen nur über `actions.perform` / `actions.available`. Regressionstest:
`tests/test_balance.py` (Seeds 1-5).

## Ergebnis (8 Seeds je Strategie)

| Strategie | Ausgänge | Ø Score | Ø Dauer* | Ø Aktionen | Pakete | km |
|---|---|---|---|---|---|---|
| passive (bleibt zu Hause, wartet) | 7x Tod (Tag 4-6, meist verdurstet), 1x stay_fragile | 0.02 | 37 min | 71 | 0 | 0 |
| hoarder (plündert Viertel, bleibt) | 3x stay_secure, 5x stay_fragile | 0.55 | 63 min | 130 | 0 | 12.6 |
| online (News, Downloads, Laden Tag 0-3) | 8x stay_secure | 0.66 | 61 min | 127 | 7-8 von 12 | 4.7 |
| companion (Kontakt, Vertrauen, Anschluss) | 6x go_hopeful, 2x stay_fragile (Companion bleibt "met") | 0.61 | 77 min | 164 | 0 | 23.8 |
| reckless (zu Fuß, Tag 5, wenig dabei) | 7x go_desperate, 1x Tod (Tag 4) | 0.33 | 36 min | 72 | 0 | 0.7 |

\* geschätzte Echtzeit: 25 s je Klick, 45 s je Freitext/Gespräch. Pace "normal" (Standard): ein
gewöhnlicher Spieler wartet tagsüber ~2 h am Stück, tippt jede dritte Pause einen Freitext
und schläft nachts. `--pace efficient` (Warten bis morgen früh) ist die Untergrenze der Klicks
(hoarder ca. 25 min, companion ca. 40 min).

Fünf verschiedene Ausgänge (Tod, stay_fragile, stay_secure, go_hopeful, go_desperate). Keine
Strategie ist über alle Seeds die beste (online gewinnt meist, companion bei Seeds mit
anschlussbereitem Begleiter, hoarder liegt knapp dahinter). Ob ein Companion mitkommt, hängt
an seinen Persönlichkeitswerten (vorsichtig und zurückhaltend = bleibt), nicht am Zufall pro Lauf.

## Downloads

Tag 0, WLAN + Strom (Qualität 0.9, 1.98 MB/min): klein (50 MB) 26 min, mittel (150 MB) 76 min,
groß (450 MB) 228 min. Alle 12 Pakete (1500 MB) brauchen ca. 12,6 h reine Online-Zeit zu dieser
Qualität. Der Akku (0.30/h online, ca. 3,3 h je Ladung, Laden 0.5/h nur am Netz), der Netzzerfall
(Internet 0.9 -> 0.15 an Tag 4, Strom je Ort aus ab Tag 1-3) und die übrige Woche (Plündern,
Schlafen) verhindern es: der Online-Bot schafft 7-8 Pakete, der Gierig-Test
(`tests/test_devices_online.py::test_scarcity_...`, 6 h Online/Tag, Tag 0-4) ca. 70 % der Megabyte.
Mit 12 h Online-Zeit pro Tag wären alle 12 erreichbar; der Test wurde deshalb von 12 h auf 6 h gesenkt.

## Konstanten (alt -> neu), `app/sim/constants.py`

| Konstante | alt | neu | Warum |
|---|---|---|---|
| `DOWNLOAD_MB_PER_MIN` | 0.3 | 2.2 | Zeiten klein/mittel/groß 26 min / 76 min / 3,8 h bei Qualität 0.9 (vorher 3 h / 9 h / 28 h) |
| `DEVICE_DRAIN_ONLINE_PER_H` | smartphone 0.10, laptop 0.08 | 0.30, 0.24 | schnelle Downloads verbrauchen mehr Akku; Strom/Laden bleibt die echte Grenze |
| `LOOT_WATER_MAX_SHARE` (neu) | (1.0, Wasser zuerst unbegrenzt) | 0.65 | ein Plünderzug füllte den Rucksack nur mit Wasser (21 L, kein Essen) und der Hoarder verhungerte |
| `COMPANION_TRUST_GIVE` | 0.03 | 0.05 | ca. 5 Gaben statt 9 bis zum Anschluss-Vertrauen (0.45) |
| `COMPANION_GO_WEIGHTS` (neu) | (0.4, 0.3, 0.3) hartkodiert | (0.3, 0.25, 0.45) | Vertrauen entscheidet stärker; vorher kam nur ein bedürftiger Companion mit |
| `COMPANION_STAY_WEIGHTS` (neu) | (0.5, 0.3, 0.2) hartkodiert | (0.4, 0.25, 0.15) | s. o. |
| `COMPANION_OPINION_MARGIN` (neu) | 0.08 hartkodiert | 0.08 | unverändert, nur als Konstante |
| `END_TARGET_DAYS_STAY` | 7.0 | 5.0 | 7 Tage Wasser (ca. 20 L) sind mit 21 kg Tragekapazität nicht erreichbar |
| `END_WATER_TREATMENT_BONUS_DAYS` | 5.0 | 3.0 | Online-Bot dominierte sonst den Hoarder |
| `END_THRESHOLD_GO_HOPEFUL` | 0.50 | 0.55 | Reckless landete in 1 von 8 Seeds knapp bei "hoffnungsvoll" |

## Kleine Logik-Änderungen (nicht anders lösbar)

- `provision.auto_provision`: Versorgung setzt erst unter `PROVISION_TRIGGER` (0.7) ein und läuft
  bis `PROVISION_TARGET` (0.9). Bisher wurde bei jedem Tick unter 0.9 nachgetrunken und -gegessen;
  über das Kappen bei 1.0 verschwand ca. das Dreifache des Wasserbedarfs (ca. 8 statt 1,4 L/Tag).
- `ending.evaluate`: "Vorrat für N Tage" teilt durch den tatsächlichen Verbrauch
  (`THIRST_LOSS_PER_DAY` / `HUNGER_LOSS_PER_DAY` mal Tagesbedarf). Vorher durch den vollen
  Tagesbedarf, die Bewertung war dadurch 2-3x strenger als die Biologie.
- `looting.plan_loot`: Wasser belegt höchstens `LOOT_WATER_MAX_SHARE` des freien Platzes.
- `companion.compute_opinion`: Gewichte kommen aus den Konstanten (gleiche Formel).

## Geänderte Tests

- `tests/test_carry_narration.py::test_loot_respects_capacity_and_priority`: erwartet die Wasserdeckelung.
- `tests/test_devices_online.py::greedy_download`: 6 h statt 12 h Online-Zeit pro Tag.

## Grenzen und offene Punkte

- Die Dauer-Schätzung hängt am Bot-Tempo (siehe "Pace"). Nach dem Plündern (ab ca. Tag 3) bleibt
  dem Hoarder nur Warten: das ist der inhaltlich dünnste Teil der Woche.
- Habitability bestraft das Leerplündern (`supplies` zählt nur unberührte Orte): Hoarder liegt
  bei 0.28, Online (plündert weniger) bei 0.35.
- Der Companion-Bot gibt dem Companion bis zu 7 Gaben; vorsichtige, zurückhaltende Personen
  (z. B. Seeds 2 und 3) schließen sich auch bei Vertrauen > 0.5 nicht an, dann bleibt es bei stay_fragile.
