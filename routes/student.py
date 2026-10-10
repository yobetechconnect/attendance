"""
Student routes: dashboard (overall + per-subject attendance %),
attendance history, self-service QR check-in token, and account
settings. Students can never mark their own attendance directly —
record_attendance() still runs the full verification chain even for
a QR token they generated.
"""

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database.db import get_db
from services import account_service, qr_service, settings_service
from utils.decorators import role_required
from utils.passwords import password_problem
from utils.validators import is_valid_email, validate_required_fields

bp = Blueprint("student", __name__, url_prefix="/student")


def _student_row(db):
    return db.execute(
        """SELECT st.*, c.name AS class_name FROM students st
           LEFT JOIN classes c ON c.id = st.class_id WHERE st.user_id = ?""",
        (g.user["id"],),
    ).fetchone()


@bp.route("/dashboard")
@role_required("student")
def dashboard():
    db = get_db()
    student = _student_row(db)
    if student is None:
        flash("No student profile is linked to this account.", "error")
        return render_template("student/dashboard.html", student=None)

    overall = db.execute(
        """SELECT
               ROUND(100.0 * SUM(CASE WHEN status IN ('present','late') THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct,
               COUNT(*) AS total
           FROM attendance_records WHERE student_id = ?""",
        (student["id"],),
    ).fetchone()

    per_subject = db.execute(
        """SELECT sub.name AS subject_name, sub.category,
                  COUNT(*) AS total,
                  SUM(CASE WHEN ar.status IN ('present','late') THEN 1 ELSE 0 END) AS attended,
                  ROUND(100.0 * SUM(CASE WHEN ar.status IN ('present','late') THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct
           FROM attendance_records ar
           JOIN attendance_sessions sess ON sess.id = ar.session_id
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           JOIN subjects sub ON sub.id = sa.subject_id
           WHERE ar.student_id = ?
           GROUP BY sub.id ORDER BY sub.name""",
        (student["id"],),
    ).fetchall()

    settings = settings_service.get_settings()
    low_attendance_warning = overall["pct"] is not None and overall["pct"] < settings["attendance_threshold_pct"]

    recent = db.execute(
        """SELECT ar.*, sub.name AS subject_name, sess.session_date
           FROM attendance_records ar
           JOIN attendance_sessions sess ON sess.id = ar.session_id
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           JOIN subjects sub ON sub.id = sa.subject_id
           WHERE ar.student_id = ? ORDER BY ar.recorded_at DESC LIMIT 15""",
        (student["id"],),
    ).fetchall()

    return render_template("student/dashboard.html", student=student, overall=overall,
                            per_subject=per_subject, recent=recent,
                            low_attendance_warning=low_attendance_warning, settings=settings)


@bp.route("/qr")
@role_required("student")
def qr_checkin():
    db = get_db()
    student = _student_row(db)
    active_sessions = db.execute(
        """SELECT sess.*, sub.name AS subject_name FROM attendance_sessions sess
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           JOIN subjects sub ON sub.id = sa.subject_id
           WHERE sa.class_id = ? AND sess.status = 'active'
           ORDER BY sess.started_at DESC""",
        (student["class_id"],),
    ).fetchall() if student and student["class_id"] else []
    return render_template("student/qr_checkin.html", active_sessions=active_sessions)


@bp.route("/qr/<int:session_id>/token", methods=("POST",))
@role_required("student")
def qr_request_token(session_id):
    db = get_db()
    student = _student_row(db)
    session = db.execute(
        """SELECT sess.* FROM attendance_sessions sess
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           WHERE sess.id = ? AND sa.class_id = ? AND sess.status = 'active'""",
        (session_id, student["class_id"]),
    ).fetchone()
    if session is None:
        return jsonify({"error": "Session not found or not active for your class."}), 404

    issued = qr_service.issue_token(db, session_id, student["id"])
    return jsonify({
        "token": issued["token"],
        "expires_at": issued["expires_at"].isoformat(timespec="seconds"),
        "ttl_seconds": issued["ttl_seconds"],
    })


@bp.route("/account", methods=("GET", "POST"))
@role_required("student")
def account_settings():
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
            return render_template("student/account.html")

        db.execute("UPDATE users SET full_name = ?, email = ?, updated_at = datetime('now') WHERE id = ?",
                   (form["full_name"].strip(), form["email"].strip().lower(), g.user["id"]))
        if changing_password:
            session["sv"] = account_service.set_own_password(db, g.user["id"], form["new_password"])
        db.commit()
        flash("Account updated.", "success")
        return redirect(url_for("student.account_settings"))

    return render_template("student/account.html")
