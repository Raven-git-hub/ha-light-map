-- Light Map SQLite query layer.
--
-- CSV is the archival source of truth; this database is a rebuildable query layer
-- (app/core/store.py). Never add state that only lives here: everything must be
-- reconstructable from the CSV archive. Each table below has a CSV stream with the same
-- columns minus the surrogate ids; a child row names its parent by the parent's natural key
-- (room_id, ts) instead, so the archive reads back without this database.
--
-- Readings are per bulb, long format (heartbeat + bulb_reading, reactive_event +
-- reactive_change), which is what pooling into learning units needs. The almanac is
-- insert-only snapshots keyed by learning unit, so history comes for free; its columns are
-- finalised with the analyser (P5).
--
-- Conventions: ts columns are ISO-8601 UTC text with microseconds (one fixed format, so
-- they sort as text); booleans are 0/1 integers.

-- One row per room per observation tick.
CREATE TABLE IF NOT EXISTS heartbeat (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    room_id     TEXT    NOT NULL,
    section     TEXT,   -- recorded at observation time (P5); NULL before
    lux         REAL,   -- nullable; 0 is a floor, not a measurement
    lux_sensors INTEGER NOT NULL DEFAULT 0,  -- how many sensors lux came from
    occupancy   INTEGER CHECK (occupancy IN (0, 1)),  -- NULL: no presence sensor available
    deferred_ms INTEGER NOT NULL DEFAULT 0,  -- how long the tick waited for a guard
    UNIQUE (room_id, ts)
);
CREATE INDEX IF NOT EXISTS heartbeat_room_ts ON heartbeat (room_id, ts);

-- Long format: one row per bulb per heartbeat.
CREATE TABLE IF NOT EXISTS bulb_reading (
    id              INTEGER PRIMARY KEY,
    heartbeat_id    INTEGER NOT NULL REFERENCES heartbeat (id),
    bulb_id         TEXT    NOT NULL,
    is_on           INTEGER CHECK (is_on IN (0, 1)),  -- NULL: HA had no usable state
    brightness      INTEGER CHECK (brightness BETWEEN 0 AND 255),
    UNIQUE (heartbeat_id, bulb_id)
);
CREATE INDEX IF NOT EXISTS bulb_reading_bulb ON bulb_reading (bulb_id, is_on);

-- One consolidated manual change in a room: one reactive window. ts is the window's start.
CREATE TABLE IF NOT EXISTS reactive_event (
    id              INTEGER PRIMARY KEY,
    ts              TEXT    NOT NULL,
    room_id         TEXT    NOT NULL,
    section         TEXT,
    window_seconds  INTEGER NOT NULL,
    lux_before      REAL,
    lux_after       REAL,
    occupancy       INTEGER CHECK (occupancy IN (0, 1)),  -- recorded, never filtered on
    UNIQUE (room_id, ts)
);
CREATE INDEX IF NOT EXISTS reactive_event_room_ts ON reactive_event (room_id, ts);

-- Long format: one row per bulb of the room per reactive event. brightness_before/
-- brightness_after, not before/after: those are SQL keywords. For a bulb the user changed,
-- after is where their last change left it; for the others, the room as the window closed.
-- user_id is the HA user behind that last change (NULL for a device: a dimmer, a remote),
-- so the analyser can tell a person from another system's API user. No stored weight:
-- weighting is the analyser's, applied at analysis time (P5).
CREATE TABLE IF NOT EXISTS reactive_change (
    id                  INTEGER PRIMARY KEY,
    reactive_event_id   INTEGER NOT NULL REFERENCES reactive_event (id),
    bulb_id             TEXT    NOT NULL,
    is_on_before        INTEGER CHECK (is_on_before IN (0, 1)),
    is_on_after         INTEGER CHECK (is_on_after IN (0, 1)),
    brightness_before   INTEGER CHECK (brightness_before BETWEEN 0 AND 255),
    brightness_after    INTEGER CHECK (brightness_after BETWEEN 0 AND 255),
    changed             INTEGER NOT NULL CHECK (changed IN (0, 1)),
    user_id             TEXT,
    UNIQUE (reactive_event_id, bulb_id)
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
CREATE INDEX IF NOT EXISTS almanac_room_unit ON almanac (room_id, learning_unit_id, ts);

CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    level       TEXT    NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error')),
    category    TEXT    NOT NULL,
    room_id     TEXT,
    message     TEXT    NOT NULL,
    detail      TEXT    -- JSON
);
CREATE INDEX IF NOT EXISTS event_log_ts ON event_log (ts);

-- Bookkeeping, not data: each archive file (its path under the archive directory) as of
-- the last write this database holds all of. At startup a file whose size or mtime differs,
-- or a file missing on either side, means this layer has fallen out of step with the
-- archive, and it is rebuilt from it. Derived from the archive like everything else here.
-- (The schema's version, a checksum of this file, is in PRAGMA user_version.)
CREATE TABLE IF NOT EXISTS archive_file (
    path        TEXT    PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL
);
