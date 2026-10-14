"""
Extra admin actions: VIEW / EDIT / DELETE for classes, subjects, teachers and
teacher-class-subject assignments (Super Administrator only).

This file only ADDS routes. It does not change the database schema and does
not touch any existing route.

Delete safety: attendance history must never be lost. A record is only
deleted when nothing important depends on it. If it does, the delete is
refused with a clear message and you can use Deactivate instead.
"""

from flask import Blueprint, flash, redirect, render_template, request, url_for

from database.db import get_db
from services import settings_service
from utils.audit import log_action
from utils.decorators import role_required
from utils.validators import is_valid_email

bp = Blueprint("admin_manage", __name__, url_prefix="/admin")
ADMIN_ROLES = ("super_admin",)

CATEGORIES = [("QURANIC", "Qur'anic"), ("ACADEMIC", "Academic"),
              ("ISLAMIC_STUDIES", "Islamic Studies"), ("LANGUAGE", "Language"),
              ("OTHER", "Other")]


def _count(db, sql, params=()):
    return db.execute(sql, params).fetchone()["c"]


def _teacher_options(db):
    rows = db.execute(
        "SELECT t.id, u.full_name FROM teachers t JOIN users u ON u.id = t.user_id "
        "WHERE t.is_active = 1 ORDER BY u.full_name").fetchall()
    return [(r["id"], r["full_name"]) for r in rows]


def _opts(rows, label="name"):
    return [(r["id"], r[label]) for r in rows]


# =================================================================== CLASSES
@bp.route("/classes/<int:class_id>")
@role_required(*ADMIN_ROLES)
def class_view(class_id):
    db = get_db()
    c = db.execute(
        """SELECT c.*, sec.name AS section_name, dep.name AS department_name,
                  u.full_name AS teacher_name, a.name AS session_name
           FROM classes c
           LEFT JOIN sections sec ON sec.id = c.section_id
           LEFT JOIN departments dep ON dep.id = c.department_id
           LEFT JOIN teachers t ON t.id = c.class_teacher_id
           LEFT JOIN users u ON u.id = t.user_id
           LEFT JOIN academic_sessions a ON a.id = c.academic_session_id
           WHERE c.id = ?""", (class_id,)).fetchone()
    if c is None:
        flash("Class not found.", "error")
        return redirect(url_for("school_admin.classes_list"))
    students = db.execute(
        """SELECT st.admission_number, u.full_name, st.status FROM students st
           JOIN users u ON u.id = st.user_id WHERE st.class_id = ? ORDER BY u.full_name""",
        (class_id,)).fetchall()
    subs = db.execute(
        """SELECT sub.name AS subject, tu.full_name AS teacher FROM subject_assignments sa
           JOIN subjects sub ON sub.id = sa.subject_id
           JOIN teachers te ON te.id = sa.teacher_id JOIN users tu ON tu.id = te.user_id
           WHERE sa.class_id = ? ORDER BY sub.name""", (class_id,)).fetchall()
    return render_template(
        "admin/manage_view.html", title="Class: " + c["name"],
        fields=[("Name", c["name"]), ("Section", c["section_name"] or "—"),
                ("Department", c["department_name"] or "—"),
                ("Class teacher", c["teacher_name"] or "—"),
                ("Academic session", c["session_name"] or "—"),
                ("Status", "Active" if c["is_active"] else "Inactive"),
                ("Students", len(students))],
        tables=[("Students", ["Admission no.", "Name", "Status"],
                 [[s["admission_number"], s["full_name"], s["status"]] for s in students]),
                ("Subjects & teachers", ["Subject", "Teacher"],
                 [[s["subject"], s["teacher"]] for s in subs])],
        edit_url=url_for("admin_manage.class_edit", class_id=class_id),
        back_url=url_for("school_admin.classes_list"))


@bp.route("/classes/<int:class_id>/edit", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def class_edit(class_id):
    db = get_db()
    c = db.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
    if c is None:
        flash("Class not found.", "error")
        return redirect(url_for("school_admin.classes_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Class name is required.", "error")
        else:
            db.execute(
                "UPDATE classes SET name = ?, section_id = ?, department_id = ?, class_teacher_id = ? WHERE id = ?",
                (name, request.form.get("section_id") or None,
                 request.form.get("department_id") or None,
                 request.form.get("class_teacher_id") or None, class_id))
            db.commit()
            log_action("class.update", "class", class_id,
                       previous_value={"name": c["name"]}, new_value={"name": name})
            flash("Class updated.", "success")
            return redirect(url_for("school_admin.classes_list"))
    sections = db.execute("SELECT * FROM sections WHERE is_active = 1 ORDER BY name").fetchall()
    depts = db.execute("SELECT * FROM departments WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template(
        "admin/manage_form.html", title="Edit class", submit_label="Save changes",
        back_url=url_for("school_admin.classes_list"),
        fields=[
            dict(name="name", label="Name", type="text", required=True,
                 value=request.form.get("name", c["name"])),
            dict(name="section_id", label="Section", type="select", options=_opts(sections),
                 value=c["section_id"], blank=True),
            dict(name="department_id", label="Department", type="select", options=_opts(depts),
                 value=c["department_id"], blank=True),
            dict(name="class_teacher_id", label="Class teacher", type="select",
                 options=_teacher_options(db), value=c["class_teacher_id"], blank=True)])


@bp.route("/classes/<int:class_id>/activate", methods=("POST",))
@role_required(*ADMIN_ROLES)
def class_activate(class_id):
    db = get_db()
    db.execute("UPDATE classes SET is_active = 1 WHERE id = ?", (class_id,))
    db.commit()
    log_action("class.activate", "class", class_id)
    flash("Class activated.", "success")
    return redirect(url_for("school_admin.classes_list"))


@bp.route("/classes/<int:class_id>/delete", methods=("POST",))
@role_required(*ADMIN_ROLES)
def class_delete(class_id):
    db = get_db()
    c = db.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
    if c is None:
        flash("Class not found.", "error")
        return redirect(url_for("school_admin.classes_list"))
    students = _count(db, "SELECT COUNT(*) c FROM students WHERE class_id = ?", (class_id,))
    sessions = _count(
        db, """SELECT COUNT(*) c FROM attendance_sessions s
               JOIN subject_assignments sa ON sa.id = s.subject_assignment_id
               WHERE sa.class_id = ?""", (class_id,))
    if students:
        flash(f"Cannot delete: {students} student(s) are in this class. Move them to another class first, "
              "or use Deactivate.", "error")
    elif sessions:
        flash("Cannot delete: this class has attendance history. Use Deactivate instead.", "error")
    else:
        db.execute("UPDATE devices SET assigned_class_id = NULL WHERE assigned_class_id = ?", (class_id,))
        db.execute("DELETE FROM subject_assignments WHERE class_id = ?", (class_id,))
        db.execute("DELETE FROM classes WHERE id = ?", (class_id,))
        db.commit()
        log_action("class.delete", "class", class_id, previous_value={"name": c["name"]})
        flash("Class deleted.", "success")
    return redirect(url_for("school_admin.classes_list"))


# ================================================================== SUBJECTS
@bp.route("/subjects/<int:subject_id>")
@role_required(*ADMIN_ROLES)
def subject_view(subject_id):
    db = get_db()
    s = db.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
    if s is None:
        flash("Subject not found.", "error")
        return redirect(url_for("school_admin.subjects_list"))
    rows = db.execute(
        """SELECT c.name AS class_name, u.full_name AS teacher, t.name AS term
           FROM subject_assignments sa JOIN classes c ON c.id = sa.class_id
           JOIN teachers te ON te.id = sa.teacher_id JOIN users u ON u.id = te.user_id
           JOIN terms t ON t.id = sa.term_id WHERE sa.subject_id = ? ORDER BY c.name""",
        (subject_id,)).fetchall()
    return render_template(
        "admin/manage_view.html", title="Subject: " + s["name"],
        fields=[("Code", s["code"]), ("Name", s["name"]),
                ("Category", s["category"].replace("_", " ").title()),
                ("Description", s["description"] or "—"),
                ("Status", "Active" if s["is_active"] else "Inactive")],
        tables=[("Taught in", ["Class", "Teacher", "Term"],
                 [[r["class_name"], r["teacher"], r["term"]] for r in rows])],
        edit_url=url_for("admin_manage.subject_edit", subject_id=subject_id),
        back_url=url_for("school_admin.subjects_list"))


@bp.route("/subjects/<int:subject_id>/edit", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def subject_edit(subject_id):
    db = get_db()
    s = db.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
    if s is None:
        flash("Subject not found.", "error")
        return redirect(url_for("school_admin.subjects_list"))
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "")
        if not code or not name or category not in dict(CATEGORIES):
            flash("Code, name and a valid category are required.", "error")
        else:
            try:
                db.execute(
                    "UPDATE subjects SET code = ?, name = ?, category = ?, description = ? WHERE id = ?",
                    (code, name, category, request.form.get("description", "").strip() or None, subject_id))
                db.commit()
                log_action("subject.update", "subject", subject_id,
                           previous_value={"code": s["code"], "name": s["name"]},
                           new_value={"code": code, "name": name})
                flash("Subject updated.", "success")
                return redirect(url_for("school_admin.subjects_list"))
            except Exception:
                db.rollback()
                flash("Another subject already uses that code.", "error")
    return render_template(
        "admin/manage_form.html", title="Edit subject", submit_label="Save changes",
        back_url=url_for("school_admin.subjects_list"),
        fields=[
            dict(name="code", label="Code", type="text", required=True, value=s["code"]),
            dict(name="name", label="Name", type="text", required=True, value=s["name"]),
            dict(name="category", label="Category", type="select", options=CATEGORIES,
                 value=s["category"]),
            dict(name="description", label="Description", type="text", value=s["description"] or "")])


@bp.route("/subjects/<int:subject_id>/activate", methods=("POST",))
@role_required(*ADMIN_ROLES)
def subject_activate(subject_id):
    db = get_db()
    db.execute("UPDATE subjects SET is_active = 1 WHERE id = ?", (subject_id,))
    db.commit()
    log_action("subject.activate", "subject", subject_id)
    flash("Subject activated.", "success")
    return redirect(url_for("school_admin.subjects_list"))


@bp.route("/subjects/<int:subject_id>/delete", methods=("POST",))
@role_required(*ADMIN_ROLES)
def subject_delete(subject_id):
    db = get_db()
    s = db.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
    if s is None:
        flash("Subject not found.", "error")
        return redirect(url_for("school_admin.subjects_list"))
    sessions = _count(
        db, """SELECT COUNT(*) c FROM attendance_sessions s
               JOIN subject_assignments sa ON sa.id = s.subject_assignment_id
               WHERE sa.subject_id = ?""", (subject_id,))
    if sessions:
        flash("Cannot delete: this subject has attendance history. Use Deactivate instead.", "error")
    else:
        db.execute("DELETE FROM subject_assignments WHERE subject_id = ?", (subject_id,))
        db.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
        db.commit()
        log_action("subject.delete", "subject", subject_id,
                   previous_value={"code": s["code"], "name": s["name"]})
        flash("Subject deleted.", "success")
    return redirect(url_for("school_admin.subjects_list"))


# ================================================================== TEACHERS
def _teacher_row(db, teacher_id):
    return db.execute(
        """SELECT t.*, u.full_name, u.email, u.is_active AS user_active, d.name AS department_name
           FROM teachers t JOIN users u ON u.id = t.user_id
           LEFT JOIN departments d ON d.id = t.department_id WHERE t.id = ?""",
        (teacher_id,)).fetchone()


@bp.route("/teachers/<int:teacher_id>")
@role_required(*ADMIN_ROLES)
def teacher_view(teacher_id):
    db = get_db()
    t = _teacher_row(db, teacher_id)
    if t is None:
        flash("Teacher not found.", "error")
        return redirect(url_for("admin_people.teachers_list"))
    rows = db.execute(
        """SELECT c.name AS class_name, sub.name AS subject, tm.name AS term
           FROM subject_assignments sa JOIN classes c ON c.id = sa.class_id
           JOIN subjects sub ON sub.id = sa.subject_id JOIN terms tm ON tm.id = sa.term_id
           WHERE sa.teacher_id = ? ORDER BY c.name""", (teacher_id,)).fetchall()
    return render_template(
        "admin/manage_view.html", title="Teacher: " + t["full_name"],
        fields=[("Full name", t["full_name"]), ("Staff ID", t["staff_id"]),
                ("Email", t["email"]), ("Phone", t["phone"] or "—"),
                ("Department", t["department_name"] or "—"),
                ("Status", "Active" if t["user_active"] else "Deactivated")],
        tables=[("Classes & subjects", ["Class", "Subject", "Term"],
                 [[r["class_name"], r["subject"], r["term"]] for r in rows])],
        edit_url=url_for("admin_manage.teacher_edit", teacher_id=teacher_id),
        back_url=url_for("admin_people.teachers_list"))


@bp.route("/teachers/<int:teacher_id>/edit", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def teacher_edit(teacher_id):
    db = get_db()
    t = _teacher_row(db, teacher_id)
    if t is None:
        flash("Teacher not found.", "error")
        return redirect(url_for("admin_people.teachers_list"))
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        staff_id = request.form.get("staff_id", "").strip()
        if not full_name or not staff_id or not is_valid_email(email):
            flash("Full name, staff ID and a valid email are required.", "error")
        else:
            try:
                db.execute("UPDATE users SET full_name = ?, email = ? WHERE id = ?",
                           (full_name, email, t["user_id"]))
                db.execute(
                    "UPDATE teachers SET staff_id = ?, phone = ?, department_id = ? WHERE id = ?",
                    (staff_id, request.form.get("phone", "").strip() or None,
                     request.form.get("department_id") or None, teacher_id))
                db.commit()
                log_action("teacher.update", "teacher", teacher_id,
                           previous_value={"staff_id": t["staff_id"], "email": t["email"]},
                           new_value={"staff_id": staff_id, "email": email})
                flash("Teacher updated.", "success")
                return redirect(url_for("admin_people.teachers_list"))
            except Exception:
                db.rollback()
                flash("Another user already has that email or staff ID.", "error")
    depts = db.execute("SELECT * FROM departments WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template(
        "admin/manage_form.html", title="Edit teacher", submit_label="Save changes",
        back_url=url_for("admin_people.teachers_list"),
        fields=[
            dict(name="full_name", label="Full name", type="text", required=True, value=t["full_name"]),
            dict(name="email", label="Email (login)", type="email", required=True, value=t["email"]),
            dict(name="staff_id", label="Staff ID", type="text", required=True, value=t["staff_id"]),
            dict(name="phone", label="Phone", type="text", value=t["phone"] or ""),
            dict(name="department_id", label="Department", type="select", options=_opts(depts),
                 value=t["department_id"], blank=True)])


@bp.route("/teachers/<int:teacher_id>/delete", methods=("POST",))
@role_required(*ADMIN_ROLES)
def teacher_delete(teacher_id):
    db = get_db()
    t = _teacher_row(db, teacher_id)
    if t is None:
        flash("Teacher not found.", "error")
        return redirect(url_for("admin_people.teachers_list"))
    uid = t["user_id"]
    history = (
        _count(db, """SELECT COUNT(*) c FROM attendance_sessions s
                      JOIN subject_assignments sa ON sa.id = s.subject_assignment_id
                      WHERE sa.teacher_id = ?""", (teacher_id,))
        + _count(db, "SELECT COUNT(*) c FROM attendance_sessions WHERE started_by_user_id = ?", (uid,))
        + _count(db, "SELECT COUNT(*) c FROM attendance_records WHERE marked_by_user_id = ?", (uid,))
        + _count(db, "SELECT COUNT(*) c FROM attendance_corrections WHERE corrected_by_user_id = ?", (uid,)))
    if history:
        flash("Cannot delete: this teacher has attendance history. Use Deactivate instead.", "error")
    else:
        db.execute("UPDATE classes SET class_teacher_id = NULL WHERE class_teacher_id = ?", (teacher_id,))
        db.execute("DELETE FROM subject_assignments WHERE teacher_id = ?", (teacher_id,))
        db.execute("DELETE FROM teachers WHERE id = ?", (teacher_id,))
        db.execute("UPDATE audit_logs SET user_id = NULL WHERE user_id = ?", (uid,))
        db.execute("UPDATE import_batches SET imported_by_user_id = NULL WHERE imported_by_user_id = ?", (uid,))
        db.execute("UPDATE parent_students SET linked_by_user_id = NULL WHERE linked_by_user_id = ?", (uid,))
        db.execute("DELETE FROM users WHERE id = ?", (uid,))
        db.commit()
        log_action("teacher.delete", "teacher", teacher_id,
                   previous_value={"name": t["full_name"], "staff_id": t["staff_id"]})
        flash("Teacher deleted.", "success")
    return redirect(url_for("admin_people.teachers_list"))


# ============================================================== ASSIGNMENTS
def _assignment(db, aid):
    return db.execute(
        """SELECT sa.*, sub.name AS subject_name, sub.code, c.name AS class_name,
                  u.full_name AS teacher_name, t.name AS term_name
           FROM subject_assignments sa
           JOIN subjects sub ON sub.id = sa.subject_id JOIN classes c ON c.id = sa.class_id
           JOIN teachers te ON te.id = sa.teacher_id JOIN users u ON u.id = te.user_id
           JOIN terms t ON t.id = sa.term_id WHERE sa.id = ?""", (aid,)).fetchone()


@bp.route("/subject-assignments/<int:aid>")
@role_required(*ADMIN_ROLES)
def assignment_view(aid):
    db = get_db()
    a = _assignment(db, aid)
    if a is None:
        flash("Assignment not found.", "error")
        return redirect(url_for("school_admin.subject_assignments"))
    sessions = _count(db, "SELECT COUNT(*) c FROM attendance_sessions WHERE subject_assignment_id = ?", (aid,))
    return render_template(
        "admin/manage_view.html", title="Assignment",
        fields=[("Subject", f"{a['code']} — {a['subject_name']}"), ("Class", a["class_name"]),
                ("Teacher", a["teacher_name"]), ("Term", a["term_name"]),
                ("Status", "Active" if a["is_active"] else "Inactive"),
                ("Attendance sessions held", sessions)],
        tables=[], edit_url=url_for("admin_manage.assignment_edit", aid=aid),
        back_url=url_for("school_admin.subject_assignments"))


@bp.route("/subject-assignments/<int:aid>/edit", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def assignment_edit(aid):
    db = get_db()
    a = _assignment(db, aid)
    if a is None:
        flash("Assignment not found.", "error")
        return redirect(url_for("school_admin.subject_assignments"))
    if request.method == "POST":
        try:
            db.execute(
                "UPDATE subject_assignments SET subject_id = ?, class_id = ?, teacher_id = ? WHERE id = ?",
                (request.form["subject_id"], request.form["class_id"], request.form["teacher_id"], aid))
            db.commit()
            log_action("subject_assignment.update", "subject_assignment", aid)
            flash("Assignment updated.", "success")
            return redirect(url_for("school_admin.subject_assignments"))
        except Exception:
            db.rollback()
            flash("That subject is already assigned to that class/teacher for this term.", "error")
    subjects = db.execute("SELECT * FROM subjects WHERE is_active = 1 ORDER BY name").fetchall()
    classes = db.execute("SELECT * FROM classes WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template(
        "admin/manage_form.html", title="Edit assignment", submit_label="Save changes",
        back_url=url_for("school_admin.subject_assignments"),
        fields=[
            dict(name="subject_id", label="Subject", type="select", options=_opts(subjects),
                 value=a["subject_id"]),
            dict(name="class_id", label="Class", type="select", options=_opts(classes),
                 value=a["class_id"]),
            dict(name="teacher_id", label="Teacher", type="select", options=_teacher_options(db),
                 value=a["teacher_id"])])


@bp.route("/subject-assignments/<int:aid>/delete", methods=("POST",))
@role_required(*ADMIN_ROLES)
def assignment_delete(aid):
    db = get_db()
    a = _assignment(db, aid)
    if a is None:
        flash("Assignment not found.", "error")
        return redirect(url_for("school_admin.subject_assignments"))
    sessions = _count(db, "SELECT COUNT(*) c FROM attendance_sessions WHERE subject_assignment_id = ?", (aid,))
    if sessions:
        db.execute("UPDATE subject_assignments SET is_active = 0 WHERE id = ?", (aid,))
        db.commit()
        log_action("subject_assignment.deactivate", "subject_assignment", aid)
        flash("This assignment has attendance history, so it was deactivated (hidden from the teacher) "
              "instead of deleted.", "info")
    else:
        db.execute("DELETE FROM subject_assignments WHERE id = ?", (aid,))
        db.commit()
        log_action("subject_assignment.delete", "subject_assignment", aid)
        flash("Assignment deleted.", "success")
    return redirect(url_for("school_admin.subject_assignments"))
