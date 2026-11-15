import re

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)


def hash_password(password):
    """Generate a secure password hash."""
    return generate_password_hash(password)


def verify_password(password_hash, password):
    """Verify a password against its stored hash."""

    if not password_hash or not password:
        return False

    try:
        return check_password_hash(password_hash, password)
    except (ValueError, TypeError):
        return False


def validate_password(password):
    """
    Password requirements:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    """

    if not password:
        return False, "Password is required."

    if len(password) < 8:
        return False, "Password must contain at least 8 characters."

    if not re.search(r"[A-Z]", password):
        return False, "Include at least one uppercase letter."

    if not re.search(r"[a-z]", password):
        return False, "Include at least one lowercase letter."

    if not re.search(r"\d", password):
        return False, "Include at least one number."

    if not re.search(r"[^A-Za-z0-9]", password):
        return False, "Include at least one special character."

    return True, "Password is valid."