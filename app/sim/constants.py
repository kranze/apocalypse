"""Tunables des Sim-Kerns (Schritt 1).

Alle Spiel-Balance-Werte an einem Ort, damit Ticks deterministisch und
nachvollziehbar bleiben. Zeit wird in Spielminuten gerechnet (siehe schema.sql).
"""
from __future__ import annotations

# --- Zeit ---------------------------------------------------------------
TICK_MINUTES = 10        # Spielminuten, um die ein advance_tick() vorrückt
MINUTES_PER_DAY = 1440

# --- Aktionsbasierte Zeit (Slice M1) -------------------------------------
# Jede Spieleraktion kostet Spielminuten; danach rückt die Welt exakt um diese
# Spanne vor (in TICK_MINUTES-Schritten, Rest als letzter Teilschritt).
DAYS_TOTAL = 7                   # Länge der Überlebens-Woche
WALK_MAX_MINUTES = 360           # eine Geh-Aktion läuft höchstens so lange
LOOT_MIN_MINUTES = 20            # Plündern: kleines Gebäude ...
LOOT_MAX_MINUTES = 60            # ... großes Gebäude
LOOT_FOOTPRINT_MIN_M2 = 20.0     # Footprint, ab dem LOOT_MIN gilt
LOOT_FOOTPRINT_MAX_M2 = 500.0    # Footprint, ab dem LOOT_MAX gilt
LOOT_REACH_M = 40.0              # Plündern nur, wenn man am Gebäude steht
SEARCH_MIN_MINUTES = 15          # Durchsuchen: kleines Gebäude ... (Footprint wie beim Plündern)
SEARCH_MAX_MINUTES = 45          # ... großes Gebäude
EAT_MINUTES = 15
PREPARE_MINUTES = 30
WAIT_MIN_MINUTES = 10
WAIT_MAX_MINUTES = 1440          # 24 h, damit "Bis morgen früh" immer passt
WAKE_HOUR = 7                    # "Bis morgen früh" = nächste 07:00 Spielzeit
SLEEP_TARGET = 0.95              # Schlafen endet, sobald so ausgeruht
SLEEP_MAX_MINUTES = 720
FREETEXT_DEFAULT_MINUTES = 15    # Freitext ohne eigene Zeitkosten
FREETEXT_MAX_MINUTES = 240       # Klemme für advance_time-Effekte
ARRIVE_REACH_M = 15.0            # so nah am Ziel gilt man als "dort"

# --- Hunger -------------------------------------------------------------
# Sättigung 0..1 (1 = satt). Ohne Nahrung in ~3 Spieltagen von 1.0 auf 0.
HUNGER_LOSS_PER_DAY = 0.33
HUNGER_SOFT = 0.5        # Interrupt-Schwelle "wird hungrig"
HUNGER_CRIT = 0.2        # Interrupt-Schwelle "kritisch hungrig"

# --- Durst & Schlaf -----------------------------------------------------
# Ohne Trinken sinkt Durst schneller als Hunger (Dehydrierung ~ Tage).
THIRST_LOSS_PER_DAY = 0.55
# Schlafdruck im Wachzustand: ~1,5 Tage wach -> erschöpft.
SLEEP_LOSS_PER_DAY = 0.66
# Wasserbedarf: ml pro kg Körpergewicht pro Tag -> daily_water_l.
WATER_ML_PER_KG = 35.0
# Aktivität: zusätzlicher Wasserverlust pro km gelaufen (Anteil am Tagesbedarf).
THIRST_ACTIVITY_PER_KM = 0.02

# --- Auto-Versorgung (kein Micromanagement) -----------------------------
# Unter ...TRIGGER wird automatisch versorgt, bis ...TARGET erreicht (falls Vorrat).
PROVISION_TRIGGER = 0.7
PROVISION_TARGET = 0.9
PROVISION_SOURCE_RADIUS_M = 30.0   # "Umgebung" = entdeckter Ort in Reichweite
# Schlaf: müder Charakter ruht automatisch (Schlaf/Tag Erholung beim Ruhen).
SLEEP_RECOVERY_PER_DAY = 2.0
SLEEP_REST_BELOW = 0.5             # ab hier ruht ein untätiger Charakter

# --- Zufriedenheit ------------------------------------------------------
SATISFACTION_MIN_WEIGHT = 0.5      # das schwächste Bedürfnis zieht stärker
SATISFACTION_ISOLATION_PER_DAY = 0.05  # Einsamkeit (alle tot) drückt langsam
SATISFACTION_SHELTER_BONUS = 0.1   # in einem Gebäude = etwas Geborgenheit
SATISFACTION_ADJUST_PER_DAY = 1.0  # Tempo, mit dem sich die Achse dem Ziel nähert

# --- Performance --------------------------------------------------------
# Bei/über diesem Hunger keine Hunger-Penalty; darunter linear bis 0.
PERF_COMFORT_HUNGER = 0.5
# Unter dieser Gesamt-Performance beginnen Sterbe-Würfe (DESIGN.md §5).
CRIT_PERFORMANCE = 0.2
# Skalierung der Sterbe-Wahrscheinlichkeit; ~50% Tod/Tag bei Performance 0.
DEATH_K = 3.5

# --- Bewegung -----------------------------------------------------------
WALK_SPEED_M_PER_MIN = 83.0     # ~5 km/h Fußgeschwindigkeit (Spielminuten)
# Aktivitäts-Energie: kcal pro kg Gesamtgewicht (Körper + Last) pro km.
K_WALK_KCAL_PER_KG_KM = 0.5

# --- Decay / Verderb ----------------------------------------------------
# Qualität 0..1 nach Halbwertszeit. Fällt sie darunter -> Item verdirbt (Senke).
SPOIL_THRESHOLD = 0.1

# --- Infrastruktur-Verfall (Slice M2, Issue #5/#7) ----------------------
# Zeitachse in Tagen seit Kollaps (fraktional: tick / MINUTES_PER_DAY). Werte 1 = intakt.
# Strom (Netz): je Ort fällt er im Fenster [OFF-FALL, OFF] auf 0; OFF = MIN + Jitter*(MAX-MIN).
INFRA_POWER_OFF_DAY_MIN = 1.0     # frühester lokaler Totalausfall
INFRA_POWER_OFF_DAY_MAX = 3.0     # spätester: danach überall aus
INFRA_POWER_FALL_DAYS = 0.4       # Dauer des Flackerns/Abfalls vor dem Ausfall
# Wasserdruck: hält bis WATER_HOLD_DAY, fällt je Ort bis OFF (Jitter ersetzt Höhenlage).
INFRA_WATER_HOLD_DAY = 1.5
INFRA_WATER_OFF_DAY_MIN = 2.4
INFRA_WATER_OFF_DAY_MAX = 3.0
# Kühlung folgt Strom; unter dieser Schwelle gilt sie als ausgefallen.
INFRA_REFRIGERATION_WARM_BELOW = 0.25
# Leitungswasser nur auto-trinkbar, solange Wasserdruck über dieser Schwelle liegt.
WATER_TAP_MIN = 0.2
# Verderb ohne Kühlung: Halbwertszeit wird durch diesen Faktor geteilt (>1 = schneller).
SPOILAGE_WARM_FACTOR = 2.5
# Struktur / Überwucherung (lange Zeitskalen, Jitter 0.7..1.3).
INFRA_STRUCT_HOLD_DAYS = 90
INFRA_STRUCT_DECAY_DAYS = 3650
INFRA_OVERGROWTH_START_DAY = 30
INFRA_OVERGROWTH_FULL_DAYS = 730
# Weltweite Stützpunkte (Tag, Wert), linear interpoliert; danach letzter Wert.
INFRA_CELLULAR_POINTS = ((0.0, 0.6), (2.0, 0.55), (4.0, 0.3), (5.0, 0.02), (6.0, 0.0))
INFRA_INTERNET_POINTS = ((0.0, 0.9), (1.0, 0.9), (2.0, 0.6), (3.0, 0.35), (4.0, 0.15),
                         (5.0, 0.02), (30.0, 0.02), (60.0, 0.0))
# Globale Phasen-Labels: (Tag-Obergrenze exklusiv, Label)
INFRA_PHASES = (
    (1.0, "Alles läuft noch"),
    (2.0, "Netze wackeln"),
    (3.5, "Strom fällt"),
    (5.0, "Das Netz stirbt"),
    (30.0, "Stille"),
    (365.0, "Verwaisung"),
    (float("inf"), "Verwilderung"),
)

# --- Capabilities -------------------------------------------------------
# Chance pro Tag, dass ein aktiver SSID-Beacon einen Funk-Kontakt erzeugt.
BEACON_CONTACT_PER_DAY = 0.5

# --- Bilanz-Prüfung -----------------------------------------------------
# Zuwachs über dieser Toleranz ohne Quelle = Bug-Alarm (Schritt 1: keine Quellen).
AUDIT_EPS = 1e-6

# --- Sterbe-Modell (Issue #20) ------------------------------------------
# Altersklassen-Grenzen in Jahren (untere Kante, exkl. Obergrenze der letzten).
# Reihenfolge muss mit SURVIVOR_BASE_SURVIVE_PER_DAY übereinstimmen.
SURVIVOR_AGE_THRESHOLDS: tuple[int, ...] = (0, 1, 5, 13, 65, 80)
# Indizes: 0=Säugling(0-<1), 1=Kleinkind(1-<5), 2=Kind(5-<13),
#          3=Erwachsener(13-<65), 4=Senior(65-<80), 5=Greis(80+)

# Tägliche Basis-Überlebenschance je Altersklasse (ALLEIN, ohne Gruppe).
# Kalibrierung:
#   Säugling allein: ~0.30/Tag → stirbt im Median nach ~1 Tag
#   Kleinkind allein: ~0.75/Tag → stirbt im Median nach ~3 Tagen
#   Kind allein: ~0.92/Tag → stirbt nach Wochen
#   Erwachsener allein: ~0.997/Tag → stirbt nach Monaten/Jahren
#   Senior allein: ~0.990/Tag → etwas schneller
#   Greis allein: ~0.975/Tag → merklich schneller
SURVIVOR_BASE_SURVIVE_PER_DAY: tuple[float, ...] = (
    0.30,   # 0: Säugling  (0-<1 J.)
    0.75,   # 1: Kleinkind (1-<5 J.)
    0.92,   # 2: Kind      (5-<13 J.)
    0.997,  # 3: Erwachsener (13-<65 J.)  — Referenz
    0.990,  # 4: Senior    (65-<80 J.)
    0.975,  # 5: Greis     (80+ J.)
)

# Erwachsenen-Altersband (fürsorgefähig): [ADULT_MIN, ADULT_MAX)
SURVIVOR_ADULT_MIN_AGE: int = 13
SURVIVOR_ADULT_MAX_AGE: int = 65

# Gruppen-Boost: Multiplikator auf (1 - p_base), d.h. er verringert die
# Sterbewahrscheinlichkeit. p_survive_group = 1 - (1-p_base)*boost_factor
# Nur angewendet, wenn Gruppe mind. 1 Erwachsenen enthält (sonst kein Boost).
# boost_factor = max(1.0, SURVIVOR_GROUP_BOOST_BASE + SURVIVOR_GROUP_BOOST_PER_ADULT * n_adults)
# gedeckelt bei SURVIVOR_GROUP_BOOST_CAP.
SURVIVOR_GROUP_BOOST_BASE: float = 2.0    # Basis-Boost mit 1 Erwachsenem
SURVIVOR_GROUP_BOOST_PER_ADULT: float = 0.5  # +0.5 je weiterem Erwachsenem
SURVIVOR_GROUP_BOOST_CAP: float = 5.0     # Deckel (≙ Faktor 5 auf Sterbe-p)

# Kinder (0-12) kriegen nur dann den Boost, wenn mind. 1 Erwachsener in Gruppe.
# Sonst bleibt ihr p_survive beim Allein-Basiswert.
SURVIVOR_CHILD_MAX_AGE: int = 13  # exkl.

# --- Survivor-Migrations-Sim (Issue #19) --------------------------------
# Ab welchem Tag kippt das Vorzeichen: Gravitation → Flucht.
SURVIVOR_T_FLEE: int = 30
# Geruchsschwelle: smell = D_local * ramp(day); über diesem Wert → Flucht.
SURVIVOR_SMELL_THRESHOLD: float = 5_000.0
# Ramp-Funktion: smell = D_local * min(day / RAMP_DAYS, 1.0)
SURVIVOR_RAMP_DAYS: int = 60
# Maximale Wanderleistung pro Spieltag in km (realistisch ~25 km/Tag).
SURVIVOR_MAX_STEP_KM: float = 25.0
# Treffdistanz fürs Gruppieren in km.
SURVIVOR_MEET_DIST_KM: float = 2.0
# Gewichtung: Dichte-Gradient vs. Anziehung zu anderen Survivors/Gruppen.
SURVIVOR_GRAVITY_WEIGHT: float = 0.7
SURVIVOR_SOCIAL_WEIGHT: float = 0.2
SURVIVOR_NOISE_WEIGHT: float = 0.1
# Bucket-Größe für den Spatial-Grid-Hash beim Gruppieren (in Grad, ~11 km).
SURVIVOR_BUCKET_DEG: float = 0.1

# --- Bewegungsmodell v2: Heimat-Anker + Sesshaftigkeit (Issue #29) -------
# Gewicht des Heimat-Ankers am Tag 0. Fällt linear auf ~0 nach DECAY_DAYS.
# Bei 2.0 überwiegt der Heimat-Anker am Anfang deutlich (Gravitation = 0.7).
SURVIVOR_HOME_WEIGHT: float = 2.0
# Nach dieser Anzahl Tage ist der Heimat-Anker auf 0 abgefallen (linear).
SURVIVOR_HOME_DECAY_DAYS: int = 21
# Mobilitäts-Rampe: Tag 0 = 0 % Mobilität; nach RAMP_DAYS = 100 % (clip).
# Tag 0–2 fast keine Bewegung, ab Tag 5 volle Mobilität.
SURVIVOR_MOBILITY_RAMP_DAYS: int = 5
# Geschwindigkeitsskalierung: Schrittweite_km = min(MAX_STEP, SPEED_SCALE * |v|).
# |v| liegt bei starkem Sog (~1.5–2.5) oft bei 1–2; wir wollen dann ~25 km.
# Kalibrierung: SPEED_SCALE * 1.0 = 12.5 km (mittel), * 2.0 = 25 km (voll).
# => SPEED_SCALE = 12.5 km / 1.0 = 12.5
SURVIVOR_SPEED_SCALE: float = 12.5
