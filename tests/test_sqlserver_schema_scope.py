"""SQL Server introspection is scoped to one schema, like every other dialect.

It was scoped to none. Every `INFORMATION_SCHEMA` query ran without a
`TABLE_SCHEMA` filter, so a database with `sales.orders` and `hr.orders` produced
a single `orders` whose column list was the two tables' columns concatenated:

    tables discovered: ['orders']
    columns for 'orders': ['id', 'total', 'id', 'employee_id']

`employee_id` then looks like a column of `sales.orders`, and the planner will
happily join on it. Silent, and strictly worse than the PostgreSQL defect it sits
beside in the roadmap — that one reported *no* tables, which at least fails loudly.

**Why not a port of the PostgreSQL fix.** There, `search_path` is set on the
session, so discovery and execution cannot drift: an unqualified name resolves to
the schema that was introspected. T-SQL has no session-level equivalent — a user's
default schema is a property of the user, changed by DDL. So the scope here is
`SCHEMA_NAME()`, the connecting user's own default schema, which gives the same
guarantee by the same mechanism: the schema that resolves unqualified names is the
schema that was read. An explicit `db_schema` that is not that schema is refused
rather than silently ignored or half-honoured.

psycopg2-style mocking throughout; no server.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

with patch.dict(sys.modules, {"pymssql": MagicMock()}):
    from dbbuddy_core.dialects.sqlserver import SQLServerDialect


@pytest.fixture
def dialect():
    return SQLServerDialect()


class RecordingCursor:
    """Records the SQL it is asked to run and replays canned answers."""

    def __init__(self, answers):
        self.answers = answers
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        for needle, rows in self.answers.items():
            if needle in sql:
                self._rows = rows
                return
        self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


def _conn(answers):
    cursor = RecordingCursor(answers)
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


# ── The collision ─────────────────────────────────────────────────────────────

def test_two_tables_of_one_name_no_longer_merge(dialect):
    # The defect itself. Scoped introspection asks the server for one schema, so
    # only one `orders` comes back and its columns are its own.
    conn, cursor = _conn({
        "INFORMATION_SCHEMA.TABLES": [("orders",)],
        "INFORMATION_SCHEMA.COLUMNS": [("id",), ("total",)],
    })
    schema = dialect.fetch_schema(conn)
    assert list(schema) == ["orders"]
    assert schema["orders"] == ["id", "total"]


def test_the_table_query_is_scoped_to_a_schema(dialect):
    conn, cursor = _conn({"INFORMATION_SCHEMA.TABLES": []})
    dialect.fetch_schema(conn)
    table_query = next(sql for sql, _ in cursor.executed if "INFORMATION_SCHEMA.TABLES" in sql)
    assert "TABLE_SCHEMA" in table_query


def test_the_column_query_is_scoped_to_a_schema(dialect):
    # The one that actually produced the merged column list.
    conn, cursor = _conn({
        "INFORMATION_SCHEMA.TABLES": [("orders",)],
        "INFORMATION_SCHEMA.COLUMNS": [("id",)],
    })
    dialect.fetch_schema(conn)
    column_query = next(sql for sql, _ in cursor.executed if "INFORMATION_SCHEMA.COLUMNS" in sql)
    assert "TABLE_SCHEMA" in column_query


def test_rich_schema_scopes_every_query(dialect):
    conn, cursor = _conn({
        "INFORMATION_SCHEMA.TABLES": [("orders",)],
        "INFORMATION_SCHEMA.COLUMNS": [("id", "int", "NO", None)],
        "TABLE_CONSTRAINTS": [],
        "REFERENTIAL_CONSTRAINTS": [],
    })
    dialect.fetch_schema_rich(conn)
    unscoped = [sql for sql, _ in cursor.executed
                if "INFORMATION_SCHEMA" in sql and "TABLE_SCHEMA" not in sql]
    assert not unscoped, f"unscoped introspection query: {unscoped}"


def test_foreign_keys_do_not_cross_schemas(dialect):
    # A constraint name is unique per schema, not per database: joining
    # KEY_COLUMN_USAGE on name alone can pair a foreign key with a *different*
    # schema's constraint and invent a relationship that does not exist.
    conn, cursor = _conn({
        "INFORMATION_SCHEMA.TABLES": [("orders",)],
        "INFORMATION_SCHEMA.COLUMNS": [("id", "int", "NO", None)],
        "TABLE_CONSTRAINTS": [],
        "REFERENTIAL_CONSTRAINTS": [],
    })
    dialect.fetch_schema_rich(conn)
    fk_query = next(sql for sql, _ in cursor.executed if "REFERENTIAL_CONSTRAINTS" in sql)
    assert fk_query.count("TABLE_SCHEMA") >= 2, "FK join is not schema-scoped on both sides"


# ── Choosing the schema ───────────────────────────────────────────────────────

def test_the_default_scope_is_the_session_schema(dialect):
    # SCHEMA_NAME(), not the literal 'dbo': a user whose default schema is
    # something else must see their own tables, and pinning dbo would be the same
    # class of mistake as PostgreSQL's hardcoded 'public'.
    conn, cursor = _conn({"INFORMATION_SCHEMA.TABLES": []})
    dialect.fetch_schema(conn)
    table_query = next(sql for sql, _ in cursor.executed if "INFORMATION_SCHEMA.TABLES" in sql)
    assert "SCHEMA_NAME()" in table_query


def test_an_explicit_schema_is_used_when_it_is_the_session_schema(dialect):
    conn = MagicMock()
    conn.cursor.return_value = RecordingCursor({"SCHEMA_NAME": [("sales",)]})
    dialect.verify_schema(conn, "sales")        # must not raise


def test_an_explicit_schema_that_is_not_the_session_schema_is_refused(dialect):
    # Refused, not ignored. T-SQL cannot switch the schema that resolves
    # unqualified names, so "honouring" it would mean introspecting one schema and
    # executing against another — discovery and execution drifting apart, which is
    # the exact failure the PostgreSQL fix exists to prevent.
    conn = MagicMock()
    conn.cursor.return_value = RecordingCursor({"SCHEMA_NAME": [("dbo",)]})
    with pytest.raises(ValueError) as exc:
        dialect.verify_schema(conn, "sales")
    message = str(exc.value)
    assert "sales" in message and "dbo" in message


def test_the_refusal_says_what_to_do_about_it(dialect):
    conn = MagicMock()
    conn.cursor.return_value = RecordingCursor({"SCHEMA_NAME": [("dbo",)]})
    with pytest.raises(ValueError) as exc:
        dialect.verify_schema(conn, "sales")
    assert "DEFAULT_SCHEMA" in str(exc.value), "an error a user cannot act on is noise"


def test_no_explicit_schema_verifies_nothing(dialect):
    # The common case: no schema requested, so there is nothing to disagree with
    # and no reason to spend a round trip.
    conn = MagicMock()
    cursor = RecordingCursor({})
    conn.cursor.return_value = cursor
    dialect.verify_schema(conn, None)
    assert cursor.executed == []


def test_comparison_is_case_insensitive(dialect):
    # SQL Server identifiers are case-insensitive under the usual collations;
    # refusing "Sales" against "sales" would be a false alarm.
    conn = MagicMock()
    conn.cursor.return_value = RecordingCursor({"SCHEMA_NAME": [("sales",)]})
    dialect.verify_schema(conn, "SALES")        # must not raise
