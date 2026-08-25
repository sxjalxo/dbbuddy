"""Audit rows carry a signature, so an edit cannot pass as an original.

`AuditLog` rows were ordinary rows. Anyone with write access to the application
database could change who did what, or what they did, and nothing would show. For
a tool that sits next to an ERP that eventually matters to a compliance reviewer,
and it matters sooner to anyone investigating an incident using these rows as
evidence.

Each row now carries an HMAC over its immutable content, keyed by the server
secret. Someone with database access but not the application key cannot produce a
valid signature, so an edited row fails verification.

## What this catches, and what it does not

**Caught:** any modification to a stored row — actor, action, entity, detail, IP,
timestamp. All of it is signed.

**Not caught:** wholesale deletion of a row. Nothing in a per-row signature says
how many rows there should be.

A hash *chain* would catch deletion, and was deliberately not built. Computing
"the previous row's hash" at insert time requires reading the current tail inside
the writing transaction; two workers doing that concurrently pick the same
predecessor and the chain forks. The verifier would then report tampering on an
honest system — which is worse than no verifier at all, because the first false
alarm teaches everyone to ignore the next one. Closing that needs a
database-assigned monotonic sequence, which is tracked separately.

So this is deliberately the half that can be made correct without serialising
every audited request behind a lock.
"""

import importlib
import os
import pathlib
import sys
import tempfile

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (str(_REPO_ROOT), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_auditsig_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "audit-test-secret-key-long-enough-12345")
os.environ.setdefault("APP_SECRET_KEY", "audit-test-app-secret")

audit_integrity = importlib.import_module("app_db.audit_integrity")
from app_db.models import AuditLog  # noqa: E402


def _row(**over):
    fields = dict(
        id="row-1", user_id="user-1", organization_id="org-1",
        entity_type="report", action="publish", entity_id="report-9",
        detail={"title": "Q3"}, ip_address="10.0.0.1", request_id="req-1",
        created_at="2026-08-25T10:00:00+00:00",
    )
    fields.update(over)
    return AuditLog(**{k: v for k, v in fields.items() if k != "created_at"},
                    created_at=_parse(fields["created_at"]))


def _parse(value):
    from datetime import datetime

    return datetime.fromisoformat(value) if isinstance(value, str) else value


# ── The signature ────────────────────────────────────────────────────────────

def test_a_signed_row_verifies():
    row = _row()
    row.entry_hash = audit_integrity.sign(row)
    assert audit_integrity.verify(row) is True


def test_an_unsigned_row_does_not_verify():
    """Rows written before this existed are reported, not silently accepted."""
    row = _row()
    row.entry_hash = None
    assert audit_integrity.verify(row) is False


@pytest.mark.parametrize("field, value", [
    ("user_id", "someone-else"),
    ("action", "login"),
    ("entity_type", "user"),
    ("entity_id", "report-1"),
    ("ip_address", "192.0.2.1"),
    ("request_id", "req-2"),
])
def test_changing_any_signed_field_breaks_the_signature(field, value):
    row = _row()
    row.entry_hash = audit_integrity.sign(row)
    setattr(row, field, value)
    assert audit_integrity.verify(row) is False


def test_changing_the_detail_breaks_the_signature():
    """The payload is where "what actually happened" lives."""
    row = _row()
    row.entry_hash = audit_integrity.sign(row)
    row.detail = {"title": "something else"}
    assert audit_integrity.verify(row) is False


def test_changing_the_timestamp_breaks_the_signature():
    """Back-dating an event is a tamper like any other."""
    from datetime import datetime, timedelta, timezone

    row = _row()
    row.entry_hash = audit_integrity.sign(row)
    row.created_at = datetime.now(timezone.utc) - timedelta(days=30)
    assert audit_integrity.verify(row) is False


def test_moving_a_row_to_another_id_breaks_the_signature():
    """The id is signed, so a valid signature cannot be lifted onto another row."""
    row = _row()
    row.entry_hash = audit_integrity.sign(row)
    row.id = "row-2"
    assert audit_integrity.verify(row) is False


def test_the_signature_is_keyed_not_merely_a_digest(monkeypatch):
    """An unkeyed hash would let anyone with DB access re-sign what they edited."""
    from app_db.config import settings

    row = _row()
    monkeypatch.setattr(settings, "APP_SECRET_KEY", "the-original-key")
    signed = audit_integrity.sign(row)

    monkeypatch.setattr(settings, "APP_SECRET_KEY", "a-different-key")
    assert audit_integrity.sign(row) != signed


def test_detail_ordering_does_not_change_the_signature():
    """JSON key order is not meaningful and must not read as tampering."""
    a, b = _row(), _row()
    a.detail = {"x": 1, "y": 2}
    b.detail = {"y": 2, "x": 1}
    assert audit_integrity.sign(a) == audit_integrity.sign(b)


# ── The verifier ─────────────────────────────────────────────────────────────

def test_verify_all_reports_the_rows_that_fail():
    rows = [_row(id=f"row-{i}") for i in range(4)]
    for row in rows:
        row.entry_hash = audit_integrity.sign(row)

    rows[2].action = "tampered"

    checked, failures = audit_integrity.verify_rows(rows)
    assert checked == 4
    assert [r.id for r in failures] == ["row-2"]


def test_verify_all_is_clean_on_an_untouched_set():
    rows = [_row(id=f"row-{i}") for i in range(3)]
    for row in rows:
        row.entry_hash = audit_integrity.sign(row)
    assert audit_integrity.verify_rows(rows) == (3, [])


# ── The verifier script ──────────────────────────────────────────────────────

def test_the_verifier_separates_unsigned_rows_from_failed_ones(tmp_path):
    """Three outcomes, not two. Calling an unsigned row "tampered" is wrong, and
    calling it "fine" is worse."""
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app_db.models import Base
    from scripts.verify_audit_log import check

    engine = create_engine("sqlite:///" + str(tmp_path / "audit.db").replace("\\", "/"))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as db:
        good = AuditLog(id="good", action="login", created_at=datetime.now(timezone.utc))
        good.entry_hash = audit_integrity.sign(good)

        edited = AuditLog(id="edited", action="login", created_at=datetime.now(timezone.utc))
        edited.entry_hash = audit_integrity.sign(edited)
        edited.action = "delete"          # after signing

        legacy = AuditLog(id="legacy", action="login", created_at=datetime.now(timezone.utc))

        db.add_all([good, edited, legacy])
        db.commit()

    verified, unsigned, failures, sequence = check(session_factory=Session)
    assert verified == 1
    assert unsigned == 1
    assert [f.id for f in failures] == ["edited"]
    # SQLite assigns no sequence value, so every row is unsequenced and gap
    # detection reports itself unavailable rather than clean. See
    # tests/test_audit_deletion_detection.py for the analysis itself.
    assert sequence.unsequenced == 3 and sequence.present == 0


def test_rows_written_through_write_audit_verify(tmp_path):
    """The real writer, not a hand-built row: id and created_at are ORM defaults,
    and signing before they exist would sign a different row than is stored."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app_db.deps import write_audit
    from app_db.models import Base
    from scripts.verify_audit_log import check

    engine = create_engine("sqlite:///" + str(tmp_path / "written.db").replace("\\", "/"))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as db:
        write_audit(db, user_id="u1", action="publish", entity_type="report",
                    entity_id="r1", detail={"title": "Q3"}, ip_address="10.0.0.1")
        db.commit()

    verified, unsigned, failures, sequence = check(session_factory=Session)
    assert (verified, unsigned, failures) == (1, 0, [])
    assert sequence.missing == 0


def test_a_signature_survives_a_database_round_trip(tmp_path):
    """SQLite returns a naive datetime for a timezone-aware column.

    Signing the raw `isoformat()` meant every row failed verification the moment
    it was read back — an honest system reported as entirely tampered, which is
    the false alarm that makes a verifier worthless. The timestamp is normalised
    to naive UTC before signing.
    """
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app_db.models import Base

    engine = create_engine("sqlite:///" + str(tmp_path / "roundtrip.db").replace("\\", "/"))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        row = AuditLog(id="rt", action="login", created_at=datetime.now(timezone.utc))
        row.entry_hash = audit_integrity.sign(row)
        db.add(row)
        db.commit()

    with Session() as db:
        reloaded = db.query(AuditLog).filter(AuditLog.id == "rt").one()
        assert reloaded.created_at.tzinfo is None, "precondition: SQLite drops the tz"
        assert audit_integrity.verify(reloaded) is True
