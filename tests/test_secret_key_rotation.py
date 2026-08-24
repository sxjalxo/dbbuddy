"""Rotating the at-rest key must not orphan everything encrypted with the old one.

The Fernet key is derived from `APP_SECRET_KEY`. Changing that secret used to make
every stored ERP password, AI provider key and MFA secret permanently unreadable —
no second key was ever tried, and there was no re-encrypt path. So the advice
"rotate your secrets periodically" was, here, "destroy your customers'
connections". In development it already bit: an unset `APP_SECRET_KEY` derives an
ephemeral one that changes on every restart.

The fix is `MultiFernet`: encrypt with the newest key, decrypt with any key still
listed. Rotation becomes a two-step a deployment can actually perform —

1. put the new key first and keep the old one in `APP_SECRET_KEYS_PREVIOUS`;
   everything already stored still decrypts, new writes use the new key;
2. run `scripts/rotate_secrets.py` to re-encrypt at rest, then drop the old key.

Between those steps nothing is unreadable, which is the property that makes the
procedure safe to start.
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

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_rotate_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "rotate-test-secret-key-long-enough-1234")
os.environ.setdefault("APP_SECRET_KEY", "rotate-test-app-secret-primary")

security = importlib.import_module("app_db.security")
from app_db.config import settings as app_settings  # noqa: E402

OLD = "the-retired-secret"
NEW = "the-current-secret"


@pytest.fixture
def keys(monkeypatch):
    """Point the module at a known primary + previous pair."""
    def use(primary: str, previous: list[str]):
        monkeypatch.setattr(app_settings, "APP_SECRET_KEY", primary)
        monkeypatch.setattr(app_settings, "APP_SECRET_KEYS_PREVIOUS", previous)
    return use


def test_a_secret_encrypted_with_the_old_key_still_decrypts(keys):
    keys(OLD, [])
    stored = security.encrypt_secret("hunter2")

    keys(NEW, [OLD])
    assert security.decrypt_secret(stored) == "hunter2"


def test_new_writes_use_the_new_key(keys):
    """Proven by the old key alone no longer being able to read them."""
    keys(NEW, [OLD])
    stored = security.encrypt_secret("fresh")

    keys(OLD, [])
    with pytest.raises(Exception):
        security.decrypt_secret(stored)


def test_dropping_the_old_key_makes_old_ciphertext_unreadable(keys):
    """The reason step 2 exists: retiring a key before re-encrypting loses data."""
    keys(OLD, [])
    stored = security.encrypt_secret("hunter2")

    keys(NEW, [])          # old key retired without re-encrypting
    with pytest.raises(Exception):
        security.decrypt_secret(stored)


def test_rotate_rewrites_ciphertext_to_the_current_key(keys):
    keys(OLD, [])
    stored = security.encrypt_secret("hunter2")

    keys(NEW, [OLD])
    rotated = security.rotate_secret(stored)
    assert rotated != stored

    # Now the old key can be dropped and the value is still readable.
    keys(NEW, [])
    assert security.decrypt_secret(rotated) == "hunter2"


def test_rotating_an_already_current_secret_is_harmless(keys):
    keys(NEW, [OLD])
    stored = security.encrypt_secret("already-current")
    assert security.decrypt_secret(security.rotate_secret(stored)) == "already-current"


def test_several_retired_keys_are_all_accepted(keys):
    """A deployment that rotated twice without re-encrypting is still recoverable."""
    keys("first", [])
    oldest = security.encrypt_secret("from-the-first-key")
    keys("second", ["first"])
    middle = security.encrypt_secret("from-the-second-key")

    keys("third", ["second", "first"])
    assert security.decrypt_secret(oldest) == "from-the-first-key"
    assert security.decrypt_secret(middle) == "from-the-second-key"


def test_the_previous_key_list_is_parsed_from_the_environment(monkeypatch):
    """Comma-separated, whitespace-tolerant, empty entries dropped."""
    from app_db.config import parse_previous_keys

    assert parse_previous_keys("a,b") == ["a", "b"]
    assert parse_previous_keys(" a , b ") == ["a", "b"]
    assert parse_previous_keys("a,,b,") == ["a", "b"]
    assert parse_previous_keys("") == []
    assert parse_previous_keys(None) == []


def test_encryption_is_unchanged_when_no_previous_keys_are_configured(keys):
    """The common case must behave exactly as it did before this feature."""
    keys(NEW, [])
    assert security.decrypt_secret(security.encrypt_secret("plain")) == "plain"


# ── The re-encrypt script ────────────────────────────────────────────────────

def _isolated_db(tmp_path, name):
    """A throwaway database with its own engine.

    Deliberately not `importlib.reload(app_db.database)`: two tests doing that
    fight over module-level engine state, and each passes alone while the pair
    fails. The rotation core takes a session factory precisely so this is
    unnecessary.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app_db.models import Base

    engine = create_engine("sqlite:///" + str(tmp_path / name).replace("\\", "/"))
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_rotate_all_rewrites_every_encrypted_column(keys, tmp_path):
    """End-to-end: store under the old key, rotate, drop the old key, still read.

    Covers all three columns at once, because the failure mode is a column nobody
    remembered to list — and that only surfaces when the retired key is dropped,
    long after the rotation looked successful.
    """
    from app_db.models import AIProviderConfig, DatabaseConnection, Organization, User

    Session = _isolated_db(tmp_path, "rotate_all.db")

    keys(OLD, [])
    with Session() as db:
        org = Organization(name="Rotate Co", slug="rotate-co", is_default=False)
        db.add(org)
        db.flush()
        user = User(email="rotate@example.com", password_hash="x", organization_id=org.id,
                    mfa_secret=security.encrypt_secret("TOTPSEED"))
        db.add(user)
        db.flush()
        db.add(DatabaseConnection(
            user_id=user.id, name="erp", engine="postgresql", host="db",
            username="u", password_encrypted=security.encrypt_secret("erp-password"),
            database="sales",
        ))
        db.add(AIProviderConfig(
            organization_id=org.id, name="p", adapter="openai_compatible",
            model="gpt-test", api_key_encrypted=security.encrypt_secret("sk-provider"),
        ))
        db.commit()

    keys(NEW, [OLD])
    from scripts.rotate_secrets import rotate_all

    rotated, failed = rotate_all(session_factory=Session)
    assert failed == 0
    assert rotated == 3, f"expected all three columns, got {rotated}"

    # The retired key is now droppable — the real test of a rotation.
    keys(NEW, [])
    with Session() as db:
        assert security.decrypt_secret(
            db.query(DatabaseConnection).one().password_encrypted) == "erp-password"
        assert security.decrypt_secret(
            db.query(AIProviderConfig).one().api_key_encrypted) == "sk-provider"
        assert security.decrypt_secret(db.query(User).one().mfa_secret) == "TOTPSEED"


def test_dry_run_writes_nothing(keys, tmp_path):
    from app_db.models import DatabaseConnection, Organization, User

    Session = _isolated_db(tmp_path, "rotate_dry.db")

    keys(OLD, [])
    with Session() as db:
        org = Organization(name="Dry Co", slug="dry-co", is_default=False)
        db.add(org)
        db.flush()
        user = User(email="dry@example.com", password_hash="x", organization_id=org.id)
        db.add(user)
        db.flush()
        db.add(DatabaseConnection(
            user_id=user.id, name="erp", engine="postgresql", host="db", username="u",
            password_encrypted=security.encrypt_secret("erp-password"), database="sales",
        ))
        db.commit()
        before = db.query(DatabaseConnection).one().password_encrypted

    keys(NEW, [OLD])
    from scripts.rotate_secrets import rotate_all

    rotated, failed = rotate_all(dry_run=True, session_factory=Session)
    assert (rotated, failed) == (1, 0)

    with Session() as db:
        assert db.query(DatabaseConnection).one().password_encrypted == before
