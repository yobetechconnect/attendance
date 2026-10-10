"""
Teacher routes: dashboard, attendance session lifecycle (start/pause/
resume/close — Section 20), the face-recognition scanner, QR
redemption, manual attendance, corrections (Section 21), and history.
"""

from datetime import datetime

from flask import (Blueprint, current_app, flash, g, jsonify, redirect,
                    render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from database.db import get_db
from services import account_service, attendance_service, face_detection, qr_service, settings_service
from services.attendance_service import close_session, record_attendance
from services.face_recognition import FaceRecognitionService
from utils.audit import log_action
from utils.decorators import role_required
from utils.passwords import password_problem
from utils.validators import is_valid_email, validate_required_fields

bp = Blueprint("teacher", __name__, url_prefix="/teacher")
TEACHER_ROLES = ("teacher", "attendance_operator", "super_admin")


def _teacher_row(db):
    return db.execute("SELECT * FROM teachers WHERE user_id = ?", (g.user["id"],)).fetchone()


def _recognizer(app):
    return FaceRecognitionService(app.config["FACE_DATA_DIR"], app.config["FACE_RECOGNITION_THRESHOLD"])


def _own_assignment_or_none(db, teacher_id, assignment_id):
    return db.execute(
        "SELECT * FROM subject_assignments WHERE id = ? AND teacher_id = ?",
        (assignment_id, teacher_id),
    ).fetchone()


def _own_session_or_none(db, teacher_id, session_id):
    return db.execute(
        """SELECT sess.* FROM attendance_sessions sess
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           WHERE sess.id = ? AND sa.teacher_id = ?""",
        (session_id, teacher_id),
    ).fetchone()


def _session_counts(db, session_id):
    stat = {"present": 0, "late": 0, "absent": 0, "excused": 0}
    for row in db.execute("SELECT status, COUNT(*) c FROM attendance_records WHERE session_id = ? GROUP BY status",
                          (session_id,)).fetchall():
        if row["status"] in stat:
            stat[row["status"]] = row["c"]
    return stat


# ------------------------------------------------------------------ dashboard
@bp.route("/dashboard")
@role_required(*TEACHER_ROLES)
def dashboard():
    db = get_db()
    teacher = _teacher_row(db)
    if teacher is None:
        flash("No teacher profile is linked to this account.", "error")
        return render_template("teacher/dashboard.html", assignments=[], active_sessions=[], todays_count=0)

    assignments = db.execute(
        """SELECT sa.*, sub.name AS subject_name, sub.code AS subject_code, c.name AS class_name,
                  (SELECT COUNT(*) FROM students st WHERE st.class_id = sa.class_id AND st.status='active') AS enrolled_count
           FROM subject_assignments sa
           JOIN subjects sub ON sub.id = sa.subject_id
           JOIN classes c ON c.id = sa.class_id
           WHERE sa.teacher_id = ? AND sa.is_active = 1
           ORDER BY c.name, sub.name""",
        (teacher["id"],),
    ).fetchall()

    active_sessions = db.execute(
        """SELECT sess.*, sub.name AS subject_name, c.name AS class_name
           FROM attendance_sessions sess
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           JOIN subjects sub ON sub.id = sa.subject_id
           JOIN classes c ON c.id = sa.class_id
           WHERE sa.teacher_id = ? AND sess.status IN ('active', 'paused')
           ORDER BY sess.started_at DESC""",
        (teacher["id"],),
    ).fetchall()

    todays_count = db.execute(
        """SELECT COUNT(*) c FROM attendance_records ar
           JOIN attendance_sessions sess ON sess.id = ar.session_id
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           WHERE sa.teacher_id = ? AND ar.recorded_at >= ?""",
        (teacher["id"], datetime.now().strftime("%Y-%m-%d")),
    ).fetchone()["c"]

    return render_template("teacher/dashboard.html", assignments=assignments,
                            active_sessions=active_sessions, todays_count=todays_count)


# ------------------------------------------------------------- session start
@bp.route("/sessions/start", methods=("POST",))
@role_required(*TEACHER_ROLES)
def sessions_start():
    db = get_db()
    teacher = _teacher_row(db)
    assignment_id = request.form.get("subject_assignment_id")
    assignment = _own_assignment_or_none(db, teacher["id"], assignment_id)
    if assignment is None:
        flash("That class/subject is not assigned to you.", "error")
        return redirect(url_for("teacher.dashboard"))

    existing = db.execute(
        "SELECT id FROM attendance_sessions WHERE subject_assignment_id = ? AND status IN ('active','paused')",
        (assignment_id,),
    ).fetchone()
    if existing:
        flash("An attendance session is already open for this class/subject.", "warning")
        return redirect(url_for("teacher.scanner", session_id=existing["id"]))

    subject = db.execute("SELECT * FROM subjects WHERE id = ?", (assignment["subject_id"],)).fetchone()
    class_row = db.execute("SELECT * FROM classes WHERE id = ?", (assignment["class_id"],)).fetchone()
    now = datetime.now()
    code = attendance_service.generate_session_code(subject["code"], class_row["name"], now)

    cur = db.execute(
        """INSERT INTO attendance_sessions
               (session_code, subject_assignment_id, session_date, scheduled_start, started_by_user_id)
           VALUES (?, ?, ?, ?, ?)""",
        (code, assignment_id, now.strftime("%Y-%m-%d"), now.isoformat(timespec="seconds"), g.user["id"]),
    )
    db.commit()
    log_action("attendance.session_started", "attendance_session", cur.lastrowid)
    flash(f"Attendance session {code} started.", "success")
    return redirect(url_for("teacher.scanner", session_id=cur.lastrowid))


@bp.route("/sessions/<int:session_id>/pause", methods=("POST",))
@role_required(*TEACHER_ROLES)
def sessions_pause(session_id):
    db = get_db()
    teacher = _teacher_row(db)
    session = _own_session_or_none(db, teacher["id"], session_id)
    if session:
        db.execute("UPDATE attendance_sessions SET status='paused', paused_at=datetime('now') WHERE id=?", (session_id,))
        db.commit()
        log_action("attendance.session_paused", "attendance_session", session_id)
        flash("Session paused.", "info")
    return redirect(url_for("teacher.scanner", session_id=session_id))


@bp.route("/sessions/<int:session_id>/resume", methods=("POST",))
@role_required(*TEACHER_ROLES)
def sessions_resume(session_id):
    db = get_db()
    teacher = _teacher_row(db)
    session = _own_session_or_none(db, teacher["id"], session_id)
    if session:
        db.execute("UPDATE attendance_sessions SET status='active', paused_at=NULL WHERE id=?", (session_id,))
        db.commit()
        log_action("attendance.session_resumed", "attendance_session", session_id)
        flash("Session resumed.", "info")
    return redirect(url_for("teacher.scanner", session_id=session_id))


@bp.route("/sessions/<int:session_id>/close", methods=("POST",))
@role_required(*TEACHER_ROLES)
def sessions_close(session_id):
    db = get_db()
    teacher = _teacher_row(db)
    session = _own_session_or_none(db, teacher["id"], session_id)
    if session is None:
        flash("Session not found.", "error")
        return redirect(url_for("teacher.dashboard"))
    close_session(db, session_id, g.user["id"])
    flash("Attendance session closed. Unmarked students recorded as absent.", "success")
    return redirect(url_for("teacher.dashboard"))


# ------------------------------------------------------------------- scanner
@bp.route("/scanner/<int:session_id>")
@role_required(*TEACHER_ROLES)
def scanner(session_id):
    db = get_db()
    teacher = _teacher_row(db)
    session = _own_session_or_none(db, teacher["id"], session_id)
    if session is None:
        flash("Session not found.", "error")
        return redirect(url_for("teacher.dashboard"))

    assignment = db.execute("SELECT * FROM subject_assignments WHERE id = ?",
                             (session["subject_assignment_id"],)).fetchone()
    subject = db.execute("SELECT * FROM subjects WHERE id = ?", (assignment["subject_id"],)).fetchone()
    class_row = db.execute("SELECT * FROM classes WHERE id = ?", (assignment["class_id"],)).fetchone()
    settings = settings_service.get_settings()

    counts = db.execute(
        """SELECT status, COUNT(*) c FROM attendance_records WHERE session_id = ? GROUP BY status""",
        (session_id,),
    ).fetchall()
    stat = {"present": 0, "late": 0, "absent": 0, "excused": 0}
    for row in counts:
        stat[row["status"]] = row["c"]
    enrolled_count = db.execute(
        "SELECT COUNT(*) c FROM students WHERE class_id = ? AND status='active'", (assignment["class_id"],)
    ).fetchone()["c"]

    live_records = db.execute(
        """SELECT ar.*, st.first_name, st.surname, st.admission_number
           FROM attendance_records ar JOIN students st ON st.id = ar.student_id
           WHERE ar.session_id = ? ORDER BY ar.recorded_at DESC LIMIT 30""",
        (session_id,),
    ).fetchall()

    return render_template(
        "teacher/scanner.html", session=session, subject=subject, class_row=class_row,
        stat=stat, enrolled_count=enrolled_count, live_records=live_records, settings=settings,
    )


@bp.route("/scanner/<int:session_id>/recognize", methods=("POST",))
@role_required(*TEACHER_ROLES)
def scanner_recognize(session_id):
    db = get_db()
    teacher = _teacher_row(db)
    session = _own_session_or_none(db, teacher["id"], session_id)
    if session is None:
        return jsonify({"state": "ERROR", "message": "Session not found."}), 404
    if session["status"] != "active":
        return jsonify({"state": "ERROR", "message": "This session is not active."}), 400

    file = request.files.get("frame")
    if file is None or file.filename == "":
        return jsonify({"state": "ERROR", "message": "No frame received."}), 400

    try:
        image = face_detection.decode_image(file.read())
    except ValueError:
        return jsonify({"state": "ERROR", "message": "Could not read the captured frame."}), 400

    try:
        face_crop = face_detection.extract_single_face(image)
    except face_detection.NoFaceDetected:
        return jsonify({"state": "WAITING", "message": "Waiting for a face..."})
    except face_detection.MultipleFacesDetected:
        return jsonify({"state": "ERROR", "message": "Multiple faces detected. One student at a time."})

    recognizer = _recognizer(current_app)
    student_id, confidence = recognizer.recognize(face_crop)

    result = record_attendance(db, session_id, student_id, "face", confidence=confidence)
    return jsonify({
        "state": result.code, "message": result.message, "student_name": result.student_name,
        "admission_number": result.admission_number, "confidence": result.confidence, "status": result.status,
        "counts": _session_counts(db, session_id),
        "recorded_at": datetime.now().strftime("%H:%M:%S"),
    })


# --------------------------------------------------------------- QR redeem
@bp.route("/scanner/<int:session_id>/qr", methods=("POST",))
@role_required(*TEACHER_ROLES)
def scanner_qr_redeem(session_id):
    """Teacher/device submits a token read from a student's QR/code display."""
    db = get_db()
    teacher = _teacher_row(db)
    session = _own_session_or_none(db, teacher["id"], session_id)
    if session is None:
        return jsonify({"state": "ERROR", "message": "Session not found."}), 404

    token = request.form.get("token", "").strip()
    redeemed_session_id, student_id, error = qr_service.redeem_token(db, token)
    if error == "expired_token":
        return jsonify({"state": "ERROR", "message": "This code has expired. Ask the student to refresh it."})
    if error == "already_used":
        return jsonify({"state": "ALREADY_RECORDED", "message": "This code was already used."})
    if error == "invalid_token":
        return jsonify({"state": "ERROR", "message": "Invalid code."})
    if redeemed_session_id != session_id:
        return jsonify({"state": "ERROR", "message": "This code was issued for a different session."})

    result = record_attendance(db, session_id, student_id, "qr")
    return jsonify({
        "state": result.code, "message": result.message, "student_name": result.student_name,
        "admission_number": result.admission_number, "status": result.status,
    })


# ------------------------------------------------------------- manual entry
@bp.route("/sessions/<int:session_id>/manual", methods=("GET", "POST"))
@role_required(*TEACHER_ROLES)
def manual_attendance(session_id):
    db = get_db()
    teacher = _teacher_row(db)
    session = _own_session_or_none(db, teacher["id"], session_id)
    if session is None:
        flash("Session not found.", "error")
        return redirect(url_for("teacher.dashboard"))

    assignment = db.execute("SELECT * FROM subject_assignments WHERE id = ?",
                             (session["subject_assignment_id"],)).fetchone()

    if request.method == "POST":
        student_id = int(request.form.get("student_id"))
        result = record_attendance(db, session_id, student_id, "manual", marked_by_user_id=g.user["id"])
        flash(result.message, "success" if result.ok else "error")
        return redirect(url_for("teacher.manual_attendance", session_id=session_id))

    roster = db.execute(
        """SELECT st.id, st.admission_number, st.first_name, st.surname,
                  EXISTS(SELECT 1 FROM attendance_records ar
                         WHERE ar.session_id = ? AND ar.student_id = st.id) AS is_marked
           FROM students st WHERE st.class_id = ? AND st.status = 'active'
           ORDER BY st.surname, st.first_name""",
        (session_id, assignment["class_id"]),
    ).fetchall()

    return render_template("teacher/manual_attendance.html", session=session, roster=roster)


# ------------------------------------------------------------------ history
@bp.route("/history")
@role_required(*TEACHER_ROLES)
def history():
    db = get_db()
    teacher = _teacher_row(db)
    sessions = db.execute(
        """SELECT sess.*, sub.name AS subject_name, c.name AS class_name,
                  (SELECT COUNT(*) FROM attendance_records ar WHERE ar.session_id = sess.id
                   AND ar.status IN ('present','late')) AS present_count,
                  (SELECT COUNT(*) FROM students st WHERE st.class_id = sa.class_id AND st.status='active') AS enrolled_count
           FROM attendance_sessions sess
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           JOIN subjects sub ON sub.id = sa.subject_id
           JOIN classes c ON c.id = sa.class_id
           WHERE sa.teacher_id = ? ORDER BY sess.started_at DESC LIMIT 50""",
        (teacher["id"],),
    ).fetchall()
    return render_template("teacher/history.html", sessions=sessions)


# ------------------------------------------------------------------ profile
@bp.route("/profile", methods=("GET", "POST"))
@role_required(*TEACHER_ROLES)
def profile():
    db = get_db()
    if request.method == "POST":
        form = request.form
        errors = validate_required_fields(form, ["full_name", "email"])
        if not is_valid_email(form.get("email", "")):
            errors.append("A valid email address is required.")

        changing_password = bool(form.get("new_password"))
        if changing_password:
            if not check_password_hash(g.user["password_hash"], form.get("current_password", "")):
                errors.append("Current password is incorrect.")
            elif password_problem(form.get("new_password", ""), g.user["email"]):
                errors.append(password_problem(form.get("new_password", ""), g.user["email"]))
            elif form.get("new_password") != form.get("confirm_password"):
                errors.append("New password and confirmation do not match.")

        other_email = db.execute("SELECT id FROM users WHERE email = ? AND id != ?",
                                  (form.get("email", "").strip().lower(), g.user["id"])).fetchone()
        if other_email:
            errors.append("That email is already in use by another account.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("teacher/profile.html")

        db.execute("UPDATE users SET full_name = ?, email = ?, updated_at = datetime('now') WHERE id = ?",
                   (form["full_name"].strip(), form["email"].strip().lower(), g.user["id"]))
        if changing_password:
            session["sv"] = account_service.set_own_password(db, g.user["id"], form["new_password"])
        db.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("teacher.profile"))

    return render_template("teacher/profile.html")
