import secrets
import re

from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort
)

from ADMIN import admin_bp
from ADMIN.auth import (
    hash_password,
    verify_password,
    validate_password
)
from ADMIN.permissions import (
    admin_login_required,
    super_admin_required,
    get_current_placehub_account,
    clear_placehub_session
)
from database import get_connection


# ==========================================
# CSRF PROTECTION
# ==========================================

def get_csrf_token():

    if "placehub_csrf_token" not in session:
        session["placehub_csrf_token"] = secrets.token_urlsafe(32)

    return session["placehub_csrf_token"]


@admin_bp.context_processor
def inject_csrf_token():

    return {"csrf_token": get_csrf_token}


def validate_csrf():

    submitted_token = request.form.get("csrf_token", "")
    stored_token = session.get("placehub_csrf_token", "")

    if not submitted_token or not stored_token:
        abort(400, description="Invalid security token.")

    if not secrets.compare_digest(submitted_token, stored_token):
        abort(400, description="Invalid security token.")


# ==========================================
# SUPER ADMIN LOGIN
# ==========================================

@admin_bp.route("/login", methods=["GET", "POST"])
def login():

    selected_role = (
        request.args.get("role")
        if request.method == "GET"
        else request.form.get("role")
    )

    if selected_role not in ("ADMIN", "SUPER_ADMIN"):
        abort(400, description="Invalid management login type.")

    # If an account is already authenticated, send it to the
    # correct area instead of allowing role switching in one session.
    current_account = get_current_placehub_account()

    if current_account:
        if current_account["role"] == "SUPER_ADMIN":
            return redirect(url_for("admin.super_admin_dashboard"))

        if current_account["role"] == "ADMIN":
            if current_account["must_change_password"]:
                return redirect(url_for("admin.admin_change_password"))
            return redirect(url_for("admin.admin_dashboard"))

    if request.method == "POST":

        validate_csrf()

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # Admin IDs are case-insensitive and stored in uppercase.
        if selected_role == "ADMIN":
            username = username.upper()

        if not username or not password:
            flash("Enter your username and password.", "danger")
            return redirect(url_for("admin.login", role=selected_role))

        connection = get_connection()

        try:
            account = connection.execute(
                """
                SELECT *
                FROM placehub_accounts
                WHERE username = ?
                  AND role = ?
                """,
                (username, selected_role)
            ).fetchone()
        finally:
            connection.close()

        if account is None or not verify_password(
            account["password_hash"], password
        ):
            flash("Invalid username or password.", "danger")
            return redirect(url_for("admin.login", role=selected_role))

        if not account["is_active"]:
            flash("This account is disabled. Contact the Super Admin.", "danger")
            return redirect(url_for("admin.login", role=selected_role))

        # Start a fresh authenticated PLACEHUB session.
        session.clear()
        session["placehub_account_id"] = account["id"]
        session["placehub_role"] = account["role"]
        session["placehub_session_version"] = account["session_version"]
        session["placehub_username"] = account["username"]

        if account["role"] == "SUPER_ADMIN":
            return redirect(url_for("admin.super_admin_dashboard"))

        if account["must_change_password"]:
            flash(
                "You must create a new password before continuing.",
                "warning"
            )
            return redirect(url_for("admin.admin_change_password"))

        return redirect(url_for("admin.admin_dashboard"))

    template = (
        "admin/superadmin/login.html"
        if selected_role == "SUPER_ADMIN"
        else "admin/admin/login.html"
    )

    return render_template(
        template,
        selected_role=selected_role
    )


# ==========================================
# OPTIONAL SUPER ADMIN PASSWORD CHANGE
# ==========================================

@admin_bp.route("/change-password", methods=["GET", "POST"])
@super_admin_required
def change_password():

    account = get_current_placehub_account()

    if not account:
        clear_placehub_session()
        return redirect(
            url_for("admin.login", role="SUPER_ADMIN")
        )

    if request.method == "GET":
        return render_template(
            "admin/superadmin/change_password.html",
            account=account
        )

    validate_csrf()

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password or not new_password or not confirm_password:
        flash("Please fill in all password fields.", "danger")
        return redirect(url_for("admin.change_password"))

    if new_password != confirm_password:
        flash("New password and confirmation do not match.", "danger")
        return redirect(url_for("admin.change_password"))

    valid, message = validate_password(new_password)

    if not valid:
        flash(message, "danger")
        return redirect(url_for("admin.change_password"))

    connection = get_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        stored_account = connection.execute(
            """
            SELECT password_hash, session_version
            FROM placehub_accounts
            WHERE id = ?
              AND role = 'SUPER_ADMIN'
              AND is_active = 1
            """,
            (account["id"],)
        ).fetchone()

        if stored_account is None:
            connection.rollback()
            clear_placehub_session()
            return redirect(
                url_for("admin.login", role="SUPER_ADMIN")
            )

        if not verify_password(
            stored_account["password_hash"],
            current_password
        ):
            connection.rollback()
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("admin.change_password"))

        if verify_password(
            stored_account["password_hash"],
            new_password
        ):
            connection.rollback()
            flash(
                "Choose a password different from your current password.",
                "danger"
            )
            return redirect(url_for("admin.change_password"))

        connection.execute(
            """
            UPDATE placehub_accounts
            SET password_hash = ?,
                must_change_password = 0,
                session_version = session_version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND is_active = 1
            """,
            (hash_password(new_password), account["id"])
        )

        connection.execute(
            """
            INSERT INTO placehub_audit_logs (
                actor_account_id,
                action,
                target_type,
                target_id,
                details
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                account["id"],
                "PASSWORD_CHANGED",
                "PLACEHUB_ACCOUNT",
                str(account["id"]),
                "Super Admin password changed successfully."
            )
        )

        updated_account = connection.execute(
            """
            SELECT session_version
            FROM placehub_accounts
            WHERE id = ?
            """,
            (account["id"],)
        ).fetchone()

        connection.commit()

        # Keep this session active and revoke older sessions.
        session["placehub_session_version"] = (
            updated_account["session_version"]
        )

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    flash("Password changed successfully.", "success")

    return redirect(
        url_for("admin.super_admin_dashboard")
    )


# ==========================================
# SUPER ADMIN DASHBOARD
# ==========================================

@admin_bp.route("/super-admin/dashboard")
@super_admin_required
def super_admin_dashboard():

    account = get_current_placehub_account()

    connection = get_connection()

    try:

        active_admins = connection.execute(
            """
            SELECT COUNT(*)
            FROM placehub_accounts
            WHERE role = 'ADMIN'
              AND is_active = 1
            """
        ).fetchone()[0]

        inactive_admins = connection.execute(
            """
            SELECT COUNT(*)
            FROM placehub_accounts
            WHERE role = 'ADMIN'
              AND is_active = 0
            """
        ).fetchone()[0]

        recent_logs = connection.execute(
            """
            SELECT
                id,
                actor_account_id,
                action,
                target_type,
                target_id,
                details,
                created_at
            FROM placehub_audit_logs
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()

    finally:
        connection.close()

    return render_template(
        "admin/superadmin/dashboard.html",
        account=account,
        active_admins=active_admins,
        inactive_admins=inactive_admins,
        recent_logs=recent_logs
    )


# ==========================================
# SUPER ADMIN - AUDIT ACTIVITY
# ==========================================

@admin_bp.route("/super-admin/audit-activity")
@super_admin_required
def audit_activity():
    """Read-only, paginated audit history for the Super Admin."""
    page = request.args.get("page", 1, type=int)
    page = max(page or 1, 1)
    per_page = 25
    search_term = request.args.get("q", "").strip()[:100]
    action_filter = request.args.get("action", "").strip()[:80]

    conditions = []
    parameters = []

    if search_term:
        like_term = f"%{search_term}%"
        conditions.append("""
            (
                CAST(log.id AS TEXT) LIKE ?
                OR log.action LIKE ?
                OR log.target_type LIKE ?
                OR log.target_id LIKE ?
                OR log.details LIKE ?
                OR actor.username LIKE ?
                OR actor.name LIKE ?
            )
        """)
        parameters.extend([like_term] * 7)

    if action_filter:
        conditions.append("log.action = ?")
        parameters.append(action_filter)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    connection = get_connection()
    try:
        total = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM placehub_audit_logs AS log
            LEFT JOIN placehub_accounts AS actor
                ON actor.id = log.actor_account_id
            {where_clause}
            """,
            parameters
        ).fetchone()[0]

        total_pages = max((total + per_page - 1) // per_page, 1)
        page = min(page, total_pages)
        offset = (page - 1) * per_page

        logs = connection.execute(
            f"""
            SELECT
                log.id,
                log.actor_account_id,
                log.action,
                log.target_type,
                log.target_id,
                log.details,
                log.created_at,
                actor.username AS actor_username,
                actor.name AS actor_name
            FROM placehub_audit_logs AS log
            LEFT JOIN placehub_accounts AS actor
                ON actor.id = log.actor_account_id
            {where_clause}
            ORDER BY log.id DESC
            LIMIT ? OFFSET ?
            """,
            parameters + [per_page, offset]
        ).fetchall()

        actions = connection.execute(
            """
            SELECT DISTINCT action
            FROM placehub_audit_logs
            ORDER BY action
            """
        ).fetchall()
    finally:
        connection.close()

    return render_template(
        "admin/superadmin/audit_activity.html",
        logs=logs,
        actions=actions,
        search_term=search_term,
        action_filter=action_filter,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page
    )


# ==========================================
# ADMIN ACCOUNT INPUT VALIDATION
# ==========================================

def validate_admin_username(username):
    """Accept Admin IDs in the exact format AD1234-5678."""
    return bool(re.fullmatch(r"AD\d{4}-\d{4}", username, re.IGNORECASE))


def validate_admin_email(email):
    """
    Accept syntactically valid email addresses from any domain.
    This supports different institutional domains and Gmail without
    hardcoding one college's domain.
    """
    pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    return bool(re.fullmatch(pattern, email))


# ==========================================
# SUPER ADMIN - ADMIN ACCOUNT MANAGEMENT
# ==========================================

def generate_temporary_password():
    """Generate a temporary password satisfying the password policy."""
    import string

    characters = (
        string.ascii_lowercase
        + string.ascii_uppercase
        + string.digits
        + "!@#$%^&*()-_=+"
    )

    password = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*()-_=+")
    ]

    password.extend(
        secrets.choice(characters) for _ in range(8)
    )

    secrets.SystemRandom().shuffle(password)
    return "".join(password)


def render_admin_accounts(
    one_time_password=None,
    one_time_username=None
):
    connection = get_connection()

    try:
        accounts = connection.execute(
            """
            SELECT
                id,
                username,
                name,
                email,
                is_active,
                must_change_password,
                created_at
            FROM placehub_accounts
            WHERE role = 'ADMIN'
            ORDER BY id DESC
            """
        ).fetchall()

    finally:
        connection.close()

    return render_template(
        "admin/superadmin/admin_accounts.html",
        accounts=accounts,
        active_count=sum(1 for a in accounts if a["is_active"]),
        inactive_count=sum(1 for a in accounts if not a["is_active"]),
        max_admins=5,
        one_time_password=one_time_password,
        one_time_username=one_time_username
    )


@admin_bp.route("/super-admin/admins")
@super_admin_required
def admin_accounts():
    return render_admin_accounts()


@admin_bp.route("/super-admin/admins/create", methods=["POST"])
@super_admin_required
def create_admin():

    validate_csrf()

    username = request.form.get("username", "").strip().upper()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()

    if not username or not name or not email:
        flash("All fields are required.", "danger")
        return redirect(url_for("admin.admin_accounts"))

    if not validate_admin_username(username):
        flash(
            "Invalid Admin ID. Use the format AD1234-5678.",
            "danger"
        )
        return redirect(url_for("admin.admin_accounts"))

    if not validate_admin_email(email):
        flash(
            "Enter a valid college or Gmail email address.",
            "danger"
        )
        return redirect(url_for("admin.admin_accounts"))

    connection = get_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        active_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM placehub_accounts
            WHERE role = 'ADMIN' AND is_active = 1
            """
        ).fetchone()[0]

        if active_count >= 5:
            connection.rollback()
            flash("Maximum of 5 active Admin accounts reached.", "danger")
            return redirect(url_for("admin.admin_accounts"))

        duplicate = connection.execute(
            """
            SELECT id
            FROM placehub_accounts
            WHERE username = ? OR email = ?
            """,
            (username, email)
        ).fetchone()

        if duplicate:
            connection.rollback()
            flash("Username or email already exists.", "danger")
            return redirect(url_for("admin.admin_accounts"))

        temporary_password = generate_temporary_password()

        valid, message = validate_password(temporary_password)
        if not valid:
            connection.rollback()
            flash("Could not generate a valid temporary password.", "danger")
            return redirect(url_for("admin.admin_accounts"))

        actor = get_current_placehub_account()

        cursor = connection.execute(
            """
            INSERT INTO placehub_accounts (
                username,
                name,
                email,
                role,
                password_hash,
                is_active,
                must_change_password,
                session_version,
                created_by
            )
            VALUES (?, ?, ?, 'ADMIN', ?, 1, 1, 0, ?)
            """,
            (
                username,
                name,
                email,
                hash_password(temporary_password),
                actor["id"]
            )
        )

        new_id = cursor.lastrowid

        connection.execute(
            """
            INSERT INTO placehub_audit_logs (
                actor_account_id,
                action,
                target_type,
                target_id,
                details
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor["id"],
                "ADMIN_CREATED",
                "PLACEHUB_ACCOUNT",
                str(new_id),
                f"Admin account created: {username}"
            )
        )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    # Display the temporary password only in this response.
    return render_admin_accounts(
        one_time_password=temporary_password,
        one_time_username=username
    )


@admin_bp.route(
    "/super-admin/admins/<int:account_id>/edit",
    methods=["POST"]
)
@super_admin_required
def update_admin(account_id):

    validate_csrf()

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()

    if not name or not email:
        flash("Name and email are required.", "danger")
        return redirect(url_for("admin.admin_accounts"))

    if not validate_admin_email(email):
        flash(
            "Enter a valid college or Gmail email address.",
            "danger"
        )
        return redirect(url_for("admin.admin_accounts"))

    actor = get_current_placehub_account()
    connection = get_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        target = connection.execute(
            """
            SELECT id, username
            FROM placehub_accounts
            WHERE id = ? AND role = 'ADMIN'
            """,
            (account_id,)
        ).fetchone()

        if target is None:
            connection.rollback()
            abort(404)

        duplicate = connection.execute(
            """
            SELECT id
            FROM placehub_accounts
            WHERE email = ? AND id != ?
            """,
            (email, account_id)
        ).fetchone()

        if duplicate:
            connection.rollback()
            flash("That email address is already in use.", "danger")
            return redirect(url_for("admin.admin_accounts"))

        connection.execute(
            """
            UPDATE placehub_accounts
            SET name = ?,
                email = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND role = 'ADMIN'
            """,
            (name, email, account_id)
        )

        connection.execute(
            """
            INSERT INTO placehub_audit_logs (
                actor_account_id, action, target_type, target_id, details
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor["id"],
                "ADMIN_UPDATED",
                "PLACEHUB_ACCOUNT",
                str(account_id),
                f"Updated Admin account: {target['username']}"
            )
        )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    flash("Admin account updated successfully.", "success")
    return redirect(url_for("admin.admin_accounts"))


@admin_bp.route(
    "/super-admin/admins/<int:account_id>/toggle",
    methods=["POST"]
)
@super_admin_required
def toggle_admin(account_id):

    validate_csrf()

    actor = get_current_placehub_account()
    connection = get_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        target = connection.execute(
            """
            SELECT id, username, is_active
            FROM placehub_accounts
            WHERE id = ? AND role = 'ADMIN'
            """,
            (account_id,)
        ).fetchone()

        if target is None:
            connection.rollback()
            abort(404)

        new_status = 0 if target["is_active"] else 1

        if new_status == 1:
            active_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM placehub_accounts
                WHERE role = 'ADMIN' AND is_active = 1
                """
            ).fetchone()[0]

            if active_count >= 5:
                connection.rollback()
                flash("Cannot enable this account: 5 active Admins already exist.", "danger")
                return redirect(url_for("admin.admin_accounts"))

        connection.execute(
            """
            UPDATE placehub_accounts
            SET is_active = ?,
                session_version = session_version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND role = 'ADMIN'
            """,
            (new_status, account_id)
        )

        action = "ADMIN_ENABLED" if new_status else "ADMIN_DISABLED"

        connection.execute(
            """
            INSERT INTO placehub_audit_logs (
                actor_account_id, action, target_type, target_id, details
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor["id"],
                action,
                "PLACEHUB_ACCOUNT",
                str(account_id),
                f"{target['username']} status changed."
            )
        )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    flash("Admin account status updated.", "success")
    return redirect(url_for("admin.admin_accounts"))


@admin_bp.route(
    "/super-admin/admins/<int:account_id>/reset-password",
    methods=["POST"]
)
@super_admin_required
def reset_admin_password(account_id):

    validate_csrf()

    actor = get_current_placehub_account()
    connection = get_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        target = connection.execute(
            """
            SELECT id, username
            FROM placehub_accounts
            WHERE id = ? AND role = 'ADMIN'
            """,
            (account_id,)
        ).fetchone()

        if target is None:
            connection.rollback()
            abort(404)

        temporary_password = generate_temporary_password()

        connection.execute(
            """
            UPDATE placehub_accounts
            SET password_hash = ?,
                must_change_password = 1,
                session_version = session_version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND role = 'ADMIN'
            """,
            (hash_password(temporary_password), account_id)
        )

        connection.execute(
            """
            INSERT INTO placehub_audit_logs (
                actor_account_id, action, target_type, target_id, details
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor["id"],
                "ADMIN_PASSWORD_RESET",
                "PLACEHUB_ACCOUNT",
                str(account_id),
                f"Temporary password generated for {target['username']}."
            )
        )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    return render_admin_accounts(
        one_time_password=temporary_password,
        one_time_username=target["username"]
    )


# ==========================================
# ADMIN FIRST-LOGIN PASSWORD CHANGE
# ==========================================

@admin_bp.route("/first-login/change-password", methods=["GET", "POST"])
@admin_login_required
def admin_change_password():

    account = get_current_placehub_account()

    if not account or account["role"] != "ADMIN":
        abort(403)

    if request.method == "GET":
        return render_template(
            "admin/admin/change_password.html",
            account=account
        )

    validate_csrf()

    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not new_password or not confirm_password:
        flash("Enter and confirm your new password.", "danger")
        return redirect(url_for("admin.admin_change_password"))

    if new_password != confirm_password:
        flash("The new password and confirmation do not match.", "danger")
        return redirect(url_for("admin.admin_change_password"))

    valid, message = validate_password(new_password)
    if not valid:
        flash(message, "danger")
        return redirect(url_for("admin.admin_change_password"))

    connection = get_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        stored = connection.execute(
            """
            SELECT password_hash
            FROM placehub_accounts
            WHERE id = ?
              AND role = 'ADMIN'
              AND is_active = 1
            """,
            (account["id"],)
        ).fetchone()

        if stored is None:
            connection.rollback()
            clear_placehub_session()
            return redirect(url_for("admin.login", role="ADMIN"))

        if verify_password(stored["password_hash"], new_password):
            connection.rollback()
            flash(
                "Choose a password different from your temporary password.",
                "danger"
            )
            return redirect(url_for("admin.admin_change_password"))

        connection.execute(
            """
            UPDATE placehub_accounts
            SET password_hash = ?,
                must_change_password = 0,
                session_version = session_version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND role = 'ADMIN'
            """,
            (hash_password(new_password), account["id"])
        )

        connection.execute(
            """
            INSERT INTO placehub_audit_logs
                (actor_account_id, action, target_type, target_id, details)
            VALUES (?, 'ADMIN_PASSWORD_CHANGED', 'PLACEHUB_ACCOUNT', ?, ?)
            """,
            (
                account["id"],
                str(account["id"]),
                f"Admin {account['username']} changed the temporary password."
            )
        )

        updated = connection.execute(
            "SELECT session_version FROM placehub_accounts WHERE id = ?",
            (account["id"],)
        ).fetchone()

        connection.commit()

        # Keep this browser session active; older sessions are revoked.
        session["placehub_session_version"] = updated["session_version"]

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    flash("Password updated successfully.", "success")
    return redirect(url_for("admin.admin_dashboard"))


# ==========================================
# ADMIN - FACULTY MANAGEMENT
# ==========================================

def generate_faculty_temporary_password():
    """Generate a temporary faculty password matching PLACEHUB policy."""
    import string

    special = "!@#$%^&*()-_=+"
    pool = string.ascii_letters + string.digits + special
    chars = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice(special),
    ]
    chars.extend(secrets.choice(pool) for _ in range(8))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def validate_faculty_email(email):
    pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    return bool(re.fullmatch(pattern, email))


def record_faculty_audit(connection, actor, action, faculty_id, details):
    connection.execute(
        """
        INSERT INTO placehub_audit_logs (
            actor_account_id, action, target_type, target_id, details
        ) VALUES (?, ?, 'FACULTY', ?, ?)
        """,
        (actor["id"], action, str(faculty_id), details)
    )


def render_faculty_management(one_time_password=None, one_time_faculty_id=None, search=""):
    account = get_current_placehub_account()
    connection = get_connection()
    try:
        if search:
            faculty_members = connection.execute(
                """
                SELECT id, tts_number, name, email, role, date_of_birth,
                       is_active, session_version
                FROM admins
                WHERE tts_number LIKE ? OR name LIKE ? OR email LIKE ?
                ORDER BY role, name
                """,
                (f"%{search}%", f"%{search}%", f"%{search}%")
            ).fetchall()
        else:
            faculty_members = connection.execute(
                """
                SELECT id, tts_number, name, email, role, date_of_birth,
                       is_active, session_version
                FROM admins ORDER BY role, name
                """
            ).fetchall()
        role_counts = connection.execute(
            "SELECT role, COUNT(*) AS total FROM admins GROUP BY role"
        ).fetchall()
    finally:
        connection.close()

    counts = {row["role"]: row["total"] for row in role_counts}
    return render_template(
        "admin/admin/faculty_management.html",
        account=account,
        faculty_members=faculty_members,
        search=search,
        total_faculty=sum(counts.values()),
        dean_count=counts.get("Dean", 0),
        hod_count=counts.get("HOD", 0),
        trainer_count=counts.get("Trainer", 0),
        one_time_password=one_time_password,
        one_time_faculty_id=one_time_faculty_id,
    )


@admin_bp.route("/faculty", methods=["GET"])
@admin_login_required
def faculty_management():
    account = get_current_placehub_account()
    if not account or account["role"] != "ADMIN":
        abort(403)
    search = request.args.get("search", "").strip()
    return render_faculty_management(search=search)


@admin_bp.route("/faculty/create", methods=["POST"])
@admin_login_required
def create_faculty():
    account = get_current_placehub_account()
    if not account or account["role"] != "ADMIN":
        abort(403)
    validate_csrf()

    tts_number = request.form.get("tts_number", "").strip().upper()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "").strip()
    date_of_birth = request.form.get("date_of_birth", "").strip()

    if not all([tts_number, name, email, role, date_of_birth]):
        flash("All faculty fields are required.", "danger")
        return redirect(url_for("admin.faculty_management"))
    if not re.fullmatch(r"TTS\d{4}", tts_number):
        flash("Faculty ID must use the format TTS1234.", "danger")
        return redirect(url_for("admin.faculty_management"))
    if role not in ("Dean", "HOD", "Trainer"):
        flash("Select a valid faculty role.", "danger")
        return redirect(url_for("admin.faculty_management"))
    if not validate_faculty_email(email):
        flash("Enter a valid email address.", "danger")
        return redirect(url_for("admin.faculty_management"))
    if not re.fullmatch(r"\d{8}", date_of_birth):
        flash("Date of birth must be in DDMMYYYY format.", "danger")
        return redirect(url_for("admin.faculty_management"))

    temporary_password = generate_faculty_temporary_password()
    valid, _ = validate_password(temporary_password)
    if not valid:
        abort(500, description="Could not generate a compliant temporary password.")

    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        duplicate = connection.execute(
            "SELECT id FROM admins WHERE tts_number = ? OR email = ?",
            (tts_number, email)
        ).fetchone()
        if duplicate:
            connection.rollback()
            flash("Faculty ID or email already exists.", "danger")
            return redirect(url_for("admin.faculty_management"))

        cursor = connection.execute(
            """
            INSERT INTO admins (
                tts_number, name, email, role, date_of_birth,
                password, is_active, session_version
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 0)
            """,
            (tts_number, name, email, role, date_of_birth,
             hash_password(temporary_password))
        )
        faculty_id = cursor.lastrowid
        record_faculty_audit(
            connection, account, "FACULTY_CREATED", faculty_id,
            f"Created {role} faculty account {tts_number}."
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    flash("Faculty account created. Copy the temporary password now.", "success")
    return render_faculty_management(temporary_password, tts_number)



@admin_bp.route("/faculty/<int:faculty_id>/edit", methods=["POST"])
@admin_login_required
def update_faculty(faculty_id):
    account = get_current_placehub_account()
    if not account or account["role"] != "ADMIN":
        abort(403)
    validate_csrf()

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "").strip()
    date_of_birth = request.form.get("date_of_birth", "").strip()

    if not all([name, email, role]):
        flash("Name, email and role are required.", "danger")
        return redirect(url_for("admin.faculty_management"))
    if date_of_birth and not re.fullmatch(r"\\d{8}", date_of_birth):
        flash("Date of birth must be in DDMMYYYY format.", "danger")
        return redirect(url_for("admin.faculty_management"))
    if not validate_faculty_email(email):
        flash("Enter a valid email address.", "danger")
        return redirect(url_for("admin.faculty_management"))
    if role not in ("Dean", "HOD", "Trainer"):
        flash("Select a valid faculty role.", "danger")
        return redirect(url_for("admin.faculty_management"))
    if not re.fullmatch(r"\d{8}", date_of_birth):
        flash("Date of birth must be in DDMMYYYY format.", "danger")
        return redirect(url_for("admin.faculty_management"))

    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        target = connection.execute(
            "SELECT id, tts_number FROM admins WHERE id = ?",
            (faculty_id,)
        ).fetchone()
        if target is None:
            connection.rollback()
            abort(404)
        duplicate = connection.execute(
            "SELECT id FROM admins WHERE email = ? AND id != ?",
            (email, faculty_id)
        ).fetchone()
        if duplicate:
            connection.rollback()
            flash("That email address is already assigned to another faculty member.", "danger")
            return redirect(url_for("admin.faculty_management"))

        if date_of_birth:
            connection.execute(
                """
                UPDATE admins
                SET name = ?, email = ?, role = ?, date_of_birth = ?
                WHERE id = ?
                """,
                (name, email, role, date_of_birth, faculty_id)
            )
        else:
            connection.execute(
                """
                UPDATE admins
                SET name = ?, email = ?, role = ?
                WHERE id = ?
                """,
                (name, email, role, faculty_id)
            )
        record_faculty_audit(
            connection, account, "FACULTY_UPDATED", faculty_id,
            f"Updated faculty account {target['tts_number']}."
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    flash("Faculty details updated.", "success")
    return redirect(url_for("admin.faculty_management"))


@admin_bp.route("/faculty/<int:faculty_id>/toggle", methods=["POST"])
@admin_login_required
def toggle_faculty(faculty_id):
    account = get_current_placehub_account()
    if not account or account["role"] != "ADMIN":
        abort(403)
    validate_csrf()

    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        target = connection.execute(
            "SELECT id, tts_number, is_active FROM admins WHERE id = ?",
            (faculty_id,)
        ).fetchone()
        if target is None:
            connection.rollback()
            abort(404)

        new_status = 0 if target["is_active"] else 1
        connection.execute(
            """
            UPDATE admins
            SET is_active = ?, session_version = session_version + 1
            WHERE id = ?
            """,
            (new_status, faculty_id)
        )
        action = "FACULTY_DISABLED" if not new_status else "FACULTY_ENABLED"
        record_faculty_audit(
            connection, account, action, faculty_id,
            f"Faculty account {target['tts_number']} status changed."
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    flash("Faculty account status updated.", "success")
    return redirect(url_for("admin.faculty_management"))


@admin_bp.route("/faculty/<int:faculty_id>/reset-password", methods=["POST"])
@admin_login_required
def reset_faculty_password(faculty_id):
    account = get_current_placehub_account()
    if not account or account["role"] != "ADMIN":
        abort(403)
    validate_csrf()

    temporary_password = generate_faculty_temporary_password()
    valid, _ = validate_password(temporary_password)
    if not valid:
        abort(500, description="Could not generate a compliant temporary password.")

    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        target = connection.execute(
            "SELECT id, tts_number FROM admins WHERE id = ?",
            (faculty_id,)
        ).fetchone()
        if target is None:
            connection.rollback()
            abort(404)

        connection.execute(
            """
            UPDATE admins
            SET password = ?, session_version = session_version + 1
            WHERE id = ?
            """,
            (hash_password(temporary_password), faculty_id)
        )
        record_faculty_audit(
            connection, account, "FACULTY_PASSWORD_RESET", faculty_id,
            f"Generated a new temporary password for {target['tts_number']}."
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    flash("Temporary password generated. It is shown only once.", "success")
    return render_faculty_management(temporary_password, target["tts_number"])



# ==========================================
# ADMIN - STUDENT MANAGEMENT
# ==========================================

def _student_academic_info(admission_year, today=None):
    from datetime import date
    today = today or date.today()
    try:
        admitted = int(admission_year)
    except (TypeError, ValueError):
        return {"batch": "Not assigned", "year_label": "Not assigned",
                "passing_out_year": None, "graduated": False}
    passing_out = admitted + 4
    if today < date(admitted, 6, 2):
        label = "Not started"
    else:
        year_number = today.year - admitted + (1 if (today.month, today.day) >= (6, 2) else 0)
        label = "Completed" if year_number > 4 else (
            f"{year_number}{ {1:'st', 2:'nd', 3:'rd'}.get(year_number, 'th') } Year"
        )
    return {"batch": f"{admitted}-{passing_out}", "year_label": label,
            "passing_out_year": passing_out, "graduated": label == "Completed"}


def _archive_completed_students(connection):
    """Archive batches from 1 November of their passing-out year."""
    connection.execute("""
        UPDATE students
        SET is_archived = 1,
            is_active = 0,
            login_enabled = 0,
            archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP),
            archive_reason = COALESCE(archive_reason, 'Automatic archive: batch completion'),
            session_version = COALESCE(session_version, 0) + 1
        WHERE admission_year IS NOT NULL
          AND COALESCE(is_archived, 0) = 0
          AND TRIM(CAST(admission_year AS TEXT)) GLOB '[0-9][0-9][0-9][0-9]'
          AND date('now', 'localtime') >= date(
              printf('%04d-11-01', CAST(admission_year AS INTEGER) + 4)
          )
    """)


def _student_management_context(search="", batch="", department="", status="active", page=1):
    from datetime import date
    account = get_current_placehub_account()
    connection = get_connection()
    try:
        _archive_completed_students(connection)
        connection.commit()

        conditions = []
        params = []
        if search:
            like = f"%{search}%"
            conditions.append("(register_number LIKE ? OR name LIKE ? OR email LIKE ?)")
            params.extend([like, like, like])
        if batch:
            try:
                admitted = int(batch)
                conditions.append("admission_year = ?")
                params.append(admitted)
            except ValueError:
                batch = ""
        if department:
            conditions.append("department = ?")
            params.append(department)
        if status == "archived":
            conditions.append("COALESCE(is_archived, 0) = 1")
        elif status == "disabled":
            conditions.append("COALESCE(is_archived, 0) = 0 AND (COALESCE(is_active, 1) = 0 OR COALESCE(login_enabled, 1) = 0)")
        else:
            conditions.append("COALESCE(is_archived, 0) = 0 AND COALESCE(is_active, 1) = 1 AND COALESCE(login_enabled, 1) = 1")

        where = "WHERE " + " AND ".join(conditions)
        total = connection.execute(f"SELECT COUNT(*) FROM students {where}", params).fetchone()[0]
        per_page = 20
        total_pages = max((total + per_page - 1) // per_page, 1)
        page = min(max(int(page or 1), 1), total_pages)
        students = connection.execute(f"""
            SELECT id, register_number, name, email, department, year,
                   admission_year, cgpa, is_active, is_archived,
                   login_enabled, archived_at
            FROM students
            {where}
            ORDER BY admission_year DESC, department, name
            LIMIT ? OFFSET ?
        """, params + [per_page, (page - 1) * per_page]).fetchall()

        batches = connection.execute("""
            SELECT DISTINCT admission_year FROM students
            WHERE admission_year IS NOT NULL ORDER BY admission_year DESC
        """).fetchall()
        departments = connection.execute("""
            SELECT DISTINCT department FROM students
            WHERE department IS NOT NULL AND TRIM(department) != ''
            ORDER BY department
        """).fetchall()
        counts = connection.execute("""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN COALESCE(is_archived,0)=0 AND COALESCE(is_active,1)=1 AND COALESCE(login_enabled,1)=1 THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN COALESCE(is_archived,0)=1 THEN 1 ELSE 0 END) AS archived,
              SUM(CASE WHEN COALESCE(is_archived,0)=0 AND (COALESCE(is_active,1)=0 OR COALESCE(login_enabled,1)=0) THEN 1 ELSE 0 END) AS disabled
            FROM students
        """).fetchone()
    finally:
        connection.close()

    enriched = []
    for row in students:
        item = dict(row)
        academic = _student_academic_info(item.get("admission_year"))
        item.update(academic)
        enriched.append(item)

    return render_template(
        "admin/admin/student_management.html",
        account=account, students=enriched, batches=batches,
        departments=departments, counts=counts, search=search,
        selected_batch=batch, selected_department=department,
        selected_status=status, page=page, total_pages=total_pages,
        total=total
    )


@admin_bp.route("/students", methods=["GET"])
@admin_login_required
def student_management():
    account = get_current_placehub_account()
    if not account or account["role"] != "ADMIN":
        abort(403)
    return _student_management_context(
        search=request.args.get("search", "").strip()[:100],
        batch=request.args.get("batch", "").strip(),
        department=request.args.get("department", "").strip(),
        status=request.args.get("status", "active").strip(),
        page=request.args.get("page", 1, type=int)
    )


@admin_bp.route("/students/<int:student_id>", methods=["GET"])
@admin_login_required
def student_management_details(student_id):
    account = get_current_placehub_account()
    if not account or account["role"] != "ADMIN":
        abort(403)
    connection = get_connection()
    try:
        _archive_completed_students(connection)
        connection.commit()
        student = connection.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        if student is None:
            abort(404)
        applications = connection.execute("""
            SELECT a.application_id, a.status, a.applied_date,
                   c.company_name, c.job_role, c.package, c.location
            FROM applications a
            LEFT JOIN companies c ON c.company_id = a.company_id
            WHERE a.student_id = ?
            ORDER BY a.application_id DESC
        """, (student_id,)).fetchall()
        summary = connection.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'Selected' THEN 1 ELSE 0 END) AS selected,
                   SUM(CASE WHEN status = 'Rejected' THEN 1 ELSE 0 END) AS rejected,
                   SUM(CASE WHEN status IN ('Shortlisted','Interview in Progress','Under Review') THEN 1 ELSE 0 END) AS in_progress
            FROM applications WHERE student_id = ?
        """, (student_id,)).fetchone()
    finally:
        connection.close()
    student_data = dict(student)
    student_data.update(_student_academic_info(student_data.get("admission_year")))
    return render_template(
        "admin/admin/student_details.html", account=account,
        student=student_data, applications=applications, summary=summary
    )


@admin_bp.route("/students/<int:student_id>/toggle-access", methods=["POST"])
@admin_login_required
def toggle_student_access(student_id):
    account = get_current_placehub_account()
    if not account or account["role"] != "ADMIN":
        abort(403)
    validate_csrf()
    enabled = request.form.get("enabled") == "1"
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        student = connection.execute(
            "SELECT id, register_number, is_archived FROM students WHERE id = ?",
            (student_id,)
        ).fetchone()
        if student is None:
            abort(404)
        if student["is_archived"]:
            connection.rollback()
            flash("Archived student accounts cannot be re-enabled.", "warning")
            return redirect(url_for("admin.student_management_details", student_id=student_id))
        connection.execute("""
            UPDATE students
            SET is_active = ?, login_enabled = ?,
                session_version = COALESCE(session_version, 0) + 1
            WHERE id = ?
        """, (1 if enabled else 0, 1 if enabled else 0, student_id))
        connection.execute("""
            INSERT INTO placehub_audit_logs
                (actor_account_id, action, target_type, target_id, details)
            VALUES (?, ?, 'STUDENT', ?, ?)
        """, (account["id"], "STUDENT_ACCESS_ENABLED" if enabled else "STUDENT_ACCESS_DISABLED",
              str(student_id), f"Student access updated for {student['register_number']}"))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    flash("Student login access updated.", "success")
    return redirect(url_for("admin.student_management_details", student_id=student_id))


# ==========================================
# ADMIN DASHBOARD LANDING PAGE
# ==========================================

@admin_bp.route("/dashboard")
@admin_login_required
def admin_dashboard():

    account = get_current_placehub_account()

    if not account or account["role"] != "ADMIN":
        abort(403)

    return render_template(
        "admin/admin/dashboard.html",
        account=account
    )


# ==========================================
# LOGOUT
# ==========================================

@admin_bp.route("/logout", methods=["POST"])
@admin_login_required
def logout():

    validate_csrf()

    clear_placehub_session()

    flash("You have been logged out successfully.", "success")

    return redirect(url_for("home"))
