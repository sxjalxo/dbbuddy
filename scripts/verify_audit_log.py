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

**Deletion is reported as a question, never a verdict.** Rows carry a
database-assigned sequence number, so a missing one is visible — but a rolled-back
transaction consumes a sequence value too, and leaves an identical hole in a
completely honest log. A gap is a prompt to go and look, not a finding.

Two limits worth knowing before you rely on it. The sequence value is **not
signed** — the database assigns it after the signature is computed — so someone
who can delete a row can also renumber the survivors; what that costs them is
rewriting every later row rather than running one DELETE, and it leaves the
sequence counter ahead of the data, which is reported separately as a tail
discrepancy. And on SQLite there is no sequence at all: those rows are reported as
unsequenced, and gap detection says so rather than reporting a clean bill.

With ``--since``, gaps are computed only within the rows fetched. Ordering is by
``created_at`` while numbering is by the sequence, and under concurrency the two
can disagree by a row or two, so a gap right at the window edge may be an artifact
of the window. Re-run without ``--since`` before believing one.
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

from app_db.audit_integrity import sequence_gaps, verify     # noqa: E402
from app_db.database import SessionLocal                     # noqa: E402
from app_db.models import AuditLog                           # noqa: E402

# Matches migration 0020. Named here rather than imported so the script keeps
# working against a database whose migration files have moved on.
SEQUENCE_NAME = "audit_logs_seq"


def _sequence_last_value(db) -> int | None:
    """The sequence's own counter, or None where the engine has no sequence.

    Read separately from the rows: it is what makes a *renumbering* visible,
    since closing a gap by rewriting later rows leaves the counter ahead of the
    data it numbered.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return None
    from sqlalchemy import text

    try:
        return db.execute(text(f"SELECT last_value FROM {SEQUENCE_NAME}")).scalar()
    except Exception:
        # No sequence (migration not applied, or a restored dump that dropped
        # it). Unknown is reported as unknown; the gap scan still runs.
        return None


def check(since: datetime | None = None, session_factory=None):
    """Verify rows. Returns ``(verified, unsigned, failures, sequence_report)``.

    ``session_factory`` is a parameter so this can run against a throwaway
    database without touching module-level engine state.
    """
    factory = session_factory or SessionLocal
    verified = 0
    unsigned = 0
    failures = []
    seq_values = []

    with factory() as db:
        query = db.query(AuditLog).order_by(AuditLog.created_at)
        if since is not None:
            query = query.filter(AuditLog.created_at >= since)

        for row in query.yield_per(500):
            # Collected for every row, signed or not: a deleted row is missing
            # from the numbering regardless of whether its neighbours are signed.
            seq_values.append(row.seq)
            if not row.entry_hash:
                unsigned += 1
                continue
            if verify(row):
                verified += 1
            else:
                failures.append(row)

        report = sequence_gaps(seq_values, last_value=_sequence_last_value(db))

    return verified, unsigned, failures, report


def _report_sequence(report, *, windowed: bool = False) -> None:
    """Print what the numbering says. Never returns a verdict — see the caveats.

    Deliberately does not affect the exit code. A gap has an innocent explanation
    (a rolled-back transaction consumes a sequence value), and a script that exits
    non-zero on one would be red on healthy systems until someone silenced it —
    at which point it stops reporting the real thing too. Signature failures are
    different: those have no innocent explanation except a key change, which the
    output already names.
    """
    if report.unsequenced and not report.present:
        print(f"sequence: {report.unsequenced} row(s) carry no sequence number — "
              "gap detection unavailable (SQLite, or written before migration 0020)")
        return

    if report.unsequenced:
        print(f"sequence: {report.unsequenced} row(s) predate the sequence and are "
              "outside this check")

    if report.present:
        print(f"sequence: {report.present} numbered row(s), {report.first}–{report.last}")

    if report.duplicates:
        print(f"\nWARNING: {report.duplicates} repeated sequence value(s). A sequence "
              "does not issue the same number twice — the column was written by "
              "something other than the database.")

    if report.missing:
        shown = ", ".join(
            str(start) if start == end else f"{start}–{end}"
            for start, end in report.ranges[:20]
        )
        more = "" if len(report.ranges) <= 20 else f" (+{len(report.ranges) - 20} more)"
        print(f"\nPOSSIBLE DELETION: {report.missing} sequence value(s) absent: {shown}{more}")
        print(
            "  A gap is a question, not a verdict — a rolled-back transaction\n"
            "  consumes a sequence value and leaves the same hole in an honest log.\n"
            "  Check whether a failed write or a rollback happened at that time."
        )
        if windowed:
            print(
                "  --since was used: rows are ordered by created_at but numbered by\n"
                "  the sequence, so a gap at the edge of the window may be an\n"
                "  artifact of the window. Re-run without --since to be sure."
            )

    if report.tail_missing:
        print(f"\nPOSSIBLE DELETION AT THE TAIL: the sequence counter is "
              f"{report.tail_missing} ahead of the highest row.")
        print(
            "  Rolled-back transactions explain small differences. A large one, or\n"
            "  one alongside a contiguous run of rows, is what renumbering to hide\n"
            "  a deletion looks like — the survivors were rewritten, the counter\n"
            "  was not."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="only check rows at or after this ISO date")
    args = parser.parse_args()

    since = datetime.fromisoformat(args.since) if args.since else None
    verified, unsigned, failures, sequence = check(since=since)

    print(f"verified: {verified}")
    if unsigned:
        print(f"unsigned: {unsigned}  (written before signing existed; not a failure)")

    _report_sequence(sequence, windowed=since is not None)

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
