"""
System settings: school identity, branding, and configurable rules.

A single row (id = 1) in system_settings holds everything so templates
and services can render/consult live values instead of hard-coded
branding or thresholds scattered through the codebase (Section 37).
"""

from flask import g

from database.db import get_db

DEFAULTS = {
    "school_name": "AL-ISTI'ANAH ACADEMY DAMATURU",
    "school_name_arabic": "أكاديمية الإستعانة دماترو",
    "tagline": "Excellence in Qur'anic Memorization & Academics",
    "address": "Damaturu, Yobe State, Nigeria",
}


def get_settings():
    """The single settings row, cached for the duration of the request."""
    cached = g.get("_settings")
    if cached is not None:
        return cached
    db = get_db()
    row = db.execute("SELECT * FROM system_settings WHERE id = 1").fetchone()
    if row is None:
        # ON CONFLICT: two simultaneous first requests must not fail each other.
        db.execute(
            """
            INSERT INTO system_settings (id, school_name, school_name_arabic, tagline, address)
            VALUES (1, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING
            """,
            (DEFAULTS["school_name"], DEFAULTS["school_name_arabic"],
             DEFAULTS["tagline"], DEFAULTS["address"]),
        )
        db.commit()
        row = db.execute("SELECT * FROM system_settings WHERE id = 1").fetchone()
    g._settings = row
    return row


def update_settings(fields: dict):
    """fields: column_name -> new value, only for known columns."""
    allowed = {
        "school_name", "school_name_arabic", "tagline", "address", "phone", "email",
        "logo_path", "logo_url", "temp_admin_can_reset_passwords", "timezone", "current_session_id", "current_term_id",
        "attendance_threshold_pct", "late_after_minutes", "session_default_minutes",
        "qr_token_ttl_seconds", "enable_face_verification", "enable_qr_verification",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_settings()
    g.pop("_settings", None)

    get_settings()  # ensure row exists
    db = get_db()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    db.execute(
        f"UPDATE system_settings SET {set_clause}, updated_at = datetime('now') WHERE id = 1",
        list(updates.values()),
    )
    db.commit()
    g.pop("_settings", None)
    return get_settings()


def current_session_and_term():
    s = get_settings()
    db = get_db()
    session_row = None
    term_row = None
    if s["current_session_id"]:
        session_row = db.execute(
            "SELECT * FROM academic_sessions WHERE id = ?", (s["current_session_id"],)
        ).fetchone()
    if s["current_term_id"]:
        term_row = db.execute(
            "SELECT * FROM terms WHERE id = ?", (s["current_term_id"],)
        ).fetchone()
    return session_row, term_row
