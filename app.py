from email.mime import application
import os

# Load environment variables before importing database.py, because that
# module reads DATABASE_PATH when it is first imported.
from dotenv import load_dotenv
load_dotenv()

import sqlite3
import uuid
import re
import json
from datetime import datetime, timezone, timedelta
from openpyxl import load_workbook
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from database import get_connection
from ADMIN import admin_bp
import ADMIN.routes
from pypdf import PdfReader
from sklearn.metrics.pairwise import cosine_similarity

# Load the transformer model only when semantic matching is requested.
# This prevents a PyTorch/Windows DLL restriction from stopping Flask startup.
_semantic_model = None
_semantic_model_load_attempted = False
_semantic_model_load_error = None


def get_semantic_model():
    global _semantic_model, _semantic_model_load_attempted, _semantic_model_load_error

    if _semantic_model_load_attempted:
        return _semantic_model

    _semantic_model_load_attempted = True
    try:
        from sentence_transformers import SentenceTransformer
        _semantic_model = SentenceTransformer("all-MiniLM-L6-v2")
    except Exception as exc:
        _semantic_model_load_error = exc
        app.logger.warning(
            "Transformer model unavailable; using TF-IDF fallback for text matching: %s",
            exc,
        )

    return _semantic_model


def calculate_text_similarity_fallback(text1, text2):
    """Approximate text similarity with TF-IDF when the transformer is unavailable."""
    from sklearn.feature_extraction.text import TfidfVectorizer

    if not text1 or not text2 or not text1.strip() or not text2.strip():
        return 0.0

    try:
        vectors = TfidfVectorizer(ngram_range=(1, 2), stop_words="english").fit_transform(
            [text1, text2]
        )
        return float(cosine_similarity(vectors[0:1], vectors[1:2])[0][0])
    except ValueError:
        # Empty vocabulary, e.g. when the texts contain only stop words.
        return 0.0
ALLOWED_PROFILE_EXTENSIONS = {"jpg", "jpeg", "png"}
def allowed_profile_file(filename):

    if "." not in filename:
        return False

    extension = filename.rsplit(".", 1)[1].lower()

    return extension in ALLOWED_PROFILE_EXTENSIONS
ALLOWED_RESUME_EXTENSIONS = {"pdf"}


def allowed_resume_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_RESUME_EXTENSIONS
    )
app = Flask(__name__)
# On Render, keep user uploads on the persistent disk.
# Locally, the existing static/uploads folder remains unchanged.
PERSISTENT_UPLOADS_DIR = os.getenv("UPLOADS_DIR")

if PERSISTENT_UPLOADS_DIR:
    persistent_uploads = os.path.abspath(PERSISTENT_UPLOADS_DIR)
    static_uploads = os.path.join(app.static_folder, "uploads")

    os.makedirs(persistent_uploads, exist_ok=True)

    if os.path.islink(static_uploads):
        if os.path.realpath(static_uploads) != os.path.realpath(persistent_uploads):
            raise RuntimeError(
                "static/uploads points to a different directory than UPLOADS_DIR."
            )
    else:
        if os.path.exists(static_uploads):
            if not os.path.isdir(static_uploads) or os.listdir(static_uploads):
                raise RuntimeError(
                    "static/uploads already contains files. Refusing to replace "
                    "it with the Render persistent-storage link."
                )
            os.rmdir(static_uploads)

        os.symlink(
            persistent_uploads,
            static_uploads,
            target_is_directory=True
        )

# Dynamic academic-year calculation. Academic year transitions on 2 June.
ACADEMIC_YEAR_START_MONTH = 6
ACADEMIC_YEAR_START_DAY = 2
COURSE_DURATION_YEARS = 4


def get_student_academic_details(admission_year, today=None):
    if today is None:
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()

    try:
        admission_year = int(admission_year)
    except (TypeError, ValueError):
        return {
            "current_year": None,
            "current_year_label": "Not assigned",
            "batch": "Not assigned",
            "is_graduated": False,
            "is_academic_year_started": False,
        }

    batch = f"{admission_year}-{admission_year + COURSE_DURATION_YEARS}"
    start_date = datetime(admission_year, 6, 2).date()

    if today < start_date:
        return {
            "current_year": None,
            "current_year_label": "Not started",
            "batch": batch,
            "is_graduated": False,
            "is_academic_year_started": False,
        }

    transition_reached = (today.month, today.day) >= (6, 2)
    year_number = today.year - admission_year + (1 if transition_reached else 0)

    if year_number > COURSE_DURATION_YEARS:
        label, numeric_year, graduated = "Completed", None, True
    elif year_number < 1:
        label, numeric_year, graduated = "Not started", None, False
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(year_number, "th")
        label, numeric_year, graduated = f"{year_number}{suffix} Year", year_number, False

    return {
        "current_year": numeric_year,
        "current_year_label": label,
        "batch": batch,
        "is_graduated": graduated,
        "is_academic_year_started": True,
    }


def validate_admission_year(value):
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("Admission Year must be a valid year.")

    current_year = datetime.now(timezone(timedelta(hours=5, minutes=30))).year
    if year < 2000 or year > current_year:
        raise ValueError(f"Admission Year must be between 2000 and {current_year}.")
    return year


def parse_eligible_batches(value):
    """Parse comma/semicolon/space-separated passing-out years."""
    if value is None:
        return []

    raw_parts = re.split(r"[,;\s]+", str(value).strip())
    years = []

    for part in raw_parts:
        if not part:
            continue

        if not re.fullmatch(r"20\d{2}", part):
            raise ValueError(
                "Enter passing-out years as four-digit years, e.g. 2027, 2028."
            )

        year = int(part)

        if year < 2000 or year > 2100:
            raise ValueError("Passing-out year must be between 2000 and 2100.")

        if year not in years:
            years.append(year)

    return sorted(years)


def get_company_batch_eligibility(company, student):
    """
    NULL/blank eligible_batches means a legacy company: do not change its
    existing eligibility behaviour until an administrator configures batches.
    """
    raw_batches = company["eligible_batches"] if "eligible_batches" in company.keys() else None
    batches = parse_eligible_batches(raw_batches) if raw_batches else []
    admission_year = student["admission_year"] if "admission_year" in student.keys() else None
    try:
        passing_out_year = int(admission_year) + COURSE_DURATION_YEARS
    except (TypeError, ValueError):
        passing_out_year = None

    configured = bool(batches)
    batch_eligible = (not configured) or (
        passing_out_year is not None and passing_out_year in batches
    )
    return {
        "eligible_batches": batches,
        "eligible_batches_label": ", ".join(map(str, batches)) if batches else "Not specified",
        "passing_out_year": passing_out_year,
        "batch_eligible": batch_eligible,
        "batch_restriction_configured": configured,
    }


app.secret_key = os.getenv("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is missing. Set it in your local .env file "
        "or in the deployment environment variables."
    )


app.register_blueprint(admin_bp)


@app.before_request
def enforce_student_account_lifecycle():
    """Apply due batch archives and revoke disabled/archived student sessions."""
    from datetime import date
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    connection = None
    try:
        connection = get_connection()
        # November 1 of each passing-out year is the archive cutoff.
        if (today.month, today.day) >= (11, 1):
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
                  AND CAST(admission_year AS INTEGER) + 4 <= ?
            """, (today.year,))
            connection.commit()

        student_id = session.get("student_id")
        if student_id:
            student = connection.execute("""
                SELECT is_active, is_archived, login_enabled
                FROM students WHERE id = ?
            """, (student_id,)).fetchone()
            if (student is None or not student["is_active"]
                    or student["is_archived"] or not student["login_enabled"]):
                session.clear()
                flash("Your student account is disabled or archived. Contact the placement office.", "warning")
                if request.endpoint not in ("student_login", "forgot_password"):
                    return redirect(url_for("student_login"))
    except Exception:
        app.logger.exception("Student account lifecycle check failed")
    finally:
        if connection is not None:
            connection.close()
@app.template_filter("ist_datetime")
def ist_datetime(value):
    """Format SQLite UTC timestamps in Indian Standard Time."""
    if not value:
        return ""
    try:
        raw_value = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw_value)
        # SQLite CURRENT_TIMESTAMP values are naive UTC timestamps.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime(
            "%d %b %Y, %I:%M %p IST"
        )
    except (TypeError, ValueError):
        # Preserve the original value rather than hiding an unexpected format.
        return str(value)


@app.context_processor
def inject_unread_notification_counts():
    """Make role-specific unread counts available to every sidebar template."""
    counts = {
        "student_unread_notifications": 0,
        "faculty_unread_notifications": 0,
    }
    try:
        connection = get_connection()
        cursor = connection.cursor()
        student_id = session.get("student_id")
        faculty_id = session.get("faculty_id")

        if student_id:
            cursor.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE recipient_type = 'student'
                   AND recipient_id = ? AND COALESCE(is_read, 0) = 0""",
                (student_id,),
            )
            counts["student_unread_notifications"] = cursor.fetchone()[0]

        if faculty_id:
            cursor.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE recipient_type = 'admin'
                   AND recipient_id = ? AND COALESCE(is_read, 0) = 0""",
                (faculty_id,),
            )
            counts["faculty_unread_notifications"] = cursor.fetchone()[0]

        connection.close()
    except Exception:
        # A missing notification table during initial setup must not break pages.
        pass
    return counts
from functools import wraps


# ==========================================
# FACULTY LOGIN REQUIRED
# ==========================================

def faculty_login_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        faculty_id = session.get("faculty_id")

        if not faculty_id:

            return redirect(
                url_for("faculty_login")
            )

        return function(*args, **kwargs)

    return wrapper
# ==========================================
# FACULTY ROLE BASED ACCESS
# ==========================================

def faculty_role_required(*allowed_roles):

    def decorator(function):

        @wraps(function)
        def wrapper(*args, **kwargs):

            faculty_id = session.get("faculty_id")
            faculty_role = session.get("faculty_role")

            # Not logged in
            if not faculty_id:

                return redirect(
                    url_for("faculty_login")
                )


            # Logged in but role not allowed
            if faculty_role not in allowed_roles:

                return """
                    <h2>Access Denied</h2>

                    <p>
                        You do not have permission
                        to access this page.
                    </p>

                    <a href="/faculty/dashboard">
                        Return to Faculty Dashboard
                    </a>
                """, 403


            return function(*args, **kwargs)

        return wrapper

    return decorator
def create_notification(cursor, recipient_type, recipient_id, title, message,
                        notification_type="general", related_url=None, sender_admin_id=None):
    """Insert one persistent notification for a validated recipient."""
    cursor.execute(
        """INSERT INTO notifications
           (recipient_type, recipient_id, sender_admin_id, title, message, notification_type, related_url)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (recipient_type, recipient_id, sender_admin_id, title, message, notification_type, related_url)
    )


def extract_resume_text(pdf_path):

    text = ""

    reader = PdfReader(pdf_path)

    for page in reader.pages:

        page_text = page.extract_text()

        if page_text:
            text += page_text + "\n"

    return text
def calculate_ats_score(resume_text):

    text = resume_text.lower()

    scores = {}

    # ==========================================
    # 1. ATS PARSEABILITY & FILE CONTENT - 15
    # ==========================================

    parse_score = 0

    if len(text.strip()) > 100:
        parse_score += 10

    if len(text.split()) >= 150:
        parse_score += 5

    scores["ATS Parseability"] = min(parse_score, 15)


    # ==========================================
    # 2. RESUME STRUCTURE - 15
    # ==========================================

    structure_sections = [
        "professional summary",
        "summary",
        "education",
        "technical skills",
        "skills",
        "experience",
        "internship",
        "projects",
        "certifications"
    ]

    found_sections = sum(
        1
        for section in structure_sections
        if section in text
    )

    structure_score = min(
        round((found_sections / 7) * 15),
        15
    )

    scores["Resume Structure"] = structure_score


    # ==========================================
    # 3. CONTACT INFORMATION - 10
    # ==========================================

    contact_score = 0

    if "@" in text:
        contact_score += 5

    phone_digits = sum(
        1 for char in text
        if char.isdigit()
    )

    if phone_digits >= 10:
        contact_score += 5

    scores["Contact Information"] = min(
        contact_score,
        10
    )


    # ==========================================
    # 4. TECHNICAL SKILLS - 15
    # ==========================================

    technical_skills = [
        "python",
        "java",
        "c",
        "c++",
        "sql",
        "html",
        "css",
        "javascript",
        "flask",
        "django",
        "react",
        "machine learning",
        "deep learning",
        "pandas",
        "numpy",
        "scikit-learn",
        "tensorflow",
        "pytorch",
        "git",
        "github",
        "mysql",
        "sqlite",
        "mongodb"
    ]

    matched_skills = sum(
        1
        for skill in technical_skills
        if skill in text
    )

    skill_score = min(
        round((matched_skills / 10) * 15),
        15
    )

    scores["Technical Skills"] = skill_score


    # ==========================================
    # 5. EXPERIENCE / INTERNSHIP - 10
    # ==========================================

    experience_keywords = [
        "internship",
        "intern",
        "experience",
        "worked",
        "developed",
        "implemented"
    ]

    experience_matches = sum(
        1
        for word in experience_keywords
        if word in text
    )

    experience_score = min(
        round((experience_matches / 3) * 10),
        10
    )

    scores["Experience"] = experience_score


    # ==========================================
    # 6. PROJECTS - 10
    # ==========================================

    project_score = 0

    if "project" in text or "projects" in text:
        project_score += 5

    project_action_words = [
        "developed",
        "implemented",
        "designed",
        "created",
        "built",
        "automated"
    ]

    action_count = sum(
        1
        for word in project_action_words
        if word in text
    )

    project_score += min(
        action_count,
        5
    )

    scores["Projects"] = min(
        project_score,
        10
    )


    # ==========================================
    # 7. EDUCATION - 10
    # ==========================================

    education_keywords = [
        "education",
        "b.tech",
        "btech",
        "bachelor",
        "university",
        "college",
        "degree",
        "cgpa"
    ]

    education_matches = sum(
        1
        for word in education_keywords
        if word in text
    )

    education_score = min(
        round((education_matches / 4) * 10),
        10
    )

    scores["Education"] = education_score


    # ==========================================
    # 8. CERTIFICATIONS - 5
    # ==========================================

    certification_score = 0

    if "certification" in text or "certifications" in text:
        certification_score = 5

    scores["Certifications"] = certification_score


    # ==========================================
    # 9. CONTENT QUALITY - 5
    # ==========================================

    action_words = [
        "developed",
        "implemented",
        "designed",
        "created",
        "built",
        "automated",
        "trained",
        "analyzed",
        "managed"
    ]

    action_count = sum(
        1
        for word in action_words
        if word in text
    )

    content_score = min(
        round((action_count / 5) * 5),
        5
    )

    scores["Content Quality"] = content_score


    # ==========================================
    # 10. LENGTH & CONTENT DENSITY - 5
    # ==========================================

    word_count = len(text.split())

    if 300 <= word_count <= 900:
        length_score = 5

    elif 200 <= word_count < 300:
        length_score = 3

    elif 900 < word_count <= 1200:
        length_score = 3

    else:
        length_score = 1

    scores["Length & Density"] = length_score


    # ==========================================
    # FINAL SCORE
    # ==========================================

    total_score = sum(scores.values())

    return total_score, scores

def check_job_eligibility(student, company, resume_match_score):
    # 1. Department check
    student_department = (
        student["department"] or ""
    ).strip().lower()

    eligible_departments = (
        company["eligible_departments"] or ""
    )

    departments = [
        department.strip().lower()
        for department in eligible_departments.split(",")
        if department.strip()
    ]

    # Normalize common department naming variations
    student_department_normalized = (
        student_department
        .replace("b.tech", "")
        .replace("btech", "")
        .replace(" ", "")
        .replace("(", "")
        .replace(")", "")
    )

    department_eligible = any(
        student_department_normalized in
        department.replace("b.tech", "")
                  .replace("btech", "")
                  .replace(" ", "")
                  .replace("(", "")
                  .replace(")", "")
        or
        department.replace("b.tech", "")
                  .replace("btech", "")
                  .replace(" ", "")
                  .replace("(", "")
                  .replace(")", "")
        in student_department_normalized
        for department in departments
    )

    # 2. CGPA check
    try:
        student_cgpa = float(student["cgpa"] or 0)
        required_cgpa = float(company["cgpa_required"] or 0)
        cgpa_eligible = student_cgpa >= required_cgpa
    except (TypeError, ValueError):
        cgpa_eligible = False

    # 3. Resume match check: minimum 40%
    resume_eligible = float(resume_match_score or 0) >= 40

    # 4. All three conditions must pass
    return (
        department_eligible
        and cgpa_eligible
        and resume_eligible
    )

def calculate_semantic_similarity(resume_text, job_text):
    semantic_model = get_semantic_model()

    if semantic_model is not None:
        resume_embedding = semantic_model.encode([resume_text])
        job_embedding = semantic_model.encode([job_text])
        similarity = cosine_similarity(
            resume_embedding,
            job_embedding
        )[0][0]
    else:
        similarity = calculate_text_similarity_fallback(resume_text, job_text)

    return round(float(similarity) * 100, 2)
# ==========================================
# SKILL ALIASES
# ==========================================

skill_aliases = {

    "ml": [
        "ml",
        "machine learning"
    ],

    "machine learning": [
        "ml",
        "machine learning"
    ],

    "ai": [
        "ai",
        "artificial intelligence"
    ],

    "artificial intelligence": [
        "ai",
        "artificial intelligence"
    ],

    "dsa": [
        "dsa",
        "data structures",
        "data structures and algorithms"
    ],

    "dbms": [
        "dbms",
        "database management system"
    ],

    "js": [
        "js",
        "javascript"
    ],

    "javascript": [
        "js",
        "javascript"
    ],

    "react": [
        "react",
        "reactjs",
        "react.js"
    ],

    "node": [
        "node",
        "nodejs",
        "node.js"
    ],

    "sql": [
        "sql",
        "mysql",
        "postgresql",
        "postgres"
    ]
}


# ==========================================
# CHECK WHETHER A SKILL EXISTS
# ==========================================

def skill_exists(resume_text, skill):

    resume_text = resume_text.lower()

    skill = skill.strip().lower()

    variations = skill_aliases.get(
        skill,
        [skill]
    )

    for variation in variations:

        if variation in resume_text:
            return True

    return False


# ==========================================
# REQUIRED SKILL SCORE
# ==========================================

def calculate_skill_match_score(resume_text, company):

    required_skills = company["skills_required"]

    skills = [
        skill.strip()
        for skill in required_skills.split(",")
        if skill.strip()
    ]

    if not skills:
        return 0

    matched_skills = 0

    for skill in skills:

        if skill_exists(
            resume_text,
            skill
        ):
            matched_skills += 1

    score = (
        matched_skills / len(skills)
    ) * 100

    return round(score, 2)


# ==========================================
# SEMANTIC SCORE
# ==========================================

def get_semantic_score(text1, text2):
    if not text1 or not text2 or not text1.strip() or not text2.strip():
        return 0.0

    semantic_model = get_semantic_model()

    if semantic_model is not None:
        embedding1 = semantic_model.encode([text1])
        embedding2 = semantic_model.encode([text2])
        similarity = cosine_similarity(
            embedding1,
            embedding2
        )[0][0]
        return float(similarity)

    # Fallback keeps resume matching operational without PyTorch.
    return calculate_text_similarity_fallback(text1, text2)


# ==========================================
# EXPERIENCE / PROJECT TEXT
# ==========================================

def get_experience_project_text(resume_text):

    resume_lower = resume_text.lower()

    start_positions = []

    for section in [
        "internship",
        "experience",
        "projects"
    ]:

        position = resume_lower.find(
            section
        )

        if position != -1:
            start_positions.append(position)

    if not start_positions:
        return ""

    start = min(start_positions)

    end_positions = []

    for section in [
        "certifications",
        "education",
        "languages"
    ]:

        position = resume_lower.find(
            section,
            start + 10
        )

        if position != -1:
            end_positions.append(position)

    if end_positions:
        end = min(end_positions)
    else:
        end = len(resume_text)

    return resume_text[
        start:end
    ].strip()


# ==========================================
# FINAL RESUME MATCH SCORE
# ==========================================

def calculate_resume_match_score(
    resume_text,
    company
):

    # --------------------------------------
    # 1. REQUIRED SKILLS - 50%
    # --------------------------------------

    skill_score = calculate_skill_match_score(
        resume_text,
        company
    )


    # --------------------------------------
    # 2. JOB ROLE - 25%
    # --------------------------------------

    job_role = company["job_role"]

    role_similarity = get_semantic_score(
        resume_text,
        job_role
    )


    # Convert semantic similarity
    # into a practical score

    role_score = min(
        max(
            (role_similarity - 0.20) / 0.40 * 100,
            0
        ),
        100
    )


    # --------------------------------------
    # 3. EXPERIENCE / PROJECTS - 25%
    # --------------------------------------

    experience_text = get_experience_project_text(
        resume_text
    )

    company_requirements = (
        company["job_role"]
        + " "
        + company["skills_required"]
    )

    experience_similarity = get_semantic_score(
        experience_text,
        company_requirements
    )


    experience_score = min(
        max(
            (experience_similarity - 0.20) / 0.40 * 100,
            0
        ),
        100
    )


    # --------------------------------------
    # FINAL SCORE
    # --------------------------------------

    final_score = (
        (skill_score * 0.50)
        +
        (role_score * 0.25)
        +
        (experience_score * 0.25)
    )


    final_score = min(
        max(final_score, 0),
        100
    )


    return round(
        final_score,
        2
    )


# ==========================================
# MATCH LEVEL
# ==========================================

def get_match_level(score):

    if score < 40:
        return "Low chance to get shortlisted"

    elif score < 70:
        return "Moderate chance to get shortlisted"

    else:
        return "High chance to get shortlisted"

def create_candidate_profile(resume_text):

    resume_lower = resume_text.lower()

    section_positions = []

    sections = [
        "professional summary",
        "technical skills",
        "internship",
        "projects"
    ]

    end_sections = [
        "education",
        "certifications",
        "languages"
    ]

    # Find the first occurrence of each section heading
    for section in sections:

        start = resume_lower.find(section)

        if start != -1:

            section_positions.append(
                (section, start)
            )

    # Sort sections according to their position
    section_positions.sort(
        key=lambda x: x[1]
    )

    extracted_sections = []

    for index, (section, start) in enumerate(
        section_positions
    ):

        end = len(resume_text)

        # Next major section
        if index + 1 < len(section_positions):

            next_start = section_positions[
                index + 1
            ][1]

            end = next_start

        # Known ending sections
        for end_section in end_sections:

            end_position = resume_lower.find(
                end_section,
                start + len(section)
            )

            if (
                end_position != -1
                and end_position < end
            ):

                end = end_position

        section_text = resume_text[
            start:end
        ].strip()

        extracted_sections.append(
            section_text
        )

    return "\n\n".join(
        extracted_sections
    )
@app.route("/")
def home():
    return render_template("index.html")
@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/student/login", methods=["GET", "POST"])
def student_login():
    if request.method == "POST":
        register_number = request.form.get("register_number", "").strip()
        password = request.form.get("password", "")

        connection = get_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("""
                SELECT *
                FROM students
                WHERE UPPER(TRIM(register_number)) = UPPER(TRIM(?))
            """, (register_number,))

            student = cursor.fetchone()

            if student:
                if (not student["is_active"] or student["is_archived"]
                        or not student["login_enabled"]):
                    flash("This student account is disabled or archived. Contact the placement office.", "warning")
                    return redirect(url_for("student_login"))

                stored_password = student["password"]

                # Support hashed and legacy plaintext passwords
                password_valid = check_password_hash(
                    stored_password, password
                ) or stored_password == password

                if password_valid:
                    # Upgrade legacy plaintext password
                    if stored_password == password:
                        cursor.execute("""
                            UPDATE students
                            SET password = ?
                            WHERE id = ?
                        """, (
                            generate_password_hash(password),
                            student["id"]
                        ))
                        connection.commit()

                    session.clear()
                    session["student_id"] = student["id"]

                    return redirect(url_for("student_dashboard"))

            flash(
                "Invalid Register Number or Password.",
                "danger"
            )
            return redirect(url_for("student_login"))

        finally:
            connection.close()

    return render_template("auth/student_login.html")


@app.route("/<role>/forgot-password", methods=["GET", "POST"])
def forgot_password(role):
    from datetime import datetime

    if role not in ("student", "faculty"):
        return redirect(url_for("home"))

    table = "students" if role == "student" else "admins"
    id_column = "register_number" if role == "student" else "tts_number"
    verified_key = f"password_reset_{role}_id"

    def normalize_dob(value):
        """Convert supported date formats into a date object."""
        if not value:
            return None

        value = str(value).strip()

        for date_format in (
            "%Y-%m-%d",
            "%d-%m-%Y",
            "%d/%m/%Y",
            "%d%m%Y",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(value, date_format).date()
            except ValueError:
                continue

        return None

    if request.method == "GET":
        session.pop(verified_key, None)

    verified_id = session.get(verified_key)

    if request.method == "POST":
        action = request.form.get("action", "verify")

        # STEP 1: Verify register/TTS number and date of birth
        if action == "verify":
            identifier = request.form.get("identifier", "").strip()
            dob_input = request.form.get("dob", "").strip()

            if not identifier or not dob_input:
                flash(
                    "Enter your register/TTS number and date of birth.",
                    "error",
                )
                return render_template(
                    "forgot_password.html",
                    role=role,
                    verified=False,
                )

            entered_dob = normalize_dob(dob_input)

            if entered_dob is None:
                flash("Enter a valid date of birth.", "error")
                return render_template(
                    "forgot_password.html",
                    role=role,
                    verified=False,
                )

            connection = get_connection()
            try:
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    SELECT id, date_of_birth
                    FROM {table}
                    WHERE UPPER(TRIM({id_column})) = UPPER(TRIM(?))
                    """,
                    (identifier,),
                )
                account = cursor.fetchone()
            finally:
                connection.close()

            stored_dob = (
                normalize_dob(account["date_of_birth"])
                if account else None
            )

            if (
                not account
                or stored_dob is None
                or entered_dob != stored_dob
            ):
                flash(
                    "The register/TTS number or date of birth "
                    "does not match our records.",
                    "error",
                )
                return render_template(
                    "forgot_password.html",
                    role=role,
                    verified=False,
                )

            session[verified_key] = account["id"]

            return render_template(
                "forgot_password.html",
                role=role,
                verified=True,
            )

        # STEP 2: Set the new password after verification
        elif action == "reset":
            verified_id = session.get(verified_key)

            if not verified_id:
                flash(
                    "Verify your details before resetting your password.",
                    "error",
                )
                return redirect(
                    url_for("forgot_password", role=role)
                )

            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if len(new_password) < 8:
                flash(
                    "Password must be at least 8 characters long.",
                    "error",
                )
                return render_template(
                    "forgot_password.html",
                    role=role,
                    verified=True,
                )

            if new_password != confirm_password:
                flash("The passwords do not match.", "error")
                return render_template(
                    "forgot_password.html",
                    role=role,
                    verified=True,
                )

            connection = get_connection()
            try:
                cursor = connection.cursor()
                cursor.execute(
                    f"UPDATE {table} SET password = ? WHERE id = ?",
                    (new_password, verified_id),
                )
                connection.commit()

                if cursor.rowcount != 1:
                    flash(
                        "Password could not be updated. Try again.",
                        "error",
                    )
                    return render_template(
                        "forgot_password.html",
                        role=role,
                        verified=True,
                    )
            finally:
                connection.close()

            session.pop(verified_key, None)
            flash(
                "Password updated successfully. Please log in.",
                "success",
            )

            login_endpoint = (
                "student_login"
                if role == "student"
                else "faculty_login"
            )
            return redirect(url_for(login_endpoint))

    return render_template(
        "forgot_password.html",
        role=role,
        verified=bool(verified_id),
    )

@app.route("/student/change-password", methods=["POST"])
def student_change_password():

    student_id = session.get("student_id")

    if not student_id:
        return redirect(url_for("student_login"))

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password or not new_password or not confirm_password:
        flash("Please fill in all password fields.", "danger")
        return redirect(url_for("student_profile"))

    if new_password != confirm_password:
        flash("New password and confirmation do not match.", "danger")
        return redirect(url_for("student_profile"))

    if len(new_password) < 8:
        flash("New password must contain at least 8 characters.", "danger")
        return redirect(url_for("student_profile"))

    if new_password == current_password:
        flash("Choose a password different from your current password.", "danger")
        return redirect(url_for("student_profile"))

    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("""
            SELECT password
            FROM students
            WHERE id = ?
        """, (student_id,))

        student = cursor.fetchone()

        if student is None:
            session.clear()
            flash("Student account not found. Please log in again.", "danger")
            return redirect(url_for("student_login"))

        stored_password = student["password"]

        # Verify either a legacy plaintext or a hashed password
        password_valid = (
            check_password_hash(stored_password, current_password)
            or stored_password == current_password
        )

        if not password_valid:
            flash("Your current password is incorrect.", "danger")
            return redirect(url_for("student_profile"))

        hashed_password = generate_password_hash(new_password)

        cursor.execute("""
            UPDATE students
            SET password = ?
            WHERE id = ?
        """, (hashed_password, student_id))

        connection.commit()

        flash("Password changed successfully.", "success")
        return redirect(url_for("student_profile"))

    except Exception:
        connection.rollback()
        app.logger.exception("Student password change failed")
        flash("Unable to change password. Please try again.", "danger")
        return redirect(url_for("student_profile"))

    finally:
        connection.close()


@app.route("/faculty/login", methods=["GET", "POST"])
def faculty_login():
    if request.method == "POST":
        tts_number = request.form.get("tts_number", "").strip()
        password = request.form.get("password", "")

        connection = get_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("""
                SELECT *
                FROM admins
                WHERE tts_number = ?
                  AND password = ?
            """, (tts_number, password))

            faculty = cursor.fetchone()
        finally:
            connection.close()

        if faculty:
            # Clear any previous student/faculty session
            session.clear()

            # Store faculty information
            session["faculty_id"] = faculty["id"]
            session["faculty_role"] = faculty["role"]

            return redirect(url_for("faculty_dashboard"))

        flash("Invalid Faculty ID or Password", "error")

    # Use the same template path for GET and failed login
    return render_template("auth/faculty_login.html")

@app.route("/faculty/dashboard")
@faculty_login_required
def faculty_dashboard():

    faculty_id = session.get("faculty_id")

    connection = get_connection()
    cursor = connection.cursor()


    # ==========================================
    # FACULTY DETAILS
    # ==========================================

    cursor.execute("""
        SELECT *
        FROM admins
        WHERE id = ?
    """, (faculty_id,))

    faculty = cursor.fetchone()


    if faculty is None:

        connection.close()

        session.clear()

        return redirect(
            url_for("faculty_login")
        )


    # ==========================================
    # TOTAL STUDENTS
    # ==========================================

    cursor.execute("""
        SELECT COUNT(*)
        FROM students
    """)

    total_students = cursor.fetchone()[0]


    # ==========================================
    # ACTIVE PLACEMENT DRIVES
    # ==========================================

    cursor.execute("""
        SELECT COUNT(*)
        FROM companies
        WHERE status = 'Open'
        AND application_deadline >= DATE('now')
    """)

    active_drives = cursor.fetchone()[0]


    # ==========================================
    # TOTAL APPLICATIONS
    # ==========================================

    cursor.execute("""
        SELECT COUNT(*)
        FROM applications
    """)

    total_applications = cursor.fetchone()[0]


    # ==========================================
    # SELECTED STUDENTS
    # ==========================================

    cursor.execute("""
        SELECT COUNT(*)
        FROM applications
        WHERE status = 'Selected'
    """)

    selected_students = cursor.fetchone()[0]


    # ==========================================
    # UPCOMING DRIVES
    # ==========================================

    cursor.execute("""
        SELECT COUNT(*)
        FROM companies
        WHERE application_deadline >= DATE('now')
    """)

    upcoming_drives = cursor.fetchone()[0]


    # ==========================================
    # RECENT PLACEMENT DRIVES
    # ==========================================

    cursor.execute("""
    SELECT
        company_id,
        company_name,
        job_role,
        package,
        location,
        application_deadline,

        CASE
            WHEN application_deadline < DATE('now')
            THEN 'Closed'
            ELSE status
        END AS status

    FROM companies

    ORDER BY company_id DESC

    LIMIT 5
""")

    companies = cursor.fetchall()


    connection.close()


    return render_template(
        "faculty_dashboard.html",

        faculty=faculty,

        total_students=total_students,

        active_drives=active_drives,

        total_applications=total_applications,

        selected_students=selected_students,

        upcoming_drives=upcoming_drives,

        companies=companies
    )
# ==========================================
# LIVE PLACEMENT DRIVES API
# ==========================================

@app.route("/api/faculty/placement-drives")
@faculty_login_required
def live_placement_drives():

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
    SELECT
        company_id,
        company_name,
        job_role,
        package,
        location,
        application_deadline,

        CASE
            WHEN application_deadline < DATE('now')
            THEN 'Closed'
            ELSE status
        END AS status

    FROM companies

    ORDER BY company_id DESC

    LIMIT 5
""")

    companies = cursor.fetchall()

    connection.close()

    return jsonify([
        {
            "company_id": company["company_id"],
            "company_name": company["company_name"],
            "job_role": company["job_role"],
            "package": company["package"],
            "location": company["location"],
            "application_deadline": company["application_deadline"],
            "status": company["status"]
        }
        for company in companies
    ])
@app.route("/company/<int:company_id>")
def company_details(company_id):

    student_id = session.get("student_id")

    if not student_id:
        return redirect(url_for("student_login"))

    connection = get_connection()
    cursor = connection.cursor()

    # ==========================================
    # GET COMPANY
    # ==========================================

    cursor.execute("""
        SELECT *
        FROM companies
        WHERE company_id = ?
    """, (company_id,))

    company = cursor.fetchone()

    if company is None:

        connection.close()

        return "Company not found", 404


    # ==========================================
    # GET STUDENT
    # ==========================================

    cursor.execute("""
        SELECT *
        FROM students
        WHERE id = ?
    """, (student_id,))

    student = cursor.fetchone()

    connection.close()

    if student is None:

        return "Student not found", 404


    # ==========================================
    # RESUME MATCH SCORE
    # ==========================================

    resume_score = 0

    if student["resume_path"]:

        resume_path = os.path.join(
            app.static_folder,
            student["resume_path"]
        )

        if os.path.exists(resume_path):

            resume_text = extract_resume_text(
                resume_path
            )

            resume_score = calculate_resume_match_score(
                resume_text,
                company
            )


    # ==========================================
    # MATCH LEVEL
    # ==========================================

    match_level = get_match_level(
        resume_score
    )


    # ==========================================
    # FINAL ELIGIBILITY
    # ==========================================

    eligible_to_apply = check_job_eligibility(
        student,
        company,
        resume_score
    )
    batch_details = get_company_batch_eligibility(company, student)
    eligible_to_apply = eligible_to_apply and batch_details["batch_eligible"]


    # ==========================================
    # SHOW COMPANY DETAILS
    # ==========================================

    return render_template(
        "company_details.html",

        company=company,

        resume_score=resume_score,

        match_level=match_level,

        eligible_to_apply=eligible_to_apply,
        **batch_details
    )


# ==========================================
# APPLY FOR COMPANY
# ==========================================

@app.route("/apply/<int:company_id>")
def apply_company(company_id):

    student_id = session.get("student_id")

    if not student_id:
        return redirect(url_for("student_login"))


    # ==========================================
    # DATABASE CONNECTION
    # ==========================================

    connection = get_connection()
    cursor = connection.cursor()


    # ==========================================
    # CHECK PLACEMENT RESTRICTION
    # ==========================================

    cursor.execute("""
        SELECT
            restriction_type,
            start_date,
            end_date,
            reason
        FROM placement_restrictions
        WHERE student_id = ?
        ORDER BY restriction_id DESC
        LIMIT 1
    """, (student_id,))

    restriction = cursor.fetchone()


    # ==========================================
    # PERMANENT BLOCK
    # ==========================================

    if restriction and restriction["restriction_type"] == "Permanent":

        reason = restriction["reason"]

        connection.close()

        return jsonify({
            "success": False,
            "restricted": True,
            "restriction_type": "Permanent",
            "message": (
                "You are currently blocked from applying for "
                "placement opportunities. Please meet the "
                "Placement Department to know the details."
            ),
            "reason": reason
        })


    # ==========================================
    # TEMPORARY BLOCK
    # ==========================================

    if restriction and restriction["restriction_type"] == "Temporary":

        cursor.execute("""
            SELECT restriction_id
            FROM placement_restrictions
            WHERE student_id = ?
              AND restriction_type = 'Temporary'
              AND DATE('now') BETWEEN start_date AND end_date
            LIMIT 1
        """, (student_id,))

        active_restriction = cursor.fetchone()


        if active_restriction:

            start_date = restriction["start_date"]
            end_date = restriction["end_date"]
            reason = restriction["reason"]

            connection.close()

            return jsonify({
                "success": False,
                "restricted": True,
                "restriction_type": "Temporary",
                "start_date": start_date,
                "end_date": end_date,
                "message": (
                    "You are currently blocked from applying for "
                    "placement opportunities. Please meet the "
                    "Placement Department to know the details."
                ),
                "reason": reason
            })


    # ==========================================
    # GET STUDENT
    # ==========================================

    cursor.execute("""
        SELECT *
        FROM students
        WHERE id = ?
    """, (student_id,))

    student = cursor.fetchone()


    # ==========================================
    # GET COMPANY
    # ==========================================

    cursor.execute("""
        SELECT *
        FROM companies
        WHERE company_id = ?
    """, (company_id,))

    company = cursor.fetchone()


    if student is None or company is None:

        connection.close()

        return jsonify({
            "success": False,
            "message": "Student or company not found."
        }), 404

    batch_details = get_company_batch_eligibility(company, student)
    if not batch_details["batch_eligible"]:
        connection.close()
        return jsonify({
            "success": False,
            "eligible": False,
            "batch_ineligible": True,
            "message": (
                "This placement is open only to passing-out batches: "
                + batch_details["eligible_batches_label"]
                + ". Your passing-out batch is "
                + (str(batch_details["passing_out_year"]) if batch_details["passing_out_year"] else "not assigned")
                + "."
            )
        })


    # ==========================================
    # CALCULATE RESUME SCORE AGAIN
    # ==========================================

    resume_score = 0

    if student["resume_path"]:

        resume_path = os.path.join(
            app.static_folder,
            student["resume_path"]
        )

        if os.path.exists(resume_path):

            resume_text = extract_resume_text(
                resume_path
            )

            resume_score = calculate_resume_match_score(
                resume_text,
                company
            )


    # ==========================================
    # CHECK ELIGIBILITY AGAIN
    # ==========================================

    eligible_to_apply = check_job_eligibility(
        student,
        company,
        resume_score
    )


    if not eligible_to_apply:

        connection.close()

        return jsonify({
            "success": False,
            "eligible": False,
            "message": "You are not eligible to apply for this placement."
        })


    # ==========================================
    # CHECK DUPLICATE APPLICATION
    # ==========================================

    cursor.execute("""
        SELECT *
        FROM applications
        WHERE student_id = ?
        AND company_id = ?
    """, (
        student_id,
        company_id
    ))

    application = cursor.fetchone()


    if application:

        connection.close()

        return jsonify({
            "success": False,
            "already_applied": True,
            "message": "You have already applied for this placement."
        })


    # ==========================================
    # SAVE APPLICATION
    # ==========================================

    cursor.execute("""
        INSERT INTO applications
        (
            student_id,
            company_id,
            applied_date,
            resume_score,
            status
        )
        VALUES
        (?, ?, DATE('now'), ?, ?)
    """, (
        student_id,
        company_id,
        resume_score,
        "Resume Screening"
    ))


    create_notification(
        cursor, "student", student_id, "Application submitted",
        f"Your application for {company['company_name']} has been submitted successfully.",
        "application", "/student/applications"
    )
    connection.commit()
    connection.close()


    # ==========================================
    # SUCCESS
    # ==========================================

    return jsonify({
        "success": True,
        "already_applied": False,
        "message": "Application submitted successfully."
    })

@app.route("/student/dashboard")
def student_dashboard():

    student_id = session.get("student_id")

    if not student_id:
        return redirect(url_for("student_login"))

    connection = get_connection()
    cursor = connection.cursor()

    # ==========================================
    # STUDENT DETAILS
    # ==========================================

    cursor.execute(
        "SELECT * FROM students WHERE id = ?",
        (student_id,)
    )

    student = cursor.fetchone()


    # ==========================================
    # APPLIED JOBS
    # ==========================================

    cursor.execute("""
        SELECT COUNT(*)
        FROM applications
        WHERE student_id = ?
    """, (student_id,))

    applied_jobs = cursor.fetchone()[0]


    # ==========================================
    # UPCOMING DRIVES
    # ==========================================

    cursor.execute("""
        SELECT COUNT(*)
        FROM companies
        WHERE application_deadline >= DATE('now')
    """)

    upcoming_drives = cursor.fetchone()[0]


    # ==========================================
    # AVAILABLE PLACEMENT DRIVES
    # ==========================================

    cursor.execute("""
        SELECT *
        FROM companies
        WHERE application_deadline >= DATE('now')
        ORDER BY application_deadline ASC
    """)

    companies = cursor.fetchall()


    connection.close()


    academic = get_student_academic_details(student["admission_year"])

    return render_template(
        "student_dashboard.html",
        student=student,
        companies=companies,
        applied_jobs=applied_jobs,
        upcoming_drives=upcoming_drives,
        current_year_label=academic["current_year_label"],
        batch=academic["batch"]
    )
@app.route("/student/job-opportunities")
def job_opportunities():

    student_id = session.get("student_id")

    if not student_id:
        return redirect(url_for("student_login"))

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        "SELECT admission_year FROM students WHERE id = ?",
        (student_id,)
    )
    student = cursor.fetchone()

    cursor.execute("""
        SELECT *
        FROM companies
        WHERE application_deadline >= DATE('now')
        ORDER BY application_deadline ASC
    """)

    companies = cursor.fetchall()
    if student:
        companies = [
            {**dict(company), **get_company_batch_eligibility(company, student)}
            for company in companies
        ]

    connection.close()

    return render_template(
        "job_opportunities.html",
        companies=companies
    )
@app.route("/student/profile", methods=["GET", "POST"])
def student_profile():

    student_id = session.get("student_id")

    if not student_id:
        return redirect(url_for("student_login"))


    # ==========================================
    # OPEN DATABASE
    # ==========================================

    connection = get_connection()
    cursor = connection.cursor()


    # ==========================================
    # GET STUDENT
    # ==========================================

    cursor.execute(
        "SELECT * FROM students WHERE id = ?",
        (student_id,)
    )

    student = cursor.fetchone()


    if student is None:

        connection.close()

        return "Student not found", 404


    # ==========================================
    # PROFILE PICTURE
    # ==========================================

    if request.method == "POST":

        if "profile_picture" in request.files:

            file = request.files["profile_picture"]

            if file and file.filename:

                if not allowed_profile_file(file.filename):

                    connection.close()

                    return (
                        "Invalid file type. "
                        "Please upload JPG, JPEG, or PNG."
                    )


                original_filename = secure_filename(
                    file.filename
                )


                extension = (
                    original_filename
                    .rsplit(".", 1)[1]
                    .lower()
                )


                unique_filename = (
                    f"{student_id}_{uuid.uuid4().hex}.{extension}"
                )


                upload_folder = os.path.join(
                    app.static_folder,
                    "uploads",
                    "profile_pictures"
                )


                os.makedirs(
                    upload_folder,
                    exist_ok=True
                )


                file_path = os.path.join(
                    upload_folder,
                    unique_filename
                )


                file.save(file_path)


                profile_path = (
                    f"uploads/profile_pictures/{unique_filename}"
                )


                cursor.execute("""
                    UPDATE students
                    SET profile_picture = ?
                    WHERE id = ?
                """, (
                    profile_path,
                    student_id
                ))


                connection.commit()


    # ==========================================
    # APPLIED JOBS
    # ==========================================

    cursor.execute("""
        SELECT COUNT(*)
        FROM applications
        WHERE student_id = ?
    """, (
        student_id,
    ))


    applied_jobs = cursor.fetchone()[0]


    # ==========================================
    # UPCOMING DRIVES
    # ==========================================

    cursor.execute("""
        SELECT COUNT(*)
        FROM companies
        WHERE application_deadline >= DATE('now')
    """)


    upcoming_drives = cursor.fetchone()[0]


    # ==========================================
    # GET UPDATED STUDENT
    # ==========================================

    cursor.execute(
        "SELECT * FROM students WHERE id = ?",
        (student_id,)
    )


    student = cursor.fetchone()


    # ==========================================
    # CLOSE DATABASE
    # ==========================================

    connection.close()


    # ==========================================
    # RENDER PROFILE
    # ==========================================

    academic = get_student_academic_details(student["admission_year"])

    return render_template(
        "components/student_profile.html",
        student=student,
        applied_jobs=applied_jobs,
        upcoming_drives=upcoming_drives,
        current_year_label=academic["current_year_label"],
        batch=academic["batch"]
    )
@app.route("/student/resume", methods=["GET", "POST"])
def student_resume():

    student_id = session.get("student_id")

    if not student_id:
        return redirect(url_for("student_login"))


    connection = get_connection()
    cursor = connection.cursor()


    # ==========================================
    # GET STUDENT
    # ==========================================

    cursor.execute(
        "SELECT * FROM students WHERE id = ?",
        (student_id,)
    )

    student = cursor.fetchone()


    if student is None:

        connection.close()

        return "Student not found", 404


    # ==========================================
    # RESUME UPLOAD / REPLACE
    # ==========================================

    if request.method == "POST":

        if "resume" not in request.files:

            connection.close()

            return "No resume selected."


        file = request.files["resume"]


        if not file or not file.filename:

            connection.close()

            return "No resume selected."


        # ==========================================
        # FILE TYPE CHECK
        # ==========================================

        if not allowed_resume_file(file.filename):

            connection.close()

            return (
                "Invalid file type. "
                "Only PDF resumes are allowed."
            )


        # ==========================================
        # CREATE UNIQUE FILE
        # ==========================================

        unique_filename = (
            f"{student_id}_{uuid.uuid4().hex}.pdf"
        )


        upload_folder = os.path.join(
            app.static_folder,
            "uploads",
            "resumes"
        )


        os.makedirs(
            upload_folder,
            exist_ok=True
        )


        file_path = os.path.join(
            upload_folder,
            unique_filename
        )


        # ==========================================
        # OLD RESUME
        # ==========================================

        old_resume = student["resume_path"]


        # ==========================================
        # SAVE NEW RESUME
        # ==========================================

        file.save(file_path)


        # ==========================================
        # EXTRACT TEXT
        # ==========================================

        extracted_text = extract_resume_text(
            file_path
        )


        # ==========================================
        # ATS SCORE
        # ==========================================

        ats_score, ats_breakdown = calculate_ats_score(
            extracted_text
        )


        # ==========================================
        # DATABASE PATH
        # ==========================================

        resume_path = (
            f"uploads/resumes/{unique_filename}"
        )


        # ==========================================
        # UPDATE DATABASE
        # ==========================================

        cursor.execute("""
            UPDATE students
            SET resume_path = ?,
                ats_score = ?
            WHERE id = ?
        """, (
            resume_path,
            ats_score,
            student_id
        ))


        connection.commit()


        # ==========================================
        # DELETE OLD RESUME
        # ==========================================

        if old_resume:

            old_file_path = os.path.join(
                app.static_folder,
                old_resume
            )


            if os.path.exists(old_file_path):

                os.remove(old_file_path)


            flash(
                "Resume replaced successfully.",
                "success"
            )

        else:

            flash(
                "Resume uploaded successfully.",
                "success"
            )


    # ==========================================
    # GET UPDATED STUDENT
    # ==========================================

    cursor.execute(
        "SELECT * FROM students WHERE id = ?",
        (student_id,)
    )

    student = cursor.fetchone()


    connection.close()


    return render_template(
        "student_resume.html",
        student=student
    )

@app.route("/student/applications")
def student_applications():
    student_id = session.get("student_id")

    if not student_id:
        return redirect(url_for("student_login"))

    connection = get_connection()
    cursor = connection.cursor()

    try:
        # Interview rounds are configured per company, while results are
        # recorded separately for each application. Keep both relationships.
        ensure_interview_tables(cursor)

        cursor.execute("""
            SELECT
                a.application_id,
                a.company_id,
                a.applied_date,
                a.resume_score,
                a.status,
                c.company_name,
                c.job_role,
                c.package,
                c.location
            FROM applications AS a
            JOIN companies AS c
                ON c.company_id = a.company_id
            WHERE a.student_id = ?
            ORDER BY a.application_id DESC
        """, (student_id,))

        application_rows = cursor.fetchall()
        applications = []

        if application_rows:
            application_ids = [row["application_id"] for row in application_rows]
            company_ids = sorted({row["company_id"] for row in application_rows})

            application_placeholders = ",".join("?" for _ in application_ids)
            company_placeholders = ",".join("?" for _ in company_ids)

            cursor.execute(f"""
                SELECT round_id, company_id, round_name, round_order
                FROM interview_rounds
                WHERE company_id IN ({company_placeholders})
                ORDER BY company_id, round_order
            """, company_ids)
            round_rows = cursor.fetchall()

            rounds_by_company = {}
            for round_row in round_rows:
                rounds_by_company.setdefault(round_row["company_id"], []).append(
                    dict(round_row)
                )

            cursor.execute(f"""
                SELECT
                    ir.result_id,
                    ir.application_id,
                    ir.round_id,
                    ir.status,
                    ir.remarks,
                    ir.updated_at
                FROM interview_results AS ir
                WHERE ir.application_id IN ({application_placeholders})
                ORDER BY ir.result_id ASC
            """, application_ids)
            result_rows = cursor.fetchall()

            # If old duplicate result rows exist, the newest result wins.
            results_by_application = {}
            for result_row in result_rows:
                results_by_application.setdefault(
                    result_row["application_id"], {}
                )[result_row["round_id"]] = dict(result_row)

            for application_row in application_rows:
                application = dict(application_row)
                company_rounds = rounds_by_company.get(
                    application["company_id"], []
                )
                saved_results = results_by_application.get(
                    application["application_id"], {}
                )

                student_rounds = []
                round_results = {}
                completed_rounds = 0

                for configured_round in company_rounds:
                    round_data = dict(configured_round)
                    saved_result = saved_results.get(round_data["round_id"])

                    round_data["status"] = (
                        saved_result["status"] if saved_result else "Pending"
                    )
                    round_data["remarks"] = (
                        saved_result.get("remarks", "") if saved_result else ""
                    )
                    round_data["updated_at"] = (
                        saved_result.get("updated_at") if saved_result else None
                    )

                    if round_data["status"] in ("Passed", "Failed"):
                        completed_rounds += 1

                    student_rounds.append(round_data)
                    round_results[round_data["round_id"]] = round_data

                # Keep the old string field available for any existing template
                # logic, while the student page uses structured round records.
                application["interview_rounds"] = "\\n".join(
                    item["round_name"] for item in student_rounds
                )
                application["rounds"] = student_rounds
                application["round_results"] = round_results
                application["completed_rounds"] = completed_rounds
                application["total_rounds"] = len(student_rounds)

                # Generate/refresh feedback only when actual round results exist.
                if saved_results:
                    application["feedback"] = refresh_application_feedback(
                        cursor, application["application_id"]
                    )
                else:
                    application["feedback"] = None

                applications.append(application)

        connection.commit()

    finally:
        connection.close()

    return render_template(
        "student_applications.html",
        applications=applications
    )

@app.route("/test-resume")
def test_resume():

    student_id = session.get("student_id")

    if not student_id:
        return redirect(url_for("student_login"))

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        "SELECT resume_path FROM students WHERE id = ?",
        (student_id,)
    )

    student = cursor.fetchone()

    connection.close()

    if not student or not student["resume_path"]:
        return "<h2>No resume uploaded.</h2>"

    pdf_path = os.path.join(
        app.static_folder,
        student["resume_path"]
    )

    if not os.path.exists(pdf_path):
        return "<h2>Resume file not found.</h2>"

    extracted_text = extract_resume_text(pdf_path)

    return f"""
    <html>
    <head>
        <title>Resume Text Test</title>
        <style>
            body {{
                font-family: Arial;
                background: #111;
                color: white;
                padding: 40px;
            }}

            .box {{
                max-width: 1000px;
                margin: auto;
                background: #222;
                padding: 30px;
                border-radius: 15px;
                white-space: pre-wrap;
                line-height: 1.6;
            }}
        </style>
    </head>

    <body>

        <h1>Extracted Resume Text</h1>

        <div class="box">
            {extracted_text}
        </div>

    </body>
    </html>
    """
@app.route("/test-ats")
def test_ats():

    student_id = session.get("student_id")

    if not student_id:
        return redirect(url_for("student_login"))

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT resume_path
        FROM students
        WHERE id = ?
    """, (student_id,))

    student = cursor.fetchone()

    connection.close()

    if not student or not student["resume_path"]:
        return "No resume uploaded."

    pdf_path = os.path.join(
        app.static_folder,
        student["resume_path"]
    )

    resume_text = extract_resume_text(pdf_path)

    ats_score, breakdown = calculate_ats_score(resume_text)

    result = f"<h2>ATS Score: {ats_score}/100</h2>"

    for category, score in breakdown.items():
        result += f"<p><b>{category}:</b> {score}</p>"

    return result
# ==========================================
# FACULTY - COMPANIES
# ==========================================

@app.route("/faculty/companies")
@faculty_login_required
@faculty_role_required("Dean", "HOD")
def faculty_companies():

    faculty_id = session.get("faculty_id")

    connection = get_connection()
    cursor = connection.cursor()


    # ==========================================
    # GET FACULTY DETAILS
    # ==========================================

    cursor.execute("""
        SELECT *
        FROM admins
        WHERE id = ?
    """, (faculty_id,))

    faculty = cursor.fetchone()


    if faculty is None:

        connection.close()

        session.clear()

        return redirect(
            url_for("faculty_login")
        )


    # ==========================================
    # GET COMPANIES
    # ==========================================

    cursor.execute("""
        SELECT
            company_id,
            company_name,
            job_role,
            package,
            location,
            cgpa_required,
            skills_required,
            eligible_departments,
            eligible_batches,
            description,
            application_deadline,

            CASE
                WHEN application_deadline < DATE('now')
                THEN 'Closed'
                ELSE status
            END AS status

        FROM companies

        ORDER BY application_deadline ASC
    """)

    companies = cursor.fetchall()


    # ==========================================
    # CLOSE DATABASE
    # ==========================================

    connection.close()


    # ==========================================
    # DISPLAY COMPANIES PAGE
    # ==========================================

    return render_template(
        "faculty_companies.html",
        faculty=faculty,
        companies=companies
    )
# ==========================================
# FACULTY - ADD COMPANY
# ==========================================

# ==========================================
# FACULTY - ADD COMPANY
# ==========================================

@app.route("/faculty/companies/add", methods=["GET", "POST"])
@faculty_login_required
@faculty_role_required("Dean", "HOD")
def faculty_add_company():
    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        job_role = request.form.get("job_role", "").strip()
        package_text = request.form.get("package", "").strip()
        location = request.form.get("location", "").strip()
        cgpa_text = request.form.get("cgpa_required", "").strip()
        skills_required = request.form.get("skills_required", "").strip()
        eligible_departments = request.form.get("eligible_departments", "").strip()
        eligible_batches_text = request.form.get("eligible_batches", "").strip()
        description = request.form.get("description", "").strip()
        application_deadline = request.form.get("application_deadline", "").strip()
        raw_rounds = request.form.get("interview_rounds", "")
        round_names = [line.strip() for line in raw_rounds.splitlines() if line.strip()]

        if not all([company_name, job_role, package_text, application_deadline, eligible_batches_text]):
            flash("Company name, job role, package, eligible passing-out batches, and application deadline are required.", "error")
            return redirect(url_for("faculty_add_company"))

        try:
            eligible_batches = parse_eligible_batches(eligible_batches_text)
            if not eligible_batches:
                raise ValueError("Enter at least one eligible passing-out year.")
            package = float(package_text)
            cgpa_required = float(cgpa_text or 0)
            if package < 0 or not 0 <= cgpa_required <= 10:
                raise ValueError
        except ValueError as error:
            flash(str(error) if str(error) else "Enter a valid package and a CGPA between 0 and 10.", "error")
            return redirect(url_for("faculty_add_company"))

        if not round_names:
            flash("Add at least one interview round, one round name per line.", "error")
            return redirect(url_for("faculty_add_company"))
        if len({name.casefold() for name in round_names}) != len(round_names):
            flash("Interview round names must not be duplicated.", "error")
            return redirect(url_for("faculty_add_company"))

        connection = get_connection()
        cursor = connection.cursor()
        try:
            ensure_interview_tables(cursor)
            cursor.execute("""
                INSERT INTO companies (
                    company_name, job_role, package, location, cgpa_required,
                    skills_required, eligible_departments, eligible_batches, description,
                    application_deadline, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                company_name, job_role, package, location, cgpa_required,
                skills_required, eligible_departments, ",".join(map(str, eligible_batches)),
                description, application_deadline, "Open"
            ))
            company_id = cursor.lastrowid
            for order, round_name in enumerate(round_names, start=1):
                cursor.execute("""
                    INSERT INTO interview_rounds (company_id, round_name, round_order)
                    VALUES (?, ?, ?)
                """, (company_id, round_name[:100], order))

            # Company announcements go to each student and each admin inbox.
            cursor.execute("SELECT id FROM students")
            for recipient in cursor.fetchall():
                create_notification(
                    cursor, "student", recipient["id"], "New placement opportunity",
                    f"{company_name} added a placement opportunity for {job_role}.",
                    "company", "/student/job-opportunities", session.get("faculty_id")
                )
            cursor.execute("SELECT id FROM admins")
            for recipient in cursor.fetchall():
                create_notification(
                    cursor, "admin", recipient["id"], "Company added",
                    f"{company_name} ({job_role}) was added to placement opportunities.",
                    "company", "/faculty/companies", session.get("faculty_id")
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        flash(f"Company added with {len(round_names)} interview round(s).", "success")
        return redirect(url_for("faculty_companies"))
    return render_template("faculty_add_company.html")


# ==========================================
# FACULTY - EDIT COMPANY
# ==========================================

@app.route(
    "/faculty/companies/edit/<int:company_id>",
    methods=["GET", "POST"]
)
@faculty_login_required
@faculty_role_required("Dean", "HOD")
def faculty_edit_company(company_id):

    connection = get_connection()
    cursor = connection.cursor()


    # ==========================================
    # GET COMPANY
    # ==========================================

    cursor.execute("""
        SELECT *
        FROM companies
        WHERE company_id = ?
    """, (company_id,))

    company = cursor.fetchone()


    if company is None:

        connection.close()

        flash(
            "Company not found.",
            "error"
        )

        return redirect(
            url_for("faculty_companies")
        )


    # ==========================================
    # UPDATE COMPANY
    # ==========================================

    if request.method == "POST":

        company_name = request.form.get(
            "company_name", ""
        ).strip()

        job_role = request.form.get(
            "job_role", ""
        ).strip()

        package = request.form.get(
            "package", ""
        ).strip()

        location = request.form.get(
            "location", ""
        ).strip()

        cgpa_required = request.form.get(
            "cgpa_required", ""
        ).strip()

        skills_required = request.form.get(
            "skills_required", ""
        ).strip()

        eligible_departments = request.form.get(
            "eligible_departments", ""
        ).strip()

        eligible_batches_text = request.form.get(
            "eligible_batches", ""
        ).strip()

        try:
            eligible_batches = parse_eligible_batches(eligible_batches_text)
        except ValueError as error:
            connection.close()
            flash(str(error), "error")
            return redirect(url_for("faculty_edit_company", company_id=company_id))

        description = request.form.get(
            "description", ""
        ).strip()

        application_deadline = request.form.get(
            "application_deadline", ""
        ).strip()

        status = request.form.get(
            "status",
            "Open"
        ).strip()


        # ==========================================
        # BASIC VALIDATION
        # ==========================================

        if not company_name:

            flash(
                "Company name is required.",
                "error"
            )

            connection.close()

            return redirect(
                url_for(
                    "faculty_edit_company",
                    company_id=company_id
                )
            )


        if not job_role:

            flash(
                "Job role is required.",
                "error"
            )

            connection.close()

            return redirect(
                url_for(
                    "faculty_edit_company",
                    company_id=company_id
                )
            )


        if not package:

            flash(
                "Package is required.",
                "error"
            )

            connection.close()

            return redirect(
                url_for(
                    "faculty_edit_company",
                    company_id=company_id
                )
            )


        if not application_deadline:

            flash(
                "Application deadline is required.",
                "error"
            )

            connection.close()

            return redirect(
                url_for(
                    "faculty_edit_company",
                    company_id=company_id
                )
            )


        # ==========================================
        # UPDATE DATABASE
        # ==========================================

        cursor.execute("""
            UPDATE companies
            SET
                company_name = ?,
                job_role = ?,
                package = ?,
                location = ?,
                cgpa_required = ?,
                skills_required = ?,
                eligible_departments = ?,
                eligible_batches = ?,
                description = ?,
                application_deadline = ?,
                status = ?
            WHERE company_id = ?
        """, (
            company_name,
            job_role,
            package,
            location,
            cgpa_required,
            skills_required,
            eligible_departments,
            ",".join(map(str, eligible_batches)) if eligible_batches else None,
            description,
            application_deadline,
            status,
            company_id
        ))


        connection.commit()

        connection.close()


        flash(
            "Company details updated successfully.",
            "success"
        )


        return redirect(
            url_for("faculty_companies")
        )


    # ==========================================
    # SHOW EDIT PAGE
    # ==========================================

    connection.close()

    return render_template(
        "faculty_edit_company.html",
        company=company
    )
# ==========================================
# GENERAL LOGOUT
# ==========================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("home"))


# ==========================================
# FACULTY LOGOUT
# ==========================================

@app.route("/faculty/logout")
def faculty_logout():

    session.pop("faculty_id", None)
    session.pop("faculty_role", None)

    return redirect(url_for("faculty_login"))
@app.route("/faculty/management")
@faculty_role_required("Dean", "HOD")
def faculty_management():

    faculty_role = session.get("faculty_role")

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            id,
            tts_number,
            name,
            email,
            role
        FROM admins
        WHERE role IN ('HOD', 'Trainer')
        ORDER BY role, name
    """)

    faculty_members = cursor.fetchall()

    connection.close()

    return render_template(
        "faculty_management.html",
        faculty_members=faculty_members,
        faculty_role=faculty_role
    )
@app.route("/faculty/add", methods=["POST"])
@faculty_role_required("Dean", "HOD")
def add_faculty():

    current_role = session.get("faculty_role")

    name = request.form["name"].strip()
    tts_number = request.form["tts_number"].strip().upper()
    email = request.form["email"].strip()
    role = request.form["role"].strip()
    date_of_birth = request.form["date_of_birth"].strip()


    # ==========================================
    # FORMAT VALIDATION
    # ==========================================

    if not re.fullmatch(r"TTS\d{4}", tts_number):
        return "Invalid TTS number format.", 400


    if not re.fullmatch(r"\d{8}", date_of_birth):
        return "Invalid date of birth format.", 400


    if not re.fullmatch(
        r"[A-Za-z0-9._%+-]+@veltech\.edu\.in",
        email
    ):
        return "Only @veltech.edu.in email addresses are allowed.", 400


    # ==========================================
    # CHECK ROLE PERMISSION
    # ==========================================

    if current_role == "HOD" and role != "Trainer":
        return "HOD can only add Trainer accounts.", 403


    if current_role == "Dean" and role not in ["HOD", "Trainer"]:
        return "Invalid faculty role.", 400


    # ==========================================
    # BASIC VALIDATION
    # ==========================================

    if not all([
        name,
        tts_number,
        email,
        role,
        date_of_birth
    ]):
        return "All fields are required.", 400


    connection = get_connection()
    cursor = connection.cursor()


    # ==========================================
    # CHECK DUPLICATE TTS NUMBER
    # ==========================================

    cursor.execute("""
        SELECT id
        FROM admins
        WHERE tts_number = ?
    """, (tts_number,))

    existing = cursor.fetchone()


    if existing:

        connection.close()

        return "Faculty TTS Number already exists.", 400


    # ==========================================
    # INITIAL PASSWORD = DATE OF BIRTH
    # ==========================================

    password = date_of_birth


    # ==========================================
    # INSERT FACULTY
    # ==========================================

    cursor.execute("""
        INSERT INTO admins
        (
            tts_number,
            name,
            email,
            role,
            date_of_birth,
            password
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        tts_number,
        name,
        email,
        role,
        date_of_birth,
        password
    ))


    connection.commit()
    connection.close()


    return redirect(
        url_for("faculty_management")
    )
@app.route("/faculty/students")
@faculty_role_required("Dean", "HOD")
def faculty_students():

    search = request.args.get("search", "").strip()
    connection = get_connection()
    cursor = connection.cursor()

    query = """
        SELECT id, register_number, name, email, department,
               year, admission_year, cgpa
        FROM students
    """

    if search:
        cursor.execute(
            query + " WHERE register_number LIKE ? OR name LIKE ? ORDER BY register_number",
            (f"%{search}%", f"%{search}%")
        )
    else:
        cursor.execute(query + " ORDER BY register_number")

    rows = cursor.fetchall()
    connection.close()

    students = []
    for row in rows:
        item = dict(row)
        item.update(get_student_academic_details(item.get("admission_year")))
        students.append(item)

    return render_template("faculty_students.html", students=students, search=search)

@app.route("/faculty/students/bulk-upload", methods=["GET", "POST"])
@faculty_role_required("Dean", "HOD")
def bulk_upload_students():

    if request.method == "POST":
        if "excel_file" not in request.files:
            return render_template("faculty_bulk_upload.html", error="Please select an Excel file.")

        file = request.files["excel_file"]
        if not file.filename:
            return render_template("faculty_bulk_upload.html", error="Please select an Excel file.")

        if not file.filename.lower().endswith(".xlsx"):
            return render_template("faculty_bulk_upload.html", error="Only .xlsx Excel files are allowed.")

        try:
            workbook = load_workbook(file, data_only=True)
            sheet = workbook.active
        except Exception:
            return render_template("faculty_bulk_upload.html", error="Unable to read the Excel file.")

        expected_headers = [
            "Register Number", "Name", "Email", "Department",
            "Admission Year", "CGPA", "Date of Birth"
        ]
        headers = [str(c.value).strip() if c.value is not None else "" for c in sheet[1]]

        if headers != expected_headers:
            return render_template(
                "faculty_bulk_upload.html",
                error=("Invalid Excel format. The first row must contain: "
                       "Register Number, Name, Email, Department, "
                       "Admission Year, CGPA, Date of Birth")
            )

        added_count, skipped_count, errors = 0, 0, []
        connection = get_connection()
        cursor = connection.cursor()

        for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            register_number = str(row[0]).strip() if row[0] is not None else ""
            name = str(row[1]).strip() if row[1] is not None else ""
            email = str(row[2]).strip() if row[2] is not None else ""
            department = str(row[3]).strip() if row[3] is not None else ""
            admission_value = row[4]
            cgpa_value = row[5]
            dob = str(row[6]).strip() if row[6] is not None else ""

            if not any([register_number, name, email, department, admission_value, cgpa_value, dob]):
                continue

            if not all([register_number, name, email, department,
                        admission_value is not None, cgpa_value is not None, dob]):
                skipped_count += 1
                errors.append(f"Row {row_number}: Missing required field.")
                continue

            register_number = register_number.upper()
            if not re.fullmatch(r"VTU\d{5}", register_number):
                skipped_count += 1
                errors.append(f"Row {row_number}: Register Number must be VTU followed by 5 digits.")
                continue

            email = email.lower()
            if not re.fullmatch(r"[A-Za-z0-9._%+-]+@veltech\.edu\.in", email):
                skipped_count += 1
                errors.append(f"Row {row_number}: Invalid Vel Tech email.")
                continue

            try:
                admission_year = validate_admission_year(admission_value)
            except ValueError as error:
                skipped_count += 1
                errors.append(f"Row {row_number}: {error}")
                continue

            try:
                cgpa = float(cgpa_value)
            except (TypeError, ValueError):
                skipped_count += 1
                errors.append(f"Row {row_number}: CGPA must be a number.")
                continue

            if not 0 <= cgpa <= 10:
                skipped_count += 1
                errors.append(f"Row {row_number}: CGPA must be between 0 and 10.")
                continue

            if not re.fullmatch(r"\d{8}", dob):
                skipped_count += 1
                errors.append(f"Row {row_number}: DOB must be DDMMYYYY.")
                continue

            cursor.execute(
                "SELECT id FROM students WHERE register_number = ? OR email = ?",
                (register_number, email)
            )
            if cursor.fetchone():
                skipped_count += 1
                errors.append(f"Row {row_number}: Register Number or Email already exists.")
                continue

            academic = get_student_academic_details(admission_year)
            legacy_year = academic["current_year"] or 1
            try:
                cursor.execute("""
                    INSERT INTO students
                    (register_number, name, email, department, year, admission_year,
                     cgpa, date_of_birth, password, resume_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    register_number, name, email, department, legacy_year,
                    admission_year, cgpa, dob, dob, None
                ))
                added_count += 1
            except Exception as error:
                skipped_count += 1
                errors.append(f"Row {row_number}: {str(error)}")

        connection.commit()
        connection.close()

        return render_template(
            "faculty_bulk_upload.html",
            success=True,
            added_count=added_count,
            skipped_count=skipped_count,
            errors=errors
        )

    return render_template("faculty_bulk_upload.html")

@app.route("/faculty/students/<int:student_id>/edit", methods=["GET", "POST"])
@faculty_role_required("Dean")
def edit_student(student_id):

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT id, register_number, name, email, department,
               year, admission_year, cgpa, date_of_birth
        FROM students WHERE id = ?
    """, (student_id,))
    student = cursor.fetchone()

    if student is None:
        connection.close()
        return "Student not found.", 404

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        department = request.form.get("department", "").strip()
        admission_value = request.form.get("admission_year", "").strip()
        cgpa_value = request.form.get("cgpa", "").strip()
        dob = request.form.get("date_of_birth", "").strip()

        if not all([name, email, department, admission_value, cgpa_value, dob]):
            connection.close()
            return "All fields are required.", 400

        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@veltech\.edu\.in", email):
            connection.close()
            return "Invalid email. Use a @veltech.edu.in email address.", 400

        if not re.fullmatch(r"\d{8}", dob):
            connection.close()
            return "Date of birth must be in DDMMYYYY format.", 400

        try:
            admission_year = validate_admission_year(admission_value)
        except ValueError as error:
            connection.close()
            return str(error), 400

        try:
            cgpa = float(cgpa_value)
        except ValueError:
            connection.close()
            return "CGPA must be a number.", 400

        if not 0 <= cgpa <= 10:
            connection.close()
            return "CGPA must be between 0 and 10.", 400

        cursor.execute(
            "SELECT id FROM students WHERE email = ? AND id != ?",
            (email, student_id)
        )
        if cursor.fetchone():
            connection.close()
            return "Another student already uses this email.", 400

        academic = get_student_academic_details(admission_year)
        legacy_year = academic["current_year"] or 1

        cursor.execute("""
            UPDATE students
            SET name = ?, email = ?, department = ?, year = ?,
                admission_year = ?, cgpa = ?, date_of_birth = ?
            WHERE id = ?
        """, (
            name, email, department, legacy_year, admission_year,
            cgpa, dob, student_id
        ))

        connection.commit()
        connection.close()
        return redirect(url_for("faculty_student_details", student_id=student_id))

    connection.close()
    student_data = dict(student)
    student_data.update(get_student_academic_details(student_data.get("admission_year")))

    return render_template(
        "faculty_edit_student.html",
        student=student_data,
        current_calendar_year=datetime.now(timezone(timedelta(hours=5, minutes=30))).year
    )

@app.route("/faculty/students/<int:student_id>")
@faculty_role_required("Dean", "HOD")
def faculty_student_details(student_id):

    connection = get_connection()
    cursor = connection.cursor()

    # ------------------------------------------
    # STUDENT DETAILS
    # ------------------------------------------
    cursor.execute("""
        SELECT
            id,
            register_number,
            name,
            email,
            department,
            year,
            admission_year,
            cgpa,
            date_of_birth,
            resume_path
        FROM students
        WHERE id = ?
    """, (student_id,))

    student = cursor.fetchone()

    if student is None:
        connection.close()
        return "Student not found", 404

    student_data = dict(student)
    student_data.update(get_student_academic_details(student_data.get("admission_year")))
    student = student_data


    # ------------------------------------------
    # APPLICATION HISTORY
    # ------------------------------------------
    cursor.execute("""
        SELECT
            applications.application_id,
            applications.applied_date,
            applications.resume_score,
            applications.status,
            companies.company_name,
            companies.job_role,
            companies.package,
            companies.location
        FROM applications
        JOIN companies
            ON applications.company_id = companies.company_id
        WHERE applications.student_id = ?
        ORDER BY applications.applied_date DESC
    """, (student_id,))

    applications = cursor.fetchall()

    # ------------------------------------------
    # PLACEMENT STATISTICS
    # ------------------------------------------

    cursor.execute("""
        SELECT COUNT(*)
        FROM applications
        WHERE student_id = ?
    """, (student_id,))

    companies_applied = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*)
        FROM applications
        WHERE student_id = ?
        AND status = 'Shortlisted'
    """, (student_id,))

    shortlisted = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*)
        FROM applications
        WHERE student_id = ?
        AND status = 'Selected'
    """, (student_id,))

    selected = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT application_id)
        FROM interview_results
        WHERE application_id IN (
            SELECT application_id
            FROM applications
            WHERE student_id = ?
        )
    """, (student_id,))

    interviews = cursor.fetchone()[0]

    # ================= PLACEMENT RESTRICTION =================

    cursor.execute("""
        SELECT
            restriction_id,
            restriction_type,
            start_date,
            end_date,
            reason,
            created_at
        FROM placement_restrictions
        WHERE student_id = ?
        ORDER BY restriction_id DESC
        LIMIT 1
    """, (student_id,))

    restriction = cursor.fetchone()
    connection.close()

    return render_template(
    "faculty_student_details.html",
    student=student,
    applications=applications,
    companies_applied=companies_applied,
    shortlisted=shortlisted,
    interviews=interviews,
    selected=selected,
    restriction=restriction
)
@app.route(
    "/faculty/students/<int:student_id>/block",
    methods=["POST"]
)
@faculty_role_required("Dean", "HOD")
def block_student(student_id):

    restriction_type = request.form["restriction_type"].strip()
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    reason = request.form["reason"].strip()

    if restriction_type not in ["Temporary", "Permanent"]:
        return "Invalid restriction type.", 400

    if not reason:
        return "Reason is required.", 400

    if restriction_type == "Temporary":

        if not start_date or not end_date:
            return "Start date and end date are required.", 400

        if end_date < start_date:
            return "End date cannot be before start date.", 400

    else:
        start_date = None
        end_date = None

    faculty_id = session.get("faculty_id")

    connection = get_connection()
    cursor = connection.cursor()

    # Check student
    cursor.execute("""
        SELECT id
        FROM students
        WHERE id = ?
    """, (student_id,))

    student = cursor.fetchone()

    if student is None:
        connection.close()
        return "Student not found.", 404

    # Notify only when this action newly blocks a student who was not already blocked.
    cursor.execute("""
        SELECT restriction_type, start_date, end_date
        FROM placement_restrictions
        WHERE student_id = ?
        ORDER BY restriction_id DESC LIMIT 1
    """, (student_id,))
    previous_restriction = cursor.fetchone()
    was_actively_blocked = False
    if previous_restriction:
        if previous_restriction["restriction_type"] == "Permanent":
            was_actively_blocked = True
        elif previous_restriction["restriction_type"] == "Temporary":
            cursor.execute("SELECT CASE WHEN DATE('now') BETWEEN ? AND ? THEN 1 ELSE 0 END",
                           (previous_restriction["start_date"], previous_restriction["end_date"]))
            was_actively_blocked = bool(cursor.fetchone()[0])

    # Remove existing restriction for this student
    cursor.execute("""
        DELETE FROM placement_restrictions
        WHERE student_id = ?
    """, (student_id,))

    # Create new restriction
    cursor.execute("""
        INSERT INTO placement_restrictions
        (
            student_id,
            restriction_type,
            start_date,
            end_date,
            reason,
            created_by
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        student_id,
        restriction_type,
        start_date,
        end_date,
        reason,
        faculty_id
    ))

    if not was_actively_blocked:
        create_notification(
            cursor, "student", student_id, "Placement restriction applied",
            f"A {restriction_type.lower()} placement restriction has been applied. Reason: {reason}",
            "restriction", "/student/notifications", faculty_id
        )

    connection.commit()
    connection.close()

    return redirect(
        url_for(
            "faculty_student_details",
            student_id=student_id
        )
    )
@app.route(
    "/faculty/students/<int:student_id>/unblock",
    methods=["POST"]
)
@faculty_role_required("Dean", "HOD")
def unblock_student(student_id):

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        DELETE FROM placement_restrictions
        WHERE student_id = ?
    """, (student_id,))

    connection.commit()
    connection.close()

    return redirect(
        url_for(
            "faculty_student_details",
            student_id=student_id
        )
    )
@app.route("/faculty/students/add", methods=["GET", "POST"])
@faculty_role_required("Dean", "HOD")
def add_student():

    if request.method == "POST":
        register_number = request.form.get("register_number", "").strip().upper()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        department = request.form.get("department", "").strip()
        admission_value = request.form.get("admission_year", "").strip()
        cgpa_value = request.form.get("cgpa", "").strip()
        dob = request.form.get("date_of_birth", "").strip()

        if not all([register_number, name, email, department,
                    admission_value, cgpa_value, dob]):
            return "All fields are required.", 400

        if not re.fullmatch(r"VTU\d{5}", register_number):
            return "Register Number must be VTU followed by 5 digits.", 400

        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@veltech\.edu\.in", email):
            return "Only @veltech.edu.in email addresses are allowed.", 400

        if not re.fullmatch(r"\d{8}", dob):
            return "Date of Birth must be in DDMMYYYY format.", 400

        try:
            admission_year = validate_admission_year(admission_value)
        except ValueError as error:
            return str(error), 400

        try:
            cgpa = float(cgpa_value)
        except ValueError:
            return "Invalid CGPA.", 400

        if not 0 <= cgpa <= 10:
            return "CGPA must be between 0 and 10.", 400

        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute("""
            SELECT id FROM students
            WHERE register_number = ? OR email = ?
        """, (register_number, email))

        if cursor.fetchone():
            connection.close()
            return "Student Register Number or Email already exists.", 400

        academic = get_student_academic_details(admission_year)
        legacy_year = academic["current_year"] or 1

        cursor.execute("""
            INSERT INTO students
            (register_number, name, email, department, year, admission_year,
             cgpa, date_of_birth, password, resume_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            register_number, name, email, department, legacy_year,
            admission_year, cgpa, dob, dob, ""
        ))

        connection.commit()
        connection.close()
        return redirect(url_for("faculty_students"))

    return render_template(
        "faculty_add_student.html",
        current_calendar_year=datetime.now(timezone(timedelta(hours=5, minutes=30))).year
    )


# ==========================================
# FACULTY - APPLICATIONS
# ==========================================

@app.route("/faculty/applications")
@faculty_login_required
def faculty_applications():

    faculty_id = session.get("faculty_id")

    connection = get_connection()
    cursor = connection.cursor()

    # Get logged-in faculty details
    cursor.execute("""
        SELECT *
        FROM admins
        WHERE id = ?
    """, (faculty_id,))

    faculty = cursor.fetchone()

    if faculty is None:
        connection.close()
        session.clear()
        return redirect(url_for("faculty_login"))

    # Get all student applications with company details
    cursor.execute("""
        SELECT
            applications.application_id,
            applications.applied_date,
            applications.resume_score,
            applications.status,
            students.register_number,
            students.name AS student_name,
            students.department,
            students.cgpa,
            companies.company_name,
            companies.job_role,
            companies.package
        FROM applications
        JOIN students
            ON applications.student_id = students.id
        JOIN companies
            ON applications.company_id = companies.company_id
        ORDER BY applications.application_id DESC
    """)

    applications = cursor.fetchall()

    # Application summary counts
    cursor.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'Applied' THEN 1 ELSE 0 END) AS applied,
            SUM(CASE WHEN status = 'Under Review' THEN 1 ELSE 0 END) AS under_review,
            SUM(CASE WHEN status = 'Shortlisted' THEN 1 ELSE 0 END) AS shortlisted,
            SUM(CASE WHEN status = 'Rejected' THEN 1 ELSE 0 END) AS rejected
        FROM applications
    """)

    stats = cursor.fetchone()

    connection.close()

    return render_template(
        "faculty_applications.html",
        faculty=faculty,
        applications=applications,
        stats=stats
    )


# ==========================================
# UPDATE APPLICATION STATUS
# ==========================================

@app.route(
    "/faculty/applications/<int:application_id>/status",
    methods=["POST"]
)
@faculty_login_required
def update_application_status(application_id):

    faculty_role = session.get("faculty_role")

    # Trainers can review applications but cannot make
    # final selection decisions through this route.
    allowed_statuses = [
        "Applied",
        "Under Review",
        "Shortlisted",
        "Rejected"
    ]

    new_status = request.form.get("status", "").strip()

    if new_status not in allowed_statuses:
        flash("Invalid application status.", "error")
        return redirect(url_for("faculty_applications"))

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT a.application_id, a.student_id, a.status AS old_status,
               c.company_name, c.job_role
        FROM applications a
        JOIN companies c ON c.company_id = a.company_id
        WHERE a.application_id = ?
    """, (application_id,))

    application = cursor.fetchone()

    if application is None:
        connection.close()
        flash("Application not found.", "error")
        return redirect(url_for("faculty_applications"))

    cursor.execute("""
        UPDATE applications
        SET status = ?
        WHERE application_id = ?
    """, (new_status, application_id))

    if new_status == "Shortlisted" and application["old_status"] != "Shortlisted":
        create_notification(
            cursor, "student", application["student_id"], "You have been shortlisted",
            f"You have been shortlisted for {application['company_name']} ({application['job_role']}).",
            "shortlist", "/student/applications", session.get("faculty_id")
        )
    elif new_status in ("Rejected", "Selected") and application["old_status"] != new_status:
        create_notification(
            cursor, "student", application["student_id"], f"Application {new_status.lower()}",
            f"Your application for {application['company_name']} is now marked {new_status.lower()}.",
            "final_result", "/student/applications", session.get("faculty_id")
        )

    connection.commit()
    connection.close()

    flash("Application status updated successfully.", "success")

    return redirect(url_for("faculty_applications"))
# ==========================================
# FACULTY - INTERVIEW ROUNDS & SELECTION
# ==========================================

def ensure_interview_tables(cursor):
    """Create the round tables when an older database has not initialized them yet."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS interview_rounds (
            round_id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            round_name TEXT NOT NULL,
            round_order INTEGER NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(company_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS interview_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            remarks TEXT,
            updated_by TEXT,
            updated_at TEXT,
            FOREIGN KEY(application_id) REFERENCES applications(application_id),
            FOREIGN KEY(round_id) REFERENCES interview_rounds(round_id)
        )
    """)
    ensure_application_feedback_table(cursor)


def ensure_application_feedback_table(cursor):
    """Create feedback storage safely for existing databases."""
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


def refresh_application_feedback(cursor, application_id):
    """
    Build evidence-based, rule-based feedback from actual interview outcomes.
    This does not estimate skill scores or infer unrecorded weaknesses.
    """
    ensure_application_feedback_table(cursor)

    cursor.execute("""
        SELECT
            r.round_name,
            r.round_order,
            COALESCE(latest.status, 'Pending') AS status,
            COALESCE(latest.remarks, '') AS remarks
        FROM interview_rounds AS r
        JOIN applications AS a ON a.company_id = r.company_id
        LEFT JOIN (
            SELECT ir1.application_id, ir1.round_id, ir1.status, ir1.remarks
            FROM interview_results AS ir1
            JOIN (
                SELECT application_id, round_id, MAX(result_id) AS latest_id
                FROM interview_results
                WHERE application_id = ?
                GROUP BY application_id, round_id
            ) AS newest ON newest.latest_id = ir1.result_id
        ) AS latest
          ON latest.round_id = r.round_id
         AND latest.application_id = a.application_id
        WHERE a.application_id = ?
        ORDER BY r.round_order
    """, (application_id, application_id))
    round_rows = cursor.fetchall()

    if not round_rows:
        return None

    passed_rounds = [row for row in round_rows if row["status"] == "Passed"]
    failed_rounds = [row for row in round_rows if row["status"] == "Failed"]
    pending_rounds = [row for row in round_rows if row["status"] == "Pending"]

    passed_count = len(passed_rounds)
    failed_count = len(failed_rounds)
    pending_count = len(pending_rounds)

    summary = (
        f"{passed_count} round(s) passed, {failed_count} round(s) failed, "
        f"and {pending_count} round(s) pending out of {len(round_rows)} configured rounds."
    )

    strengths = [
        f"Successfully cleared the {row['round_name']} round."
        for row in passed_rounds
    ]

    improvement_areas = []
    recommendations = []

    for row in failed_rounds:
        item = {
            "round_name": row["round_name"],
            "remarks": (row["remarks"] or "").strip()
        }
        improvement_areas.append(item)
        if item["remarks"]:
            recommendations.append(
                f"Review the faculty feedback for {row['round_name']}: {item['remarks']}"
            )
        else:
            recommendations.append(
                f"Review the topics and preparation approach relevant to the "
                f"{row['round_name']} round. No specific faculty remarks were recorded."
            )

    if failed_count == 0 and pending_count > 0:
        recommendations.append(
            "Continue preparing for the remaining interview rounds. "
            "Their outcomes have not been recorded yet."
        )
    elif failed_count == 0 and pending_count == 0 and passed_count > 0:
        recommendations.append(
            "You have cleared all configured interview rounds. "
            "Keep your placement documents and communication ready for the next steps."
        )

    if passed_count == 0 and failed_count == 0:
        recommendations.append(
            "Feedback will become more meaningful after at least one interview result is recorded."
        )

    if not strengths:
        strengths = ["No passed interview round has been recorded yet."]

    cursor.execute("""
        INSERT INTO application_feedback (
            application_id, passed_count, failed_count, pending_count,
            summary, strengths, improvement_areas, recommendations, generated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(application_id) DO UPDATE SET
            passed_count = excluded.passed_count,
            failed_count = excluded.failed_count,
            pending_count = excluded.pending_count,
            summary = excluded.summary,
            strengths = excluded.strengths,
            improvement_areas = excluded.improvement_areas,
            recommendations = excluded.recommendations,
            generated_at = CURRENT_TIMESTAMP
    """, (
        application_id,
        passed_count,
        failed_count,
        pending_count,
        summary,
        json.dumps(strengths, ensure_ascii=False),
        json.dumps(improvement_areas, ensure_ascii=False),
        json.dumps(recommendations, ensure_ascii=False)
    ))

    return {
        "passed_count": passed_count,
        "failed_count": failed_count,
        "pending_count": pending_count,
        "summary": summary,
        "strengths": strengths,
        "improvement_areas": improvement_areas,
        "recommendations": recommendations
    }


@app.route("/faculty/selection")
@faculty_login_required
@faculty_role_required("Dean", "HOD")
def faculty_selection():
    faculty_id = session.get("faculty_id")
    connection = get_connection()
    cursor = connection.cursor()
    ensure_interview_tables(cursor)

    cursor.execute("SELECT * FROM admins WHERE id = ?", (faculty_id,))
    faculty = cursor.fetchone()
    if faculty is None:
        connection.close()
        session.clear()
        return redirect(url_for("faculty_login"))

    # Include active interview applications and final outcomes.
    cursor.execute("""
        SELECT a.application_id, a.applied_date, a.resume_score, a.status,
               s.register_number, s.name AS student_name, s.department, s.cgpa,
               c.company_id, c.company_name, c.job_role, c.package
        FROM applications AS a
        JOIN students AS s ON s.id = a.student_id
        JOIN companies AS c ON c.company_id = a.company_id
        WHERE a.status IN (
            'Shortlisted', 'Interview in Progress', 'Selected', 'Rejected'
        )
        ORDER BY
            CASE
                WHEN a.status = 'Interview in Progress' THEN 0
                WHEN a.status = 'Shortlisted' THEN 1
                ELSE 2
            END,
            a.application_id DESC
    """)
    candidates = cursor.fetchall()

    cursor.execute("""
        SELECT company_id, company_name
        FROM companies
        WHERE company_id IN (
            SELECT DISTINCT a.company_id
            FROM applications a
            WHERE a.status IN (
                'Shortlisted', 'Interview in Progress', 'Selected', 'Rejected'
            )
        )
        ORDER BY company_name
    """)
    companies_for_rounds = cursor.fetchall()

    cursor.execute("""
        SELECT round_id, company_id, round_name, round_order
        FROM interview_rounds
        ORDER BY company_id, round_order
    """)
    all_rounds = cursor.fetchall()
    rounds_by_company = {}
    for round_row in all_rounds:
        rounds_by_company.setdefault(round_row["company_id"], []).append(dict(round_row))

    cursor.execute("""
        SELECT ir.result_id, ir.application_id, ir.round_id, ir.status,
               ir.remarks, ir.updated_at
        FROM interview_results ir
        JOIN applications a ON a.application_id = ir.application_id
    """)
    all_results = cursor.fetchall()
    results_by_application = {}
    for result in all_results:
        results_by_application.setdefault(result["application_id"], {})[result["round_id"]] = dict(result)

    # Add round list and recorded results to each candidate for the template.
    candidate_rows = []
    for candidate in candidates:
        row = dict(candidate)
        row["rounds"] = rounds_by_company.get(candidate["company_id"], [])
        row["round_results"] = results_by_application.get(candidate["application_id"], {})
        completed = sum(
            1 for r in row["rounds"]
            if row["round_results"].get(r["round_id"], {}).get("status") in ("Passed", "Failed")
        )
        row["completed_rounds"] = completed
        row["total_rounds"] = len(row["rounds"])
        row["current_round"] = next(
            (r for r in row["rounds"]
             if row["round_results"].get(r["round_id"], {}).get("status") not in ("Passed", "Failed")),
            None
        )
        candidate_rows.append(row)

    cursor.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'Shortlisted' THEN 1 ELSE 0 END) AS shortlisted,
            SUM(CASE WHEN status = 'Interview in Progress' THEN 1 ELSE 0 END) AS in_progress,
            SUM(CASE WHEN status = 'Selected' THEN 1 ELSE 0 END) AS selected,
            SUM(CASE WHEN status = 'Rejected' THEN 1 ELSE 0 END) AS rejected
        FROM applications
    """)
    stats = cursor.fetchone()

    # Only companies with applications need to be configured from this page.
    connection.commit()
    connection.close()
    return render_template(
        "faculty_selection.html",
        faculty=faculty,
        candidates=candidate_rows,
        companies_for_rounds=companies_for_rounds,
        rounds_by_company=rounds_by_company,
        stats=stats
    )


@app.route("/faculty/selection/company/<int:company_id>/rounds", methods=["POST"])
@faculty_login_required
@faculty_role_required("Dean", "HOD")
def configure_company_rounds(company_id):
    """Save a variable number of company interview rounds, one round per line."""
    raw_rounds = request.form.get("rounds", "")
    round_names = [line.strip() for line in raw_rounds.splitlines() if line.strip()]
    if not round_names:
        flash("Enter at least one interview round, one per line.", "error")
        return redirect(url_for("faculty_selection"))

    # Avoid duplicate names while retaining the user's order.
    normalized = [name.casefold() for name in round_names]
    if len(normalized) != len(set(normalized)):
        flash("Round names must be unique for a company.", "error")
        return redirect(url_for("faculty_selection"))

    connection = get_connection()
    cursor = connection.cursor()
    ensure_interview_tables(cursor)

    cursor.execute("SELECT company_id FROM companies WHERE company_id = ?", (company_id,))
    if cursor.fetchone() is None:
        connection.close()
        flash("Company not found.", "error")
        return redirect(url_for("faculty_selection"))

    # Do not replace a round setup after any result has been recorded.
    cursor.execute("""
        SELECT COUNT(*)
        FROM interview_results ir
        JOIN interview_rounds r ON r.round_id = ir.round_id
        WHERE r.company_id = ?
    """, (company_id,))
    if cursor.fetchone()[0] > 0:
        connection.close()
        flash("Rounds cannot be changed after results have been recorded. Contact an administrator to reset them.", "error")
        return redirect(url_for("faculty_selection"))

    cursor.execute("DELETE FROM interview_rounds WHERE company_id = ?", (company_id,))
    for order, name in enumerate(round_names, start=1):
        cursor.execute("""
            INSERT INTO interview_rounds (company_id, round_name, round_order)
            VALUES (?, ?, ?)
        """, (company_id, name[:100], order))

    connection.commit()
    connection.close()
    flash(f"Saved {len(round_names)} interview round(s).", "success")
    return redirect(url_for("faculty_selection"))


@app.route(
    "/faculty/selection/<int:application_id>/round/<int:round_id>",
    methods=["POST"]
)
@faculty_login_required
@faculty_role_required("Dean", "HOD")
def update_interview_round(application_id, round_id):
    result_status = request.form.get("result_status", "").strip()
    remarks = request.form.get("remarks", "").strip()
    if result_status not in ("Passed", "Failed"):
        flash("Choose Passed or Failed for the interview round.", "error")
        return redirect(url_for("faculty_selection"))

    connection = get_connection()
    cursor = connection.cursor()
    ensure_interview_tables(cursor)

    cursor.execute("""
        SELECT a.application_id, a.status, a.company_id, a.student_id,
               s.name AS student_name, c.company_name
        FROM applications a
        JOIN students s ON s.id = a.student_id
        JOIN companies c ON c.company_id = a.company_id
        WHERE a.application_id = ?
    """, (application_id,))
    application = cursor.fetchone()
    if application is None:
        connection.close()
        flash("Application not found.", "error")
        return redirect(url_for("faculty_selection"))

    if application["status"] not in ("Shortlisted", "Interview in Progress"):
        connection.close()
        flash("Only shortlisted or in-progress applications can be updated.", "error")
        return redirect(url_for("faculty_selection"))

    cursor.execute("""
        SELECT round_id, company_id, round_name, round_order
        FROM interview_rounds
        WHERE round_id = ? AND company_id = ?
    """, (round_id, application["company_id"]))
    chosen_round = cursor.fetchone()
    if chosen_round is None:
        connection.close()
        flash("That round does not belong to this company.", "error")
        return redirect(url_for("faculty_selection"))

    cursor.execute("""
        SELECT round_id, round_order
        FROM interview_rounds
        WHERE company_id = ?
        ORDER BY round_order
    """, (application["company_id"],))
    ordered_rounds = cursor.fetchall()
    if not ordered_rounds:
        connection.close()
        flash("Configure this company's interview rounds first.", "error")
        return redirect(url_for("faculty_selection"))

    # Enforce round-by-round progression: earlier rounds must be Passed.
    for earlier in ordered_rounds:
        if earlier["round_order"] >= chosen_round["round_order"]:
            break
        cursor.execute("""
            SELECT status FROM interview_results
            WHERE application_id = ? AND round_id = ?
            ORDER BY result_id DESC LIMIT 1
        """, (application_id, earlier["round_id"]))
        previous = cursor.fetchone()
        if not previous or previous["status"] != "Passed":
            connection.close()
            flash("Complete and pass all previous rounds before updating this round.", "error")
            return redirect(url_for("faculty_selection"))

    # A failed round ends this application; passing the last round selects the student.
    cursor.execute("""
        SELECT result_id FROM interview_results
        WHERE application_id = ? AND round_id = ?
        ORDER BY result_id DESC LIMIT 1
    """, (application_id, round_id))
    existing = cursor.fetchone()
    updated_by = str(session.get("faculty_id", ""))

    if existing:
        cursor.execute("""
            UPDATE interview_results
            SET status = ?, remarks = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE result_id = ?
        """, (result_status, remarks, updated_by, existing["result_id"]))
    else:
        cursor.execute("""
            INSERT INTO interview_results
                (application_id, round_id, status, remarks, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (application_id, round_id, result_status, remarks, updated_by))

    if result_status == "Failed":
        new_application_status = "Rejected"
    else:
        cursor.execute("""
            SELECT COUNT(*)
            FROM interview_rounds r
            WHERE r.company_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM interview_results ir
                  WHERE ir.application_id = ?
                    AND ir.round_id = r.round_id
                    AND ir.status = 'Passed'
              )
        """, (application["company_id"], application_id))
        remaining = cursor.fetchone()[0]
        new_application_status = "Selected" if remaining == 0 else "Interview in Progress"

    cursor.execute("""
        UPDATE applications SET status = ? WHERE application_id = ?
    """, (new_application_status, application_id))

    # Keep the student's feedback synchronized with the newly saved result.
    refresh_application_feedback(cursor, application_id)

    create_notification(
        cursor, "student", application["student_id"],
        f"{chosen_round['round_name']}: {result_status}",
        f"Your result for the {chosen_round['round_name']} round at {application['company_name']} is {result_status.lower()}.",
        "round_result", "/student/applications", session.get("faculty_id")
    )
    if new_application_status in ("Selected", "Rejected"):
        create_notification(
            cursor, "student", application["student_id"],
            f"Final result: {new_application_status}",
            f"Your application to {application['company_name']} is {new_application_status.lower()}.",
            "final_result", "/student/applications", session.get("faculty_id")
        )

    connection.commit()
    connection.close()
    flash(
        f"{chosen_round['round_name']}: {result_status}. Application status: {new_application_status}.",
        "success"
    )
    return redirect(url_for("faculty_selection"))


@app.route("/student/notifications")
def student_notifications():
    student_id = session.get("student_id")
    if not student_id:
        return redirect(url_for("student_login"))
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""SELECT * FROM notifications
                      WHERE recipient_type = 'student' AND recipient_id = ?
                      ORDER BY created_at DESC, notification_id DESC""", (student_id,))
    notifications = cursor.fetchall()
    connection.close()
    return render_template("student_notifications.html", notifications=notifications)


@app.route("/student/notifications/<int:notification_id>/read", methods=["POST"])
def mark_student_notification_read(notification_id):
    student_id = session.get("student_id")
    if not student_id:
        return redirect(url_for("student_login"))
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("UPDATE notifications SET is_read = 1 WHERE notification_id = ? AND recipient_type = 'student' AND recipient_id = ?", (notification_id, student_id))
    connection.commit()
    connection.close()
    return redirect(url_for("student_notifications"))


@app.route("/faculty/notifications")
@faculty_login_required
def faculty_notifications():
    faculty_id = session.get("faculty_id")
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""SELECT * FROM notifications
                      WHERE recipient_type = 'admin' AND recipient_id = ?
                      ORDER BY created_at DESC, notification_id DESC""", (faculty_id,))
    notifications = cursor.fetchall()
    connection.close()
    can_announce = session.get("faculty_role") in ("Dean", "HOD")
    return render_template("faculty_notifications.html", notifications=notifications, can_announce=can_announce)


@app.route("/faculty/notifications/<int:notification_id>/read", methods=["POST"])
@faculty_login_required
def mark_faculty_notification_read(notification_id):
    faculty_id = session.get("faculty_id")
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("UPDATE notifications SET is_read = 1 WHERE notification_id = ? AND recipient_type = 'admin' AND recipient_id = ?", (notification_id, faculty_id))
    connection.commit()
    connection.close()
    return redirect(url_for("faculty_notifications"))


@app.route("/faculty/notifications/send", methods=["GET", "POST"])
@faculty_login_required
@faculty_role_required("Dean", "HOD")
def faculty_send_announcement():
    connection = get_connection()
    cursor = connection.cursor()
    if request.method == "POST":
        target = request.form.get("target", "all").strip()
        title = request.form.get("title", "").strip()
        message = request.form.get("message", "").strip()
        student_id_text = request.form.get("student_id", "").strip()
        if not title or not message:
            flash("Title and message are required.", "error")
            connection.close()
            return redirect(url_for("faculty_send_announcement"))
        if target == "all":
            cursor.execute("SELECT id FROM students")
            recipients = cursor.fetchall()
        elif target == "student":
            if not student_id_text.isdigit():
                flash("Enter a valid student ID.", "error")
                connection.close()
                return redirect(url_for("faculty_send_announcement"))
            cursor.execute("SELECT id FROM students WHERE id = ?", (int(student_id_text),))
            one_student = cursor.fetchone()
            if not one_student:
                flash("Student not found. Use the student's database ID.", "error")
                connection.close()
                return redirect(url_for("faculty_send_announcement"))
            recipients = [one_student]
        else:
            flash("Choose all students or one student.", "error")
            connection.close()
            return redirect(url_for("faculty_send_announcement"))
        for recipient in recipients:
            create_notification(cursor, "student", recipient["id"], title, message, "announcement", "/student/notifications", session.get("faculty_id"))
        connection.commit()
        connection.close()
        flash(f"Announcement sent to {len(recipients)} student(s).", "success")
        return redirect(url_for("faculty_notifications"))
    cursor.execute("SELECT id, name, register_number FROM students ORDER BY name")
    students = cursor.fetchall()
    connection.close()
    return render_template("faculty_send_announcement.html", students=students)


@app.route("/faculty/my-profile", methods=["GET", "POST"])
def faculty_my_profile():

    # 1. Check whether faculty is logged in
    faculty_id = session.get("faculty_id")

    if not faculty_id:
        flash("Please log in to access your profile.", "warning")
        return redirect(url_for("faculty_login"))

    # 2. Connect to the existing database
    conn = get_connection()
    cursor = conn.cursor()

    try:
        # 3. Fetch the logged-in faculty account
        cursor.execute(
            """
            SELECT *
            FROM admins
            WHERE id = ?
            """,
            (faculty_id,)
        )

        faculty = cursor.fetchone()

        if not faculty:
            session.clear()
            flash(
                "Your account could not be found. Please log in again.",
                "danger"
            )
            return redirect(url_for("faculty_login"))

        # 4. Handle profile updates
        if request.method == "POST":

            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()

            # Password changes are temporarily disabled.
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not name or not email:
                flash("Name and email are required.", "danger")
                return redirect(url_for("faculty_my_profile"))

            # Prevent password changes until login uses password hashing
            if current_password or new_password or confirm_password:
                flash(
                    "Password changes are temporarily unavailable. "
                    "Please update the password system first.",
                    "warning"
                )
                return redirect(url_for("faculty_my_profile"))

            # 5. Check whether another account uses this email
            cursor.execute(
                """
                SELECT id
                FROM admins
                WHERE LOWER(email) = LOWER(?)
                  AND id != ?
                """,
                (email, faculty_id)
            )

            if cursor.fetchone():
                flash(
                    "This email is already registered to another account.",
                    "danger"
                )
                return redirect(url_for("faculty_my_profile"))

            # 6. Update name and email
            cursor.execute(
                """
                UPDATE admins
                SET name = ?, email = ?
                WHERE id = ?
                """,
                (name, email, faculty_id)
            )

            conn.commit()

            flash("Profile updated successfully.", "success")
            return redirect(url_for("faculty_my_profile"))

        # 7. Render profile page
        return render_template(
            "faculty/my_profile.html",
            admin=faculty,
            faculty=faculty
        )

    except sqlite3.Error:
        conn.rollback()

        flash(
            "A database error occurred while accessing your profile.",
            "danger"
        )

        return redirect(url_for("faculty_dashboard"))

    finally:
        conn.close()

# ==========================================
# FACULTY - PLACEMENT REPORTS
# ==========================================

@app.route("/faculty/reports")
@faculty_login_required
def faculty_reports():

    connection = get_connection()
    cursor = connection.cursor()

    try:
        # --------------------------------------
        # 1. Logged-in faculty details
        # --------------------------------------
        faculty_id = session.get("faculty_id")

        cursor.execute("""
            SELECT id, name, tts_number, role
            FROM admins
            WHERE id = ?
        """, (faculty_id,))

        faculty = cursor.fetchone()

        if faculty is None:
            session.clear()
            return redirect(url_for("faculty_login"))

        # --------------------------------------
        # 2. Overall student and company totals
        # --------------------------------------
        cursor.execute("SELECT COUNT(*) FROM students")
        total_students = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM companies")
        total_companies = cursor.fetchone()[0]

        # A drive is active only if its status is Open
        # and its deadline has not passed.
        cursor.execute("""
            SELECT COUNT(*)
            FROM companies
            WHERE LOWER(TRIM(status)) = 'open'
              AND DATE(application_deadline) >= DATE('now')
        """)
        active_drives = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM applications")
        total_applications = cursor.fetchone()[0]

        # --------------------------------------
        # 3. Unique placed students
        # --------------------------------------
        # A student selected by multiple companies
        # is counted only once in this KPI.
        cursor.execute("""
            SELECT COUNT(DISTINCT student_id)
            FROM applications
            WHERE LOWER(TRIM(status)) = 'selected'
        """)
        placed_students = cursor.fetchone()[0]

        # --------------------------------------
        # 4. Placement and selection rates
        # --------------------------------------
        student_placement_rate = (
            placed_students * 100.0 / total_students
            if total_students > 0 else 0.0
        )

        cursor.execute("""
            SELECT COUNT(*)
            FROM applications
            WHERE LOWER(TRIM(status)) = 'selected'
        """)
        selected_applications = cursor.fetchone()[0]

        application_selection_rate = (
            selected_applications * 100.0 / total_applications
            if total_applications > 0 else 0.0
        )

        # --------------------------------------
        # 5. Average package of selected offers
        # --------------------------------------
        # This is an offer-level average, not the
        # average salary of unique placed students.
        cursor.execute("""
            SELECT AVG(c.package)
            FROM applications AS a
            JOIN companies AS c
              ON c.company_id = a.company_id
            WHERE LOWER(TRIM(a.status)) = 'selected'
              AND c.package IS NOT NULL
        """)
        average_selected_package = cursor.fetchone()[0] or 0.0

        # --------------------------------------
        # 6. Department-wise placement results
        # --------------------------------------
        cursor.execute("""
            SELECT
                s.department,
                COUNT(DISTINCT s.id) AS total_students,
                COUNT(DISTINCT CASE
                    WHEN LOWER(TRIM(a.status)) = 'selected'
                    THEN s.id
                END) AS placed_students
            FROM students AS s
            LEFT JOIN applications AS a
              ON a.student_id = s.id
            GROUP BY s.department
            ORDER BY s.department
        """)

        department_rows = cursor.fetchall()
        department_reports = []

        for row in department_rows:
            department_total = row["total_students"]
            department_placed = row["placed_students"]

            department_rate = (
                department_placed * 100.0 / department_total
                if department_total > 0 else 0.0
            )

            department_reports.append({
                "department": row["department"],
                "total_students": department_total,
                "placed_students": department_placed,
                "placement_rate": round(department_rate, 2)
            })

        # --------------------------------------
        # 7. Company-wise recruitment results
        # --------------------------------------
        cursor.execute("""
            SELECT
                c.company_name,
                c.job_role,
                c.package,
                COUNT(a.application_id) AS applications_received,
                COUNT(CASE
                    WHEN LOWER(TRIM(a.status)) = 'selected'
                    THEN 1
                END) AS selected_applications
            FROM companies AS c
            LEFT JOIN applications AS a
              ON a.company_id = c.company_id
            GROUP BY
                c.company_id,
                c.company_name,
                c.job_role,
                c.package
            ORDER BY applications_received DESC,
                     c.company_name ASC
        """)

        company_reports = cursor.fetchall()

        # --------------------------------------
        # 8. Application status distribution
        # --------------------------------------
        cursor.execute("""
            SELECT
                CASE
                    WHEN TRIM(status) = '' OR status IS NULL
                    THEN 'Unspecified'
                    ELSE TRIM(status)
                END AS status,
                COUNT(*) AS total
            FROM applications
            GROUP BY
                CASE
                    WHEN TRIM(status) = '' OR status IS NULL
                    THEN 'Unspecified'
                    ELSE TRIM(status)
                END
            ORDER BY total DESC, status ASC
        """)

        application_statuses = cursor.fetchall()

        return render_template(
            "faculty_reports.html",
            faculty=faculty,
            total_students=total_students,
            total_companies=total_companies,
            active_drives=active_drives,
            total_applications=total_applications,
            placed_students=placed_students,
            selected_applications=selected_applications,
            student_placement_rate=round(student_placement_rate, 2),
            application_selection_rate=round(
                application_selection_rate, 2
            ),
            average_selected_package=round(
                average_selected_package, 2
            ),
            department_reports=department_reports,
            company_reports=company_reports,
            application_statuses=application_statuses
        )

    finally:
        connection.close()
def add_interview_rounds_column():
    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("PRAGMA table_info(companies)")
        columns = [row[1] for row in cursor.fetchall()]

        if "interview_rounds" not in columns:
            cursor.execute("""
                ALTER TABLE companies
                ADD COLUMN interview_rounds TEXT DEFAULT ''
            """)
            connection.commit()
    finally:
        connection.close()
@app.context_processor
def inject_unread_notification_count():
    unread_notification_count = 0

    student_id = session.get("student_id")
    faculty_id = session.get("faculty_id")

    if student_id or faculty_id:
        connection = get_connection()
        cursor = connection.cursor()

        if student_id:
            cursor.execute("""
                SELECT COUNT(*)
                FROM notifications
                WHERE recipient_type = 'student'
                  AND recipient_id = ?
                  AND is_read = 0
            """, (student_id,))

        elif faculty_id:
            cursor.execute("""
                SELECT COUNT(*)
                FROM notifications
                WHERE recipient_type = 'admin'
                  AND recipient_id = ?
                  AND is_read = 0
            """, (faculty_id,))

        unread_notification_count = cursor.fetchone()[0]
        connection.close()

    return {
        "unread_notification_count": unread_notification_count
    }
if __name__ == "__main__":
    app.run(debug=True)