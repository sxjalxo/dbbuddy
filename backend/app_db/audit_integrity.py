"""Signatures that make an edited audit row detectable.

``audit_logs`` rows were ordinary rows. Anyone with write access to the
application database could change who did what, and nothing would show — which
matters most in exactly the situation these rows exist for: someone reconstructing
an incident, using them as evidence.

Each row carries an HMAC over its immutable content, keyed by the server secret.
Database access alone is no longer enough to forge one.

## Keyed, not merely hashed

A plain digest would let anyone who could edit a row also recompute its hash. The
key is what separates "can write to the database" from "can write to the database
*and* holds the application secret", and those are usually different people —
often a DBA and a deployment, or an intruder and neither.

It is derived from ``APP_SECRET_KEY`` through a distinct label, so the audit key
is not the encryption key even though both come from one secret. Rotating that
secret invalidates existing signatures: they were made by the old key, and the new
one legitimately cannot verify them. That is a real consequence and is documented
in the rotation procedure rather than worked around, because the alternative —
re-signing during rotation — would mean the rotation tool can forge audit rows.

## What this catches, and what it does not

**Caught:** any modification to a stored row.

**Not caught by the signature alone:** deleting one outright. Nothing in a per-row
signature says how many rows there should be.

A hash *chain* would catch deletion and was deliberately not built. Computing "the
previous row's hash" at insert time means reading the current tail inside the
writing transaction; two workers doing that concurrently choose the same
predecessor and the chain forks. The verifier would then report tampering on an
honest system, and the first false alarm is what teaches everyone to ignore the
next one.

What was built instead is a database-assigned monotonic sequence (``seq``, migration
``0020``), so gaps are visible without any app-side coordination — concurrency is
the database's problem there, and it has already solved it. See "Deletion
detection" below for what a gap is worth and what it still does not close.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timezone
from typing import NamedTuple

from .config import settings

# Distinct from the at-rest encryption key derived from the same secret. Reusing
# one key for two purposes means a weakness in either becomes a weakness in both.
_KEY_LABEL = b"dbbuddy-audit-integrity-v1"

# Every field that describes what happened. Adding a meaningful column to
# AuditLog means adding it here — an unsigned field is one an attacker may edit
# freely, and the omission is invisible until someone relies on it.
_SIGNED_FIELDS = (
    "id", "user_id", "organization_id", "entity_type", "action",
    "entity_id", "ip_address", "request_id",
)


def _key() -> bytes:
    seed = (settings.APP_SECRET_KEY or settings.JWT_SECRET or "").encode("utf-8")
    return hmac.new(_KEY_LABEL, seed, hashlib.sha256).digest()


def _canonical(row) -> bytes:
    """A stable byte representation of one row.

    ``detail`` is serialised with sorted keys: JSON object order is not meaningful,
    and letting it change the signature would report tampering every time an
    unrelated dict happened to be built in a different order.
    """
    payload = {field: getattr(row, field, None) for field in _SIGNED_FIELDS}

    # Normalised to naive UTC with fixed precision, deliberately.
    #
    # The obvious `created_at.isoformat()` is wrong: SQLite hands back a *naive*
    # datetime even for a timezone-aware column, so a row signed with "+00:00" in
    # it fails verification the moment it is read back. Every row would then be
    # reported as tampered on a completely honest system — the exact false alarm
    # that teaches people to ignore the verifier.
    created_at = getattr(row, "created_at", None)
    if created_at is None:
        payload["created_at"] = None
    else:
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
        payload["created_at"] = created_at.strftime("%Y-%m-%dT%H:%M:%S.%f")

    detail = getattr(row, "detail", None)
    payload["detail"] = json.dumps(detail, sort_keys=True, default=str) if detail else None

    return json.dumps(payload, sort_keys=True, default=str).encode("utf-8")


def sign(row) -> str:
    """The signature for this row."""
    return hmac.new(_key(), _canonical(row), hashlib.sha256).hexdigest()


def verify(row) -> bool:
    """Whether this row still matches its signature.

    An unsigned row returns False rather than True: rows written before signing
    existed are *reported*, not silently accepted, so "verified" never quietly
    means "not checked".
    """
    stored = getattr(row, "entry_hash", None)
    if not stored:
        return False
    return hmac.compare_digest(stored, sign(row))


def verify_rows(rows) -> tuple[int, list]:
    """Check many rows. Returns ``(checked, failures)``."""
    failures = [row for row in rows if not verify(row)]
    return len(list(rows)), failures


# ── Deletion detection ────────────────────────────────────────────────────────
#
# ``audit_logs.seq`` is assigned by the database from a sequence, so the rows
# carry 1, 2, 3, … and a missing 2 is visible without the application
# coordinating anything — which is what ruled out the hash chain (see the module
# docstring). The analysis below is a pure function over the values so it can be
# checked exactly, and so it behaves the same wherever the rows came from.
#
# **What a gap is worth.** It is a question, not a verdict. A rolled-back
# transaction consumes a sequence value and leaves a hole in a completely honest
# log; so does a failed insert. Any wording stronger than "possible" would produce
# false accusations, and a check that cries wolf is one people learn to skip.
#
# **What this does not close.** The sequence value is not covered by the row's
# signature, and cannot be: the database assigns it after the signature is
# computed. Someone with write access who deletes a row can therefore renumber the
# survivors and close the gap behind them. What it costs them is rewriting every
# later row rather than running one DELETE, and it leaves the sequence's own
# counter ahead of the data — which ``last_value`` reports, and which is why the
# tail check exists.

class SequenceReport(NamedTuple):
    """What the sequence values say about rows that are no longer there."""

    first: int | None           # lowest value seen, None when there are none
    last: int | None            # highest value seen
    present: int                # distinct values seen
    expected: int               # how many there would be with no gaps: last-first+1
    missing: int                # expected - present
    ranges: list                # [(start, end), …] of absent values, inclusive
    duplicates: int             # values seen more than once — impossible from a sequence
    unsequenced: int            # rows with no value: predate the column, or SQLite
    tail_missing: int           # how far the sequence counter runs ahead of `last`


def sequence_gaps(values, last_value: int | None = None) -> SequenceReport:
    """Analyse a collection of sequence values, in any order.

    ``last_value`` is the sequence's own counter, where the engine has one. Pass
    None when it is unknown (SQLite), and the tail check is skipped rather than
    guessed at.
    """
    seen = set()
    duplicates = 0
    unsequenced = 0

    for value in values:
        if value is None:
            unsequenced += 1
            continue
        if value in seen:
            # Counting a repeat as another present row would let it cancel out a
            # real gap, which is the one thing this must never do.
            duplicates += 1
            continue
        seen.add(value)

    if not seen:
        return SequenceReport(None, None, 0, 0, 0, [], duplicates, unsequenced, 0)

    first, last = min(seen), max(seen)
    expected = last - first + 1
    present = len(seen)

    ranges = []
    if present < expected:
        run_start = None
        for value in range(first, last + 1):
            if value in seen:
                if run_start is not None:
                    ranges.append((run_start, value - 1))
                    run_start = None
            elif run_start is None:
                run_start = value
        if run_start is not None:           # unreachable while `last` is present
            ranges.append((run_start, last))

    # max(0, …): a counter *behind* the data is nonsensical, and must not become
    # a negative count that reads as "rows found".
    tail = max(0, last_value - last) if last_value is not None else 0

    return SequenceReport(first, last, present, expected, expected - present,
                          ranges, duplicates, unsequenced, tail)
