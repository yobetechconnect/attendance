"""
Student and teacher management, guardian records, and bulk student import.

Who can do what (enforced server-side on every route):
  Super Administrator ........ everything on this page, plus teachers
  Temporary Administrator 1/2  student management only: list/search/view,
                               register, edit details, issue credentials,
                               and reset passwords IF the Super Administrator
                               has enabled that in School Settings.

Newly issued temporary passwords are shown ONCE in a confirmation dialog
rendered directly in the POST response (never via flash/session/cookies,
never logged, never stored in plaintext) and the response is no-store.

Students are never hard-deleted — only their status changes — because
attendance history references them.
"""

from uuid import uuid4

from flask import (Blueprint, flash, g, redirect, render_template, request, url_for)
from werkzeug.utils import secure_filename

from database.db import get_db
from services import account_service, import_service, settings_service
from services.account_service import credential
from utils.audit import log_action
from utils.decorators import permission_required, role_required
from utils.validators import is_valid_email, validate_required_fields

bp = Blueprint("admin_people", __name__, url_prefix="/admin")
ADMIN_ROLES = ("super_admin",)
STUDENT_STATUSES = ("active", "graduated", "suspended", "withdrawn", "transferred", "deferred", "inactive")
PER_PAGE = 50


def _page_arg() -> int:
    try:
        return max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        return 1


# ======================================================================
# TEACHERS  (Super Administrator only)
# ======================================================================
def _teachers_context(db):
    q = request.args.get("q", "").strip()
    page = _page_arg()
    where, params = "", []
    if q:
        where = "WHERE u.full_name LIKE ? OR t.staff_id LIKE ? OR u.email LIKE ?"
        params = [f"%{q}%"] * 3
    total = db.execute(f"SELECT COUNT(*) c FROM teachers t JOIN users u ON u.id = t.user_id {where}", params).fetchone()["c"]
    rows = db.execute(
        f"""SELECT t.*, u.full_name, u.email, u.is_active, u.must_change_password, d.name AS department_name
            FROM teachers t JOIN users u ON u.id = t.user_id
            LEFT JOIN departments d ON d.id = t.department_id
            {where} ORDER BY u.full_name LIMIT ? OFFSET ?""",
        params + [PER_PAGE, (page - 1) * PER_PAGE],
    ).fetchall()
    departments = db.execute("SELECT * FROM departments WHERE is_active = 1 ORDER BY name").fetchall()
    return dict(rows=rows, departments=departments, q=q, page=page, per_page=PER_PAGE, total=total)


@bp.route("/teachers", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def teachers_list():
    db = get_db()
    credentials = None
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["full_name", "email", "staff_id"])
        if not is_valid_email(request.form.get("email", "")):
            errors.append("A valid email is required.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            try:
                # one transaction: user + teacher profile succeed or fail together
                user_id, temp = account_service.create_user(
                    db, request.form["email"], "teacher", request.form["full_name"])
                cur = db.execute(
                    "INSERT INTO teachers (user_id, staff_id, department_id, phone) VALUES (?, ?, ?, ?)",
                    (user_id, request.form["staff_id"].strip(), request.form.get("department_id") or None,
                     request.form.get("phone", "").strip() or None),
                )
                db.commit()
                log_action("teacher.create", "teacher", cur.lastrowid, new_value={"staff_id": request.form["staff_id"].strip()})
                credentials = [credential(
                    title="Teacher account created successfully",
                    name=request.form["full_name"].strip(), identifier_label="Staff ID",
                    identifier=request.form["staff_id"].strip(),
                    username=request.form["email"].strip().lower(), password=temp, role_label="Teacher")]
            except Exception:
                flash("A teacher or user with that email or staff ID already exists.", "error")
            else:
                pass
        if credentials is None:
            return redirect(url_for("admin_people.teachers_list"))

    return render_template("admin/teachers_list.html", credentials=credentials, **_teachers_context(db))


@bp.route("/teachers/<int:teacher_id>/reset-password", methods=("POST",))
@role_required(*ADMIN_ROLES)
def reset_teacher_password(teacher_id):
    db = get_db()
    row = db.execute(
        "SELECT t.id, t.staff_id, t.user_id, u.full_name, u.email FROM teachers t JOIN users u ON u.id = t.user_id WHERE t.id = ?",
        (teacher_id,)).fetchone()
    if row is None:
        flash("Teacher not found.", "error")
        return redirect(url_for("admin_people.teachers_list"))
    temp = account_service.reset_password(db, row["user_id"])
    db.commit()
    log_action("teacher.password_reset", "teacher", teacher_id)
    credentials = [credential(
        title="Password reset successfully", name=row["full_name"], identifier_label="Staff ID",
        identifier=row["staff_id"], username=row["email"], password=temp, role_label="Teacher")]
    return render_template("admin/teachers_list.html", credentials=credentials, **_teachers_context(db))


@bp.route("/teachers/<int:teacher_id>/toggle-active", methods=("POST",))
@role_required(*ADMIN_ROLES)
def toggle_teacher_active(teacher_id):
    db = get_db()
    row = db.execute("SELECT t.*, u.is_active AS user_active FROM teachers t JOIN users u ON u.id=t.user_id WHERE t.id=?",
                     (teacher_id,)).fetchone()
    if row:
        new_state = row["user_active"] == 0
        account_service.set_active(db, row["user_id"], new_state)
        db.commit()
        log_action("teacher.toggle_active", "teacher", teacher_id, new_value={"is_active": int(new_state)})
        flash("Teacher status updated.", "success")
    return redirect(url_for("admin_people.teachers_list"))


# ======================================================================
# STUDENTS  (Super Administrator + Temporary Administrators)
# ======================================================================
def _students_context(db):
    q = request.args.get("q", "").strip()
    class_filter = request.args.get("class_id", "")
    status_filter = request.args.get("status", "")
    page = _page_arg()

    where_clauses, params = [], []
    if q:
        where_clauses.append("(st.first_name LIKE ? OR st.surname LIKE ? OR st.admission_number LIKE ?)")
        params += [f"%{q}%"] * 3
    if class_filter:
        where_clauses.append("st.class_id = ?")
        params.append(class_filter)
    if status_filter:
        where_clauses.append("st.status = ?")
        params.append(status_filter)
    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total = db.execute(f"SELECT COUNT(*) c FROM students st {where}", params).fetchone()["c"]
    rows = db.execute(
        f"""SELECT st.*, u.email, u.is_active, u.must_change_password, c.name AS class_name
            FROM students st JOIN users u ON u.id = st.user_id
            LEFT JOIN classes c ON c.id = st.class_id
            {where} ORDER BY st.surname, st.first_name LIMIT ? OFFSET ?""",
        params + [PER_PAGE, (page - 1) * PER_PAGE],
    ).fetchall()
    classes = db.execute("SELECT * FROM classes WHERE is_active = 1 ORDER BY name").fetchall()
    return dict(rows=rows, classes=classes, q=q, class_filter=class_filter, status_filter=status_filter,
                statuses=STUDENT_STATUSES, page=page, per_page=PER_PAGE, total=total)


def _link_or_create_parent(db, student_id, form, actor_id):
    """Optional parent/guardian login created together with the student.
    Returns (credential_dict_or_None, message_or_None). Raises on DB errors so the
    caller's single transaction rolls everything back."""
    p_name = form.get("parent_name", "").strip()
    p_email = form.get("parent_email", "").strip().lower()
    p_phone = form.get("parent_phone", "").strip()
    relationship = form.get("parent_relationship", "").strip() or None
    if not (p_name or p_email or p_phone):
        return None, None
    if not p_name:
        raise ValueError("Enter the parent/guardian's name to create a parent account.")
    if p_email and not is_valid_email(p_email):
        raise ValueError("The parent's email address is not valid.")
    if not p_email and not account_service.slug(p_phone):
        raise ValueError("Enter the parent's email or phone number so they can sign in.")
    login = p_email or account_service.parent_login_email_from_phone(p_phone)

    existing = db.execute(
        "SELECT u.id, u.role, p.id AS parent_id FROM users u LEFT JOIN parents p ON p.user_id = u.id WHERE lower(u.email) = ?",
        (login,)).fetchone()
    cred = None
    if existing is not None:
        if existing["role"] != "parent" or existing["parent_id"] is None:
            raise ValueError("That email/phone already belongs to a non-parent account.")
        parent_id = existing["parent_id"]      # sibling case: link to the existing parent login
    else:
        user_id, temp = account_service.create_user(db, login, "parent", p_name)
        parent_id = db.execute("INSERT INTO parents (user_id, phone) VALUES (?, ?)",
                               (user_id, p_phone or None)).lastrowid
        cred = credential(title="Parent account created", name=p_name, identifier_label="Linked student",
                          identifier=None, username=login, password=temp, role_label="Parent / Guardian")
    db.execute(
        "INSERT INTO parent_students (parent_id, student_id, relationship, linked_by_user_id) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (parent_id, student_id) DO NOTHING",
        (parent_id, student_id, relationship, actor_id))
    log_action("parent.link", "student", student_id, new_value={"parent_id": parent_id})
    return cred, None


@bp.route("/students", methods=("GET", "POST"))
@permission_required("students.view")
def students_list():
    db = get_db()
    credentials = None
    if request.method == "POST":
        if not _can("students.create"):
            return _forbidden()
        settings = settings_service.get_settings()
        errors = validate_required_fields(request.form, ["first_name", "surname", "admission_number", "class_id"])
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            admission = request.form["admission_number"].strip()
            full_name = f"{request.form['first_name'].strip()} {request.form['surname'].strip()}"
            login = account_service.student_login_email(admission)
            try:
                # ONE transaction: student login + profile (+ parent login + link) all or nothing
                user_id, temp = account_service.create_user(db, login, "student", full_name)
                cur = db.execute(
                    """INSERT INTO students
                           (user_id, admission_number, first_name, middle_name, surname, gender,
                            date_of_birth, phone, address, class_id, academic_session_id, quranic_level)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, admission, request.form["first_name"].strip(),
                     request.form.get("middle_name", "").strip() or None, request.form["surname"].strip(),
                     request.form.get("gender") or None, request.form.get("date_of_birth") or None,
                     request.form.get("phone", "").strip() or None, request.form.get("address", "").strip() or None,
                     request.form["class_id"], settings["current_session_id"],
                     request.form.get("quranic_level", "").strip() or None),
                )
                student_id = cur.lastrowid
                parent_cred, _ = _link_or_create_parent(db, student_id, request.form, g.user["id"])
                db.commit()
            except ValueError as exc:
                db.rollback()
                flash(str(exc), "error")
            except Exception:
                db.rollback()
                flash("A student with that admission number already exists (or the parent login conflicts).", "error")
            else:
                log_action("student.create", "student", student_id, new_value={"admission_number": admission})
                credentials = [credential(
                    title="Student account created successfully", name=full_name,
                    identifier_label="Admission number", identifier=admission,
                    username=login, password=temp, role_label="Student")]
                if parent_cred:
                    parent_cred["identifier"] = admission
                    credentials.append(parent_cred)
        if credentials is None:
            return redirect(url_for("admin_people.students_list"))

    return render_template("admin/students_list.html", credentials=credentials, **_students_context(db))


@bp.route("/students/<int:student_id>/reset-password", methods=("POST",))
@permission_required("students.reset_password")
def reset_student_password(student_id):
    db = get_db()
    row = db.execute(
        "SELECT st.id, st.admission_number, st.user_id, u.full_name, u.email FROM students st JOIN users u ON u.id = st.user_id WHERE st.id = ?",
        (student_id,)).fetchone()
    if row is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin_people.students_list"))
    temp = account_service.reset_password(db, row["user_id"])
    db.commit()
    log_action("student.password_reset", "student", student_id)
    credentials = [credential(
        title="Password reset successfully", name=row["full_name"], identifier_label="Admission number",
        identifier=row["admission_number"], username=row["email"], password=temp, role_label="Student")]
    return render_template("admin/students_list.html", credentials=credentials, **_students_context(db))


def _student_row(db, student_id):
    return db.execute(
        """SELECT st.*, u.email, u.is_active, c.name AS class_name FROM students st
           JOIN users u ON u.id = st.user_id LEFT JOIN classes c ON c.id = st.class_id
           WHERE st.id = ?""", (student_id,)).fetchone()


@bp.route("/students/<int:student_id>")
@permission_required("students.view")
def student_view(student_id):
    db = get_db()
    student = _student_row(db, student_id)
    if student is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin_people.students_list"))
    guardians = db.execute("SELECT * FROM guardians WHERE student_id = ?", (student_id,)).fetchall()
    biometric = db.execute("SELECT * FROM biometric_profiles WHERE student_id = ?", (student_id,)).fetchone()
    parents = db.execute(
        """SELECT p.id, u.full_name, u.email, ps.relationship FROM parent_students ps
           JOIN parents p ON p.id = ps.parent_id JOIN users u ON u.id = p.user_id
           WHERE ps.student_id = ? ORDER BY u.full_name""", (student_id,)).fetchall()
    return render_template("admin/student_view.html", student=student, guardians=guardians,
                           biometric=biometric, parents=parents, statuses=STUDENT_STATUSES)


@bp.route("/students/<int:student_id>/edit", methods=("GET", "POST"))
@permission_required("students.edit")
def student_edit(student_id):
    db = get_db()
    student = _student_row(db, student_id)
    if student is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin_people.students_list"))
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["first_name", "surname", "class_id"])
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            f = request.form
            before = {k: student[k] for k in ("first_name", "middle_name", "surname", "gender", "date_of_birth",
                                              "phone", "address", "class_id", "quranic_level")}
            new = {
                "first_name": f["first_name"].strip(), "middle_name": f.get("middle_name", "").strip() or None,
                "surname": f["surname"].strip(), "gender": f.get("gender") or None,
                "date_of_birth": f.get("date_of_birth") or None, "phone": f.get("phone", "").strip() or None,
                "address": f.get("address", "").strip() or None, "class_id": int(f["class_id"]),
                "quranic_level": f.get("quranic_level", "").strip() or None,
            }
            db.execute(
                """UPDATE students SET first_name = ?, middle_name = ?, surname = ?, gender = ?, date_of_birth = ?,
                          phone = ?, address = ?, class_id = ?, quranic_level = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (new["first_name"], new["middle_name"], new["surname"], new["gender"], new["date_of_birth"],
                 new["phone"], new["address"], new["class_id"], new["quranic_level"], student_id))
            db.execute("UPDATE users SET full_name = ?, updated_at = datetime('now') WHERE id = ?",
                       (f"{new['first_name']} {new['surname']}", student["user_id"]))
            db.commit()
            log_action("student.update", "student", student_id, previous_value=before, new_value=new)
            flash("Student details updated.", "success")
            return redirect(url_for("admin_people.student_view", student_id=student_id))
    classes = db.execute("SELECT * FROM classes WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template("admin/student_edit.html", student=student, classes=classes)


@bp.route("/students/<int:student_id>/status", methods=("POST",))
@role_required(*ADMIN_ROLES)
def update_student_status(student_id):
    db = get_db()
    new_status = request.form.get("status")
    if new_status not in STUDENT_STATUSES:
        flash("Invalid status.", "error")
    else:
        before = db.execute("SELECT status FROM students WHERE id = ?", (student_id,)).fetchone()
        db.execute("UPDATE students SET status = ?, updated_at = datetime('now') WHERE id = ?", (new_status, student_id))
        db.commit()
        log_action("student.status_change", "student", student_id,
                   previous_value={"status": before["status"] if before else None}, new_value={"status": new_status})
        flash("Student status updated.", "success")
    return redirect(url_for("admin_people.student_view", student_id=student_id))


@bp.route("/students/<int:student_id>/guardians", methods=("POST",))
@permission_required("students.edit")
def add_guardian(student_id):
    db = get_db()
    errors = validate_required_fields(request.form, ["full_name"])
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        db.execute(
            """INSERT INTO guardians (student_id, full_name, relationship, phone, email,
                                       is_emergency_contact, notify_by_sms, notify_by_email)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (student_id, request.form["full_name"].strip(), request.form.get("relationship", "").strip() or None,
             request.form.get("phone", "").strip() or None, request.form.get("email", "").strip() or None,
             1 if request.form.get("is_emergency_contact") else 0,
             1 if request.form.get("notify_by_sms") else 0, 1 if request.form.get("notify_by_email") else 0),
        )
        db.commit()
        log_action("guardian.create", "student", student_id)
        flash("Guardian added.", "success")
    return redirect(url_for("admin_people.student_view", student_id=student_id))


# ---------------------------------------------------------------- bulk import
@bp.route("/students/import", methods=("GET", "POST"))
@permission_required("students.create")
def students_import():
    db = get_db()
    settings = settings_service.get_settings()
    summary = None
    credentials = None
    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or not file.filename:
            flash("Choose a CSV file first.", "error")
        elif not settings["current_session_id"]:
            flash("No academic session is active. Set one in Settings first.", "error")
        else:
            csv_text = file.read().decode("utf-8-sig", errors="replace")
            dry_run = bool(request.form.get("preview_only"))
            summary = import_service.preview_and_import(
                db, csv_text, g.user["id"], secure_filename(file.filename),
                settings["current_session_id"], dry_run=dry_run,
            )
            if not dry_run:
                log_action("student.bulk_import", "import_batch", None, new_value={
                    "total": summary["total_rows"], "success": summary["success_count"],
                })
                flash(
                    f"Import complete: {summary['success_count']} succeeded, "
                    f"{summary['duplicate_count']} duplicates, {summary['invalid_class_count']} invalid class, "
                    f"{summary['missing_field_count']} missing fields.",
                    "success" if summary["success_count"] else "error",
                )
                credentials = [
                    credential(title="Student account created successfully", name=c["name"],
                               identifier_label="Admission number", identifier=c["admission_number"],
                               username=c["username"], password=c["password"], role_label="Student")
                    for c in summary.pop("credentials", [])
                ]
    return render_template("admin/students_import.html", summary=summary, credentials=credentials or None)


@bp.route("/students/import/template.csv")
@permission_required("students.create")
def students_import_template():
    from flask import Response
    return Response(import_service.csv_template(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=student_import_template.csv"})


# ------------------------------------------------------------------ helpers
def _can(permission):
    from utils.permissions import has_permission
    return has_permission(g.user, permission)


def _forbidden():
    from flask import abort
    abort(403)
