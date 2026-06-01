-- ============================================================================
-- schema.sql — Wasteland Sim, Schritt 1 (deterministischer Sim-Kern, kein LLM)
-- SQLite. Auf Erweiterbarkeit zu späteren Phasen ausgelegt:
--   * group_id / character.type / location.* sind schon vorhanden, auch wo
--     Schritt 1 sie kaum nutzt, damit spätere Phasen nicht migrieren müssen.
--   * Agenten-, NPC-, Adjudikator-Tabellen kommen erst ab Schritt 2+ dazu.
-- Konvention: Zeit = ganzzahliger Tick in Spiel-MINUTEN seit Kollaps.
--             Datum/Uhrzeit leitet die App daraus ab (start_datetime + tick).
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- WELT (Singleton: genau eine Zeile, id = 1)
-- ---------------------------------------------------------------------------
CREATE TABLE world (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    world_seed      INTEGER NOT NULL,           -- globaler Seed für alle Lazy-Gen
    tick            INTEGER NOT NULL DEFAULT 0, -- Spielminuten seit Kollaps
    start_datetime  TEXT    NOT NULL,           -- ISO; Kollaps-Zeitpunkt
    phase           INTEGER NOT NULL DEFAULT 1, -- 1..5 (siehe DESIGN.md §4)
    -- Wetter als einfacher Snapshot; ist Anzeige, keine Simulation (DESIGN.md)
    weather_temp_c  REAL    NOT NULL DEFAULT 15.0,
    weather_state   TEXT    NOT NULL DEFAULT 'clear', -- clear|clouds|rain|storm|snow
    weather_wind_kmh REAL   NOT NULL DEFAULT 0.0
);

-- ---------------------------------------------------------------------------
-- ITEM-KATALOG (Definitionen, nicht Instanzen)
-- ---------------------------------------------------------------------------
CREATE TABLE item_catalog (
    id              TEXT PRIMARY KEY,           -- z.B. 'canned_beans', 'water_1l'
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,              -- food|water|tool|material|fuel|medical|misc
    weight_kg       REAL NOT NULL DEFAULT 0.0,  -- pro Einheit
    kcal_per_unit   REAL,                       -- nur food; sonst NULL
    -- Decay: Halbwertszeit der Qualität in Spielminuten bei Referenztemp.
    -- NULL = praktisch nicht verderblich (z.B. Werkzeug, Konserve sehr lang).
    decay_halflife_min  INTEGER,
    decay_temp_ref_c    REAL DEFAULT 15.0,      -- Referenztemp für Decay-Rate
    stackable       INTEGER NOT NULL DEFAULT 1, -- 0|1
    -- Zubereitung: braucht das Item Hitze+Wasser, bevor es essbar ist?
    -- Die Hitzequelle selbst ist später Sache des Adjudikators (siehe sim/heat.py).
    needs_preparation INTEGER NOT NULL DEFAULT 0,   -- 0|1
    requires_water_l  REAL    NOT NULL DEFAULT 0.0, -- benötigtes Wasser je Einheit
    prepared_into     TEXT                          -- Ziel-Item nach Zubereitung
);

-- ---------------------------------------------------------------------------
-- LOCATIONS (aus OSM; Footprint + Typ. Inventar erst bei Entdeckung!)
-- ---------------------------------------------------------------------------
CREATE TABLE locations (
    id                  INTEGER PRIMARY KEY,
    osm_id              TEXT,                   -- Herkunft aus OSM
    type                TEXT NOT NULL,          -- house|supermarket|fuel_station|hardware|pharmacy|...
    name                TEXT,
    lat                 REAL NOT NULL,
    lon                 REAL NOT NULL,
    footprint_m2        REAL,                   -- für prozedurale Grid-Größe
    -- Lazy Generation Steuerung:
    discovery_status    TEXT NOT NULL DEFAULT 'undiscovered', -- undiscovered|discovered|depleted
    discovered_at_tick  INTEGER,                -- NULL bis entdeckt
    generation_seed     INTEGER,                -- = hash(world_seed, location.id); deterministisch
    -- Zustand der Location selbst (Brand, Einsturz etc., DESIGN.md §10)
    structure_state     TEXT NOT NULL DEFAULT 'intact' -- intact|damaged|burned|collapsed
);
CREATE INDEX idx_locations_status ON locations(discovery_status);
CREATE INDEX idx_locations_geo    ON locations(lat, lon);
-- osm_id ist der stabile Anker pro Ort -> eindeutig, damit der Loader
-- idempotent per ON CONFLICT(osm_id) upserten kann (kein Duplikat bei Re-Import).
CREATE UNIQUE INDEX idx_locations_osm_id ON locations(osm_id);

-- Inventar EINER Location. Zeilen existieren erst nach Entdeckung (Lazy Gen).
CREATE TABLE location_inventory (
    id              INTEGER PRIMARY KEY,
    location_id     INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    item_id         TEXT    NOT NULL REFERENCES item_catalog(id),
    quantity        REAL    NOT NULL,
    quality         REAL    NOT NULL DEFAULT 1.0,  -- 0.0..1.0 (Decay)
    produced_tick   INTEGER NOT NULL DEFAULT 0     -- für Decay-Berechnung
);
CREATE INDEX idx_loc_inv_location ON location_inventory(location_id);

-- ---------------------------------------------------------------------------
-- GRUPPEN (Schritt 1: nur die Spieler-Gruppe; später NPC-Gruppen)
-- ---------------------------------------------------------------------------
CREATE TABLE groups (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    is_player_group INTEGER NOT NULL DEFAULT 0,   -- 0|1
    leader_id       INTEGER,                      -- FK characters.id (nullable)
    -- Lagerkapazität pro Kategorie als JSON (Schritt 1: simpel/großzügig)
    storage_capacity_json TEXT NOT NULL DEFAULT '{}'
);

-- Ressourcenbestand EINER Gruppe (aggregierter Besitz, nicht Location-gebunden)
CREATE TABLE group_inventory (
    id              INTEGER PRIMARY KEY,
    group_id        INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    item_id         TEXT    NOT NULL REFERENCES item_catalog(id),
    quantity        REAL    NOT NULL,
    quality         REAL    NOT NULL DEFAULT 1.0,
    acquired_tick   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(group_id, item_id, quality)            -- gleiche Quality stapelt
);
CREATE INDEX idx_grp_inv_group ON group_inventory(group_id);

-- ---------------------------------------------------------------------------
-- CHARAKTERE (Schritt 1: nur Spieler; type & group_id für später vorbereitet)
-- ---------------------------------------------------------------------------
CREATE TABLE characters (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'player', -- player|npc (Schritt 1: nur player)
    group_id        INTEGER REFERENCES groups(id),
    age             INTEGER,
    weight_kg       REAL DEFAULT 75.0,
    -- Profil (Onboarding): treibt Bedarf (Größe/Geschlecht/Geburtsdatum) und
    -- speist den Adjudikator (Beruf/Bildung/Hobbys/Selbstbeschreibung).
    birthdate       TEXT,                       -- ISO; Alter = start_datetime - birthdate
    sex             TEXT,                       -- m|f|x (für BMR)
    height_cm       REAL,
    profession      TEXT,
    education       TEXT,
    family          TEXT,
    hobbies         TEXT,
    self_description TEXT,
    home_lat        REAL,                       -- wo der Spieler erwacht (Zuhause)
    home_lon        REAL,
    -- Position in der Welt
    lat             REAL,
    lon             REAL,
    -- Bedürfnis-Achsen 0.0..1.0 (1.0 = voll versorgt). Schritt 1 aktiv: hunger.
    -- Rest vorhanden, aber Sim-Kern aktualisiert in Schritt 1 nur hunger.
    hunger          REAL NOT NULL DEFAULT 1.0,
    thirst          REAL NOT NULL DEFAULT 1.0,
    sleep           REAL NOT NULL DEFAULT 1.0,
    injury          REAL NOT NULL DEFAULT 1.0,  -- 1.0 = unverletzt
    exposure        REAL NOT NULL DEFAULT 1.0,
    -- abgeleitete Achse: Zufriedenheit (aus Bedürfnis-Deckung + Komfort − Isolation)
    satisfaction    REAL NOT NULL DEFAULT 1.0,
    -- abgeleitet: multiplikative Stapelung (DESIGN.md §5)
    performance     REAL NOT NULL DEFAULT 1.0,
    is_alive        INTEGER NOT NULL DEFAULT 1,
    -- Tagesbedarf (für Verbrauch); Default ~2500 kcal / ~2.5 L Wasser
    daily_kcal      REAL NOT NULL DEFAULT 2500.0,
    daily_water_l   REAL NOT NULL DEFAULT 2.5,
    -- Fuß-Routing: aktuelles Ziel + verbleibende Wegpunkte (JSON [[lat,lon],...]).
    -- NULL = steht still. Bewegung wird im Tick (Phase 1) abgelaufen.
    dest_lat        REAL,
    dest_lon        REAL,
    path_json       TEXT
);
CREATE INDEX idx_char_group ON characters(group_id);

-- ---------------------------------------------------------------------------
-- EREIGNIS-/TICK-LOG (Interrupts, Historie, Debug; auch Storytelling-Material)
-- ---------------------------------------------------------------------------
CREATE TABLE events (
    id              INTEGER PRIMARY KEY,
    tick            INTEGER NOT NULL,
    category        TEXT NOT NULL,              -- need|world|location|system
    severity        TEXT NOT NULL DEFAULT 'info', -- info|soft|decision  (DESIGN.md §4 Interrupt-Stufen)
    subject_type    TEXT,                       -- character|location|group|world
    subject_id      INTEGER,
    message         TEXT NOT NULL,
    payload_json    TEXT                        -- strukturierte Zusatzdaten
);
CREATE INDEX idx_events_tick ON events(tick);

-- ---------------------------------------------------------------------------
-- BILANZ-PRÜFUNG (Sicherheitsnetz gegen Erzeugung aus dem Nichts, DESIGN.md §6)
-- Snapshot der Welt-Gesamtmengen pro Tick; Sim-Kern vergleicht gegen
-- erwartete Quellen/Senken. Abweichung = Bug-Alarm.
-- ---------------------------------------------------------------------------
CREATE TABLE resource_audit (
    id              INTEGER PRIMARY KEY,
    tick            INTEGER NOT NULL,
    item_id         TEXT NOT NULL REFERENCES item_catalog(id),
    total_world     REAL NOT NULL,              -- Summe location_inventory
    total_groups    REAL NOT NULL,              -- Summe group_inventory
    expected_delta  REAL NOT NULL DEFAULT 0.0,  -- erwartete Änderung ggü. Vortick
    actual_delta    REAL NOT NULL DEFAULT 0.0,
    flagged         INTEGER NOT NULL DEFAULT 0  -- 1 wenn |actual-expected| > eps
);
CREATE INDEX idx_audit_tick ON resource_audit(tick);

-- ---------------------------------------------------------------------------
-- RESSOURCEN-LEDGER (laufendes Soll je Item = Σ Quellen − Σ Senken)
-- Quellen (Schritt 1): Lazy Generation bei Entdeckung.
-- Senken: Verbrauch (Essen), Verderb. Plündern ist Transfer -> ledger-neutral.
-- Der Tick-Audit vergleicht den Ist-Bestand gegen dieses Soll; jede Drift
-- (in beide Richtungen) bedeutet eine Mutation außerhalb der Sim-Funktionen.
-- ---------------------------------------------------------------------------
CREATE TABLE resource_ledger (
    item_id         TEXT PRIMARY KEY REFERENCES item_catalog(id),
    expected_total  REAL NOT NULL DEFAULT 0.0
);

-- ---------------------------------------------------------------------------
-- KNOWLEDGE BASE (verbindliche Fakten für den Adjudikator, DESIGN.md §8/§9)
-- Provenance-Vorrang: curated > player_verified > llm_inferred.
-- Beispiel-Topic 'heat_source': welche Items liefern Hitze zum Zubereiten.
-- ---------------------------------------------------------------------------
CREATE TABLE knowledge_base (
    id              INTEGER PRIMARY KEY,
    topic           TEXT NOT NULL,              -- z.B. 'provides:heat'
    key             TEXT NOT NULL,              -- z.B. item_id
    value           TEXT,                       -- JSON oder Skalar
    provenance      TEXT NOT NULL DEFAULT 'curated', -- curated|player_verified|llm_inferred
    created_tick    INTEGER,
    UNIQUE(topic, key)
);
CREATE INDEX idx_kb_topic ON knowledge_base(topic);

-- ---------------------------------------------------------------------------
-- CAPABILITIES (persistenter Welt-State aus adjudizierten Aktionen, DESIGN.md §8)
-- z.B. ein SSID-Beacon: hat Upkeep (laufende Kosten je Tick) und Folgen.
-- ---------------------------------------------------------------------------
CREATE TABLE capabilities (
    id              INTEGER PRIMARY KEY,
    ctype           TEXT NOT NULL,              -- z.B. 'ssid_beacon'
    owner_group     INTEGER REFERENCES groups(id),
    location_id     INTEGER REFERENCES locations(id),
    params          TEXT,                       -- JSON
    active          INTEGER NOT NULL DEFAULT 1,
    created_tick    INTEGER,
    upkeep          TEXT                        -- JSON: {item, per_tick}
);
CREATE INDEX idx_cap_active ON capabilities(active);

INSERT INTO knowledge_base (topic, key, value, provenance, created_tick) VALUES
  ('provides:heat',        'firewood',    '{"consume": 1}', 'curated', 0),
  ('provides:power',       'generator',   '{"consume": 0}', 'curated', 0),
  ('provides:transmitter', 'wifi_router', '{"consume": 0}', 'curated', 0),
  ('capability_recipe:ssid_beacon', 'ssid_beacon',
   '{"requires": ["power", "transmitter"], "upkeep": {"item": "gasoline", "per_tick": 0.02}, "range_km": 1.5}',
   'curated', 0);

-- ============================================================================
-- MINIMALER SEED FÜR SCHRITT 1 (Beispiel-Items; vom Generator nutzbar)
-- ============================================================================
INSERT INTO item_catalog (id, name, category, weight_kg, kcal_per_unit, decay_halflife_min, stackable, needs_preparation, requires_water_l, prepared_into) VALUES
  ('canned_beans',  'Dose Bohnen',        'food',     0.40,  330,   NULL,    1, 0, 0.0, NULL),
  ('canned_meat',   'Dose Fleisch',       'food',     0.30,  450,   NULL,    1, 0, 0.0, NULL),
  ('bread_loaf',    'Brot',               'food',     0.50,  1100,  4320,    1, 0, 0.0, NULL),  -- ~3 Tage
  ('milk_1l',       'Milch 1L',           'food',     1.03,  640,   2880,    1, 0, 0.0, NULL),  -- ~2 Tage
  ('water_1l',      'Wasser 1L',          'water',    1.00,  NULL,  NULL,    1, 0, 0.0, NULL),
  ('pasta_500g',    'Nudeln 500g (roh)',  'food',     0.50,  1750,  NULL,    1, 1, 0.5, 'meal_pasta'),  -- braucht Hitze+Wasser
  ('meal_pasta',    'Gekochte Nudeln',    'food',     0.55,  1750,  1440,    1, 0, 0.0, NULL),  -- ~1 Tag haltbar
  ('crowbar',       'Kuhfuß',             'tool',     1.20,  NULL,  NULL,    0, 0, 0.0, NULL),
  ('flashlight',    'Stirnlampe',         'tool',     0.15,  NULL,  NULL,    0, 0, 0.0, NULL),
  ('firewood',      'Brennholz (Scheit)', 'fuel',     1.50,  NULL,  NULL,    1, 0, 0.0, NULL),
  ('generator',     'Stromgenerator',     'tool',    25.00,  NULL,  NULL,    0, 0, 0.0, NULL),
  ('wifi_router',   'WLAN-Router',        'tool',     1.00,  NULL,  NULL,    0, 0, 0.0, NULL),
  ('gasoline',      'Benzin 5L',          'fuel',     4.00,  NULL,  NULL,    1, 0, 0.0, NULL);

-- Welt-Singleton initialisieren (world_seed später vom App-Start gesetzt)
INSERT INTO world (id, world_seed, tick, start_datetime, phase)
VALUES (1, 0, 0, '2026-09-01T06:00:00', 1);

-- Spieler-Gruppe + Spielercharakter (Platzhalter; App setzt Startposition)
INSERT INTO groups (id, name, is_player_group) VALUES (1, 'Spieler', 1);
INSERT INTO characters (id, name, type, group_id, age, hunger)
VALUES (1, 'Spieler', 'player', 1, 35, 1.0);
UPDATE groups SET leader_id = 1 WHERE id = 1;
