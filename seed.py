"""
Development/testing seed script for the AL-ISTI'ANAH ACADEMY DAMATURU
attendance system.

Creates one super admin, one teacher, three test students, an active
academic session/term, a class, a subject and a subject assignment
(the teacher teaching that subject to that class this term) — clearly
labeled as dev/test data. Do not use these credentials or fake
personal data in a real deployment.

Usage:
    python seed.py
"""

import os

from werkzeug.security import generate_password_hash

from app import create_app
from database.db import get_db, init_db

# --- Clearly labeled development/test accounts ---
DEV_PASSWORD = "DevPass123!"  # same password for all seeded accounts, DEV ONLY

ADMIN = {"email": "admin@dev.al-istianah.local", "full_name": "Dev Admin"}
TEACHER = {
    "email": "teacher@dev.al-istianah.local",
    "full_name": "Malam Musa Ibrahim",
    "staff_id": "DEV-STAFF-001",
}
STUDENTS = [
    {
        "email": "student1@dev.al-istianah.local",
        "first_name": "Ahmed",
        "surname": "Bello",
        "admission_number": "ALI/2026/0001",
    },
    {
        "email": "student2@dev.al-istianah.local",
        "first_name": "Fatima",
        "surname": "Usman",
        "admission_number": "ALI/2026/0002",
    },
    {
        "email": "student3@dev.al-istianah.local",
        "first_name": "Yusuf",
        "surname": "Garba",
        "admission_number": "ALI/2026/0003",
    },
]
SECTION_NAME = "Secondary"
CLASS_NAME = "JSS 2A"
SUBJECT = {"code": "MATH101", "name": "Mathematics", "category": "ACADEMIC"}
ACADEMIC_SESSION = {"name": "2026/2027", "start_date": "2026-09-01", "end_date": "2027-07-31"}
TERM = {"name": "First Term", "start_date": "2026-09-01", "end_date": "2026-12-19"}


def run():
    app = create_app()
    # SAFETY: these accounts share one publicly-known dev password. Never allow them
    # on a production database.
    if app.config.get("DATABASE_URL") and os.environ.get("ALLOW_DEV_SEED") != "yes-i-know":
        raise SystemExit("Refusing to seed: DATABASE_URL is set (production). seed.py is for local development only.")
    with app.app_context():
        init_db()
        db = get_db()

        existing = db.execute(
            "SELECT id FROM users WHERE email = ?", (ADMIN["email"],)
        ).fetchone()
        if existing:
            print("Seed data already present — skipping.")
            return

        pw_hash = generate_password_hash(DEV_PASSWORD)

        # System settings row (school identity + defaults) always exists
        db.execute(
            "INSERT INTO system_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
        )

        # Academic session + term
        cur = db.execute(
            "INSERT INTO academic_sessions (name, start_date, end_date, is_active) VALUES (?, ?, ?, 1)",
            (ACADEMIC_SESSION["name"], ACADEMIC_SESSION["start_date"], ACADEMIC_SESSION["end_date"]),
        )
        session_id = cur.lastrowid
        cur = db.execute(
            "INSERT INTO terms (academic_session_id, name, start_date, end_date, is_active) VALUES (?, ?, ?, ?, 1)",
            (session_id, TERM["name"], TERM["start_date"], TERM["end_date"]),
        )
        term_id = cur.lastrowid
        db.execute(
            "UPDATE system_settings SET current_session_id = ?, current_term_id = ? WHERE id = 1",
            (session_id, term_id),
        )

        # Section
        cur = db.execute("INSERT INTO sections (name) VALUES (?)", (SECTION_NAME,))
        section_id = cur.lastrowid

        # Admin
        db.execute(
            "INSERT INTO users (email, password_hash, role, full_name) VALUES (?, ?, 'super_admin', ?)",
            (ADMIN["email"], pw_hash, ADMIN["full_name"]),
        )

        # Teacher
        cur = db.execute(
            "INSERT INTO users (email, password_hash, role, full_name) VALUES (?, ?, 'teacher', ?)",
            (TEACHER["email"], pw_hash, TEACHER["full_name"]),
        )
        teacher_user_id = cur.lastrowid
        cur = db.execute(
            "INSERT INTO teachers (user_id, staff_id) VALUES (?, ?)",
            (teacher_user_id, TEACHER["staff_id"]),
        )
        teacher_id = cur.lastrowid

        # Class, taught by the seeded teacher, in the active session
        cur = db.execute(
            """INSERT INTO classes (name, section_id, academic_session_id, class_teacher_id)
               VALUES (?, ?, ?, ?)""",
            (CLASS_NAME, section_id, session_id, teacher_id),
        )
        class_id = cur.lastrowid

        # Subject
        cur = db.execute(
            "INSERT INTO subjects (code, name, category) VALUES (?, ?, ?)",
            (SUBJECT["code"], SUBJECT["name"], SUBJECT["category"]),
        )
        subject_id = cur.lastrowid

        # Subject assignment: this teacher teaches this subject to this class this term
        cur = db.execute(
            """INSERT INTO subject_assignments (subject_id, class_id, teacher_id, term_id)
               VALUES (?, ?, ?, ?)""",
            (subject_id, class_id, teacher_id, term_id),
        )
        assignment_id = cur.lastrowid

        # Students, each placed in the seeded class
        for s in STUDENTS:
            cur = db.execute(
                "INSERT INTO users (email, password_hash, role, full_name) VALUES (?, ?, 'student', ?)",
                (s["email"], pw_hash, f"{s['first_name']} {s['surname']}"),
            )
            student_user_id = cur.lastrowid
            cur = db.execute(
                """INSERT INTO students
                       (user_id, admission_number, first_name, surname, class_id, academic_session_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'active')""",
                (student_user_id, s["admission_number"], s["first_name"], s["surname"], class_id, session_id),
            )
            student_id = cur.lastrowid
            db.execute(
                "INSERT INTO class_enrollments (student_id, subject_assignment_id) VALUES (?, ?)",
                (student_id, assignment_id),
            )

        db.commit()

        print("Seed complete. Development/test accounts created:")
        print(f"  Admin:   {ADMIN['email']} / {DEV_PASSWORD}")
        print(f"  Teacher: {TEACHER['email']} / {DEV_PASSWORD}")
        for s in STUDENTS:
            print(f"  Student: {s['email']} / {DEV_PASSWORD}")


if __name__ == "__main__":
    run()
