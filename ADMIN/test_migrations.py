
import sqlite3
import shutil
from pathlib import Path

import database as db
from ADMIN.migrations import run_placehub_migrations


# Project root is the current working directory.
ORIGINAL_DB = Path("database/database.db").resolve()
TEST_DB = Path("database/database_placehub_test.db").resolve()


def get_record_counts(connection):
    """Capture record counts from all existing tables."""

    tables = connection.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
    """).fetchall()

    return {
        row["name"]: connection.execute(
            f'SELECT COUNT(*) FROM "{row["name"]}"'
        ).fetchone()[0]
        for row in tables
    }


def test_migration():

    if not ORIGINAL_DB.exists():
        raise FileNotFoundError(
            f"Original database not found: {ORIGINAL_DB}"
        )

    if TEST_DB.exists():
        raise FileExistsError(
            f"Test database already exists: {TEST_DB}\n"
            "Rename or remove the old test copy before running again."
        )

    # 1. Copy the original database.
    shutil.copy2(ORIGINAL_DB, TEST_DB)

    print("\n1. Original database copied successfully.")

    # 2. Point the existing database connection to the test copy.
    db.DATABASE = str(TEST_DB)

    # 3. Capture original record counts.
    connection = db.get_connection()
    original_counts = get_record_counts(connection)
    connection.close()

    print("\n2. Original record counts captured.")

    # 4. Run migration on the test database.
    run_placehub_migrations()

    print("\n3. First migration completed.")

    # 5. Run migration again to test repeatability.
    run_placehub_migrations()

    print("\n4. Second migration completed.")

    # 6. Verify existing records.
    connection = db.get_connection()
    new_counts = get_record_counts(connection)

    preserved = True

    print("\n5. Existing record verification:")

    for table, count in original_counts.items():

        new_count = new_counts.get(table, 0)

        if count == new_count:
            print(f"PASS: {table} - {count} records preserved")
        else:
            preserved = False
            print(
                f"FAIL: {table} - "
                f"before={count}, after={new_count}"
            )

    # 7. Verify required new tables.
    required_tables = {
        "placehub_accounts",
        "placehub_audit_logs"
    }

    existing_tables = set(new_counts.keys())

    tables_ok = required_tables.issubset(existing_tables)

    print("\n6. New table verification:")
    print("PASS" if tables_ok else "FAIL")

    # 8. Verify database integrity.
    integrity = connection.execute(
        "PRAGMA integrity_check"
    ).fetchone()[0]

    print("\n7. Database integrity:", integrity)

    # 9. Verify foreign-key relationships.
    connection.execute("PRAGMA foreign_keys = ON")

    foreign_key_errors = connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()

    print(
        "\n8. Foreign-key check:",
        "PASS" if not foreign_key_errors else "FAIL"
    )

    connection.close()

    # 10. Final result.
    if (
        preserved
        and tables_ok
        and integrity == "ok"
        and not foreign_key_errors
    ):
        print("\nSUCCESS: PLACEHUB migration tests passed.")
        print(f"Test database: {TEST_DB}")

    else:
        raise RuntimeError(
            "Migration test failed. "
            "Do not use this migration on the original database."
        )


if __name__ == "__main__":
    test_migration()
