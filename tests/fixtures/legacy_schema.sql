-- ============================================================
-- AL-ISTI'ANAH ACADEMY DAMATURU
-- Smart Attendance & School Management System — Database Schema
-- SQLite (dev/small-scale). See README for the PostgreSQL migration
-- note required before real multi-writer production load.
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- ACADEMIC STRUCTURE (Section 5/6/14) — created before
-- system_settings and classes/students because they reference it.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS academic_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,          -- e.g. "2026/2027"
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS terms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,                 -- e.g. "First Term"
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1)),
    UNIQUE (academic_session_id, name)
);
CREATE INDEX IF NOT EXISTS idx_terms_session ON terms(academic_session_id);

-- ------------------------------------------------------------
-- SYSTEM SETTINGS: single-row table holding school identity &
-- configurable branding/attendance rules (Section 37).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_settings (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    school_name             TEXT NOT NULL DEFAULT "AL-ISTI'ANAH ACADEMY DAMATURU",
    school_name_arabic      TEXT NOT NULL DEFAULT 'أكاديمية الإستعانة دماترو',
    tagline                 TEXT NOT NULL DEFAULT "Excellence in Qur'anic Memorization & Academics",
    address                 TEXT NOT NULL DEFAULT 'Damaturu, Yobe State, Nigeria',
    phone                   TEXT,
    email                   TEXT,
    logo_path               TEXT,                  -- relative path under static/branding/
    timezone                TEXT NOT NULL DEFAULT 'Africa/Lagos',
    current_session_id      INTEGER REFERENCES academic_sessions(id) ON DELETE SET NULL,
    current_term_id         INTEGER REFERENCES terms(id) ON DELETE SET NULL,
    attendance_threshold_pct REAL NOT NULL DEFAULT 75.0,   -- below this => "low attendance"
    late_after_minutes      INTEGER NOT NULL DEFAULT 15,   -- Section 16 default rule
    session_default_minutes INTEGER NOT NULL DEFAULT 60,
    qr_token_ttl_seconds    INTEGER NOT NULL DEFAULT 90,
    enable_face_verification INTEGER NOT NULL DEFAULT 1 CHECK (enable_face_verification IN (0,1)),
    enable_qr_verification  INTEGER NOT NULL DEFAULT 1 CHECK (enable_qr_verification IN (0,1)),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- USERS: base authentication table for all roles (Section 7/8)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN (
                        'super_admin', 'school_admin', 'teacher',
                        'student', 'attendance_operator', 'auditor'
                    )),
    full_name       TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,                          -- failed-login protection (Section 8)
    last_login_at   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

CREATE TABLE IF NOT EXISTS sections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,          -- e.g. "Primary", "Secondary", "Qur'anic"
    description     TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1))
);

CREATE TABLE IF NOT EXISTS departments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id      INTEGER REFERENCES sections(id) ON DELETE SET NULL,
    name            TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    UNIQUE (section_id, name)
);

-- ------------------------------------------------------------
-- PEOPLE (Section 9/10/12) — teachers before classes, since
-- classes.class_teacher_id references teachers.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS teachers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    staff_id        TEXT NOT NULL UNIQUE,
    department_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    phone           TEXT,
    photo_path      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_teachers_user ON teachers(user_id);

CREATE TABLE IF NOT EXISTS classes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,                 -- e.g. "JSS 2A" — admin-defined, not hard-coded
    section_id      INTEGER REFERENCES sections(id) ON DELETE SET NULL,
    department_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE CASCADE,
    class_teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name, academic_session_id)
);
CREATE INDEX IF NOT EXISTS idx_classes_session ON classes(academic_session_id);

CREATE TABLE IF NOT EXISTS subjects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL CHECK (category IN (
                        'QURANIC', 'ACADEMIC', 'ISLAMIC_STUDIES', 'LANGUAGE', 'OTHER'
                    )),
    description     TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A subject as actually taught in one class, by one teacher, for one term
-- (Section 13: "class, teacher, session, term, status")
CREATE TABLE IF NOT EXISTS subject_assignments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id      INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    class_id        INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    teacher_id      INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    term_id         INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    UNIQUE (subject_id, class_id, teacher_id, term_id)
);
CREATE INDEX IF NOT EXISTS idx_subj_assign_class ON subject_assignments(class_id);
CREATE INDEX IF NOT EXISTS idx_subj_assign_teacher ON subject_assignments(teacher_id);

CREATE TABLE IF NOT EXISTS students (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    admission_number    TEXT NOT NULL UNIQUE,
    first_name          TEXT NOT NULL,
    middle_name         TEXT,
    surname             TEXT NOT NULL,
    photo_path          TEXT,
    gender              TEXT CHECK (gender IN ('male', 'female')),
    date_of_birth       TEXT,
    phone               TEXT,
    address             TEXT,
    class_id            INTEGER REFERENCES classes(id) ON DELETE SET NULL,
    academic_session_id INTEGER REFERENCES academic_sessions(id) ON DELETE SET NULL,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                            'active', 'graduated', 'suspended', 'withdrawn',
                            'transferred', 'deferred', 'inactive'
                        )),
    quranic_level       TEXT,                       -- e.g. "Juz 5" — optional (Section 26)
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_students_user ON students(user_id);
CREATE INDEX IF NOT EXISTS idx_students_class ON students(class_id);
CREATE INDEX IF NOT EXISTS idx_students_status ON students(status);
CREATE INDEX IF NOT EXISTS idx_students_admission ON students(admission_number);

CREATE TABLE IF NOT EXISTS guardians (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id      INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    full_name       TEXT NOT NULL,
    relationship    TEXT,
    phone           TEXT,
    email           TEXT,
    is_emergency_contact INTEGER NOT NULL DEFAULT 0 CHECK (is_emergency_contact IN (0,1)),
    notify_by_sms   INTEGER NOT NULL DEFAULT 0 CHECK (notify_by_sms IN (0,1)),
    notify_by_email INTEGER NOT NULL DEFAULT 0 CHECK (notify_by_email IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_guardians_student ON guardians(student_id);

-- Students <-> individually-registered subjects (beyond their class default set)
CREATE TABLE IF NOT EXISTS class_enrollments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id      INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    subject_assignment_id INTEGER NOT NULL REFERENCES subject_assignments(id) ON DELETE CASCADE,
    enrolled_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (student_id, subject_assignment_id)
);
CREATE INDEX IF NOT EXISTS idx_class_enroll_student ON class_enrollments(student_id);
CREATE INDEX IF NOT EXISTS idx_class_enroll_assign ON class_enrollments(subject_assignment_id);

-- ------------------------------------------------------------
-- DEVICES (Section 34)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_code     TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    location        TEXT,
    assigned_class_id INTEGER REFERENCES classes(id) ON DELETE SET NULL,
    api_key_hash    TEXT NOT NULL,                 -- device auth credential
    status          TEXT NOT NULL DEFAULT 'offline' CHECK (status IN ('online', 'offline')),
    software_version TEXT,
    last_seen_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- ATTENDANCE ENGINE (Section 15-21)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attendance_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_code        TEXT NOT NULL UNIQUE,      -- e.g. ATT-20260925-MATH-JSS2A-0800-XXXX
    subject_assignment_id INTEGER NOT NULL REFERENCES subject_assignments(id) ON DELETE CASCADE,
    device_id           INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    session_date        TEXT NOT NULL,
    scheduled_start     TEXT NOT NULL,
    scheduled_end       TEXT,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                            'active', 'paused', 'closed'
                        )),
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    paused_at           TEXT,
    closed_at           TEXT,
    started_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_att_sessions_assignment ON attendance_sessions(subject_assignment_id);
CREATE INDEX IF NOT EXISTS idx_att_sessions_status ON attendance_sessions(status);
CREATE INDEX IF NOT EXISTS idx_att_sessions_date ON attendance_sessions(session_date);

CREATE TABLE IF NOT EXISTS attendance_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
    student_id      INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK (status IN (
                        'present', 'late', 'absent', 'excused', 'pending_verification'
                    )),
    verification_method TEXT NOT NULL DEFAULT 'manual' CHECK (verification_method IN (
                        'face', 'qr', 'rfid', 'manual', 'offline_sync'
                    )),
    confidence      REAL,                          -- face match confidence, if applicable
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    marked_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    quran_juz       TEXT,                          -- optional Qur'anic fields (Section 26)
    quran_surah     TEXT,
    quran_note      TEXT,
    UNIQUE (session_id, student_id)                -- one attendance per student per session
);
CREATE INDEX IF NOT EXISTS idx_records_session ON attendance_records(session_id);
CREATE INDEX IF NOT EXISTS idx_records_student ON attendance_records(student_id);
CREATE INDEX IF NOT EXISTS idx_records_status ON attendance_records(status);

-- Every correction to an existing record — never a silent overwrite (Section 21)
CREATE TABLE IF NOT EXISTS attendance_corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL REFERENCES attendance_records(id) ON DELETE CASCADE,
    previous_status TEXT NOT NULL,
    new_status      TEXT NOT NULL,
    reason          TEXT NOT NULL,
    corrected_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE SET NULL,
    corrected_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_corrections_record ON attendance_corrections(record_id);

-- Every verification attempt, successful or not (Section 17/18 audit trail)
CREATE TABLE IF NOT EXISTS verification_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES attendance_sessions(id) ON DELETE CASCADE,
    student_id      INTEGER REFERENCES students(id) ON DELETE SET NULL,
    method          TEXT NOT NULL CHECK (method IN ('face', 'qr', 'rfid', 'manual')),
    result          TEXT NOT NULL CHECK (result IN (
                        'success', 'rejected_duplicate', 'rejected_not_enrolled',
                        'rejected_expired_token', 'rejected_no_match', 'rejected_multi_face',
                        'error'
                    )),
    confidence      REAL,
    device_id       INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_verif_events_session ON verification_events(session_id);

-- Short-lived, session-specific QR tokens (Section 17: never a static student ID)
CREATE TABLE IF NOT EXISTS qr_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
    student_id      INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    token           TEXT NOT NULL UNIQUE,
    expires_at      TEXT NOT NULL,
    used_at         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qr_tokens_session ON qr_tokens(session_id);

-- Biometric profile metadata, separated from the face engine itself (Section 39)
CREATE TABLE IF NOT EXISTS biometric_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id      INTEGER NOT NULL UNIQUE REFERENCES students(id) ON DELETE CASCADE,
    sample_count    INTEGER NOT NULL DEFAULT 0,
    model_path      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    enrolled_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- OFFLINE SYNC (Section 35) — event queue a device/client posts to,
-- reconciled by the sync endpoint. This is the data model + status
-- machine; conflict-resolution policy is documented in README, not
-- a fully automated resolver.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS offline_sync_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid       TEXT NOT NULL UNIQUE,          -- client-generated UUID, dedupe key
    device_id       INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    session_id      INTEGER REFERENCES attendance_sessions(id) ON DELETE SET NULL,
    student_id      INTEGER REFERENCES students(id) ON DELETE SET NULL,
    payload_json    TEXT NOT NULL,                 -- raw event as captured offline
    occurred_at     TEXT NOT NULL,                 -- client-side timestamp
    received_at     TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                        'pending', 'synced', 'rejected', 'conflict'
                    )),
    resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_events_status ON offline_sync_events(status);

-- ------------------------------------------------------------
-- NOTIFICATIONS (Section 36) — provider-agnostic queue
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_type  TEXT NOT NULL CHECK (recipient_type IN ('user', 'guardian')),
    recipient_id    INTEGER NOT NULL,              -- users.id or guardians.id depending on type
    channel         TEXT NOT NULL CHECK (channel IN ('sms', 'email', 'whatsapp', 'in_app')),
    category        TEXT NOT NULL,                 -- e.g. 'low_attendance', 'correction', 'announcement'
    subject         TEXT,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'failed')),
    provider        TEXT,                          -- which backend handled it (nullable = no-op dev provider)
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);

-- ------------------------------------------------------------
-- AUDIT LOG (Section 22) — append-only, never editable by app code
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action          TEXT NOT NULL,                 -- e.g. 'student.create', 'attendance.correction'
    resource_type   TEXT,
    resource_id     INTEGER,
    previous_value  TEXT,                          -- JSON snapshot, nullable
    new_value       TEXT,                          -- JSON snapshot, nullable
    reason          TEXT,
    ip_address      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);

-- ------------------------------------------------------------
-- BULK IMPORT BATCHES (Section 11) — track each import run
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS import_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    filename        TEXT,
    total_rows      INTEGER NOT NULL DEFAULT 0,
    success_count   INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    invalid_class_count INTEGER NOT NULL DEFAULT 0,
    missing_field_count INTEGER NOT NULL DEFAULT 0,
    error_report_json TEXT,                        -- per-row error detail
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
