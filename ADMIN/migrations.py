
from database import get_connection


def get_columns(cursor, table_name):
    """Return the existing column names of a table."""
    return {
        row["name"]
        for row in cursor.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def add_column_if_missing(cursor, table_name, column_name, definition):
    """Add a column only if it does not already exist."""
    columns = get_columns(cursor, table_name)

    if column_name not in columns:
        cursor.execute(
            f"ALTER TABLE {table_name} "
            f"ADD COLUMN {column_name} {definition}"
        )
        print(f"Added {table_name}.{column_name}")


def run_placehub_migrations():
    """
    Prepare the existing PLACEHUB database for
    Super Admin and Admin account management.

    Existing records and placement relationships are preserved.
    This function does not delete or reset existing data.
    """

    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("BEGIN IMMEDIATE")

        # Ensure the existing tables are available.
        existing_tables = {
            row["name"]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table'"
            ).fetchall()
        }

        required_tables = {"admins", "students"}

        missing_tables = required_tables - existing_tables

        if missing_tables:
            raise RuntimeError(
                "Existing database tables are missing: "
                + ", ".join(sorted(missing_tables))
                + ". Initialize the existing database first."
            )

        # ==========================================
        # 1. PLACEHUB SUPER ADMIN AND ADMIN ACCOUNTS
        # ==========================================

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS placehub_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                username TEXT NOT NULL UNIQUE,

                name TEXT NOT NULL,

                email TEXT NOT NULL UNIQUE,

                role TEXT NOT NULL
                    CHECK (role IN ('SUPER_ADMIN', 'ADMIN')),

                password_hash TEXT NOT NULL,

                is_active INTEGER NOT NULL DEFAULT 1
                    CHECK (is_active IN (0, 1)),

                must_change_password INTEGER NOT NULL DEFAULT 1
                    CHECK (must_change_password IN (0, 1)),

                session_version INTEGER NOT NULL DEFAULT 0,

                created_by INTEGER,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (created_by)
                    REFERENCES placehub_accounts(id)
            )
        """)

        # ==========================================
        # 2. PLACEHUB ACTIVITY LOGS
        # ==========================================

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS placehub_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                actor_account_id INTEGER,

                action TEXT NOT NULL,

                target_type TEXT,

                target_id TEXT,

                reason TEXT,

                details TEXT,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (actor_account_id)
                    REFERENCES placehub_accounts(id)
            )
        """)

        # ==========================================
        # 3. FACULTY ACCOUNT MANAGEMENT
        # ==========================================

        add_column_if_missing(
            cursor,
            "admins",
            "is_active",
            "INTEGER NOT NULL DEFAULT 1"
        )

        add_column_if_missing(
            cursor,
            "admins",
            "session_version",
            "INTEGER NOT NULL DEFAULT 0"
        )

        # ==========================================
        # 4. STUDENT ACCOUNT MANAGEMENT

        # Admission year drives dynamic academic-year and batch calculations.
        # Existing students remain unassigned until an Admin sets this value.
        add_column_if_missing(
            cursor, "students", "admission_year", "INTEGER"
        )

        # ==========================================

        add_column_if_missing(
            cursor,
            "students",
            "is_active",
            "INTEGER NOT NULL DEFAULT 1"
        )

        add_column_if_missing(
            cursor,
            "students",
            "is_archived",
            "INTEGER NOT NULL DEFAULT 0"
        )

        add_column_if_missing(
            cursor,
            "students",
            "login_enabled",
            "INTEGER NOT NULL DEFAULT 1"
        )

        add_column_if_missing(
            cursor,
            "students",
            "archived_at",
            "TEXT"
        )

        add_column_if_missing(
            cursor,
            "students",
            "archive_reason",
            "TEXT"
        )

        add_column_if_missing(
            cursor,
            "students",
            "session_version",
            "INTEGER NOT NULL DEFAULT 0"
        )

        # ==========================================
        # 5. COMPATIBILITY WITH EXISTING APP.PY
        # ==========================================

        add_column_if_missing(
            cursor,
            "students",
            "profile_picture",
            "TEXT"
        )

        add_column_if_missing(
            cursor,
            "students",
            "ats_score",
            "REAL"
        )

        # ==========================================
        # 6. INDEXES
        # ==========================================

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_placehub_accounts_role_status
            ON placehub_accounts(role, is_active)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_placehub_audit_created
            ON placehub_audit_logs(created_at)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_students_archive_status
            ON students(is_archived, login_enabled)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_students_admission_year
            ON students(admission_year)
        """)


        connection.commit()

        print("PLACEHUB database migrations completed successfully.")

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()
