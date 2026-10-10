"""
Bulk student import (Section 11).

Validates every row before touching the database, flags duplicates
(within the file AND against existing admission numbers) and unknown
classes, then inserts row-by-row inside SAVEPOINTs so one bad row can never corrupt
the batch or take down the whole import.

Every imported student gets their OWN random temporary password (they used
to share one). The plaintext list is returned once in `summary["credentials"]`
for the confirmation dialog; only hashes are stored.
"""

import csv
import io
import json

from services.account_service import student_login_email
from utils.passwords import generate_temp_password, hash_password

REQUIRED_FIELDS = ["admission_number", "first_name", "surname", "class_name"]
TEMPLATE_HEADERS = [
    "admission_number", "first_name", "middle_name", "surname", "gender",
    "date_of_birth", "phone", "address", "class_name",
]


def csv_template() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow(["AIA/2026/0001", "Amina", "Bello", "Yusuf", "female",
                      "2013-04-12", "08012345678", "Damaturu", "JSS 1A"])
    return buf.getvalue()


def preview_and_import(db, csv_text: str, imported_by_user_id: int, filename: str,
                        academic_session_id: int, dry_run: bool = False) -> dict:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)

    class_lookup = {
        r["name"].strip().lower(): r["id"]
        for r in db.execute("SELECT id, name FROM classes WHERE academic_session_id = ?",
                             (academic_session_id,)).fetchall()
    }
    existing_admission_numbers = {
        r["admission_number"].upper() for r in db.execute("SELECT admission_number FROM students").fetchall()
    }

    seen_in_batch = set()
    errors = []
    to_insert = []

    for i, row in enumerate(rows, start=2):  # row 1 is the header
        row = {k: (v or "").strip() for k, v in row.items()}
        missing = [f for f in REQUIRED_FIELDS if not row.get(f)]
        if missing:
            errors.append({"row": i, "type": "missing_field", "detail": f"Missing: {', '.join(missing)}"})
            continue

        admission_number = row["admission_number"]
        if admission_number.upper() in existing_admission_numbers or admission_number.upper() in seen_in_batch:
            errors.append({"row": i, "type": "duplicate", "detail": f"Duplicate admission number {admission_number}"})
            continue

        class_id = class_lookup.get(row["class_name"].strip().lower())
        if class_id is None:
            errors.append({"row": i, "type": "invalid_class", "detail": f"Unknown class '{row['class_name']}'"})
            continue

        seen_in_batch.add(admission_number.upper())
        to_insert.append({**row, "class_id": class_id})

    summary = {
        "total_rows": len(rows),
        "success_count": 0,
        "duplicate_count": sum(1 for e in errors if e["type"] == "duplicate"),
        "invalid_class_count": sum(1 for e in errors if e["type"] == "invalid_class"),
        "missing_field_count": sum(1 for e in errors if e["type"] == "missing_field"),
        "errors": errors,
        "credentials": [],
    }

    if dry_run:
        summary["success_count"] = len(to_insert)  # would-succeed count for the preview
        return summary

    credentials = []
    inserted = 0

    for record in to_insert:
        temp = generate_temp_password()
        login = student_login_email(record["admission_number"])
        try:
            with db.savepoint("import_row"):
                cur = db.execute(
                    """INSERT INTO users (email, password_hash, role, full_name, must_change_password)
                       VALUES (?, ?, 'student', ?, 1)""",
                    (login, hash_password(temp), f"{record['first_name']} {record['surname']}"),
                )
                user_id = cur.lastrowid
                db.execute(
                    """INSERT INTO students
                           (user_id, admission_number, first_name, middle_name, surname, gender,
                            date_of_birth, phone, address, class_id, academic_session_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id, record["admission_number"], record["first_name"],
                        record.get("middle_name") or None, record["surname"],
                        record.get("gender") or None, record.get("date_of_birth") or None,
                        record.get("phone") or None, record.get("address") or None,
                        record["class_id"], academic_session_id,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — row-level isolation, batch must continue
            errors.append({"row": "?", "type": "error", "detail": type(exc).__name__})
        else:
            inserted += 1
            credentials.append({"name": f"{record['first_name']} {record['surname']}",
                                "admission_number": record["admission_number"],
                                "username": login, "password": temp})

    summary["success_count"] = inserted
    summary["credentials"] = credentials

    db.execute(
        """INSERT INTO import_batches
               (imported_by_user_id, filename, total_rows, success_count, duplicate_count,
                invalid_class_count, missing_field_count, error_report_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (imported_by_user_id, filename, summary["total_rows"], summary["success_count"],
         summary["duplicate_count"], summary["invalid_class_count"], summary["missing_field_count"],
         json.dumps(errors)),
    )
    db.commit()

    return summary
