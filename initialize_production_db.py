"""
Initialize a NEW PLACEHUB production database on the Render persistent disk.

Run once from the project root after setting DATABASE_PATH to an absolute
path on the mounted Render disk, e.g. /var/data/database.db.

Safety:
- Refuses to run if the target database already contains any application tables.
- Does not call insert_sample_data().
- Does not create a Dean, Faculty, Admin, Student, Company, or Super Admin record.
- Does not read, copy, or modify the local database/database.db.
"""

import os
import sqlite3

database_path = os.getenv("DATABASE_PATH")

if not database_path:
    raise RuntimeError(
        "DATABASE_PATH is missing. Set it to the Render persistent-disk path, "
        "for example /var/data/database.db."
    )

if not os.path.isabs(database_path):
    raise RuntimeError("DATABASE_PATH must be an absolute path on the Render disk.")

# database.py reads DATABASE_PATH at import time, so validate it first.
from database import DATABASE, create_tables, add_cgpa_column
from ADMIN.migrations import run_placehub_migrations


def get_application_tables(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def main():
    resolved_target = os.path.abspath(database_path)
    resolved_module_path = os.path.abspath(DATABASE)

    if resolved_module_path != resolved_target:
        raise RuntimeError(
            "database.py is pointing to a different file than DATABASE_PATH. "
            "Stop and check the Render environment variables."
        )

    os.makedirs(os.path.dirname(resolved_target), exist_ok=True)

    # Never silently initialize or overwrite an existing database.
    check = sqlite3.connect(resolved_target)
    try:
        existing_tables = get_application_tables(check)
    finally:
        check.close()

    if existing_tables:
        raise RuntimeError(
            "Refusing to initialize a non-empty database. Existing tables: "
            + ", ".join(sorted(existing_tables))
        )

    print(f"Creating fresh production schema at {resolved_target}")

    # 1. The nine core application tables.
    create_tables()

    # 2. Compatibility migration for the students.cgpa column.
    add_cgpa_column()

    # 3. PLACEHUB account, audit, archive, and compatibility migrations.
    run_placehub_migrations()

    # 4. Company batch eligibility migration. This is required by app.py.
    connection = sqlite3.connect(resolved_target)
    try:
        company_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(companies)").fetchall()
        }
        if "eligible_batches" not in company_columns:
            connection.execute(
                "ALTER TABLE companies ADD COLUMN eligible_batches TEXT"
            )
            connection.commit()
    finally:
        connection.close()

    # Verify the complete schema against the current project database design.
    expected_tables = {
        "students",
        "placement_restrictions",
        "admins",
        "companies",
        "applications",
        "interview_rounds",
        "interview_results",
        "notifications",
        "application_feedback",
        "placehub_accounts",
        "placehub_audit_logs",
    }

    verify = sqlite3.connect(resolved_target)
    try:
        actual_tables = get_application_tables(verify)
        missing = expected_tables - actual_tables
        unexpected = actual_tables - expected_tables

        if missing or unexpected:
            raise RuntimeError(
                f"Schema table mismatch. Missing: {sorted(missing)}; "
                f"unexpected: {sorted(unexpected)}"
            )

        required_columns = {
            "companies": {"eligible_batches"},
            "students": {
                "cgpa", "admission_year", "is_active", "is_archived",
                "login_enabled", "archived_at", "archive_reason",
                "session_version", "profile_picture", "ats_score",
            },
            "admins": {"is_active", "session_version"},
        }

        for table, expected in required_columns.items():
            actual = {
                row[1]
                for row in verify.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            missing_columns = expected - actual
            if missing_columns:
                raise RuntimeError(
                    f"{table} is missing required columns: "
                    f"{sorted(missing_columns)}"
                )

        # This initializer must leave all operational/account tables empty.
        must_be_empty = [
            "students",
            "admins",
            "companies",
            "applications",
            "interview_rounds",
            "interview_results",
            "notifications",
            "application_feedback",
            "placement_restrictions",
            "placehub_accounts",
            "placehub_audit_logs",
        ]

        non_empty = {}
        for table in must_be_empty:
            count = verify.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count:
                non_empty[table] = count

        if non_empty:
            raise RuntimeError(
                "Expected a schema-only database, but found rows: "
                + str(non_empty)
            )

        integrity = verify.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")

    finally:
        verify.close()

    print("SUCCESS: all 11 tables and required columns are present.")
    print("SUCCESS: database is empty and ready for the Super Admin transfer.")
    print("No sample or hardcoded accounts were inserted.")


if __name__ == "__main__":
    main()
