"""Tunables des Sim-Kerns (Schritt 1).

Alle Spiel-Balance-Werte an einem Ort, damit Ticks deterministisch und
nachvollziehbar bleiben. Zeit wird in Spielminuten gerechnet (siehe schema.sql).
"""
from __future__ import annotations

# --- Zeit ---------------------------------------------------------------
TICK_MINUTES = 10        # Spielminuten, um die ein advance_tick() vorrückt
MINUTES_PER_DAY = 1440

# --- Hunger -------------------------------------------------------------
# Sättigung 0..1 (1 = satt). Ohne Nahrung in ~3 Spieltagen von 1.0 auf 0.
HUNGER_LOSS_PER_DAY = 0.33
HUNGER_SOFT = 0.5        # Interrupt-Schwelle "wird hungrig"
HUNGER_CRIT = 0.2        # Interrupt-Schwelle "kritisch hungrig"

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

# --- Bilanz-Prüfung -----------------------------------------------------
# Zuwachs über dieser Toleranz ohne Quelle = Bug-Alarm (Schritt 1: keine Quellen).
AUDIT_EPS = 1e-6
