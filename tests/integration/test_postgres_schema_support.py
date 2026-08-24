"""A PostgreSQL database that keeps its tables outside `public` must still work.

Every introspection query in the dialect was scoped to `table_schema = 'public'`.
A database using a named schema — ordinary for an ERP, and what Hibernate and
Entity Framework produce by default — reported **no tables at all**. Not an error:
an empty schema, and every question failing to ground with "I couldn't match that
to any tables or columns in your database."

Found while building the PostgreSQL dogfood target, which had to be handed a whole
database rather than a schema to work around it.

Two halves to the fix, and both are tested here:

* introspection follows the connection's ``search_path`` rather than assuming
  ``public``, so a role or connection that already selects a schema just works;
* a schema can be named explicitly on the connection, which is what a UI needs —
  a user should not have to alter their role to point DB Buddy at a schema.

Opt-in like the rest of `tests/integration/`:

    docker compose -f docker-compose.test.yml up -d
    pytest -m integration
"""

import os

import pytest

from dbbuddy_core.db import connect_db

pytestmark = pytest.mark.integration

PG = dict(
    host=os.getenv("PG_HOST", "127.0.0.1"),
    user=os.getenv("PG_USER", "postgres"),
    password=os.getenv("PG_PASSWORD", "postgres"),
    database=os.getenv("PG_DB", "testdb"),
    port=int(os.getenv("PG_PORT", "5433")),
)

SCHEMA_NAME = "dbbuddy_named_schema_test"


def _connect(**over):
    params = dict(PG)
    params.update(over)
    conn = connect_db(
        params["host"], params["user"], params["password"], params["database"],
        engine="postgresql", port=params["port"],
        **({"db_schema": params["db_schema"]} if "db_schema" in params else {}),
    )
    if conn is None:
        pytest.skip(f"PostgreSQL not reachable at {params['host']}:{params['port']}")
    return conn


@pytest.fixture(scope="module")
def named_schema():
    """A table that exists only in a named schema."""
    conn = _connect()
    raw = conn._raw
    cur = conn.cursor()
    cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA_NAME}" CASCADE')
    cur.execute(f'CREATE SCHEMA "{SCHEMA_NAME}"')
    cur.execute(
        f'CREATE TABLE "{SCHEMA_NAME}".invoice '
        "(id INT PRIMARY KEY, customer_id INT, total NUMERIC(12,2))"
    )
    cur.execute(
        f'CREATE TABLE "{SCHEMA_NAME}".invoice_line '
        "(id INT PRIMARY KEY, invoice_id INT REFERENCES "
        f'"{SCHEMA_NAME}".invoice(id), amount NUMERIC(12,2))'
    )
    cur.execute(f'INSERT INTO "{SCHEMA_NAME}".invoice VALUES (1, 1, 10.00)')
    try:
        raw.commit()
    except Exception:                              # noqa: BLE001
        pass
    yield SCHEMA_NAME
    try:
        cur = conn.cursor()
        cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA_NAME}" CASCADE')
        raw.commit()
    except Exception:                              # noqa: BLE001
        pass


def test_named_schema_is_invisible_without_being_selected(named_schema):
    """The default connection still sees `public` only — that is correct.

    Pins the boundary: the fix follows the search path, it does not start
    returning every schema in the database.
    """
    conn = _connect()
    schema = conn.dialect.fetch_schema(conn._raw)
    assert "invoice" not in schema


def test_a_schema_named_on_the_connection_is_introspected(named_schema):
    conn = _connect(db_schema=named_schema)
    schema = conn.dialect.fetch_schema(conn._raw)
    assert "invoice" in schema, schema
    assert "invoice_line" in schema, schema
    assert "total" in schema["invoice"]


def test_rich_metadata_follows_the_same_scope(named_schema):
    """Types, primary keys and foreign keys, not just names.

    The join graph is built from declared foreign keys; if `fetch_schema_rich`
    kept its own `public` assumption, tables would resolve and joins would
    silently fall back to name guessing.
    """
    conn = _connect(db_schema=named_schema)
    rich = conn.dialect.fetch_schema_rich(conn._raw)
    tables = rich.tables
    assert "invoice_line" in tables, sorted(tables)
    assert tables["invoice"].primary_keys() == ["id"]
    fks = tables["invoice_line"].foreign_keys
    assert any(fk.referenced_table == "invoice" for fk in fks), fks


def test_queries_execute_against_the_named_schema(named_schema):
    conn = _connect(db_schema=named_schema)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM invoice")
    row = cur.fetchone()
    assert (row["n"] if isinstance(row, dict) else row[0]) == 1
