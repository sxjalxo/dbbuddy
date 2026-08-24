"""Re-encrypt every at-rest secret under the current ``APP_SECRET_KEY``.

Step 2 of a key rotation:

1. Move the old value into ``APP_SECRET_KEYS_PREVIOUS`` and put the new one in
   ``APP_SECRET_KEY``. Nothing becomes unreadable — ``MultiFernet`` decrypts with
   whichever key fits — and new writes already use the new key.
2. **Run this script.** Every stored secret is decrypted with whichever key fits
   and rewritten under the primary one.
3. Remove the old value from ``APP_SECRET_KEYS_PREVIOUS``.

Skipping step 2 and going straight to step 3 permanently destroys every stored ERP
password, AI provider key and MFA secret. That was the only available behaviour
before this script existed, which is why "rotate your secrets periodically" was
advice this system could not survive.

Three columns hold encrypted values:

    database_connections.password_encrypted
    ai_provider_configs.api_key_encrypted
    users.mfa_secret

Usage, from the repo root:

    APP_DATABASE_URL=... APP_SECRET_KEY=<new> APP_SECRET_KEYS_PREVIOUS=<old> \\
        python scripts/rotate_secrets.py --dry-run
    ... python scripts/rotate_secrets.py

``--dry-run`` reports what would change and writes nothing. Run it first: it is
also the check that every row can still be decrypted at all, which is the thing
you want to know *before* dropping a key.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app_db.database import SessionLocal                      # noqa: E402
from app_db.models import AIProviderConfig, DatabaseConnection, User  # noqa: E402
from app_db.security import rotate_secret                     # noqa: E402

# (model, attribute, human label). Adding a new encrypted column means adding it
# here — a column that is not listed is silently left on the old key, and only
# discovered when the key is finally dropped.
TARGETS = [
    (DatabaseConnection, "password_encrypted", "connection password"),
    (AIProviderConfig, "api_key_encrypted", "AI provider key"),
    (User, "mfa_secret", "MFA secret"),
]


def rotate_all(dry_run: bool = False, session_factory=None) -> tuple[int, int]:
    """Rewrite every stored secret. Returns ``(rotated, failed)``.

    ``session_factory`` defaults to the app's own. It is a parameter so this can
    be exercised against a throwaway database without reaching into module-level
    engine state — which is both how the tests use it and the reason they do not
    interfere with each other.
    """
    rotated = 0
    failed = 0
    factory = session_factory or SessionLocal

    with factory() as db:
        for model, attribute, label in TARGETS:
            rows = db.query(model).all()
            for row in rows:
                current = getattr(row, attribute, None)
                if not current:
                    continue
                try:
                    fresh = rotate_secret(current)
                except Exception as exc:              # noqa: BLE001
                    # Reported, not raised: one unreadable row must not stop the
                    # rest from being rotated, and the operator needs the full
                    # list to decide whether it is safe to drop the old key.
                    failed += 1
                    print(f"  ! {label} {getattr(row, 'id', '?')}: {type(exc).__name__}: {exc}")
                    continue
                if not dry_run:
                    setattr(row, attribute, fresh)
                rotated += 1

        if dry_run:
            db.rollback()
        else:
            db.commit()

    return rotated, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and write nothing")
    args = parser.parse_args()

    # ASCII only. This is run from an operator's console during a key rotation,
    # and a Windows terminal on a legacy code page raises UnicodeEncodeError on a
    # decorative arrow — turning a routine procedure into a crash at the worst
    # possible moment.
    mode = "DRY RUN - nothing will be written" if args.dry_run else "rewriting secrets"
    print(f"-> {mode}")

    rotated, failed = rotate_all(dry_run=args.dry_run)

    print(f"\n{rotated} secret(s) {'would be ' if args.dry_run else ''}rotated, {failed} failed")
    if failed:
        print(
            "\nSome rows could not be decrypted with any configured key. Do NOT drop\n"
            "the old key from APP_SECRET_KEYS_PREVIOUS — the listed rows would be\n"
            "unrecoverable. Check that every retired key is still listed."
        )
        return 1
    if not args.dry_run and rotated:
        print("\nSafe to remove the retired key(s) from APP_SECRET_KEYS_PREVIOUS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
