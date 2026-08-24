"""CLI authorization + connection-resolution logic.

The CLI is analyst-only: every data command must refuse an account that lacks
``query:run`` with a clear message and a non-zero exit. These tests drive that
logic with a fake session, so no backend or network is involved.
"""

import pytest

import dbbuddy.main as cli


class FakeSession:
    def __init__(self, permissions, roles=("analyst",), connections=None):
        self._me = {"email": "u@x.io", "roles": list(roles), "permissions": list(permissions)}
        self._connections = list(connections or [])
        self.api_url = "http://localhost:8000"

    def me(self):
        return self._me

    def list_connections(self):
        return self._connections


# ── _require_cli_access ────────────────────────────────────────────────────────

def test_require_cli_access_allows_analyst():
    me = cli._require_cli_access(FakeSession(["query:run", "schema:analyze"]))
    assert me["email"] == "u@x.io"


def test_require_cli_access_denies_without_query_run(capsys):
    with pytest.raises(SystemExit) as exc:
        cli._require_cli_access(FakeSession(["report:view"], roles=["user"]))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "only available to accounts with the 'query:run' permission" in err
    assert "roles: user" in err


def test_require_cli_access_reports_auth_required(capsys):
    class Unauth(FakeSession):
        def me(self):
            raise cli.AuthRequired("Not logged in. Run 'dbbuddy login'.")

    with pytest.raises(SystemExit) as exc:
        cli._require_cli_access(Unauth([]))
    # sys.exit(msg) carries the message as the exit code (a string), which is
    # what a shell prints to stderr and treats as a failure.
    assert "Not logged in" in str(exc.value)


# ── _resolve_connection_payload ────────────────────────────────────────────────

class _Args:
    def __init__(self, **kw):
        self.connection = kw.get("connection")
        self.host = kw.get("host")
        self.user = kw.get("user")
        self.password = kw.get("password")
        self.database = kw.get("database")
        self.engine = kw.get("engine")
        self.port = kw.get("port")


def test_resolve_connection_by_name():
    sess = FakeSession(["query:run"], connections=[{"id": "abc-123", "name": "prod"}])
    payload = cli._resolve_connection_payload(sess, _Args(connection="prod"))
    assert payload == {"connection_id": "abc-123"}


def test_resolve_connection_by_id():
    sess = FakeSession(["query:run"], connections=[{"id": "abc-123", "name": "prod"}])
    payload = cli._resolve_connection_payload(sess, _Args(connection="abc-123"))
    assert payload == {"connection_id": "abc-123"}


def test_resolve_connection_unknown_name_exits(capsys):
    sess = FakeSession(["query:run"], connections=[{"id": "abc-123", "name": "prod"}])
    with pytest.raises(SystemExit) as exc:
        cli._resolve_connection_payload(sess, _Args(connection="staging"))
    assert "No saved connection" in str(exc.value)


def test_resolve_connection_inline_credentials_warns(capsys):
    sess = FakeSession(["query:run"])
    payload = cli._resolve_connection_payload(
        sess, _Args(host="db.local", user="erp", password="pw", database="sales", engine="mysql")
    )
    assert payload["host"] == "db.local" and payload["database"] == "sales"
    assert "connection_id" not in payload
    assert "not saved" in capsys.readouterr().err


def test_resolve_connection_none_selected_exits(capsys):
    sess = FakeSession(["query:run"])
    with pytest.raises(SystemExit) as exc:
        cli._resolve_connection_payload(sess, _Args())
    assert "No connection selected" in str(exc.value)
