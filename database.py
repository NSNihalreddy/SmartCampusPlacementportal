"""
PLACEHUB database connection layer.

Local development:
    Uses the existing SQLite file at DATABASE_PATH (default:
    database/database.db).

Render/Turso:
    Set DATABASE_BACKEND=turso, TURSO_DATABASE_URL, and TURSO_AUTH_TOKEN.

The public get_connection() interface is intentionally unchanged.
"""

import os
import sqlite3

DATABASE_BACKEND = os.getenv("DATABASE_BACKEND", "sqlite").strip().lower()
DATABASE = os.getenv("DATABASE_PATH", "database/database.db")


def _raise_sqlite_compatible_error(exc):
    """Translate Turso DB-API failures into sqlite3.Error for existing handlers."""
    if isinstance(exc, sqlite3.Error):
        raise exc
    raise sqlite3.DatabaseError(str(exc)) from exc

if DATABASE_BACKEND == "sqlite":
    os.makedirs(os.path.dirname(os.path.abspath(DATABASE)), exist_ok=True)


class CompatibleRow:
    """Supports both sqlite3.Row-style name and positional indexing."""

    __slots__ = ("_values", "_names", "_mapping")

    def __init__(self, names, values):
        self._names = tuple(names or ())
        self._values = tuple(values or ())
        self._mapping = dict(zip(self._names, self._values))

    def __getitem__(self, key):
        return self._mapping[key] if isinstance(key, str) else self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._names)

    def values(self):
        return list(self._values)

    def items(self):
        return list(zip(self._names, self._values))

    def get(self, key, default=None):
        return self._mapping.get(key, default)

    def __repr__(self):
        return repr(self._mapping)


class TursoCursor:
    """Cursor adapter that refreshes metadata after every statement."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = getattr(cursor, "lastrowid", None)
        self.rowcount = getattr(cursor, "rowcount", -1)
        self.description = getattr(cursor, "description", None)

    def _refresh_metadata(self):
        self.lastrowid = getattr(self._cursor, "lastrowid", None)
        self.rowcount = getattr(self._cursor, "rowcount", -1)
        self.description = getattr(self._cursor, "description", None)

    def execute(self, sql, parameters=()):
        try:
            self._cursor.execute(sql, parameters)
            self._refresh_metadata()
            return self
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def executemany(self, sql, seq_of_parameters):
        try:
            self._cursor.executemany(sql, seq_of_parameters)
            self._refresh_metadata()
            return self
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def executescript(self, sql_script):
        try:
            self._cursor.executescript(sql_script)
            self._refresh_metadata()
            return self
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def _convert(self, row):
        if row is None:
            return None
        description = self.description or getattr(self._cursor, "description", None)
        names = [column[0] for column in description] if description else []
        if isinstance(row, CompatibleRow):
            return row
        if hasattr(row, "keys") and not isinstance(row, (tuple, list)):
            names = list(row.keys())
            values = [row[name] for name in names]
        else:
            values = row
        return CompatibleRow(names, values)

    def fetchone(self):
        try:
            return self._convert(self._cursor.fetchone())
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def fetchall(self):
        try:
            return [self._convert(row) for row in self._cursor.fetchall()]
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def fetchmany(self, size=None):
        try:
            rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
            return [self._convert(row) for row in rows]
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def __iter__(self):
        for row in self._cursor:
            yield self._convert(row)

    def close(self):
        return self._cursor.close()

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class TursoConnection:
    """Connection adapter exposing the subset of sqlite3 used by PLACEHUB."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        try:
            cursor = self._connection.execute(sql, parameters)
            return TursoCursor(cursor)
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def executemany(self, sql, seq_of_parameters):
        try:
            cursor = self._connection.executemany(sql, seq_of_parameters)
            return TursoCursor(cursor)
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def executescript(self, sql_script):
        try:
            cursor = self._connection.executescript(sql_script)
            return TursoCursor(cursor)
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def cursor(self):
        try:
            return TursoCursor(self._connection.cursor())
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def commit(self):
        try:
            return self._connection.commit()
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def rollback(self):
        try:
            return self._connection.rollback()
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    def close(self):
        try:
            return self._connection.close()
        except Exception as exc:
            _raise_sqlite_compatible_error(exc)

    @property
    def in_transaction(self):
        return getattr(self._connection, "in_transaction", False)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def get_connection():
    """Return a SQLite or Turso connection with PLACEHUB-compatible rows."""
    if DATABASE_BACKEND == "sqlite":
        connection = sqlite3.connect(DATABASE)
        connection.row_factory = sqlite3.Row
        return connection

    if DATABASE_BACKEND == "turso":
        database_url = os.getenv("TURSO_DATABASE_URL")
        auth_token = os.getenv("TURSO_AUTH_TOKEN")
        if not database_url or not auth_token:
            raise RuntimeError(
                "Turso is selected but TURSO_DATABASE_URL or TURSO_AUTH_TOKEN is missing."
            )
        try:
            import turso_serverless
        except ImportError as exc:
            raise RuntimeError(
                "Turso backend requires turso_serverless. Install it with "
                "'pip install turso_serverless'."
            ) from exc
        return TursoConnection(
            turso_serverless.connect(database_url, auth_token=auth_token)
        )

    raise RuntimeError(
        f"Unsupported DATABASE_BACKEND={DATABASE_BACKEND!r}. Use 'sqlite' or 'turso'."
    )


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
