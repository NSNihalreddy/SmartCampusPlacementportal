import sqlite3

DATABASE = "database/database.db"


connection = sqlite3.connect(DATABASE)

cursor = connection.cursor()


# ==========================================
# DELETE OLD COMPANY-RELATED DATA
# ==========================================

# Delete interview results first
cursor.execute("""
    DELETE FROM interview_results
    WHERE application_id IN (
        SELECT application_id
        FROM applications
        WHERE company_id IN (
            SELECT company_id
            FROM companies
        )
    )
""")


# Delete interview rounds
cursor.execute("""
    DELETE FROM interview_rounds
    WHERE company_id IN (
        SELECT company_id
        FROM companies
    )
""")


# Delete old applications
cursor.execute("""
    DELETE FROM applications
    WHERE company_id IN (
        SELECT company_id
        FROM companies
    )
""")


# Delete old companies
cursor.execute("""
    DELETE FROM companies
""")


# ==========================================
# ADD 4 NEW DUMMY COMPANIES
# ==========================================

companies = [

    (
        "Wipro",
        "Project Engineer",
        6.5,
        "Bangalore",
        6.5,
        "Python, Java, SQL, DSA",
        "AI & DS,CSE,IT,ECE",
        "Campus hiring for software engineering and technology roles.",
        "2026-09-10",
        "Open"
    ),

    (
        "Cognizant",
        "Programmer Analyst",
        6.75,
        "Chennai",
        7.0,
        "Java, Python, SQL, OOP",
        "AI & DS,CSE,IT",
        "Fresh graduate hiring for application development roles.",
        "2026-09-15",
        "Open"
    ),

    (
        "Capgemini",
        "Software Engineer",
        7.0,
        "Pune",
        6.5,
        "Java, Python, SQL, Git",
        "AI & DS,CSE,IT,ECE",
        "Technology recruitment drive for software engineering positions.",
        "2026-09-20",
        "Open"
    ),

    (
        "HCLTech",
        "Graduate Engineer Trainee",
        6.0,
        "Noida",
        6.0,
        "Python, Java, SQL, HTML, CSS",
        "AI & DS,CSE,IT,ECE",
        "Graduate recruitment for software and technology development roles.",
        "2026-09-25",
        "Open"
    )

]


cursor.executemany("""
    INSERT INTO companies
    (
        company_name,
        job_role,
        package,
        location,
        cgpa_required,
        skills_required,
        eligible_departments,
        description,
        application_deadline,
        status
    )

    VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", companies)


connection.commit()


# ==========================================
# DISPLAY NEW COMPANIES
# ==========================================

cursor.execute("""
    SELECT
        company_id,
        company_name,
        job_role,
        package,
        location
    FROM companies
    ORDER BY company_id
""")


rows = cursor.fetchall()


print("\nCompanies after reset:\n")

for row in rows:

    print(
        row[0],
        "|",
        row[1],
        "|",
        row[2],
        "|",
        row[3],
        "LPA |",
        row[4]
    )


connection.close()


print("\nCompany data reset successfully.")