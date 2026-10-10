"""
Append-only audit logging (Section 22).

Every sensitive action in the system should call log_action() so there
is a durable, non-editable record of who did what, when, and why.
Nothing in the application ever UPDATEs or DELETEs a row in
audit_logs — only auth.py's own account lockout writes to `users`,
never to this table.
"""

import json

from flask import g, request

from database.db import get_db


def log_action(action: str, resource_type: str = None, resource_id: int = None,
                previous_value: dict = None, new_value: dict = None, reason: str = None):
    """Write one audit log row. Safe to call even with no logged-in user
    (e.g. failed login attempts) — user_id will be NULL in that case."""
    db = get_db()
    user = g.get("user")
    db.execute(
        """
        INSERT INTO audit_logs
            (user_id, action, resource_type, resource_id,
             previous_value, new_value, reason, ip_address)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["id"] if user is not None else None,
            action,
            resource_type,
            resource_id,
            json.dumps(previous_value) if previous_value is not None else None,
            json.dumps(new_value) if new_value is not None else None,
            reason,
            request.remote_addr if request else None,
        ),
    )
    db.commit()
