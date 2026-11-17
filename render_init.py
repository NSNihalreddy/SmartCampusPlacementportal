"""
Render-only PLACEHUB startup initialization.

This script is called by Render's start command before Gunicorn.
It creates missing SQLite tables and applies the existing idempotent
migrations. It does not delete tables or reset existing records.

Render Free has an ephemeral filesystem. Database contents can be lost
when the service is replaced or restarted. Local development does not
call this script; it continues using the existing local startup flow.
"""

import os
import re

from werkzeug.security import generate_password_hash

from database import get_connection, create_tables, add_cgpa_column
from ADMIN.migrations import run_placehub_migrations


def ensure_database_schema():
    """Create missing tables and apply existing safe, repeatable migrations."""
    create_tables()
    add_cgpa_column()
    run_placehub_migrations()

    connection = get_connection()
    try:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(companies)"
            ).fetchall()
        }
        if "eligible_batches" not in columns:
            connection.execute(
                "ALTER TABLE companies ADD COLUMN eligible_batches TEXT"
            )
            connection.commit()
    finally:
        connection.close()


def validate_super_admin_details(username, name, email, password):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{4,50}", username or ""):
        raise RuntimeError(
            "PLACEHUB_SUPER_ADMIN_USERNAME must be 4-50 characters "
            "and contain only letters, numbers, underscores, dots or hyphens."
        )

    if not name or not name.strip():
        raise RuntimeError("PLACEHUB_SUPER_ADMIN_NAME is required.")

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""):
        raise RuntimeError("PLACEHUB_SUPER_ADMIN_EMAIL must be a valid email.")

    if not password or len(password) < 8:
        raise RuntimeError("PLACEHUB_SUPER_ADMIN_PASSWORD must be at least 8 characters.")
    if not re.search(r"[a-z]", password):
        raise RuntimeError("Super Admin password must contain a lowercase letter.")
    if not re.search(r"[A-Z]", password):
        raise RuntimeError("Super Admin password must contain an uppercase letter.")
    if not re.search(r"[0-9]", password):
        raise RuntimeError("Super Admin password must contain a number.")
    if not re.search(r"[^A-Za-z0-9]", password):
        raise RuntimeError("Super Admin password must contain a special character.")


def ensure_super_admin():
    """
    Create the Super Admin only when the database has none.
    Existing Super Admin credentials are never overwritten.
    """
    connection = get_connection()
    try:
        existing = connection.execute(
            """
            SELECT id
            FROM placehub_accounts
            WHERE role = 'SUPER_ADMIN'
            LIMIT 1
            """
        ).fetchone()

        if existing:
            print("PLACEHUB Super Admin already exists; leaving it unchanged.")
            return

        username = os.getenv("PLACEHUB_SUPER_ADMIN_USERNAME", "").strip()
        name = os.getenv("PLACEHUB_SUPER_ADMIN_NAME", "").strip()
        email = os.getenv("PLACEHUB_SUPER_ADMIN_EMAIL", "").strip().lower()
        password = os.getenv("PLACEHUB_SUPER_ADMIN_PASSWORD", "")

        validate_super_admin_details(username, name, email, password)

        connection.execute("BEGIN IMMEDIATE")

        # Re-check after acquiring the write lock.
        existing = connection.execute(
            """
            SELECT id
            FROM placehub_accounts
            WHERE role = 'SUPER_ADMIN'
            LIMIT 1
            """
        ).fetchone()

        if existing:
            connection.rollback()
            print("PLACEHUB Super Admin already exists; leaving it unchanged.")
            return

        connection.execute(
            """
            INSERT INTO placehub_accounts (
                username,
                name,
                email,
                role,
                password_hash,
                is_active,
                must_change_password,
                session_version
            )
            VALUES (?, ?, ?, 'SUPER_ADMIN', ?, 1, 0, 0)
            """,
            (username, name, email, generate_password_hash(password)),
        )
        connection.commit()
        print("PLACEHUB Super Admin created successfully.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main():
    ensure_database_schema()
    ensure_super_admin()
    print("PLACEHUB Render initialization completed.")


if __name__ == "__main__":
    main()
