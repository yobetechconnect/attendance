"""
Route-protection decorators (server-side authorization).

login_required       -> user must be authenticated (any role)
role_required        -> authenticated AND holding one of the given roles
permission_required  -> authenticated AND holding a named permission
                        (see utils/permissions.py)

Anyone unauthorized gets a real 403 (or a redirect to login when not
signed in), never a silent success. Every attempt by a signed-in user
without rights is written to the audit log.
"""

from functools import wraps

from flask import abort, flash, g, redirect, request, url_for

from utils.permissions import has_permission


def _deny(reason):
    try:
        from utils.audit import log_action
        log_action("security.access_denied", "route", None,
                   new_value={"path": request.path, "method": request.method}, reason=reason)
    except Exception:
        pass
    abort(403)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = g.get("user")
            if user is None:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("auth.login"))
            if user["role"] not in roles:
                _deny(f"role {user['role']} not in {roles}")
            return view(*args, **kwargs)

        return wrapped

    return decorator


def permission_required(permission):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = g.get("user")
            if user is None:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("auth.login"))
            if not has_permission(user, permission):
                _deny(f"missing permission {permission}")
            return view(*args, **kwargs)

        return wrapped

    return decorator
