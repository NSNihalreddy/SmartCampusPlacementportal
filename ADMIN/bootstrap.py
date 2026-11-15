
import os
import re
import sqlite3
import getpass

from database import get_connection
from werkzeug.security import generate_password_hash


def validate_password(password):
    """
    PLACEHUB password requirements:
    - Minimum 8 characters
    - One lowercase letter
    - One uppercase letter
    - One number
    - One special character
    """

    if len(password) < 8:
        return False, "Password must contain at least 8 characters."

    if not re.search(r"[a-z]", password):
        return False, "Password must contain a lowercase letter."

    if not re.search(r"[A-Z]", password):
        return False, "Password must contain an uppercase letter."

    if not re.search(r"[0-9]", password):
        return False, "Password must contain a number."

    if not re.search(r"[^A-Za-z0-9]", password):
        return False, "Password must contain a special character."

    return True, "Password meets all requirements."


def bootstrap_super_admin():
    """
    Create the initial PLACEHUB Super Admin.

    Run this script only during initial system setup.
    """

    connection = get_connection()

    try:
        cursor = connection.cursor()

        # Check that the PLACEHUB migration has been applied.
        table_exists = cursor.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'placehub_accounts'
        """).fetchone()

        if not table_exists:
            print(
                "ERROR: PLACEHUB account table does not exist.\n"
                "Run the approved database migration first."
            )
            return

        # Prevent creating another Super Admin.
        existing_super_admin = cursor.execute("""
            SELECT id
            FROM placehub_accounts
            WHERE role = 'SUPER_ADMIN'
            LIMIT 1
        """).fetchone()

        if existing_super_admin:
            print(
                "A Super Admin account already exists.\n"
                "Bootstrap is not allowed again."
            )
            return

        print("\nPLACEHUB — Initial Super Admin Setup\n")

        username = input("Enter Super Admin username: ").strip()
        name = input("Enter Super Admin full name: ").strip()
        email = input("Enter Super Admin email: ").strip().lower()

        password = getpass.getpass(
            "Enter initial Super Admin password: "
        )

        confirm_password = getpass.getpass(
            "Confirm initial password: "
        )

        if not username or not name or not email:
            print("ERROR: All account details are required.")
            return

        if not re.fullmatch(
            r"[A-Za-z0-9_.-]{4,50}", username
        ):
            print(
                "ERROR: Username must be 4–50 characters "
                "and contain only letters, numbers, "
                "underscores, dots or hyphens."
            )
            return

        if not re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+", email
        ):
            print("ERROR: Enter a valid email address.")
            return

        if password != confirm_password:
            print("ERROR: Passwords do not match.")
            return

        valid, message = validate_password(password)

        if not valid:
            print(f"ERROR: {message}")
            return

        # Hash the password before saving.
        password_hash = generate_password_hash(password)

        # Acquire the write lock before checking again.
        # This prevents concurrent bootstrap operations.
        cursor.execute("BEGIN IMMEDIATE")

        existing_super_admin = cursor.execute("""
            SELECT id
            FROM placehub_accounts
            WHERE role = 'SUPER_ADMIN'
            LIMIT 1
        """).fetchone()

        if existing_super_admin:
            connection.rollback()
            print("ERROR: Super Admin was already created.")
            return

        cursor.execute("""
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
            VALUES (?, ?, ?, 'SUPER_ADMIN', ?, 1, 1, 0)
        """, (
            username,
            name,
            email,
            password_hash
        ))

        connection.commit()

        print("\nSuper Admin created successfully.")
        print("Username:", username)
        print("First login will require a password change.")

    except sqlite3.IntegrityError:
        connection.rollback()
        print(
            "ERROR: Username or email already exists, "
            "or the account violates a database constraint."
        )

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    bootstrap_super_admin()
