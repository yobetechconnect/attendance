"""
QR attendance verification (Section 17).

Deliberately NOT "a permanent student ID in a QR code, auto-trusted".
Every token is: minted per session, per student, random, short-lived,
single-use, and validated server-side against the session it belongs
to before attendance_service.record_attendance() is ever called.
"""

import secrets
from datetime import datetime, timedelta

from services.settings_service import get_settings


def issue_token(db, session_id: int, student_id: int) -> dict:
    settings = get_settings()
    ttl = settings["qr_token_ttl_seconds"] or 90
    token = secrets.token_urlsafe(24)
    expires_at = datetime.now() + timedelta(seconds=ttl)

    db.execute(
        """INSERT INTO qr_tokens (session_id, student_id, token, expires_at)
           VALUES (?, ?, ?, ?)""",
        (session_id, student_id, token, expires_at.isoformat(timespec="seconds")),
    )
    db.commit()
    return {"token": token, "expires_at": expires_at, "ttl_seconds": ttl}


def redeem_token(db, token: str):
    """
    Validate and consume a QR token. Returns (session_id, student_id) on
    success, or (None, None, error_code) on failure. Does NOT itself
    call record_attendance — the route does that so all methods
    (face/QR/manual) funnel through the same chain.
    """
    row = db.execute("SELECT * FROM qr_tokens WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None, None, "invalid_token"
    if row["used_at"] is not None:
        return None, None, "already_used"
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        return None, None, "expired_token"

    db.execute("UPDATE qr_tokens SET used_at = datetime('now') WHERE id = ?", (row["id"],))
    db.commit()
    return row["session_id"], row["student_id"], None
