from functools import wraps

from flask import (
    session,
    redirect,
    url_for,
    abort,
    request
)

from database import get_connection


def get_current_placehub_account():
    """
    Fetch and validate the currently logged-in PLACEHUB account.

    Every protected request checks:
    1. The account exists.
    2. The account is active.
    3. The session version matches the database.
    4. The session role matches the database role.
    5. The account has a recognized PLACEHUB role.
    """

    account_id = session.get("placehub_account_id")

    if not account_id:
        return None

    connection = get_connection()

    try:
        account = connection.execute(
            """
            SELECT
                id,
                username,
                name,
                email,
                role,
                is_active,
                must_change_password,
                session_version
            FROM placehub_accounts
            WHERE id = ?
            """,
            (account_id,)
        ).fetchone()

    finally:
        connection.close()

    if account is None:
        session.clear()
        return None

    if not account["is_active"]:
        session.clear()
        return None

    if account["session_version"] != session.get(
        "placehub_session_version"
    ):
        session.clear()
        return None

    if account["role"] != session.get("placehub_role"):
        session.clear()
        return None

    if account["role"] not in ("SUPER_ADMIN", "ADMIN"):
        session.clear()
        return None

    return account


def clear_placehub_session():
    """Clear the PLACEHUB login session."""
    session.clear()


def admin_login_required(function):
    """
    Protect routes requiring an authenticated PLACEHUB account.

    ADMIN:
        Must change the generated temporary password before accessing
        normal Admin pages.

    SUPER_ADMIN:
        Is not forced to change password. The decorator still permits
        authenticated Super Admin access to shared routes such as logout.
    """

    @wraps(function)
    def wrapper(*args, **kwargs):

        account = get_current_placehub_account()

        if account is None:
            return redirect(
                url_for("admin.login", role="ADMIN")
            )

        # Only regular Admin accounts have mandatory first-login
        # password change. Super Admin passwords remain optional to change.
        if (
            account["role"] == "ADMIN"
            and account["must_change_password"]
        ):

            allowed_endpoints = {
                "admin.admin_change_password",
                "admin.logout"
            }

            if request.endpoint not in allowed_endpoints:
                return redirect(
                    url_for("admin.admin_change_password")
                )

        return function(*args, **kwargs)

    return wrapper


def super_admin_required(function):
    """
    Allow access only to authenticated, active Super Admin accounts.

    Super Admin password changes are optional.
    """

    @wraps(function)
    def wrapper(*args, **kwargs):

        account = get_current_placehub_account()

        if account is None:
            return redirect(
                url_for("admin.login", role="SUPER_ADMIN")
            )

        if account["role"] != "SUPER_ADMIN":
            abort(403)

        # Do not force Super Admin to change password.
        return function(*args, **kwargs)

    return wrapper
