"""
Attendance engine.

Owns: session-code generation, the present/late/absent status rule,
the verification chain (the single place that decides whether a
face/QR/manual check-in is allowed to become an attendance_records
row), and authorized corrections with a full audit trail.

Design note (Section 17): this module does NOT know how a face was
recognized or a QR code was read — it only receives "this student_id
was verified by this method with this confidence" from the scanner /
QR endpoint / manual form. That keeps the recognition engine
replaceable without touching attendance rules.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from services.settings_service import get_settings
from utils.audit import log_action
from utils.timeutil import now_str

VERIFICATION_METHODS = ("face", "qr", "rfid", "manual", "offline_sync")
ATTENDANCE_STATUSES = ("present", "late", "absent", "excused", "pending_verification")


@dataclass
class AttendanceResult:
    ok: bool
    code: str  # NOT_RECOGNIZED / ALREADY_RECORDED / NOT_ENROLLED / SESSION_CLOSED / RECORDED / ERROR
    message: str
    student_name: Optional[str] = None
    admission_number: Optional[str] = None
    confidence: Optional[float] = None
    status: Optional[str] = None


def generate_session_code(subject_code: str, class_name: str, when: datetime) -> str:
    """e.g. ATT-20260925-MATH-JSS2A-0800-9F3A — human-traceable and unique."""
    clean_subject = "".join(ch for ch in subject_code.upper() if ch.isalnum())[:8]
    clean_class = "".join(ch for ch in class_name.upper() if ch.isalnum())[:8]
    return (
        f"ATT-{when.strftime('%Y%m%d')}-{clean_subject}-{clean_class}"
        f"-{when.strftime('%H%M')}-{secrets.token_hex(2).upper()}"
    )


def compute_status(session_row, check_in_time: datetime) -> str:
    """Present/Late rule (Section 16), threshold configurable in system_settings."""
    settings = get_settings()
    late_after = settings["late_after_minutes"] or 15
    start = datetime.fromisoformat(session_row["scheduled_start"])
    minutes_late = (check_in_time - start).total_seconds() / 60.0
    if minutes_late <= late_after:
        return "present"
    return "late"


def _log_verification_event(db, session_id, student_id, method, result, confidence=None, device_id=None):
    db.execute(
        """INSERT INTO verification_events
               (session_id, student_id, method, result, confidence, device_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, student_id, method, result, confidence, device_id),
    )


def record_attendance(db, session_id: int, student_id: Optional[int], method: str,
                       confidence: Optional[float] = None, marked_by_user_id: Optional[int] = None,
                       device_id: Optional[int] = None, quran_fields: Optional[dict] = None) -> AttendanceResult:
    """
    The single validation chain every verification method funnels
    through. Never inserts a record unless every check passes.
    """
    if method not in VERIFICATION_METHODS:
        return AttendanceResult(False, "ERROR", "Unknown verification method.")

    if student_id is None:
        _log_verification_event(db, session_id, None, method, "rejected_no_match", confidence, device_id)
        db.commit()
        return AttendanceResult(False, "NOT_RECOGNIZED", "No match found.", confidence=confidence)

    session = db.execute("SELECT * FROM attendance_sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None:
        return AttendanceResult(False, "ERROR", "Attendance session not found.")
    if session["status"] != "active":
        _log_verification_event(db, session_id, student_id, method, "error", confidence, device_id)
        db.commit()
        return AttendanceResult(False, "SESSION_CLOSED", "This attendance session is not active.")

    student = db.execute(
        """SELECT st.*, u.full_name, u.is_active FROM students st
           JOIN users u ON u.id = st.user_id WHERE st.id = ?""",
        (student_id,),
    ).fetchone()
    if student is None or not student["is_active"] or student["status"] != "active":
        _log_verification_event(db, session_id, student_id, method, "rejected_not_enrolled", confidence, device_id)
        db.commit()
        return AttendanceResult(False, "ERROR", "Student record not found or inactive.")

    assignment = db.execute(
        "SELECT * FROM subject_assignments WHERE id = ?", (session["subject_assignment_id"],)
    ).fetchone()
    enrolled_in_class = student["class_id"] == assignment["class_id"]
    if not enrolled_in_class:
        _log_verification_event(db, session_id, student_id, method, "rejected_not_enrolled", confidence, device_id)
        db.commit()
        return AttendanceResult(
            False, "NOT_ENROLLED", "Student is not in this class.",
            student_name=student["full_name"], admission_number=student["admission_number"],
        )

    status = compute_status(session, datetime.now())
    quran_fields = quran_fields or {}
    # Concurrency-safe: the UNIQUE(session_id, student_id) constraint decides the winner
    # when two scans/clicks arrive together; the loser inserts nothing.
    cur = db.execute(
        """INSERT INTO attendance_records
               (session_id, student_id, status, verification_method, confidence, recorded_at,
                marked_by_user_id, quran_juz, quran_surah, quran_note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (session_id, student_id) DO NOTHING""",
        (
            session_id, student_id, status, method, confidence, now_str(), marked_by_user_id,
            quran_fields.get("juz"), quran_fields.get("surah"), quran_fields.get("note"),
        ),
    )
    if cur.rowcount == 0:
        _log_verification_event(db, session_id, student_id, method, "rejected_duplicate", confidence, device_id)
        db.commit()
        return AttendanceResult(
            False, "ALREADY_RECORDED", "Attendance already recorded for this session.",
            student_name=student["full_name"], admission_number=student["admission_number"],
            confidence=confidence,
        )
    _log_verification_event(db, session_id, student_id, method, "success", confidence, device_id)
    db.commit()

    return AttendanceResult(
        True, "RECORDED", "Attendance recorded.",
        student_name=student["full_name"], admission_number=student["admission_number"],
        confidence=confidence, status=status,
    )


def correct_attendance(db, record_id: int, new_status: str, reason: str, corrected_by_user_id: int) -> bool:
    """Never a silent edit (Section 21): logs previous/new status, who, when, why."""
    if new_status not in ATTENDANCE_STATUSES:
        return False
    if not reason or not reason.strip():
        return False

    record = db.execute("SELECT * FROM attendance_records WHERE id = ?", (record_id,)).fetchone()
    if record is None:
        return False

    previous_status = record["status"]
    db.execute("UPDATE attendance_records SET status = ? WHERE id = ?", (new_status, record_id))
    db.execute(
        """INSERT INTO attendance_corrections
               (record_id, previous_status, new_status, reason, corrected_by_user_id)
           VALUES (?, ?, ?, ?, ?)""",
        (record_id, previous_status, new_status, reason.strip(), corrected_by_user_id),
    )
    db.commit()

    log_action(
        "attendance.correction", "attendance_record", record_id,
        previous_value={"status": previous_status}, new_value={"status": new_status},
        reason=reason.strip(),
    )
    return True


def close_session(db, session_id: int, closed_by_user_id: int):
    """Close a session and mark every enrolled-but-unmarked student absent."""
    session = db.execute("SELECT * FROM attendance_sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None or session["status"] == "closed":
        return

    assignment = db.execute(
        "SELECT * FROM subject_assignments WHERE id = ?", (session["subject_assignment_id"],)
    ).fetchone()

    roster = db.execute(
        "SELECT id FROM students WHERE class_id = ? AND status = 'active'",
        (assignment["class_id"],),
    ).fetchall()
    marked = {
        r["student_id"] for r in db.execute(
            "SELECT student_id FROM attendance_records WHERE session_id = ?", (session_id,)
        ).fetchall()
    }
    for student in roster:
        if student["id"] not in marked:
            db.execute(
                """INSERT INTO attendance_records (session_id, student_id, status, verification_method, recorded_at)
                   VALUES (?, ?, 'absent', 'manual', ?)
                   ON CONFLICT (session_id, student_id) DO NOTHING""",
                (session_id, student["id"], now_str()),
            )

    db.execute(
        "UPDATE attendance_sessions SET status = 'closed', closed_at = datetime('now') WHERE id = ?",
        (session_id,),
    )
    db.commit()
    log_action("attendance.session_closed", "attendance_session", session_id)
