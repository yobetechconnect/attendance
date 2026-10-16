-- ============================================================
-- 0003 OFFLINE FACE (Stage 2/3)  -  ADDITIVE ONLY
-- Existing LBPH data (biometric_profiles + face_data files) is untouched.
-- ============================================================

-- consent + lifecycle for browser face templates
ALTER TABLE face_templates ADD COLUMN consent_confirmed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE face_templates ADD COLUMN consent_note TEXT;
ALTER TABLE face_templates ADD COLUMN deactivated_at TEXT;

-- At most ONE active template per student and engine: accidental duplicate
-- enrollments are impossible at the database level.
CREATE UNIQUE INDEX IF NOT EXISTS uq_face_templates_one_active
    ON face_templates(student_id, engine) WHERE is_active = 1;

-- what produced a face match (for audit and for re-checking after a model change)
ALTER TABLE attendance_records ADD COLUMN match_engine TEXT;
ALTER TABLE attendance_records ADD COLUMN match_engine_version TEXT;
ALTER TABLE attendance_records ADD COLUMN match_template_uid TEXT;
