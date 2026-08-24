"""Check every audit row against its signature.

Each row in ``audit_logs`` carries an HMAC over its content, keyed by the server
secret (``app_db/audit_integrity.py``). This walks them and reports any that no
longer match — meaning the row was changed after it was written by someone who did
not hold the key.

Run it periodically, and run it before relying on these rows as evidence.

    APP_DATABASE_URL=... APP_SECRET_KEY=... python scripts/verify_audit_log.py
    ... python scripts/verify_audit_log.py --since 2026-08-01

Exit codes: ``0`` all signed rows verified, ``1`` at least one failed.

## Reading the output honestly

**Unsigned** rows are counted separately from **failed** ones. A row written
before signing existed has no signature, and reporting that as tampering would be
wrong; reporting it as fine would be worse. It is reported as what it is:
unverifiable.

**A failure is not proof of malice.** Restoring a backup taken under a different
`APP_SECRET_KEY`, or rotating that secret, invalidates signatures made by the old
key — the rows are intact, the key that vouched for them is gone. Check that
before concluding anything. What a failure does mean is that these rows can no
longer be used as evidence on their own.

**Deletion is not detected.** A per-row signature says nothing about how many rows
there should be. See ``app_db/audit_integrity.py`` for why a chain was not built
and what would be needed to close that.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app_db.audit_integrity import verify                    # noqa: E402
from app_db.database import SessionLocal                     # noqa: E402
from app_db.models import AuditLog                           # noqa: E402


def check(since: datetime | None = None, session_factory=None) -> tuple[int, int, list]:
    """Verify rows. Returns ``(verified, unsigned, failures)``.

    ``session_factory`` is a parameter so this can run against a throwaway
    database without touching module-level engine state.
    """
    factory = session_factory or SessionLocal
    verified = 0
    unsigned = 0
    failures = []

    with factory() as db:
        query = db.query(AuditLog).order_by(AuditLog.created_at)
        if since is not None:
            query = query.filter(AuditLog.created_at >= since)

        for row in query.yield_per(500):
            if not row.entry_hash:
                unsigned += 1
                continue
            if verify(row):
                verified += 1
            else:
                failures.append(row)

    return verified, unsigned, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="only check rows at or after this ISO date")
    args = parser.parse_args()

    since = datetime.fromisoformat(args.since) if args.since else None
    verified, unsigned, failures = check(since=since)

    print(f"verified: {verified}")
    if unsigned:
        print(f"unsigned: {unsigned}  (written before signing existed; not a failure)")

    if failures:
        print(f"\nFAILED: {len(failures)} row(s) no longer match their signature\n")
        for row in failures[:50]:
            print(f"  {row.created_at}  {row.id}  actor={row.user_id} "
                  f"action={row.action} entity={row.entity_type}")
        if len(failures) > 50:
            print(f"  ... and {len(failures) - 50} more")
        print(
            "\nBefore concluding tampering: a backup restored under a different\n"
            "APP_SECRET_KEY, or a rotation of that secret, invalidates signatures\n"
            "made by the old key. Either way these rows cannot stand as evidence\n"
            "on their own."
        )
        return 1

    print("\nAll signed rows verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
