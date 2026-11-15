
import sqlite3

db_path = "database/database.db"

connection = sqlite3.connect(db_path)

try:
    connection.execute("PRAGMA foreign_keys = ON")

    connection.execute("BEGIN")

    # Delete dependent records first
    connection.execute("DELETE FROM interview_results")
    connection.execute("DELETE FROM interview_rounds")
    connection.execute("DELETE FROM applications")
    connection.execute("DELETE FROM companies")

    connection.commit()
    print("Company data and related application/interview records cleared.")

    print("Remaining companies:",
          connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0])

    print("Student profiles preserved:",
          connection.execute("SELECT COUNT(*) FROM students").fetchone()[0])

    print("Faculty/admin accounts preserved:",
          connection.execute("SELECT COUNT(*) FROM admins").fetchone()[0])

except Exception:
    connection.rollback()
    print("Reset failed. Changes were rolled back.")
    raise

finally:
    connection.close()
