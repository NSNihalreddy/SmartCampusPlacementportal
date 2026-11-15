
from ADMIN.migrations import run_placehub_migrations

if __name__ == "__main__":
    print("Starting PLACEHUB database migration...")

    run_placehub_migrations()

    print("PLACEHUB migration completed successfully.")
