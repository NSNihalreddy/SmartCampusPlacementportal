
from database import get_connection


def verify_database():

    connection = get_connection()
    cursor = connection.cursor()

    try:
        print("\n========== PLACEHUB DATABASE VERIFICATION ==========\n")

        # 1. Check required tables
        tables = {
            row["name"]
            for row in cursor.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
            """).fetchall()
        }

        required_tables = {
            "admins",
            "students",
            "placehub_accounts",
            "placehub_audit_logs"
        }

        print("1. TABLE VERIFICATION")

        for table in sorted(required_tables):
            status = "PASS" if table in tables else "FAIL"
            print(f"{table}: {status}")

        # 2. Check account-management columns
        print("\n2. FACULTY COLUMNS")

        faculty_columns = {
            row["name"]
            for row in cursor.execute(
                "PRAGMA table_info(admins)"
            ).fetchall()
        }

        for column in ["is_active", "session_version"]:
            print(
                f"{column}: "
                f"{'PASS' if column in faculty_columns else 'FAIL'}"
            )

        print("\n3. STUDENT COLUMNS")

        student_columns = {
            row["name"]
            for row in cursor.execute(
                "PRAGMA table_info(students)"
            ).fetchall()
        }

        expected_student_columns = [
            "is_active",
            "is_archived",
            "login_enabled",
            "archived_at",
            "archive_reason",
            "session_version"
        ]

        for column in expected_student_columns:
            print(
                f"{column}: "
                f"{'PASS' if column in student_columns else 'FAIL'}"
            )

        # 3. Count existing users
        print("\n4. EXISTING ACCOUNT COUNTS")

        faculty_count = cursor.execute(
            "SELECT COUNT(*) FROM admins"
        ).fetchone()[0]

        student_count = cursor.execute(
            "SELECT COUNT(*) FROM students"
        ).fetchone()[0]

        print("Faculty accounts:", faculty_count)
        print("Student accounts:", student_count)

        # 4. Check Super Admin creation status
        super_admin_count = cursor.execute("""
            SELECT COUNT(*)
            FROM placehub_accounts
            WHERE role = 'SUPER_ADMIN'
        """).fetchone()[0]

        admin_count = cursor.execute("""
            SELECT COUNT(*)
            FROM placehub_accounts
            WHERE role = 'ADMIN'
        """).fetchone()[0]

        print("\n5. PLACEHUB ACCOUNTS")
        print("Super Admin accounts:", super_admin_count)
        print("Admin accounts:", admin_count)

        # 5. SQLite integrity
        integrity = cursor.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        print("\n6. DATABASE INTEGRITY:", integrity)

        # 6. Foreign key verification
        cursor.execute("PRAGMA foreign_keys = ON")

        foreign_key_errors = cursor.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

        print(
            "7. FOREIGN KEY CHECK:",
            "PASS" if not foreign_key_errors else "FAIL"
        )

        # 7. Final result
        all_tables_ok = required_tables.issubset(tables)

        all_columns_ok = (
            {"is_active", "session_version"}.issubset(faculty_columns)
            and set(expected_student_columns).issubset(student_columns)
        )

        if (
            all_tables_ok
            and all_columns_ok
            and integrity == "ok"
            and not foreign_key_errors
        ):
            print("\nSUCCESS: DATABASE VERIFICATION PASSED.")
        else:
            print("\nERROR: DATABASE VERIFICATION FAILED.")

    finally:
        connection.close()


if __name__ == "__main__":
    verify_database()
