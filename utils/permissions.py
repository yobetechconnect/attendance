"""
Central permission table. Authorization is enforced SERVER-SIDE on every
protected route through `utils.decorators.permission_required` /
`role_required`; hiding a menu link is only cosmetic.

Roles:
  super_admin  - the only role with unrestricted administrative access
  temp_admin   - "Temporary Administrator 1/2": student management ONLY
  teacher / attendance_operator - take attendance for assigned classes
  student      - own attendance + QR check-in (unchanged)
  parent       - Parent Portal: linked children only
  auditor      - read-only audit log / attendance reports
  school_admin - LEGACY role from the earlier version; deliberately has no
                 privileges now (only the Super Administrator is unrestricted)
"""

from flask import g

ALL = "*"

ROLE_PERMISSIONS = {
    "super_admin": {ALL},
    "temp_admin": {
        "students.view",
        "students.create",
        "students.edit",
        "students.credentials",
        # "students.reset_password" is granted only if the Super Administrator
        # enables it in School Settings (see has_permission()).
    },
}

STAFF_ADMIN_ROLES = ("super_admin",)                 # unrestricted admin pages
STUDENT_MANAGER_ROLES = ("super_admin", "temp_admin")  # student-management pages


def has_permission(user, permission: str, settings=None) -> bool:
    if user is None:
        return False
    role = user["role"]
    perms = ROLE_PERMISSIONS.get(role, set())
    if ALL in perms or permission in perms:
        return True
    if role == "temp_admin" and permission == "students.reset_password":
        if settings is None:
            from services.settings_service import get_settings
            settings = get_settings()
        return bool(settings["temp_admin_can_reset_passwords"])
    return False


def can(permission: str) -> bool:
    """Convenience for templates and routes: does the current user hold `permission`?"""
    return has_permission(g.get("user"), permission)
