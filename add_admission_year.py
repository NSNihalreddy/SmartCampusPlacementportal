
import sqlite3
from pathlib import Path

# Database location
DATABASE = Path(__file__).resolve().parent / "database" / "database.db"


def add_admission_year_column():
    if not DATABASE.exists():
        print("ERROR: Database file not found.")
        return

    connection = sqlite3.connect(DATABASE)

    try:
        cursor = connection.cursor()

        # Check existing students table columns
        cursor.execute("PRAGMA table_info(students)")
        columns = [row[1] for row in cursor.fetchall()]

        if "admission_year" in columns:
            print("admission_year column already exists.")
        else:
            cursor.execute("""
                ALTER TABLE students
                ADD COLUMN admission_year INTEGER
            """)

            connection.commit()
            print("admission_year column added successfully.")

        # Verify the updated schema
        cursor.execute("PRAGMA table_info(students)")
        updated_columns = [row[1] for row in cursor.fetchall()]

        print("\nSTUDENTS TABLE COLUMNS:")
        for column in updated_columns:
            print("-", column)

        # Verify existing records were preserved
        cursor.execute("""
            SELECT register_number, name, year, admission_year
            FROM students
        """)

        print("\nEXISTING STUDENT RECORDS:")
        for student in cursor.fetchall():
            print(student)

    except sqlite3.Error as error:
        connection.rollback()
        print("Database migration failed:", error)

    finally:
        connection.close()


if __name__ == "__main__":
    add_admission_year_column()
