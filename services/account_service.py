"""
User-account lifecycle shared by students, teachers, parents and the two
temporary administrators: creation with a generated temporary password,
password reset, and self-service password change.

Rules enforced here (one place, so every role behaves the same):
- Only a password HASH is stored; the plaintext temporary password is
  returned to the caller exactly once so the confirmation dialog can show it.
- New and reset accounts must change the password at first login.
- A reset or deactivation bumps `session_version`, which logs out every
  device still holding the old session, and resets the lockout counters.
"""

from utils.passwords import generate_temp_password, hash_password

STUDENT_EMAIL_DOMAIN = "students.al-istianah.local"
PARENT_EMAIL_DOMAIN = "parents.al-istianah.local"


def slug(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def student_login_email(admission_number: str) -> str:
    return f"{slug(admission_number)}@{STUDENT_EMAIL_DOMAIN}"


def parent_login_email_from_phone(phone: str) -> str:
    return f"{slug(phone)}@{PARENT_EMAIL_DOMAIN}"


def create_user(db, email: str, role: str, full_name: str, temp_admin_slot=None):
    """Insert a user with a fresh temporary password. Returns (user_id, plaintext_password).
    Caller commits (so related rows can be created in the same transaction)."""
    temp = generate_temp_password()
    cur = db.execute(
        """INSERT INTO users (email, password_hash, role, full_name, must_change_password, temp_admin_slot)
           VALUES (?, ?, ?, ?, 1, ?)""",
        (email.strip().lower(), hash_password(temp), role, full_name.strip(), temp_admin_slot),
    )
    return cur.lastrowid, temp


def reset_password(db, user_id: int) -> str:
    """Issue a new temporary password; the previous password stops working immediately."""
    temp = generate_temp_password()
    db.execute(
        """UPDATE users
              SET password_hash = ?, must_change_password = 1,
                  session_version = session_version + 1,
                  failed_login_count = 0, locked_until = NULL,
                  password_changed_at = datetime('now'), updated_at = datetime('now')
            WHERE id = ?""",
        (hash_password(temp), user_id),
    )
    return temp


def set_own_password(db, user_id: int, new_password: str) -> int:
    """User chose a new password. Returns the new session_version for the live session."""
    db.execute(
        """UPDATE users
              SET password_hash = ?, must_change_password = 0,
                  session_version = session_version + 1,
                  password_changed_at = datetime('now'), updated_at = datetime('now')
            WHERE id = ?""",
        (hash_password(new_password), user_id),
    )
    return db.execute("SELECT session_version FROM users WHERE id = ?", (user_id,)).fetchone()["session_version"]


def set_active(db, user_id: int, active: bool):
    """Activate/deactivate. Deactivation also kills any live sessions."""
    db.execute(
        """UPDATE users SET is_active = ?, session_version = session_version + 1,
                  updated_at = datetime('now') WHERE id = ?""",
        (1 if active else 0, user_id),
    )


def credential(*, title, name, identifier_label=None, identifier=None, username, password, role_label):
    """One entry for the confirmation dialog (templates/partials/credentials_dialog.html)."""
    return {
        "title": title, "name": name, "identifier_label": identifier_label, "identifier": identifier,
        "username": username, "password": password, "role_label": role_label,
    }
