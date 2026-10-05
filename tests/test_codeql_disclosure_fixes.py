"""Three disclosure paths CodeQL flagged, each verified rather than assumed.

* **Driver text reaching the client.** `/analyze` returns a 502 whose `detail` is
  the `DatabaseUnavailableError` message, and one raise site interpolated the raw
  driver exception into it. Driver messages routinely embed the DSN, host, port and
  username of the *customer's* database — the reconnaissance an
  authenticated-but-untrusted caller wants, and useless to a legitimate one. Every
  other raise site was already a fixed string; this one was not.

* **A reset token written to the log in production.** With `SMTP_HOST` unset the
  email sender logs the message instead, body included — and a password-reset body
  contains a single-use account-takeover link. That is the right behaviour for a
  development instance and unacceptable for a real one, so the console transport is
  now refused under `DBBUDDY_ENV=production` rather than silently logging secrets.

* **A credentials file created world-readable.** The CLI wrote its token file and
  *then* chmod'ed it, leaving a window in which any local user could read it.
"""

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch


_REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_REPO_ROOT), str(_REPO_ROOT / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── Driver text must not reach the client ─────────────────────────────────────

DRIVER_LEAK = (
    'connection to server at "erp-prod.internal" (10.2.3.4), port 5432 failed: '
    'FATAL: password authentication failed for user "svc_reporting"'
)


def test_the_analyze_failure_message_omits_the_driver_text():
    from dbbuddy_core import pipeline

    message = pipeline._analysis_failure_message(
        engine="postgresql", database="erp", host="erp-prod.internal",
        exc=RuntimeError(DRIVER_LEAK),
    )
    assert "svc_reporting" not in message
    assert "10.2.3.4" not in message
    assert "password authentication failed" not in message


def test_the_analyze_failure_message_still_says_what_failed():
    # An error a user cannot act on is noise. The connection details are the
    # caller's own — they are what identifies which connection broke.
    from dbbuddy_core import pipeline

    message = pipeline._analysis_failure_message(
        engine="postgresql", database="erp", host="erp-prod.internal",
        exc=RuntimeError(DRIVER_LEAK),
    )
    assert "erp" in message and "postgresql" in message


def test_the_driver_text_is_logged_rather_than_discarded(caplog):
    # It is what an operator needs; it just belongs in the log, not the response.
    from dbbuddy_core import pipeline

    with caplog.at_level("ERROR"):
        pipeline._analysis_failure_message(
            engine="postgresql", database="erp", host="h",
            exc=RuntimeError(DRIVER_LEAK),
        )
    assert any("svc_reporting" in r.getMessage() for r in caplog.records)


# ── A reset token must not be logged in production ────────────────────────────

# `console_transport_allowed` reads the environment at call time, like every other
# setting in that module, so these need no reimport. Reloading app_db.config under
# DBBUDDY_ENV=production would trip its production fail-fast (missing JWT_SECRET,
# SQLite database, ...) and test the wrong thing entirely.
import app_db.email as email  # noqa: E402


def test_console_transport_is_refused_in_production(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("DBBUDDY_ENV", "production")
    assert email.console_transport_allowed() is False


def test_console_transport_is_fine_in_development(monkeypatch):
    # A fresh install must not look broken; this is why the transport exists.
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("DBBUDDY_ENV", "development")
    assert email.console_transport_allowed() is True


def test_an_unset_environment_defaults_to_permitting_it(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("DBBUDDY_ENV", raising=False)
    assert email.console_transport_allowed() is True


def test_a_production_send_without_smtp_does_not_log_the_body(monkeypatch, caplog):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("DBBUDDY_ENV", "production")
    with caplog.at_level("WARNING"):
        sent = email.send_email(to="a@b.io", subject="Reset your password",
                                body="Open https://app/reset?token=SECRET-TOKEN-123")
    assert sent is False, "a message that was not delivered must not report success"
    assert not any("SECRET-TOKEN-123" in r.getMessage() for r in caplog.records)


def test_a_development_send_still_shows_the_link(monkeypatch, caplog):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("DBBUDDY_ENV", "development")
    with caplog.at_level("WARNING"):
        sent = email.send_email(to="a@b.io", subject="Reset your password",
                                body="Open https://app/reset?token=SECRET-TOKEN-123")
    assert sent is True
    assert any("SECRET-TOKEN-123" in r.getMessage() for r in caplog.records)


# ── The credentials file must never exist world-readable ──────────────────────

def test_the_credentials_file_is_created_restricted(tmp_path, monkeypatch):
    from dbbuddy import session

    monkeypatch.setattr(session, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(session, "_creds_path", lambda: tmp_path / "credentials.json")

    session.save_credentials({"access_token": "secret"})
    path = tmp_path / "credentials.json"
    assert path.exists()

    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"credentials are {oct(mode)}, readable by others"


def test_the_file_is_opened_restricted_not_chmodded_afterwards(tmp_path, monkeypatch):
    # The window is the point: creating it with the default umask and *then*
    # tightening it leaves an interval in which any local user can read the token.
    from dbbuddy import session

    monkeypatch.setattr(session, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(session, "_creds_path", lambda: tmp_path / "credentials.json")

    with patch.object(session.os, "open", wraps=session.os.open) as opened:
        session.save_credentials({"access_token": "secret"})

    assert opened.called, "the file is not being opened with an explicit mode"
    mode = opened.call_args[0][2] if len(opened.call_args[0]) > 2 else None
    assert mode == 0o600, f"opened with mode {mode!r}, expected 0o600"


def test_the_round_trip_still_works(tmp_path, monkeypatch):
    from dbbuddy import session

    monkeypatch.setattr(session, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(session, "_creds_path", lambda: tmp_path / "credentials.json")

    session.save_credentials({"access_token": "secret", "refresh_token": "r"})
    assert session.load_credentials()["access_token"] == "secret"
