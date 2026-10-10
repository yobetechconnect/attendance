"""
Super-Administrator-only account management:

  * Temporary Administrator 1 and 2 — create, edit, activate/deactivate,
    reset credentials, and the policy switch that lets them reset student
    passwords.
  * Parent / guardian accounts — create, link to one or more students,
    unlink, reset password, activate/deactivate.

Every route is `role_required("super_admin")`: a Temporary Administrator who
types these URLs gets a 403 (and the attempt is audit-logged).
Temporary passwords are displayed once, in a dialog rendered directly in the
POST response, never via flash/session, never logged.
"""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from database.db import get_db
from services import account_service, settings_service
from services.account_service import credential
from utils.audit import log_action
from utils.decorators import role_required
from utils.validators import is_valid_email

bp = Blueprint("admin_accounts", __name__, url_prefix="/admin")
SUPER = ("super_admin",)
SLOTS = (1, 2)
PER_PAGE = 50


def _slot_or_404(slot):
    if slot not in SLOTS:
        abort(404)
    return slot


# ======================================================================
# TEMPORARY ADMINISTRATORS
# ======================================================================
def _temp_admin_context(db):
    rows = {r["temp_admin_slot"]: r for r in db.execute(
        "SELECT * FROM users WHERE role = 'temp_admin' AND temp_admin_slot IS NOT NULL").fetchall()}
    return dict(slots=[(s, rows.get(s)) for s in SLOTS], settings=settings_service.get_settings())


@bp.route("/temp-admins")
@role_required(*SUPER)
def temp_admins():
    return render_template("admin/temp_admins.html", **_temp_admin_context(get_db()))


@bp.route("/temp-admins/<int:slot>/create", methods=("POST",))
@role_required(*SUPER)
def temp_admin_create(slot):
    _slot_or_404(slot)
    db = get_db()
    name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    if not name or not is_valid_email(email):
        flash("A name and a valid email are required.", "error")
        return redirect(url_for("admin_accounts.temp_admins"))
    try:
        user_id, temp = account_service.create_user(db, email, "temp_admin", name, temp_admin_slot=slot)
        db.commit()
    except Exception:
        flash("That slot is already in use, or the email already belongs to another account.", "error")
        return redirect(url_for("admin_accounts.temp_admins"))
    log_action("temp_admin.create", "user", user_id, new_value={"slot": slot, "email": email})
    credentials = [credential(
        title=f"Temporary Administrator {slot} created successfully", name=name,
        identifier_label="Assigned role", identifier=f"Temporary Administrator {slot} (student management only)",
        username=email, password=temp, role_label=f"Temporary Administrator {slot}")]
    return render_template("admin/temp_admins.html", credentials=credentials, **_temp_admin_context(db))


def _temp_admin_user(db, slot):
    user = db.execute("SELECT * FROM users WHERE role = 'temp_admin' AND temp_admin_slot = ?", (slot,)).fetchone()
    if user is None:
        abort(404)
    return user


@bp.route("/temp-admins/<int:slot>/reset-password", methods=("POST",))
@role_required(*SUPER)
def temp_admin_reset(slot):
    _slot_or_404(slot)
    db = get_db()
    user = _temp_admin_user(db, slot)
    temp = account_service.reset_password(db, user["id"])
    db.commit()
    log_action("temp_admin.password_reset", "user", user["id"], new_value={"slot": slot})
    credentials = [credential(
        title=f"Temporary Administrator {slot}: password reset successfully", name=user["full_name"],
        identifier_label="Assigned role", identifier=f"Temporary Administrator {slot}",
        username=user["email"], password=temp, role_label=f"Temporary Administrator {slot}")]
    return render_template("admin/temp_admins.html", credentials=credentials, **_temp_admin_context(db))


@bp.route("/temp-admins/<int:slot>/toggle-active", methods=("POST",))
@role_required(*SUPER)
def temp_admin_toggle(slot):
    _slot_or_404(slot)
    db = get_db()
    user = _temp_admin_user(db, slot)
    new_active = not bool(user["is_active"])
    account_service.set_active(db, user["id"], new_active)   # deactivation also ends live sessions
    db.commit()
    log_action("temp_admin.activate" if new_active else "temp_admin.deactivate", "user", user["id"],
               new_value={"slot": slot})
    flash(f"Temporary Administrator {slot} {'activated' if new_active else 'deactivated'}.", "success")
    return redirect(url_for("admin_accounts.temp_admins"))


@bp.route("/temp-admins/<int:slot>/update", methods=("POST",))
@role_required(*SUPER)
def temp_admin_update(slot):
    """Change who holds this slot (name/email). Forces a new password so the previous holder is locked out."""
    _slot_or_404(slot)
    db = get_db()
    user = _temp_admin_user(db, slot)
    name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    if not name or not is_valid_email(email):
        flash("A name and a valid email are required.", "error")
        return redirect(url_for("admin_accounts.temp_admins"))
    try:
        db.execute("UPDATE users SET full_name = ?, email = ?, updated_at = datetime('now') WHERE id = ?",
                   (name, email, user["id"]))
        temp = account_service.reset_password(db, user["id"])
        db.commit()
    except Exception:
        flash("That email already belongs to another account.", "error")
        return redirect(url_for("admin_accounts.temp_admins"))
    log_action("temp_admin.update", "user", user["id"],
               previous_value={"email": user["email"], "full_name": user["full_name"]},
               new_value={"email": email, "full_name": name, "slot": slot})
    credentials = [credential(
        title=f"Temporary Administrator {slot} updated; new password issued", name=name,
        identifier_label="Assigned role", identifier=f"Temporary Administrator {slot}",
        username=email, password=temp, role_label=f"Temporary Administrator {slot}")]
    return render_template("admin/temp_admins.html", credentials=credentials, **_temp_admin_context(db))


@bp.route("/temp-admins/policy", methods=("POST",))
@role_required(*SUPER)
def temp_admin_policy():
    allow = 1 if request.form.get("temp_admin_can_reset_passwords") else 0
    before = settings_service.get_settings()["temp_admin_can_reset_passwords"]
    settings_service.update_settings({"temp_admin_can_reset_passwords": allow})
    log_action("settings.temp_admin_policy", "system_settings", 1,
               previous_value={"temp_admin_can_reset_passwords": before},
               new_value={"temp_admin_can_reset_passwords": allow})
    flash("Policy saved.", "success")
    return redirect(url_for("admin_accounts.temp_admins"))


# ======================================================================
# PARENT / GUARDIAN ACCOUNTS
# ======================================================================
def _parents_context(db):
    q = request.args.get("q", "").strip()
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    where, params = "", []
    if q:
        where = "WHERE u.full_name LIKE ? OR u.email LIKE ? OR p.phone LIKE ?"
        params = [f"%{q}%"] * 3
    total = db.execute(f"SELECT COUNT(*) c FROM parents p JOIN users u ON u.id = p.user_id {where}", params).fetchone()["c"]
    rows = db.execute(
        f"""SELECT p.id, p.phone, u.full_name, u.email, u.is_active,
                   (SELECT COUNT(*) FROM parent_students ps WHERE ps.parent_id = p.id) AS children
            FROM parents p JOIN users u ON u.id = p.user_id {where}
            ORDER BY u.full_name LIMIT ? OFFSET ?""",
        params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
    return dict(rows=rows, q=q, page=page, per_page=PER_PAGE, total=total)


@bp.route("/parents", methods=("GET", "POST"))
@role_required(*SUPER)
def parents_list():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        admissions = [a.strip() for a in request.form.get("admission_numbers", "").replace("\n", ",").split(",") if a.strip()]
        error = None
        if not name:
            error = "The parent/guardian's name is required."
        elif email and not is_valid_email(email):
            error = "The email address is not valid."
        elif not email and not account_service.slug(phone):
            error = "Enter an email or a phone number so the parent can sign in."
        login = email or account_service.parent_login_email_from_phone(phone)
        students = []
        if error is None:
            for adm in admissions:
                st = db.execute("SELECT id, admission_number FROM students WHERE upper(admission_number) = ?", (adm.upper(),)).fetchone()
                if st is None:
                    error = f"No student with admission number '{adm}'."
                    break
                students.append(st)
        if error:
            flash(error, "error")
            return redirect(url_for("admin_accounts.parents_list"))
        try:
            user_id, temp = account_service.create_user(db, login, "parent", name)
            parent_id = db.execute("INSERT INTO parents (user_id, phone) VALUES (?, ?)", (user_id, phone or None)).lastrowid
            for st in students:
                db.execute("INSERT INTO parent_students (parent_id, student_id, relationship, linked_by_user_id) VALUES (?, ?, ?, ?)",
                           (parent_id, st["id"], request.form.get("relationship", "").strip() or None, g.user["id"]))
            db.commit()
        except Exception:
            flash("A parent account with that email/phone already exists.", "error")
            return redirect(url_for("admin_accounts.parents_list"))
        log_action("parent.create", "parent", parent_id, new_value={"linked_students": [s["admission_number"] for s in students]})
        credentials = [credential(
            title="Parent account created successfully", name=name, identifier_label="Linked students",
            identifier=", ".join(s["admission_number"] for s in students) or "none yet",
            username=login, password=temp, role_label="Parent / Guardian")]
        return render_template("admin/parents.html", credentials=credentials, **_parents_context(db))

    return render_template("admin/parents.html", **_parents_context(db))


def _parent_or_404(db, parent_id):
    row = db.execute(
        """SELECT p.id, p.phone, p.user_id, u.full_name, u.email, u.is_active FROM parents p
           JOIN users u ON u.id = p.user_id WHERE p.id = ?""", (parent_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _parent_detail(db, parent_id, **extra):
    parent = _parent_or_404(db, parent_id)
    children = db.execute(
        """SELECT st.id, st.admission_number, st.first_name, st.surname, st.status, c.name AS class_name, ps.relationship
           FROM parent_students ps JOIN students st ON st.id = ps.student_id
           LEFT JOIN classes c ON c.id = st.class_id
           WHERE ps.parent_id = ? ORDER BY st.surname, st.first_name""", (parent_id,)).fetchall()
    return render_template("admin/parent_view.html", parent=parent, children=children, **extra)


@bp.route("/parents/<int:parent_id>")
@role_required(*SUPER)
def parent_view(parent_id):
    return _parent_detail(get_db(), parent_id)


@bp.route("/parents/<int:parent_id>/link", methods=("POST",))
@role_required(*SUPER)
def parent_link(parent_id):
    db = get_db()
    _parent_or_404(db, parent_id)
    adm = request.form.get("admission_number", "").strip()
    st = db.execute("SELECT id FROM students WHERE upper(admission_number) = ?", (adm.upper(),)).fetchone()
    if st is None:
        flash(f"No student with admission number '{adm}'.", "error")
    else:
        cur = db.execute(
            "INSERT INTO parent_students (parent_id, student_id, relationship, linked_by_user_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (parent_id, student_id) DO NOTHING",
            (parent_id, st["id"], request.form.get("relationship", "").strip() or None, g.user["id"]))
        db.commit()
        if cur.rowcount:
            log_action("parent.link", "student", st["id"], new_value={"parent_id": parent_id})
            flash("Student linked to this parent account.", "success")
        else:
            flash("That student is already linked to this parent.", "info")
    return redirect(url_for("admin_accounts.parent_view", parent_id=parent_id))


@bp.route("/parents/<int:parent_id>/unlink/<int:student_id>", methods=("POST",))
@role_required(*SUPER)
def parent_unlink(parent_id, student_id):
    db = get_db()
    _parent_or_404(db, parent_id)
    db.execute("DELETE FROM parent_students WHERE parent_id = ? AND student_id = ?", (parent_id, student_id))
    db.commit()
    log_action("parent.unlink", "student", student_id, new_value={"parent_id": parent_id})
    flash("Link removed. The parent can no longer see this student.", "success")
    return redirect(url_for("admin_accounts.parent_view", parent_id=parent_id))


@bp.route("/parents/<int:parent_id>/reset-password", methods=("POST",))
@role_required(*SUPER)
def parent_reset(parent_id):
    db = get_db()
    parent = _parent_or_404(db, parent_id)
    temp = account_service.reset_password(db, parent["user_id"])
    db.commit()
    log_action("parent.password_reset", "parent", parent_id)
    credentials = [credential(
        title="Password reset successfully", name=parent["full_name"], identifier_label=None, identifier=None,
        username=parent["email"], password=temp, role_label="Parent / Guardian")]
    return _parent_detail(db, parent_id, credentials=credentials)


@bp.route("/parents/<int:parent_id>/toggle-active", methods=("POST",))
@role_required(*SUPER)
def parent_toggle(parent_id):
    db = get_db()
    parent = _parent_or_404(db, parent_id)
    new_active = not bool(parent["is_active"])
    account_service.set_active(db, parent["user_id"], new_active)
    db.commit()
    log_action("parent.activate" if new_active else "parent.deactivate", "parent", parent_id)
    flash("Parent account activated." if new_active else "Parent account deactivated.", "success")
    return redirect(url_for("admin_accounts.parent_view", parent_id=parent_id))
