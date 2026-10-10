"""
Parent Portal.

A parent/guardian signs in with their own account (users.role = 'parent')
and sees ONLY the students linked to that account through
`parent_students`. Isolation is enforced in the SQL of every query — each
student lookup joins `parent_students` to the signed-in parent's user id —
so changing a student id in the URL, a query string or a form can never
reveal another family's child. A student that exists but is not linked
returns the same 404 as one that does not exist (no information leak), and
the attempt is written to the audit log.

Shown to parents: attendance dates, subjects, statuses, summaries and
percentages. NOT shown: face-recognition confidence, who marked it,
verification method, other students, or any administrative data.
"""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from database.db import get_db
from services import settings_service
from utils.audit import log_action
from utils.decorators import role_required

bp = Blueprint("parent", __name__, url_prefix="/parent")
PER_PAGE = 25
STATUSES = ("present", "late", "absent", "excused", "pending_verification")


def _pct(attended, total):
    return round(100.0 * attended / total, 1) if total else None


def _linked_child_or_404(db, student_id):
    """The ONLY way the portal loads a student: it must be linked to the signed-in parent."""
    child = db.execute(
        """SELECT st.id, st.first_name, st.middle_name, st.surname, st.admission_number, st.status,
                  c.name AS class_name
           FROM students st
           JOIN parent_students ps ON ps.student_id = st.id
           JOIN parents p ON p.id = ps.parent_id
           LEFT JOIN classes c ON c.id = st.class_id
           WHERE st.id = ? AND p.user_id = ?""",
        (student_id, g.user["id"]),
    ).fetchone()
    if child is None:
        log_action("security.parent_access_denied", "student", student_id)
        abort(404)
    return child


@bp.route("/")
@role_required("parent")
def dashboard():
    db = get_db()
    settings = settings_service.get_settings()
    rows = db.execute(
        """SELECT st.id, st.first_name, st.middle_name, st.surname, st.admission_number, st.status,
                  c.name AS class_name, COUNT(ar.id) AS total,
                  COALESCE(SUM(CASE WHEN ar.status IN ('present','late') THEN 1 ELSE 0 END), 0) AS attended,
                  MAX(ar.recorded_at) AS last_recorded
           FROM parent_students ps
           JOIN parents p ON p.id = ps.parent_id
           JOIN students st ON st.id = ps.student_id
           LEFT JOIN classes c ON c.id = st.class_id
           LEFT JOIN attendance_records ar ON ar.student_id = st.id
           WHERE p.user_id = ?
           GROUP BY st.id, c.name
           ORDER BY st.first_name, st.surname""",
        (g.user["id"],),
    ).fetchall()
    children = [dict(r, pct=_pct(r["attended"], r["total"])) for r in rows]
    return render_template("parent/dashboard.html", children=children,
                           threshold=settings["attendance_threshold_pct"])


@bp.route("/children/<int:student_id>")
@role_required("parent")
def child(student_id):
    db = get_db()
    child_row = _linked_child_or_404(db, student_id)
    settings = settings_service.get_settings()

    status = request.args.get("status", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1

    totals = db.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END), 0) AS present,
                  COALESCE(SUM(CASE WHEN status = 'late' THEN 1 ELSE 0 END), 0) AS late,
                  COALESCE(SUM(CASE WHEN status = 'absent' THEN 1 ELSE 0 END), 0) AS absent,
                  COALESCE(SUM(CASE WHEN status = 'excused' THEN 1 ELSE 0 END), 0) AS excused
           FROM attendance_records WHERE student_id = ?""", (student_id,)).fetchone()
    attended = totals["present"] + totals["late"]
    pct = _pct(attended, totals["total"])

    per_subject = db.execute(
        """SELECT sub.name AS subject_name, COUNT(*) AS total,
                  SUM(CASE WHEN ar.status IN ('present','late') THEN 1 ELSE 0 END) AS attended
           FROM attendance_records ar
           JOIN attendance_sessions sess ON sess.id = ar.session_id
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           JOIN subjects sub ON sub.id = sa.subject_id
           WHERE ar.student_id = ? GROUP BY sub.id, sub.name ORDER BY sub.name""", (student_id,)).fetchall()
    subjects = [dict(r, pct=_pct(r["attended"], r["total"])) for r in per_subject]

    days = db.execute(
        """SELECT sess.session_date,
                  SUM(CASE WHEN ar.status = 'present' THEN 1 ELSE 0 END) AS present,
                  SUM(CASE WHEN ar.status = 'late' THEN 1 ELSE 0 END) AS late,
                  SUM(CASE WHEN ar.status = 'absent' THEN 1 ELSE 0 END) AS absent,
                  SUM(CASE WHEN ar.status = 'excused' THEN 1 ELSE 0 END) AS excused
           FROM attendance_records ar JOIN attendance_sessions sess ON sess.id = ar.session_id
           WHERE ar.student_id = ?
           GROUP BY sess.session_date ORDER BY sess.session_date DESC LIMIT 30""", (student_id,)).fetchall()

    clauses, params = ["ar.student_id = ?"], [student_id]
    if status in STATUSES:
        clauses.append("ar.status = ?"); params.append(status)
    if date_from:
        clauses.append("sess.session_date >= ?"); params.append(date_from)
    if date_to:
        clauses.append("sess.session_date <= ?"); params.append(date_to)
    where = " AND ".join(clauses)
    base = f"""FROM attendance_records ar
               JOIN attendance_sessions sess ON sess.id = ar.session_id
               JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
               JOIN subjects sub ON sub.id = sa.subject_id
               WHERE {where}"""
    total_rows = db.execute(f"SELECT COUNT(*) c {base}", params).fetchone()["c"]
    records = db.execute(
        f"""SELECT sess.session_date, sub.name AS subject_name, ar.status, ar.recorded_at {base}
            ORDER BY sess.session_date DESC, ar.recorded_at DESC LIMIT ? OFFSET ?""",
        params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()

    return render_template(
        "parent/child.html", child=child_row, totals=totals, pct=pct, attended=attended,
        subjects=subjects, days=days, records=records, status=status, statuses=STATUSES,
        date_from=date_from, date_to=date_to, page=page, per_page=PER_PAGE, total_rows=total_rows,
        threshold=settings["attendance_threshold_pct"],
    )
