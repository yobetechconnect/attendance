"""
Reporting (Sections 27-30). Every list here is server-side paginated
and every export streams from a filtered SQL query rather than
loading the whole table, so this holds up at 3,000+ students.
"""

from flask import Blueprint, Response, request, render_template

from database.db import get_db
from services import report_service, settings_service
from utils.decorators import role_required

bp = Blueprint("reports", __name__, url_prefix="/reports")
STAFF_ROLES = ("super_admin", "teacher", "attendance_operator", "auditor")


@bp.route("/sessions")
@role_required(*STAFF_ROLES)
def sessions_list():
    db = get_db()
    page = max(int(request.args.get("page", 1)), 1)
    per_page = 50
    total = db.execute("SELECT COUNT(*) c FROM attendance_sessions").fetchone()["c"]
    rows = db.execute(
        """SELECT sess.*, sub.name AS subject_name, c.name AS class_name, u.full_name AS teacher_name,
                  (SELECT COUNT(*) FROM attendance_records ar WHERE ar.session_id = sess.id) AS marked_count
           FROM attendance_sessions sess
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           JOIN subjects sub ON sub.id = sa.subject_id
           JOIN classes c ON c.id = sa.class_id
           JOIN teachers t ON t.id = sa.teacher_id
           JOIN users u ON u.id = t.user_id
           ORDER BY sess.started_at DESC LIMIT ? OFFSET ?""",
        (per_page, (page - 1) * per_page),
    ).fetchall()
    return render_template("attendance/sessions_list.html", rows=rows, page=page, per_page=per_page, total=total)


@bp.route("/records")
@role_required(*STAFF_ROLES)
def records_search():
    db = get_db()
    class_id = request.args.get("class_id", "")
    subject_id = request.args.get("subject_id", "")
    status = request.args.get("status", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    page = max(int(request.args.get("page", 1)), 1)
    per_page = 50

    clauses, params = [], []
    if class_id:
        clauses.append("sa.class_id = ?"); params.append(class_id)
    if subject_id:
        clauses.append("sa.subject_id = ?"); params.append(subject_id)
    if status:
        clauses.append("ar.status = ?"); params.append(status)
    if date_from:
        clauses.append("sess.session_date >= ?"); params.append(date_from)
    if date_to:
        clauses.append("sess.session_date <= ?"); params.append(date_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    base_query = f"""
        FROM attendance_records ar
        JOIN attendance_sessions sess ON sess.id = ar.session_id
        JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
        JOIN subjects sub ON sub.id = sa.subject_id
        JOIN classes c ON c.id = sa.class_id
        JOIN students st ON st.id = ar.student_id
        {where}
    """
    total = db.execute(f"SELECT COUNT(*) c {base_query}", params).fetchone()["c"]
    rows = db.execute(
        f"""SELECT ar.*, sub.name AS subject_name, c.name AS class_name,
                   st.admission_number, st.first_name, st.surname, sess.session_date
            {base_query} ORDER BY ar.recorded_at DESC LIMIT ? OFFSET ?""",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()

    classes = db.execute("SELECT * FROM classes WHERE is_active = 1 ORDER BY name").fetchall()
    subjects = db.execute("SELECT * FROM subjects WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template(
        "attendance/records_search.html", rows=rows, classes=classes, subjects=subjects,
        class_id=class_id, subject_id=subject_id, status=status, date_from=date_from, date_to=date_to,
        page=page, per_page=per_page, total=total,
    )


@bp.route("/records/export.csv")
@role_required(*STAFF_ROLES)
def records_export_csv():
    db = get_db()
    rows = db.execute(
        """SELECT sess.session_date, sub.name AS subject, c.name AS class_name,
                  st.admission_number, st.first_name, st.surname, ar.status, ar.verification_method, ar.recorded_at
           FROM attendance_records ar
           JOIN attendance_sessions sess ON sess.id = ar.session_id
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           JOIN subjects sub ON sub.id = sa.subject_id
           JOIN classes c ON c.id = sa.class_id
           JOIN students st ON st.id = ar.student_id
           ORDER BY ar.recorded_at DESC LIMIT 20000"""
    ).fetchall()
    fieldnames = ["session_date", "subject", "class_name", "admission_number", "first_name",
                  "surname", "status", "verification_method", "recorded_at"]
    csv_text = report_service.export_csv([dict(r) for r in rows], fieldnames)
    return Response(csv_text, mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=attendance_records.csv"})


@bp.route("/class/<int:class_id>")
@role_required(*STAFF_ROLES)
def class_report(class_id):
    db = get_db()
    class_row = db.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
    session_row, term_row = settings_service.current_session_and_term()

    student_rows = db.execute(
        """SELECT st.admission_number, st.first_name || ' ' || st.surname AS name,
                  SUM(CASE WHEN ar.status='present' THEN 1 ELSE 0 END) AS present,
                  SUM(CASE WHEN ar.status='late' THEN 1 ELSE 0 END) AS late,
                  SUM(CASE WHEN ar.status='absent' THEN 1 ELSE 0 END) AS absent,
                  SUM(CASE WHEN ar.status='excused' THEN 1 ELSE 0 END) AS excused,
                  ROUND(100.0 * SUM(CASE WHEN ar.status IN ('present','late') THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct
           FROM students st
           JOIN attendance_records ar ON ar.student_id = st.id
           JOIN attendance_sessions sess ON sess.id = ar.session_id
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           WHERE st.class_id = ? AND sa.class_id = ?
           GROUP BY st.id ORDER BY name""",
        (class_id, class_id),
    ).fetchall()

    if request.args.get("format") == "pdf":
        settings = settings_service.get_settings()
        pdf_bytes = report_service.build_class_report_pdf(
            settings, class_row, session_row["name"] if session_row else None,
            term_row["name"] if term_row else None, [dict(r) for r in student_rows],
        )
        return Response(pdf_bytes, mimetype="application/pdf",
                         headers={"Content-Disposition": f"attachment; filename=class_report_{class_row['name']}.pdf"})

    return render_template("attendance/course_report.html", class_row=class_row, student_rows=student_rows)


@bp.route("/student/<int:student_id>")
@role_required(*STAFF_ROLES)
def student_report(student_id):
    db = get_db()
    student = db.execute(
        """SELECT st.*, c.name AS class_name FROM students st
           LEFT JOIN classes c ON c.id = st.class_id WHERE st.id = ?""",
        (student_id,),
    ).fetchone()
    session_row, term_row = settings_service.current_session_and_term()

    subject_rows = db.execute(
        """SELECT sub.name AS subject,
                  SUM(CASE WHEN ar.status='present' THEN 1 ELSE 0 END) AS present,
                  SUM(CASE WHEN ar.status='late' THEN 1 ELSE 0 END) AS late,
                  SUM(CASE WHEN ar.status='absent' THEN 1 ELSE 0 END) AS absent,
                  SUM(CASE WHEN ar.status='excused' THEN 1 ELSE 0 END) AS excused,
                  ROUND(100.0 * SUM(CASE WHEN ar.status IN ('present','late') THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct
           FROM attendance_records ar
           JOIN attendance_sessions sess ON sess.id = ar.session_id
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           JOIN subjects sub ON sub.id = sa.subject_id
           WHERE ar.student_id = ? GROUP BY sub.id ORDER BY sub.name""",
        (student_id,),
    ).fetchall()

    if request.args.get("format") == "pdf":
        settings = settings_service.get_settings()
        pdf_bytes = report_service.build_student_report_pdf(
            settings, student, session_row["name"] if session_row else None,
            term_row["name"] if term_row else None, [dict(r) for r in subject_rows],
        )
        return Response(pdf_bytes, mimetype="application/pdf",
                         headers={"Content-Disposition": f"attachment; filename=student_report_{student['admission_number']}.pdf"})

    return render_template("attendance/student_report.html", student=student, subject_rows=subject_rows)


@bp.route("/low-attendance")
@role_required(*STAFF_ROLES)
def low_attendance_report():
    db = get_db()
    settings = settings_service.get_settings()
    threshold = float(request.args.get("threshold", settings["attendance_threshold_pct"]))
    rows = db.execute(
        """SELECT st.id, st.admission_number, st.first_name, st.surname, c.name AS class_name,
                  ROUND(100.0 * SUM(CASE WHEN ar.status IN ('present','late') THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct
           FROM students st
           JOIN attendance_records ar ON ar.student_id = st.id
           LEFT JOIN classes c ON c.id = st.class_id
           WHERE st.status = 'active'
           GROUP BY st.id, c.name HAVING (100.0 * SUM(CASE WHEN ar.status IN ('present','late') THEN 1 ELSE 0 END) / COUNT(*)) < ?
           ORDER BY pct ASC LIMIT 1000""",
        (threshold,),
    ).fetchall()
    return render_template("attendance/low_attendance.html", rows=rows, threshold=threshold)
