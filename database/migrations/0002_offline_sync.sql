-- ============================================================
-- 0002 OFFLINE SYNC (Stage 1)
-- ADDITIVE ONLY: new nullable/defaulted columns, new tables, new indexes.
-- Nothing is dropped, renamed or rewritten. Existing rows keep working:
-- online sessions/records get source = 'online' and NULL client ids.
-- ============================================================

-- Sessions created from an offline device carry the device's stable UUID
ALTER TABLE attendance_sessions ADD COLUMN client_uid TEXT;
ALTER TABLE attendance_sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'online';
ALTER TABLE attendance_sessions ADD COLUMN device_uid TEXT;
ALTER TABLE attendance_sessions ADD COLUMN synced_at TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_att_sessions_client_uid
    ON attendance_sessions(client_uid) WHERE client_uid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_att_sessions_natural
    ON attendance_sessions(subject_assignment_id, session_date, source);

ALTER TABLE attendance_records ADD COLUMN client_uid TEXT;
ALTER TABLE attendance_records ADD COLUMN source TEXT NOT NULL DEFAULT 'online';
ALTER TABLE attendance_records ADD COLUMN device_uid TEXT;
ALTER TABLE attendance_records ADD COLUMN client_recorded_at TEXT;
ALTER TABLE attendance_records ADD COLUMN synced_at TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_att_records_client_uid
    ON attendance_records(client_uid) WHERE client_uid IS NOT NULL;

-- Phones/tablets that have synchronised (one row per device + user)
CREATE TABLE IF NOT EXISTS client_devices (
    id              {{PK}},
    device_uid      TEXT NOT NULL,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_agent      TEXT,
    first_seen_at   TEXT NOT NULL DEFAULT {{NOW}},
    last_seen_at    TEXT NOT NULL DEFAULT {{NOW}},
    last_sync_at    TEXT,
    UNIQUE (device_uid, user_id)
);

-- Idempotency ledger: one row per client record the server has DECIDED about
-- (created, duplicate, conflict or rejected). Replaying a request returns the
-- stored decision instead of repeating the work.
CREATE TABLE IF NOT EXISTS sync_receipts (
    id              {{PK}},
    client_uid      TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL CHECK (kind IN (
                        'attendance_session', 'attendance_record', 'enrollment', 'correction'
                    )),
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    device_uid      TEXT,
    server_id       INTEGER,
    result          TEXT NOT NULL,
    reason          TEXT,
    received_at     TEXT NOT NULL DEFAULT {{NOW}}
);
CREATE INDEX IF NOT EXISTS idx_sync_receipts_user ON sync_receipts(user_id);

-- Everything the server did NOT silently accept: conflicts and rejections,
-- with both versions kept for administrator review.
CREATE TABLE IF NOT EXISTS sync_conflicts (
    id                  {{PK}},
    kind                TEXT NOT NULL,
    session_id          INTEGER REFERENCES attendance_sessions(id) ON DELETE SET NULL,
    record_id           INTEGER REFERENCES attendance_records(id) ON DELETE SET NULL,
    student_id          INTEGER REFERENCES students(id) ON DELETE SET NULL,
    user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
    device_uid          TEXT,
    client_uid          TEXT,
    reason              TEXT NOT NULL,
    existing_json       TEXT,
    incoming_json       TEXT NOT NULL,
    resolution          TEXT NOT NULL CHECK (resolution IN (
                            'needs_review', 'kept_existing', 'applied_incoming', 'dismissed'
                        )),
    created_at          TEXT NOT NULL DEFAULT {{NOW}},
    reviewed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at         TEXT,
    review_note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_conflicts_resolution ON sync_conflicts(resolution);

-- STAGE 2/3 PREPARATION (empty until the browser face engine ships).
-- Browser templates live here, tagged by engine, and are NEVER mixed with
-- the existing LBPH data (which stays in the face_data files).
CREATE TABLE IF NOT EXISTS face_templates (
    id                  {{PK}},
    student_id          INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    engine              TEXT NOT NULL,
    engine_version      TEXT NOT NULL,
    enrollment_uid      TEXT NOT NULL UNIQUE,
    template_b64        TEXT NOT NULL,
    sample_count        INTEGER NOT NULL DEFAULT 1,
    quality_score       {{REAL}},
    source              TEXT NOT NULL DEFAULT 'online',
    device_uid          TEXT,
    created_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at          TEXT NOT NULL DEFAULT {{NOW}},
    synced_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_face_templates_student ON face_templates(student_id, engine, is_active);
