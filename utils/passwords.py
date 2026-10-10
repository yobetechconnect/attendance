"""
Temporary-password generation and password rules.

Temporary passwords come from the `secrets` module (cryptographically
secure). The alphabet leaves out look-alike characters (0/O, 1/l/I) so
a password read off a screen or dictated to a parent is not mistyped.
Only the hash is ever stored; the plaintext exists in memory for the
single response that shows it, and is never logged.
"""

import re
import secrets

from werkzeug.security import generate_password_hash

_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_LOWER = "abcdefghijkmnopqrstuvwxyz"
_DIGIT = "23456789"
_SYMBOL = "@#$%&*?!"
_ALL = _UPPER + _LOWER + _DIGIT + _SYMBOL


def generate_temp_password(length: int = 12) -> str:
    """Random password guaranteed to contain upper, lower, digit and symbol."""
    length = max(length, 10)
    chars = [secrets.choice(_UPPER), secrets.choice(_LOWER),
             secrets.choice(_DIGIT), secrets.choice(_SYMBOL)]
    chars += [secrets.choice(_ALL) for _ in range(length - len(chars))]
    # Fisher-Yates with a CSPRNG so the guaranteed characters aren't always first
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def hash_password(password: str) -> str:
    return generate_password_hash(password)


MIN_LENGTH = 8


def password_problem(password: str, email: str = "", name: str = ""):
    """Return a human-readable problem with a NEW user-chosen password, or None."""
    if not password or len(password) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must contain at least one letter and one number."
    low = password.lower()
    local = (email or "").split("@")[0].lower()
    if local and len(local) >= 4 and local in low:
        return "Password must not contain your login name."
    if low in {"password1", "12345678", "password123", "qwerty123", "admin1234"}:
        return "That password is too common. Choose something less guessable."
    return None
