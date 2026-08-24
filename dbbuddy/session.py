"""Backend session for the DB Buddy CLI.

The CLI is a first-class API client: it authenticates once, caches its tokens
securely, and then calls the same REST endpoints the web application uses. It
never talks to an ERP database directly (that is the backend's job) and keeps no
platform state of its own — so anything an analyst does from the CLI shows up in
the web app, and vice-versa.

Credentials live in ``~/.dbbuddy/credentials.json`` (0600). A long-lived
personal API key is the durable credential; short-lived JWT access/refresh
tokens are derived from it (or from an interactive login) and refreshed
transparently.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import requests

DEFAULT_API_URL = "http://localhost:8000"
_TIMEOUT = 30


class SessionError(Exception):
    """A recoverable session/auth problem with a user-facing message."""


class AuthRequired(SessionError):
    """No usable credentials — the user must run ``dbbuddy login`` first."""


def _config_dir() -> Path:
    # Overridable so tests (and CI) never touch a real home directory.
    override = os.environ.get("DBBUDDY_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".dbbuddy"


def _creds_path() -> Path:
    return _config_dir() / "credentials.json"


def load_credentials() -> dict:
    path = _creds_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_credentials(data: dict) -> None:
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = _creds_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Best-effort lock-down of the token file (no-op semantics on some Windows FS).
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def clear_credentials() -> None:
    path = _creds_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def resolve_api_url(cli_arg: str | None = None) -> str:
    """--api-url > $DBBUDDY_API_URL > stored > default."""
    url = cli_arg or os.environ.get("DBBUDDY_API_URL") or load_credentials().get("api_url") or DEFAULT_API_URL
    return url.rstrip("/")


class Session:
    """Authenticated HTTP client for the DB Buddy backend."""

    def __init__(self, api_url: str | None = None):
        self.api_url = resolve_api_url(api_url)
        creds = load_credentials()
        self.access_token: str | None = creds.get("access_token")
        self.refresh_token: str | None = creds.get("refresh_token")
        # An API key from the environment always wins — the automation story.
        self.api_key: str | None = os.environ.get("DBBUDDY_API_KEY") or creds.get("api_key")

    # ── persistence ────────────────────────────────────────────────────────
    def _persist(self) -> None:
        creds = load_credentials()
        creds.update({
            "api_url": self.api_url,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
        })
        # Never persist an env-provided key; only an explicitly stored one.
        if self.api_key and not os.environ.get("DBBUDDY_API_KEY"):
            creds["api_key"] = self.api_key
        save_credentials(creds)

    # ── low-level HTTP ───────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self.api_url}{path}"

    def _raw(self, method: str, path: str, *, auth: bool = True, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        if auth:
            if not self.access_token:
                self._ensure_token()
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            return requests.request(method, self._url(path), headers=headers, timeout=_TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise SessionError(
                f"Could not reach the DB Buddy backend at {self.api_url}: {exc}\n"
                f"Is the backend running? Set the address with --api-url or DBBUDDY_API_URL."
            ) from exc

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Authenticated request with one transparent token refresh on 401."""
        resp = self._raw(method, path, auth=True, **kwargs)
        if resp.status_code == 401:
            self._refresh_or_reexchange()
            resp = self._raw(method, path, auth=True, **kwargs)
        return resp

    @staticmethod
    def _detail(resp: requests.Response) -> str:
        try:
            return resp.json().get("detail") or resp.text
        except (json.JSONDecodeError, ValueError):
            return resp.text or f"HTTP {resp.status_code}"

    # ── auth ──────────────────────────────────────────────────────────────────
    def _ensure_token(self) -> None:
        if self.access_token:
            return
        if self.api_key:
            self._exchange_api_key()
            return
        raise AuthRequired("Not logged in. Run 'dbbuddy login' or 'dbbuddy login --api-key'.")

    def _refresh_or_reexchange(self) -> None:
        """Refresh the access token; fall back to re-exchanging the API key."""
        if self.refresh_token:
            resp = self._raw("POST", "/auth/refresh", auth=False,
                             json={"refresh_token": self.refresh_token})
            if resp.status_code == 200:
                body = resp.json()
                self.access_token = body["access_token"]
                self.refresh_token = body.get("refresh_token", self.refresh_token)
                self._persist()
                return
        if self.api_key:
            self._exchange_api_key()
            return
        raise AuthRequired("Your session expired. Run 'dbbuddy login' again.")

    def _exchange_api_key(self) -> None:
        resp = self._raw("POST", "/auth/keys/exchange", auth=False, json={"api_key": self.api_key})
        if resp.status_code != 200:
            raise AuthRequired(f"API key rejected: {self._detail(resp)}")
        body = resp.json()
        self.access_token = body["access_token"]
        self.refresh_token = body["refresh_token"]
        self._persist()

    def login_password(self, email: str, password: str) -> dict:
        """Password login. Returns the raw LoginResult (may require MFA)."""
        resp = self._raw("POST", "/auth/login", auth=False, json={"email": email, "password": password})
        if resp.status_code != 200:
            raise SessionError(f"Login failed: {self._detail(resp)}")
        body = resp.json()
        if not body.get("mfa_required"):
            self.access_token = body["access_token"]
            self.refresh_token = body["refresh_token"]
            self._persist()
        return body

    def complete_mfa(self, challenge_token: str, code: str) -> None:
        resp = self._raw("POST", "/auth/mfa/login", auth=False,
                         json={"challenge_token": challenge_token, "code": code})
        if resp.status_code != 200:
            raise SessionError(f"MFA verification failed: {self._detail(resp)}")
        body = resp.json()
        self.access_token = body["access_token"]
        self.refresh_token = body["refresh_token"]
        self._persist()

    def login_api_key(self, api_key: str) -> None:
        self.api_key = api_key.strip()
        self._exchange_api_key()  # validate immediately and cache tokens

    def logout(self) -> None:
        # Best-effort server-side audit; always clear locally.
        try:
            if self.access_token:
                self._raw("POST", "/auth/logout", auth=True)
        except SessionError:
            pass
        self.access_token = self.refresh_token = self.api_key = None
        clear_credentials()

    # ── typed helpers ──────────────────────────────────────────────────────
    def me(self) -> dict:
        resp = self.request("GET", "/auth/me")
        if resp.status_code != 200:
            raise SessionError(f"Could not fetch account: {self._detail(resp)}")
        return resp.json()

    def create_key(self, name: str) -> dict:
        resp = self.request("POST", "/auth/keys", json={"name": name})
        if resp.status_code not in (200, 201):
            raise SessionError(f"Could not create key: {self._detail(resp)}")
        return resp.json()

    def list_keys(self) -> list[dict]:
        resp = self.request("GET", "/auth/keys")
        if resp.status_code != 200:
            raise SessionError(f"Could not list keys: {self._detail(resp)}")
        return resp.json()

    def revoke_key(self, key_id: str) -> None:
        resp = self.request("DELETE", f"/auth/keys/{key_id}")
        if resp.status_code not in (204, 200):
            raise SessionError(f"Could not revoke key: {self._detail(resp)}")

    def list_connections(self) -> list[dict]:
        resp = self.request("GET", "/connections")
        if resp.status_code != 200:
            raise SessionError(f"Could not list connections: {self._detail(resp)}")
        return resp.json()

    def create_connection(self, payload: dict) -> dict:
        resp = self.request("POST", "/connections", json=payload)
        if resp.status_code not in (200, 201):
            raise SessionError(f"Could not create connection: {self._detail(resp)}")
        return resp.json()

    def delete_connection(self, connection_id: str) -> None:
        resp = self.request("DELETE", f"/connections/{connection_id}")
        if resp.status_code not in (204, 200):
            raise SessionError(f"Could not delete connection: {self._detail(resp)}")

    def list_charts(self) -> list[dict]:
        resp = self.request("GET", "/charts")
        if resp.status_code != 200:
            raise SessionError(f"Could not list charts: {self._detail(resp)}")
        return resp.json()

    def update_chart(self, chart_id: str, payload: dict) -> dict:
        resp = self.request("PATCH", f"/charts/{chart_id}", json=payload)
        if resp.status_code != 200:
            raise SessionError(f"Could not update chart: {self._detail(resp)}")
        return resp.json()

    def publish_chart(self, chart_id: str, visibility: str = "organization") -> dict:
        resp = self.request("POST", f"/charts/{chart_id}/publish", json={"visibility": visibility})
        if resp.status_code not in (200, 201):
            raise SessionError(f"Could not publish chart: {self._detail(resp)}")
        return resp.json()

    def unpublish_chart(self, chart_id: str) -> None:
        resp = self.request("POST", f"/charts/{chart_id}/unpublish")
        if resp.status_code not in (200, 204):
            raise SessionError(f"Could not unpublish chart: {self._detail(resp)}")

    def query(self, payload: dict) -> dict:
        resp = self.request("POST", "/query", json=payload)
        if resp.status_code != 200:
            raise SessionError(self._detail(resp))
        return resp.json()

    def analyze(self, payload: dict) -> dict:
        resp = self.request("POST", "/analyze", json=payload)
        if resp.status_code != 200:
            raise SessionError(self._detail(resp))
        return resp.json()

    def insights(self, payload: dict) -> dict:
        resp = self.request("POST", "/insights/generate", json=payload)
        if resp.status_code != 200:
            raise SessionError(self._detail(resp))
        return resp.json()

    def insights_ask(self, payload: dict) -> dict:
        resp = self.request("POST", "/insights/ask", json=payload)
        if resp.status_code != 200:
            raise SessionError(self._detail(resp))
        return resp.json()
