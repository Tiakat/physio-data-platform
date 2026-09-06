-- Lab Research Data Platform, database schema.
-- Runs automatically the first time the db container starts.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Identity is isolated in its own schema with its own grants.
-- Nothing in the analysis path ever joins to it.
CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS core;

SET search_path = core, public;

CREATE TABLE projects (
    project_id      SERIAL PRIMARY KEY,
    code            TEXT UNIQUE NOT NULL,        -- IPAMS, V-RAPS, ...
    name            TEXT,
    profile_file    TEXT NOT NULL,               -- profiles/ipams.yaml
    retention_days  INT DEFAULT 3650,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE participants (
    participant_id  SERIAL PRIMARY KEY,
    project_id      INT REFERENCES projects ON DELETE CASCADE,
    code            TEXT NOT NULL,               -- P-0142, assigned by the platform
    source_label    TEXT,                        -- original folder name, for traceability
    enrolled_on     DATE,
    UNIQUE (project_id, code)
);

CREATE TABLE demographics (
    participant_id  INT PRIMARY KEY REFERENCES participants ON DELETE CASCADE,
    age             NUMERIC,
    sex             TEXT,
    bmi             NUMERIC,
    asa             INT,
    surgery_type    TEXT,
    extra           JSONB DEFAULT '{}'::jsonb    -- per project study variables
);

CREATE TABLE sessions (
    session_id      SERIAL PRIMARY KEY,
    participant_id  INT REFERENCES participants ON DELETE CASCADE,
    number          INT DEFAULT 1,
    session_date    DATE,
    location        TEXT,                        -- OR^^BLOC03, CU1^^Bed13
    UNIQUE (participant_id, number)
);

CREATE TABLE devices (
    device_id       SERIAL PRIMARY KEY,
    code            TEXT UNIQUE NOT NULL,        -- infinity, bettercare, nol, bis, pump
    manufacturer    TEXT,
    default_rate_hz NUMERIC
);

-- One row per physical file that ever arrived. Never deleted.
CREATE TABLE recordings (
    recording_id    SERIAL PRIMARY KEY,
    session_id      INT REFERENCES sessions ON DELETE CASCADE,
    device_id       INT REFERENCES devices,
    file_name       TEXT NOT NULL,
    source_path     TEXT,                        -- path in the original archive
    raw_uri         TEXT,                        -- object storage key, immutable
    sha256          TEXT NOT NULL,
    size_bytes      BIGINT,
    tier            TEXT NOT NULL,               -- A parsed, B kept, C ignored
    parse_status    TEXT NOT NULL DEFAULT 'pending',
        -- pending | parsed | not_supported | failed
    status          TEXT NOT NULL DEFAULT 'RECEIVED',
        -- RECEIVED RAW VALIDATED STANDARDISED PROCESSED QC_PENDING APPROVED REJECTED FAILED
    supersedes      INT REFERENCES recordings,   -- set when a corrected file arrives
    received_at     TIMESTAMPTZ DEFAULT now(),
    pipeline_version TEXT,
    UNIQUE (sha256)
);
CREATE INDEX ON recordings (session_id);
CREATE INDEX ON recordings (status);
CREATE INDEX ON recordings (parse_status);

-- Time series. Long format so any project can add a variable without a migration.
CREATE TABLE signals (
    recording_id    INT NOT NULL REFERENCES recordings ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL,
    variable        TEXT NOT NULL,               -- ART_MEAN, NOL, HR ...
    value           DOUBLE PRECISION,
    valid           BOOLEAN DEFAULT TRUE         -- false when the source flagged it
);
SELECT create_hypertable('signals', 'ts', chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);
CREATE INDEX ON signals (recording_id, variable, ts DESC);
CREATE UNIQUE INDEX uq_signals_recording_variable_ts ON signals (recording_id, variable, ts);

CREATE TABLE validation (
    validation_id   SERIAL PRIMARY KEY,
    recording_id    INT REFERENCES recordings ON DELETE CASCADE,
    check_name      TEXT NOT NULL,
    result          TEXT NOT NULL,               -- PASS WARNING FAIL
    detail          TEXT,
    run_at          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON validation (recording_id);

CREATE TABLE qc_results (
    qc_id           SERIAL PRIMARY KEY,
    recording_id    INT REFERENCES recordings ON DELETE CASCADE,
    flag            TEXT NOT NULL,               -- SIGNAL_FLAT, CHANNEL_HELD ...
    severity        TEXT NOT NULL,               -- info warning error
    measured        NUMERIC,
    threshold       NUMERIC,
    variable        TEXT,
    run_at          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON qc_results (recording_id);

CREATE TABLE qc_reviews (
    review_id       SERIAL PRIMARY KEY,
    recording_id    INT REFERENCES recordings ON DELETE CASCADE,
    reviewer        TEXT NOT NULL,
    decision        TEXT NOT NULL,               -- APPROVE REJECT REPROCESS
    comment         TEXT,
    reviewed_at     TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE processing_runs (
    run_id          SERIAL PRIMARY KEY,
    recording_id    INT REFERENCES recordings ON DELETE CASCADE,
    stage           TEXT NOT NULL,               -- validate standardise process qc
    pipeline_version TEXT,
    parameters      JSONB,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    status          TEXT,                        -- ok failed
    message         TEXT
);

-- Which modalities exist per participant. This is what makes honest cohort
-- filtering possible and is refreshed after every ingestion.
CREATE MATERIALIZED VIEW coverage AS
SELECT p.participant_id,
       p.project_id,
       p.code AS participant_code,
       d.code AS device,
       count(*) FILTER (WHERE r.parse_status = 'parsed')      AS parsed_files,
       count(*)                                                AS all_files,
       sum(r.size_bytes)                                       AS bytes
FROM participants p
JOIN sessions   s ON s.participant_id = p.participant_id
JOIN recordings r ON r.session_id = s.session_id
JOIN devices    d ON d.device_id = r.device_id
GROUP BY p.participant_id, p.project_id, p.code, d.code;
CREATE UNIQUE INDEX ON coverage (participant_id, device);

CREATE TABLE users (
    user_id         SERIAL PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    full_name       TEXT,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL,   -- director, scientist, assistant, collaborator
    active          BOOLEAN DEFAULT TRUE
);

CREATE TABLE user_projects (
    user_id         INT REFERENCES users ON DELETE CASCADE,
    project_id      INT REFERENCES projects ON DELETE CASCADE,
    PRIMARY KEY (user_id, project_id)
);

CREATE TABLE audit_log (
    audit_id        BIGSERIAL PRIMARY KEY,
    username        TEXT,
    action          TEXT NOT NULL,
    object_type     TEXT,
    object_id       TEXT,
    detail          JSONB,
    at              TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON audit_log (at DESC);

-- Restricted. Only the director and the data scientist hold rights here.
CREATE TABLE identity.link (
    participant_id  INT PRIMARY KEY REFERENCES core.participants ON DELETE CASCADE,
    hospital_id     TEXT,
    full_name       TEXT,
    note            TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

INSERT INTO devices (code, manufacturer, default_rate_hz) VALUES
  ('infinity',   'Draeger',   1),
  ('bettercare', 'BetterCare',200),
  ('nol',        'Medasense', 0.2),
  ('bis',        'Medtronic', NULL),
  ('pump',       'Fresenius', NULL),
  ('other',      NULL,        NULL)
ON CONFLICT DO NOTHING;
