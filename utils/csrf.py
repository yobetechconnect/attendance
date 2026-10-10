"""
Lightweight CSRF protection (no external dependency).

A random token is generated once per session and exposed to templates
via `csrf_token()`. Every state-changing request (POST/PUT/PATCH/DELETE)
must include a matching `csrf_token` form field or `X-CSRF-Token`
header, checked in app.py's before_request hook.
"""

import secrets

from flask import request, session


def get_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def csrf_protect():
    """
    Validate the CSRF token on state-changing requests. Returns True if
    the request is safe to proceed, False if it should be rejected.
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return True

    token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    expected = session.get("csrf_token")

    return bool(token) and bool(expected) and secrets.compare_digest(token, expected)
