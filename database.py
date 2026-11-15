import os
import sqlite3

# Local development keeps using database/database.db.
# Render can override this with DATABASE_PATH=/var/data/database.db
DATABASE = os.getenv("DATABASE_PATH", "database/database.db")

# Ensure the database directory exists for both local and deployed paths.
os.makedirs(os.path.dirname(os.path.abspath(DATABASE)), exist_ok=True)


def get_connection():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def create_tables():

    connection = get_connection()
    cursor = connection.cursor()

    # ================= STUDENTS =================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS students(

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        register_number TEXT UNIQUE NOT NULL,

        name TEXT NOT NULL,

        email TEXT UNIQUE NOT NULL,

        department TEXT NOT NULL,

        year INTEGER NOT NULL,

        cgpa REAL NOT NULL DEFAULT 0,

        date_of_birth TEXT NOT NULL,

        password TEXT NOT NULL,

        resume_path TEXT

    )
    """)
    # ================= PLACEMENT RESTRICTIONS =================

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS placement_restrictions(

        restriction_id INTEGER PRIMARY KEY AUTOINCREMENT,

        student_id INTEGER NOT NULL,

        restriction_type TEXT NOT NULL,

        start_date TEXT,

        end_date TEXT,

        reason TEXT NOT NULL,

        created_by INTEGER,

        created_at TEXT DEFAULT CURRENT_TIMESTAMP,

        FOREIGN KEY(student_id)
            REFERENCES students(id),

        FOREIGN KEY(created_by)
            REFERENCES admins(id)
    )
    """)
    # ================= ADMINS =================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS admins(

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        tts_number TEXT UNIQUE NOT NULL,

        name TEXT NOT NULL,

        email TEXT UNIQUE NOT NULL,

        role TEXT NOT NULL,

        date_of_birth TEXT NOT NULL,

        password TEXT NOT NULL

    )
    """)

    # ================= COMPANIES =================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS companies(

        company_id INTEGER PRIMARY KEY AUTOINCREMENT,

        company_name TEXT NOT NULL,

        job_role TEXT NOT NULL,

        package REAL NOT NULL,

        location TEXT NOT NULL,

        cgpa_required REAL NOT NULL,

        skills_required TEXT NOT NULL,

        eligible_departments TEXT NOT NULL,

        description TEXT,

        application_deadline TEXT NOT NULL,

        status TEXT NOT NULL,

        eligible_batches TEXT

    )
    """)

    # ================= APPLICATIONS =================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS applications(

        application_id INTEGER PRIMARY KEY AUTOINCREMENT,

        student_id INTEGER NOT NULL,

        company_id INTEGER NOT NULL,

        applied_date TEXT NOT NULL,

        resume_score REAL,

        status TEXT NOT NULL,

        FOREIGN KEY(student_id) REFERENCES students(id),

        FOREIGN KEY(company_id) REFERENCES companies(company_id)

    )
    """)

    # ================= INTERVIEW ROUNDS =================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS interview_rounds(
        round_id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        round_name TEXT NOT NULL,
        round_order INTEGER NOT NULL,
        FOREIGN KEY(company_id) REFERENCES companies(company_id),
        UNIQUE(company_id, round_order)
    )
    """)

    # Different companies can have different numbers and names of rounds.
    # round_order records the order in which candidates must complete them.

    # ================= INTERVIEW RESULTS =================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS interview_results(
        result_id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        round_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('Pending', 'Passed', 'Failed')),
        remarks TEXT,
        updated_by TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(application_id) REFERENCES applications(application_id),
        FOREIGN KEY(round_id) REFERENCES interview_rounds(round_id),
        UNIQUE(application_id, round_id)
    )
    """)

    # ================= NOTIFICATIONS =================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS notifications (
        notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient_type TEXT NOT NULL
            CHECK (recipient_type IN ('student', 'admin')),
        recipient_id INTEGER NOT NULL,
        sender_admin_id INTEGER,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        notification_type TEXT NOT NULL DEFAULT 'general',
        related_url TEXT,
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (sender_admin_id) REFERENCES admins(id)
    )
    """)

    # ================= APPLICATION FEEDBACK =================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS application_feedback (
        feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL UNIQUE,
        passed_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        pending_count INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL,
        strengths TEXT NOT NULL DEFAULT '[]',
        improvement_areas TEXT NOT NULL DEFAULT '[]',
        recommendations TEXT NOT NULL DEFAULT '[]',
        generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(application_id) REFERENCES applications(application_id)
    )
    """)

    connection.commit()
    connection.close()


def add_cgpa_column():

    connection = get_connection()
    cursor = connection.cursor()

    columns = [
        row["name"]
        for row in cursor.execute(
            "PRAGMA table_info(students)"
        )
    ]

    if "cgpa" not in columns:

        cursor.execute("""
            ALTER TABLE students
            ADD COLUMN cgpa REAL NOT NULL DEFAULT 0
        """)

        connection.commit()

        print("CGPA column added successfully.")

    else:

        print("CGPA column already exists.")

    connection.close()


# def insert_sample_companies():

#     connection = get_connection()

#     cursor = connection.cursor()

#     companies = [

#         (
#             "TCS",
#             "Software Engineer",
#             7.2,
#             "Hyderabad",
#             7.0,
#             "Python, SQL, DSA",
#             "AI & DS,CSE,IT",
#             "Campus Placement Drive",
#             "2026-09-10",
#             "Open"
#         ),

#         (
#             "Infosys",
#             "System Engineer",
#             5.5,
#             "Bangalore",
#             6.5,
#             "Java, SQL",
#             "AI & DS,CSE",
#             "Hiring Freshers",
#             "2026-09-15",
#             "Open"
#         ),

#         (
#             "Accenture",
#             "Associate Software Engineer",
#             6.5,
#             "Chennai",
#             6.8,
#             "Python, Aptitude",
#             "AI & DS,CSE,IT,ECE",
#             "Campus Recruitment",
#             "2026-09-20",
#             "Open"
#         )

#     ]

#     cursor.executemany("""

#     INSERT OR IGNORE INTO companies
#     (
#         company_name,
#         job_role,
#         package,
#         location,
#         cgpa_required,
#         skills_required,
#         eligible_departments,
#         description,
#         application_deadline,
#         status
#     )

#     VALUES
#     (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

#     """, companies)

#     connection.commit()

#     connection.close()

#     print("Sample companies inserted successfully.")

if __name__ == "__main__":
    create_tables()
    add_cgpa_column()
    print("Database schema initialized successfully. No sample accounts were created.")
