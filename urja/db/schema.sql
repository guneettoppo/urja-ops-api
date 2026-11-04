-- Local index of the Urja Meter Ops portal.
-- Derived data only: every row here can be rebuilt from the portal itself.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meters (
    meter_id            TEXT PRIMARY KEY,
    serial_no           TEXT,
    make                TEXT,
    phase_type          TEXT,              -- single | three
    install_status      TEXT,              -- installed | faulty | decommissioned
    install_type        TEXT,              -- whole_current | ct_operated
    build               TEXT,              -- legacy | v2 (the portal's own split)
    dt_code             TEXT,
    dt_name             TEXT,
    feeder_code         TEXT,
    feeder_name         TEXT,
    substation_code     TEXT,
    substation_name     TEXT,
    subdivision_code    TEXT,
    subdivision_name    TEXT,
    division_code       TEXT,
    division_name       TEXT,
    circle_code         TEXT,
    circle_name         TEXT,
    zone_code           TEXT,
    zone_name           TEXT,
    lat                 REAL,
    lng                 REAL,
    data_quality        TEXT NOT NULL DEFAULT '[]',
    first_reading_at    TEXT,
    last_reading_at     TEXT,
    reading_count       INTEGER NOT NULL DEFAULT 0,
    granularity_seconds INTEGER,
    synced_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meters_status ON meters(install_status);
CREATE INDEX IF NOT EXISTS idx_meters_make   ON meters(make);
CREATE INDEX IF NOT EXISTS idx_meters_feeder ON meters(feeder_code);
CREATE INDEX IF NOT EXISTS idx_meters_dt     ON meters(dt_code);
CREATE INDEX IF NOT EXISTS idx_meters_phase  ON meters(phase_type);

CREATE TABLE IF NOT EXISTS dts (
    code         TEXT PRIMARY KEY,
    name         TEXT,
    feeder_code  TEXT,
    capacity_kva INTEGER
);

CREATE TABLE IF NOT EXISTS readings (
    meter_id TEXT NOT NULL REFERENCES meters(meter_id) ON DELETE CASCADE,
    ts       TEXT NOT NULL,            -- ISO-8601 with +05:30
    kwh      REAL,                     -- cumulative register, NOT consumption
    kvah     REAL,
    volt_r   REAL,
    PRIMARY KEY (meter_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    state           TEXT NOT NULL,     -- running | ok | partial | failed
    meters_synced   INTEGER NOT NULL DEFAULT 0,
    readings_synced INTEGER NOT NULL DEFAULT 0,
    failures        INTEGER NOT NULL DEFAULT 0,
    detail          TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
