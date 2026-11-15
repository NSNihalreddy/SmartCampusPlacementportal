"""
Run this once from your project root:
    python migrate_company_batches.py

This adds a nullable column only. Existing company records and applications
are not deleted or rewritten. NULL means the legacy company has no batch
restriction configured yet.
"""
from database import get_connection


def main():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("PRAGMA table_info(companies)")
    columns = {row[1] for row in cursor.fetchall()}

    if "eligible_batches" not in columns:
        cursor.execute(
            "ALTER TABLE companies ADD COLUMN eligible_batches TEXT"
        )
        connection.commit()
        print("eligible_batches column added successfully.")
    else:
        print("eligible_batches column already exists. No change needed.")

    connection.close()


if __name__ == "__main__":
    main()
