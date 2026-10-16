-- Light Map SQLite query layer (skeleton).
--
-- CSV is the archival source of truth; this database is a rebuildable query layer
-- (app/core/store.py). Never add state that only lives here: everything must be
-- reconstructable from the CSV archive.
--
-- Readings are per bulb, long format (heartbeat + bulb_reading), which is what pooling into
-- learning units needs. The almanac is insert-only snapshots keyed by learning unit, so
-- history comes for free. Exact columns are finalised when store.py and the analyser are
-- ported (P4/P5).
--
-- Conventions: ts columns are ISO-8601 UTC text; booleans are 0/1 integers.

-- One row per observation tick.
CREATE TABLE IF NOT EXISTS heartbeat (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    room_id     TEXT    NOT NULL,
    section     TEXT,
    lux         REAL,   -- nullable; 0 is a floor, not a measurement
    occupancy   INTEGER NOT NULL CHECK (occupancy IN (0, 1))
);

-- Long format: one row per bulb per heartbeat.
CREATE TABLE IF NOT EXISTS bulb_reading (
    id              INTEGER PRIMARY KEY,
    heartbeat_id    INTEGER NOT NULL REFERENCES heartbeat (id),
    bulb_id         TEXT    NOT NULL,
    brightness      INTEGER,
    is_on           INTEGER NOT NULL CHECK (is_on IN (0, 1))
);

-- A consolidated manual change. brightness_before/brightness_after, not before/after:
-- those are SQL keywords.
CREATE TABLE IF NOT EXISTS reactive_event (
    id                  INTEGER PRIMARY KEY,
    ts                  TEXT    NOT NULL,
    room_id             TEXT    NOT NULL,
    section             TEXT,
    bulb_id             TEXT    NOT NULL,
    brightness_before   INTEGER,
    brightness_after    INTEGER,
    weight              REAL
);

-- Insert-only learned snapshots, keyed by learning unit. Never updated in place.
CREATE TABLE IF NOT EXISTS almanac (
    id                  INTEGER PRIMARY KEY,
    ts                  TEXT    NOT NULL,
    room_id             TEXT    NOT NULL,
    section             TEXT    NOT NULL,
    learning_unit_id    TEXT    NOT NULL,
    lux_target          REAL,
    lux_margin          REAL,
    maintenance_enabled INTEGER CHECK (maintenance_enabled IN (0, 1)),
    max_step_pct        REAL,
    trust_weight        REAL,
    valid_from          TEXT,
    mode                TEXT
);

CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    level       TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    room_id     TEXT,
    message     TEXT    NOT NULL,
    detail      TEXT    -- JSON
);
