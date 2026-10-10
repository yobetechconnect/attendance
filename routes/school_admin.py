"""
School admin routes: dashboard, school settings/branding, and the
academic structure (sessions, terms, sections, departments, classes,
subjects, subject assignments). Section 5/6/14/23/37.

Anything destructive (delete) is intentionally absent — classes,
subjects, etc. are deactivated, never deleted, since attendance
history may reference them (Section 9's "don't destroy history" rule
applies to the whole structure, not just students).
"""

from datetime import datetime

from flask import (Blueprint, current_app, flash, g, redirect,
                    render_template, request, send_from_directory, url_for)

from database.db import get_db
from services import settings_service
from utils.audit import log_action
from utils.decorators import role_required
from utils.validators import is_non_empty, validate_logo_url, validate_required_fields

bp = Blueprint("school_admin", __name__, url_prefix="/admin")

ADMIN_ROLES = ("super_admin",)   # only the Super Administrator is unrestricted


def _num(raw, default, cast, lo, hi):
    try:
        return min(max(cast(raw), lo), hi)
    except (TypeError, ValueError):
        return default


@bp.route("/")
@role_required(*ADMIN_ROLES)
def dashboard():
    db = get_db()
    settings = settings_service.get_settings()
    session_row, term_row = settings_service.current_session_and_term()
    today = datetime.now().strftime("%Y-%m-%d")

    total_students = db.execute(
        "SELECT COUNT(*) c FROM students WHERE status = 'active'"
    ).fetchone()["c"]

    today_stats = db.execute(
        """
        SELECT ar.status, COUNT(*) c
        FROM attendance_records ar
        JOIN attendance_sessions s ON s.id = ar.session_id
        WHERE s.session_date = ?
        GROUP BY ar.status
        """,
        (today,),
    ).fetchall()
    stats = {"present": 0, "late": 0, "absent": 0, "excused": 0, "pending_verification": 0}
    for row in today_stats:
        stats[row["status"]] = row["c"]
    marked_today = sum(stats.values())
    attendance_pct = round(100.0 * (stats["present"] + stats["late"]) / marked_today, 1) if marked_today else 0.0

    active_sessions = db.execute(
        "SELECT COUNT(*) c FROM attendance_sessions WHERE status IN ('active','paused')"
    ).fetchone()["c"]

    counts = {
        "teachers": db.execute("SELECT COUNT(*) c FROM teachers WHERE is_active = 1").fetchone()["c"],
        "classes": db.execute("SELECT COUNT(*) c FROM classes WHERE is_active = 1").fetchone()["c"],
        "subjects": db.execute("SELECT COUNT(*) c FROM subjects WHERE is_active = 1").fetchone()["c"],
        "sections": db.execute("SELECT COUNT(*) c FROM sections WHERE is_active = 1").fetchone()["c"],
    }

    low_attendance = db.execute(
        """
        SELECT st.id, st.first_name, st.surname, st.admission_number,
               ROUND(100.0 * SUM(CASE WHEN ar.status IN ('present','late') THEN 1 ELSE 0 END) / COUNT(*), 1) pct
        FROM students st
        JOIN attendance_records ar ON ar.student_id = st.id
        WHERE st.status = 'active'
        GROUP BY st.id
        HAVING (100.0 * SUM(CASE WHEN ar.status IN ('present','late') THEN 1 ELSE 0 END) / COUNT(*)) < ?
        ORDER BY pct ASC LIMIT 10
        """,
        (settings["attendance_threshold_pct"],),
    ).fetchall()

    return render_template(
        "admin/dashboard.html", stats=stats, marked_today=marked_today,
        attendance_pct=attendance_pct, total_students=total_students,
        active_sessions=active_sessions, counts=counts, settings=settings,
        session_row=session_row, term_row=term_row, low_attendance=low_attendance,
    )


# ---------------------------------------------------------------- settings
@bp.route("/settings", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def settings_page():
    db = get_db()
    if request.method == "POST":
        fields = {
            "school_name": request.form.get("school_name", "").strip(),
            "school_name_arabic": request.form.get("school_name_arabic", "").strip(),
            "tagline": request.form.get("tagline", "").strip(),
            "address": request.form.get("address", "").strip(),
            "phone": request.form.get("phone", "").strip() or None,
            "email": request.form.get("email", "").strip() or None,
            "timezone": request.form.get("timezone", "Africa/Lagos").strip(),
            "attendance_threshold_pct": _num(request.form.get("attendance_threshold_pct"), 75.0, float, 0, 100),
            "late_after_minutes": _num(request.form.get("late_after_minutes"), 15, int, 0, 600),
            "session_default_minutes": _num(request.form.get("session_default_minutes"), 60, int, 1, 600),
            "qr_token_ttl_seconds": _num(request.form.get("qr_token_ttl_seconds"), 90, int, 10, 3600),
            "enable_face_verification": 1 if request.form.get("enable_face_verification") else 0,
            "enable_qr_verification": 1 if request.form.get("enable_qr_verification") else 0,
        }
        current_session_id = request.form.get("current_session_id")
        current_term_id = request.form.get("current_term_id")
        if current_session_id:
            fields["current_session_id"] = int(current_session_id)
        if current_term_id:
            fields["current_term_id"] = int(current_term_id)

        # Logo is configured by https image URL (validated; never fetched by the server)
        logo_url, logo_error = validate_logo_url(request.form.get("logo_url", ""))
        if logo_error:
            flash(logo_error, "error")
            return redirect(url_for("school_admin.settings_page"))
        fields["logo_url"] = logo_url

        before = dict(settings_service.get_settings())
        settings_service.update_settings(fields)
        log_action("settings.update", "system_settings", 1, previous_value=before, new_value=fields)
        flash("School settings updated.", "success")
        return redirect(url_for("school_admin.settings_page"))

    settings = settings_service.get_settings()
    sessions = db.execute("SELECT * FROM academic_sessions ORDER BY start_date DESC").fetchall()
    terms = db.execute("SELECT * FROM terms ORDER BY start_date DESC").fetchall()
    return render_template("admin/settings.html", settings=settings, sessions=sessions, terms=terms)


@bp.route("/branding/<path:filename>")
def branding_file(filename):
    return send_from_directory(current_app.config["LOGO_UPLOAD_DIR"], filename)


# ---------------------------------------------------------- academic sessions
@bp.route("/academic-sessions", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def academic_sessions():
    db = get_db()
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["name", "start_date", "end_date"])
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            cur = db.execute(
                "INSERT INTO academic_sessions (name, start_date, end_date) VALUES (?, ?, ?)",
                (request.form["name"].strip(), request.form["start_date"], request.form["end_date"]),
            )
            db.commit()
            log_action("academic_session.create", "academic_session", cur.lastrowid)
            flash("Academic session created.", "success")
        return redirect(url_for("school_admin.academic_sessions"))

    rows = db.execute("SELECT * FROM academic_sessions ORDER BY start_date DESC").fetchall()
    return render_template("admin/academic_sessions.html", rows=rows)


@bp.route("/academic-sessions/<int:session_id>/activate", methods=("POST",))
@role_required(*ADMIN_ROLES)
def activate_academic_session(session_id):
    db = get_db()
    db.execute("UPDATE academic_sessions SET is_active = 0")
    db.execute("UPDATE academic_sessions SET is_active = 1 WHERE id = ?", (session_id,))
    settings_service.update_settings({"current_session_id": session_id})
    db.commit()
    log_action("academic_session.activate", "academic_session", session_id)
    flash("Academic session activated.", "success")
    return redirect(url_for("school_admin.academic_sessions"))


# --------------------------------------------------------------------- terms
@bp.route("/terms", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def terms():
    db = get_db()
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["academic_session_id", "name", "start_date", "end_date"])
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            cur = db.execute(
                "INSERT INTO terms (academic_session_id, name, start_date, end_date) VALUES (?, ?, ?, ?)",
                (int(request.form["academic_session_id"]), request.form["name"].strip(),
                 request.form["start_date"], request.form["end_date"]),
            )
            db.commit()
            log_action("term.create", "term", cur.lastrowid)
            flash("Term created.", "success")
        return redirect(url_for("school_admin.terms"))

    rows = db.execute(
        """SELECT t.*, s.name AS session_name FROM terms t
           JOIN academic_sessions s ON s.id = t.academic_session_id
           ORDER BY t.start_date DESC"""
    ).fetchall()
    sessions = db.execute("SELECT * FROM academic_sessions ORDER BY start_date DESC").fetchall()
    return render_template("admin/terms.html", rows=rows, sessions=sessions)


@bp.route("/terms/<int:term_id>/activate", methods=("POST",))
@role_required(*ADMIN_ROLES)
def activate_term(term_id):
    db = get_db()
    term = db.execute("SELECT * FROM terms WHERE id = ?", (term_id,)).fetchone()
    if term:
        db.execute("UPDATE terms SET is_active = 0 WHERE academic_session_id = ?", (term["academic_session_id"],))
        db.execute("UPDATE terms SET is_active = 1 WHERE id = ?", (term_id,))
        settings_service.update_settings({"current_term_id": term_id})
        db.commit()
        log_action("term.activate", "term", term_id)
        flash("Term activated.", "success")
    return redirect(url_for("school_admin.terms"))


# ------------------------------------------------------------ sections/depts
@bp.route("/sections", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def sections():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not is_non_empty(name):
            flash("Section name is required.", "error")
        else:
            try:
                cur = db.execute("INSERT INTO sections (name, description) VALUES (?, ?)",
                                  (name, request.form.get("description", "").strip() or None))
                db.commit()
                log_action("section.create", "section", cur.lastrowid)
                flash("Section created.", "success")
            except Exception:
                flash("A section with that name already exists.", "error")
        return redirect(url_for("school_admin.sections"))

    rows = db.execute("SELECT * FROM sections ORDER BY name").fetchall()
    return render_template("admin/sections.html", rows=rows)


@bp.route("/departments", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def departments():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        section_id = request.form.get("section_id") or None
        if not is_non_empty(name):
            flash("Department name is required.", "error")
        else:
            cur = db.execute("INSERT INTO departments (section_id, name) VALUES (?, ?)",
                              (section_id, name))
            db.commit()
            log_action("department.create", "department", cur.lastrowid)
            flash("Department created.", "success")
        return redirect(url_for("school_admin.departments"))

    rows = db.execute(
        """SELECT d.*, s.name AS section_name FROM departments d
           LEFT JOIN sections s ON s.id = d.section_id ORDER BY d.name"""
    ).fetchall()
    sections_rows = db.execute("SELECT * FROM sections WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template("admin/departments.html", rows=rows, sections=sections_rows)


# ------------------------------------------------------------------ classes
@bp.route("/classes", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def classes_list():
    db = get_db()
    settings = settings_service.get_settings()
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["name"])
        session_id = settings["current_session_id"] or request.form.get("academic_session_id")
        if not session_id:
            errors.append("No academic session is active. Create/activate one first.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            cur = db.execute(
                """INSERT INTO classes (name, section_id, department_id, academic_session_id, class_teacher_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (request.form["name"].strip(), request.form.get("section_id") or None,
                 request.form.get("department_id") or None, session_id,
                 request.form.get("class_teacher_id") or None),
            )
            db.commit()
            log_action("class.create", "class", cur.lastrowid)
            flash("Class created.", "success")
        return redirect(url_for("school_admin.classes_list"))

    rows = db.execute(
        """SELECT c.*, sec.name AS section_name, dep.name AS department_name,
                  t.staff_id, u.full_name AS teacher_name,
                  (SELECT COUNT(*) FROM students st WHERE st.class_id = c.id AND st.status='active') AS student_count
           FROM classes c
           LEFT JOIN sections sec ON sec.id = c.section_id
           LEFT JOIN departments dep ON dep.id = c.department_id
           LEFT JOIN teachers t ON t.id = c.class_teacher_id
           LEFT JOIN users u ON u.id = t.user_id
           ORDER BY c.name"""
    ).fetchall()
    sections_rows = db.execute("SELECT * FROM sections WHERE is_active = 1 ORDER BY name").fetchall()
    departments_rows = db.execute("SELECT * FROM departments WHERE is_active = 1 ORDER BY name").fetchall()
    teachers_rows = db.execute(
        "SELECT t.id, u.full_name FROM teachers t JOIN users u ON u.id = t.user_id WHERE t.is_active = 1 ORDER BY u.full_name"
    ).fetchall()
    return render_template("admin/classes.html", rows=rows, sections=sections_rows,
                            departments=departments_rows, teachers=teachers_rows)


@bp.route("/classes/<int:class_id>/deactivate", methods=("POST",))
@role_required(*ADMIN_ROLES)
def deactivate_class(class_id):
    db = get_db()
    db.execute("UPDATE classes SET is_active = 0 WHERE id = ?", (class_id,))
    db.commit()
    log_action("class.deactivate", "class", class_id)
    flash("Class deactivated.", "info")
    return redirect(url_for("school_admin.classes_list"))


# ----------------------------------------------------------------- subjects
@bp.route("/subjects", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def subjects_list():
    db = get_db()
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["code", "name", "category"])
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            try:
                cur = db.execute(
                    "INSERT INTO subjects (code, name, category, description) VALUES (?, ?, ?, ?)",
                    (request.form["code"].strip().upper(), request.form["name"].strip(),
                     request.form["category"], request.form.get("description", "").strip() or None),
                )
                db.commit()
                log_action("subject.create", "subject", cur.lastrowid)
                flash("Subject created.", "success")
            except Exception:
                flash("A subject with that code already exists.", "error")
        return redirect(url_for("school_admin.subjects_list"))

    rows = db.execute("SELECT * FROM subjects ORDER BY category, name").fetchall()
    return render_template("admin/subjects.html", rows=rows)


@bp.route("/subjects/<int:subject_id>/deactivate", methods=("POST",))
@role_required(*ADMIN_ROLES)
def deactivate_subject(subject_id):
    db = get_db()
    db.execute("UPDATE subjects SET is_active = 0 WHERE id = ?", (subject_id,))
    db.commit()
    log_action("subject.deactivate", "subject", subject_id)
    flash("Subject deactivated.", "info")
    return redirect(url_for("school_admin.subjects_list"))


# ------------------------------------------------------- subject assignments
@bp.route("/subject-assignments", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def subject_assignments():
    db = get_db()
    settings = settings_service.get_settings()
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["subject_id", "class_id", "teacher_id"])
        term_id = settings["current_term_id"] or request.form.get("term_id")
        if not term_id:
            errors.append("No term is active. Set one in Settings first.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            try:
                cur = db.execute(
                    """INSERT INTO subject_assignments (subject_id, class_id, teacher_id, term_id)
                       VALUES (?, ?, ?, ?)""",
                    (request.form["subject_id"], request.form["class_id"], request.form["teacher_id"], term_id),
                )
                db.commit()
                log_action("subject_assignment.create", "subject_assignment", cur.lastrowid)
                flash("Teacher assigned to class/subject.", "success")
            except Exception:
                flash("This subject is already assigned to that class/teacher/term.", "error")
        return redirect(url_for("school_admin.subject_assignments"))

    rows = db.execute(
        """SELECT sa.*, sub.code, sub.name AS subject_name, c.name AS class_name,
                  u.full_name AS teacher_name, t.name AS term_name
           FROM subject_assignments sa
           JOIN subjects sub ON sub.id = sa.subject_id
           JOIN classes c ON c.id = sa.class_id
           JOIN teachers te ON te.id = sa.teacher_id
           JOIN users u ON u.id = te.user_id
           JOIN terms t ON t.id = sa.term_id
           ORDER BY c.name, sub.name"""
    ).fetchall()
    subjects_rows = db.execute("SELECT * FROM subjects WHERE is_active = 1 ORDER BY name").fetchall()
    classes_rows = db.execute("SELECT * FROM classes WHERE is_active = 1 ORDER BY name").fetchall()
    teachers_rows = db.execute(
        "SELECT t.id, u.full_name FROM teachers t JOIN users u ON u.id = t.user_id WHERE t.is_active = 1 ORDER BY u.full_name"
    ).fetchall()
    return render_template("admin/subject_assignments.html", rows=rows, subjects=subjects_rows,
                            classes=classes_rows, teachers=teachers_rows)


# --------------------------------------------------------------- audit log
@bp.route("/audit-log")
@role_required("super_admin", "auditor")
def audit_log():
    db = get_db()
    page = max(int(request.args.get("page", 1)), 1)
    per_page = 50
    total = db.execute("SELECT COUNT(*) c FROM audit_logs").fetchone()["c"]
    rows = db.execute(
        """SELECT al.*, u.full_name, u.email FROM audit_logs al
           LEFT JOIN users u ON u.id = al.user_id
           ORDER BY al.created_at DESC LIMIT ? OFFSET ?""",
        (per_page, (page - 1) * per_page),
    ).fetchall()
    return render_template("admin/audit_log.html", rows=rows, page=page, per_page=per_page, total=total)
