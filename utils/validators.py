"""Server-side input validation helpers. Never trust client-side checks alone."""

import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value: str) -> bool:
    return bool(value) and bool(EMAIL_RE.match(value.strip()))


def is_non_empty(value: str) -> bool:
    return bool(value) and bool(value.strip())


def is_valid_password(value: str, min_length: int = 8) -> bool:
    return bool(value) and len(value) >= min_length


def validate_required_fields(data: dict, fields: list[str]) -> list[str]:
    """Return a list of error messages for any missing/blank required fields."""
    errors = []
    for field in fields:
        if not is_non_empty(data.get(field, "")):
            errors.append(f"{field.replace('_', ' ').title()} is required.")
    return errors


# ---------------------------------------------------------------- logo URL
from urllib.parse import urlparse

LOGO_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")


def validate_logo_url(url: str):
    """Return (clean_url_or_None, error_or_None). Empty string clears the logo.

    Only https image URLs are accepted (an http image would be blocked as
    mixed content on the https site). No credentials in the URL, no
    scripts/data: URIs. The server never fetches the URL."""
    url = (url or "").strip()
    if not url:
        return None, None
    if len(url) > 500:
        return None, "Logo URL is too long (500 characters maximum)."
    if any(ch.isspace() for ch in url) or any(ord(ch) < 32 for ch in url):
        return None, "Logo URL must not contain spaces."
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None, "Logo URL must start with https://"
    if not parsed.hostname or "." not in parsed.hostname:
        return None, "Logo URL must include a valid website address."
    if parsed.username or parsed.password:
        return None, "Logo URL must not contain a username or password."
    if not parsed.path.lower().endswith(LOGO_EXTENSIONS):
        return None, "Logo URL must point directly to an image file (.png, .jpg, .jpeg, .gif, .webp, .svg or .ico)."
    return url, None
