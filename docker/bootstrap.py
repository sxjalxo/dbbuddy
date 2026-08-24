"""Prepare the demo instance: accounts, and a connection pointing at the sample
database.

Run from ``backend/`` after the app database is reachable. Idempotent — the
backend entrypoint runs it on every start, and a re-run resets the demo
passwords and re-points the connection rather than creating duplicates.

This exists so the demo is one command. A visitor opens the app, signs in as the
analyst, and the sample database is already connected — no registration, no
connection form, no Analyze click before the first question.

**Demo credentials only.** Everything here is public knowledge in a public repo.
The production checklist (docs/DEPLOYMENT.md) requires removing these accounts;
`DBBUDDY_ENV=production` is deliberately not set in the demo compose file.
"""

import os
import sys

sys.path.insert(0, ".")

from app_db.database import SessionLocal, init_db  # noqa: E402
from app_db.models import DatabaseConnection, User  # noqa: E402
from app_db.security import encrypt_secret  # noqa: E402

# The connection the analyst account sees on first login. Host and credentials
# come from the compose environment so this file has nothing machine-specific.
CONNECTION_NAME = os.getenv("DEMO_CONNECTION_NAME", "Demo ERP (sample data)")
DEMO_ENGINE = "postgresql"
DEMO_HOST = os.getenv("DEMO_DB_HOST", "db")
DEMO_PORT = int(os.getenv("DEMO_DB_PORT", "5432"))
DEMO_USER = os.getenv("DEMO_DB_USER", "dbbuddy")
DEMO_PASSWORD = os.getenv("DEMO_DB_PASSWORD", "dbbuddy")
DEMO_DATABASE = os.getenv("DEMO_DB_NAME", "dbbuddy_demo")

# Which account owns the demo connection. Connections are per user, so this has
# to be the account the visitor actually signs in as.
OWNER_EMAIL = os.getenv("DEMO_OWNER_EMAIL", "analyst@dbbuddy.io")


def main() -> None:
    init_db()  # migrations + default org + roles

    # Reuse the existing account seeder rather than duplicating role wiring.
    import seed_test_accounts

    seed_test_accounts.main()

    with SessionLocal() as db:
        owner = db.query(User).filter_by(email=OWNER_EMAIL).one_or_none()
        if owner is None:
            print(f"! {OWNER_EMAIL} not found — skipping the demo connection.")
            return

        conn = (
            db.query(DatabaseConnection)
            .filter_by(user_id=owner.id, name=CONNECTION_NAME)
            .one_or_none()
        )
        if conn is None:
            conn = DatabaseConnection(user_id=owner.id, name=CONNECTION_NAME)
            db.add(conn)
            action = "created"
        else:
            action = "updated"

        conn.engine = DEMO_ENGINE
        conn.host = DEMO_HOST
        conn.port = DEMO_PORT
        conn.username = DEMO_USER
        conn.password_encrypted = encrypt_secret(DEMO_PASSWORD)
        conn.database = DEMO_DATABASE
        db.commit()

        print(
            f"  {action}: connection {CONNECTION_NAME!r} -> "
            f"{DEMO_ENGINE}://{DEMO_HOST}:{DEMO_PORT}/{DEMO_DATABASE} (owner: {OWNER_EMAIL})"
        )

    print("Demo instance ready.")


if __name__ == "__main__":
    main()
