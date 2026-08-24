"""Create demo/test accounts for evaluation. Idempotent — safe to re-run.

Run from the backend/ directory with the app DB configured:

    cd backend
    APP_DATABASE_URL=sqlite:///./dbbuddy_app.db PYTHONPATH=.. python seed_test_accounts.py

Creates one account per supported demo role (all in the default organization) with known
passwords, for demoing the role-branched experiences:

    admin@dbbuddy.io     / Admin#12345     -> Platform admin console (orgs, users, audit)
    orgadmin@dbbuddy.io  / OrgAdmin#12345  -> Org admin console (own-org users, connections, audit)
    analyst@dbbuddy.io   / Analyst#12345   -> Analyst workspace (query/charts)
    client@dbbuddy.io    / Client#12345    -> read-only Reports viewer

These are DEV/DEMO credentials. Do not ship them to production.
"""

import sys

# Allow running as `python seed_test_accounts.py` from backend/.
sys.path.insert(0, ".")

from app_db.database import SessionLocal, init_db  # noqa: E402
from app_db.models import Organization, Role, User  # noqa: E402
from app_db.security import hash_password  # noqa: E402

ACCOUNTS = [
    ("admin@dbbuddy.io", "Admin#12345", "Platform Admin", "admin"),
    ("orgadmin@dbbuddy.io", "OrgAdmin#12345", "Demo Org Admin", "org_admin"),
    ("analyst@dbbuddy.io", "Analyst#12345", "Demo Analyst", "analyst"),
    ("client@dbbuddy.io", "Client#12345", "Demo Client", "user"),
]

# Legacy second analyst demo login. Keep old rows for audit/data references, but
# make re-running this seed remove it as a usable login.
DEPRECATED_ACCOUNTS = ["demo@dbbuddy.io"]


def main() -> None:
    init_db()  # ensure schema + default org + roles exist
    with SessionLocal() as db:
        org = db.query(Organization).filter_by(is_default=True).one()
        for email in DEPRECATED_ACCOUNTS:
            user = db.query(User).filter_by(email=email).one_or_none()
            if user is not None:
                user.is_active = False
                user.roles = []
                print(f"  disabled: {email}  (deprecated demo login)")

        for email, password, full_name, role_name in ACCOUNTS:
            role = db.query(Role).filter_by(name=role_name).one()
            user = db.query(User).filter_by(email=email).one_or_none()
            if user is None:
                user = User(
                    email=email, password_hash=hash_password(password),
                    full_name=full_name, organization_id=org.id,
                )
                db.add(user)
                action = "created"
            else:
                user.password_hash = hash_password(password)  # reset to known value
                action = "updated"
            user.roles = [role]
            print(f"  {action}: {email}  (role={role_name}, password={password})")
        db.commit()
    print("Done. Test accounts ready in the default organization.")


if __name__ == "__main__":
    main()
