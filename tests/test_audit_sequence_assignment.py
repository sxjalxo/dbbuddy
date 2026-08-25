"""The sequence is assigned by the database, or honestly reported as absent.

`test_audit_deletion_detection.py` tests the analysis; this tests the plumbing —
that ``audit_logs.seq`` is actually populated by the engine, that the application
never supplies it, and that an engine without a sequence says so instead of
looking clean.

Engine-conditional on purpose rather than skipped. Locally this runs on SQLite and
asserts the *degraded* contract, which is the one most deployments of a
development instance get. In CI the PostgreSQL application-database job sets
``APP_DATABASE_URL``, migration ``0020`` creates a real ``SEQUENCE``, and the same
test asserts the numbering. A test that only ran on Postgres would leave the
SQLite behaviour — the one that must not silently report "no gaps" — unchecked.
"""

import importlib
import os
import pathlib
import sys
import tempfile
import uuid

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_auditseq_test.db")
os.close(_DB_FD)
# setdefault, not assignment: CI's PostgreSQL job sets this, and that run is the
# whole point of the Postgres branch below.
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "auditseq-test-secret-key-long-enough1")
os.environ.setdefault("APP_SECRET_KEY", "auditseq-test-app-secret")

from fastapi.testclient import TestClient  # noqa: E402

main = importlib.import_module("main")

from app_db.database import SessionLocal, engine  # noqa: E402
from app_db.deps import write_audit  # noqa: E402
from app_db.models import AuditLog  # noqa: E402

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
from verify_audit_log import check  # noqa: E402

ON_POSTGRES = engine.dialect.name == "postgresql"


@pytest.fixture(scope="module")
def app_db():
    """Start the app once so init_db runs the migrations."""
    with TestClient(main.app):
        yield


@pytest.fixture
def written(app_db):
    """Three audit rows, tagged so they can be told apart from any others."""
    marker = f"seqtest-{uuid.uuid4()}"
    with SessionLocal() as db:
        for i in range(3):
            write_audit(db, user_id=None, action="test", entity_type=marker,
                        entity_id=str(i))
        db.commit()

    with SessionLocal() as db:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.entity_type == marker)
            .order_by(AuditLog.created_at)
            .all()
        )
    assert len(rows) == 3
    return rows


def test_the_column_is_declared_as_database_supplied():
    # write_audit fills id and created_at itself because both are signed. seq is
    # not signed and must come from the database; an app-assigned counter would
    # reintroduce the coordination problem that ruled out a hash chain.
    #
    # FetchedValue is how that is declared to SQLAlchemy: it means "the server
    # produces this", so the column is left out of the INSERT and the server
    # default applies. A plain nullable column would have NULL written over it.
    from sqlalchemy import FetchedValue

    column = AuditLog.__table__.c.seq
    assert isinstance(column.server_default, FetchedValue)
    assert column.default is None                 # no Python-side default either


@pytest.mark.skipif(not ON_POSTGRES, reason="needs a PostgreSQL application database")
def test_postgres_assigns_a_contiguous_run(written):
    values = [row.seq for row in written]
    assert all(v is not None for v in values)
    assert sorted(values) == list(range(min(values), min(values) + 3))


@pytest.mark.skipif(not ON_POSTGRES, reason="needs a PostgreSQL application database")
def test_the_verifier_sees_no_gaps_in_an_untouched_log(written):
    _, _, _, report = check(session_factory=SessionLocal)
    assert report.missing == 0
    assert report.duplicates == 0


@pytest.mark.skipif(not ON_POSTGRES, reason="needs a PostgreSQL application database")
def test_deleting_a_row_shows_up_as_a_gap(written):
    victim = written[1]
    with SessionLocal() as db:
        db.query(AuditLog).filter(AuditLog.id == victim.id).delete()
        db.commit()

    _, _, _, report = check(session_factory=SessionLocal)
    assert report.missing >= 1
    assert any(start <= victim.seq <= end for start, end in report.ranges)


@pytest.mark.skipif(ON_POSTGRES, reason="describes the engine without a sequence")
def test_sqlite_leaves_the_column_null_rather_than_faking_it(written):
    assert [row.seq for row in written] == [None, None, None]


@pytest.mark.skipif(ON_POSTGRES, reason="describes the engine without a sequence")
def test_sqlite_reports_detection_unavailable_not_clean(written):
    # The failure mode this guards against: unsequenced rows counted as "no gaps
    # found", so an engine that cannot detect deletion reports a clean bill.
    _, _, _, report = check(session_factory=SessionLocal)
    assert report.present == 0
    assert report.unsequenced >= 3
    assert report.first is None
