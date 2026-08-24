"""Outbound email, with a sender you can actually run without a SaaS account.

DB Buddy needs to send exactly one kind of message today — a password-reset link —
and needs to do it in three very different situations:

* a developer running the stack locally, who wants the link *visible*, not
  delivered;
* a self-hoster with an SMTP relay;
* a deployment wired to something else entirely.

So the transport is chosen by configuration, and the default is the console. A
missing SMTP host is not an error: it means "print it", which is the honest
behaviour for a stack that has not been told where to send mail. Failing the
request instead would make password reset look broken on every fresh install.

Delivery failures never propagate to the caller. The request endpoint answers
identically whether or not an address exists (see routers/auth.py), and an
exception escaping here would turn a mail-server outage into exactly the
enumeration oracle that design avoids.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def _setting(name: str, default: str = "") -> str:
    """Read at call time so tests and containers can change it without reimport."""
    return (os.getenv(name) or default).strip()


def transport() -> str:
    """``smtp`` when a host is configured, otherwise ``console``."""
    if _setting("SMTP_HOST"):
        return "smtp"
    return "console"


def sender_address() -> str:
    return _setting("EMAIL_FROM", "no-reply@dbbuddy.local")


def app_base_url() -> str:
    """Where the frontend lives, for links that a human has to click.

    Defaults to the dev frontend, matching ``ALLOWED_ORIGINS``.
    """
    return _setting("APP_BASE_URL", "http://localhost:3000").rstrip("/")


def send_email(*, to: str, subject: str, body: str) -> bool:
    """Send one plain-text message. Returns whether it was handed off.

    Never raises: see the module docstring.
    """
    if transport() == "console":
        # Deliberately a log line and not a print: it belongs in the same stream
        # as everything else the backend says, and a container captures it.
        logger.warning(
            "email not configured (SMTP_HOST unset) — printing instead of sending\n"
            "  to:      %s\n  subject: %s\n%s", to, subject, body,
        )
        return True

    message = EmailMessage()
    message["From"] = sender_address()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    host = _setting("SMTP_HOST")
    port = int(_setting("SMTP_PORT", "587") or 587)
    user = _setting("SMTP_USER")
    password = _setting("SMTP_PASSWORD")
    use_tls = _setting("SMTP_STARTTLS", "1").lower() not in {"0", "false", "no"}

    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if use_tls:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
        return True
    except Exception as exc:                      # noqa: BLE001 — see module docstring
        logger.error("failed to send email to %s: %s", to, exc)
        return False


def send_email_verification(*, to: str, token: str, ttl_hours: int) -> bool:
    """Send the address-confirmation link."""
    link = f"{app_base_url()}/verify-email?token={token}"
    body = (
        "Confirm this address to finish setting up your DB Buddy account.\n\n"
        f"{link}\n\n"
        f"The link works once and expires in {ttl_hours} hours.\n\n"
        "If you didn't create an account, you can ignore this message."
    )
    return send_email(to=to, subject="Confirm your DB Buddy address", body=body)


def send_password_reset(*, to: str, token: str, ttl_minutes: int) -> bool:
    """Send the reset link.

    Kept as its own function rather than inlined at the call site so the mail can
    be shaped in one place — and so tests have a seam that carries the token
    without parsing it back out of a message body.
    """
    link = f"{app_base_url()}/reset-password?token={token}"
    body = (
        "Someone asked to reset the password for this DB Buddy account.\n\n"
        f"{link}\n\n"
        f"The link works once and expires in {ttl_minutes} minutes.\n\n"
        "If it wasn't you, nothing has changed and you can ignore this message."
    )
    return send_email(to=to, subject="Reset your DB Buddy password", body=body)
