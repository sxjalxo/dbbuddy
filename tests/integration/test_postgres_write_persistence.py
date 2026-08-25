"""A write executed against PostgreSQL has to still be there afterwards.

`connect_db` bounds every connection with `SET statement_timeout`. On psycopg2 that
statement **opens a transaction**, so the very next thing callers do —
`conn.autocommit = True`, to keep a write from holding locks — raised
``set_session cannot be used inside a transaction``. Both call sites swallowed the
exception, so the write ran inside a transaction nobody committed and vanished
when the connection closed. No error anywhere; `/execute` reported
"1 row(s) affected".

Invisible on SQLite, which autocommits, so the whole unit suite stayed green —
the same class of defect as the `ORDER BY "SUM(...)"` one. It needs a real
PostgreSQL to see, which is what this is.

Opt-in like the rest of `tests/integration/`:

    docker compose -f docker-compose.test.yml up -d postgres
    pytest -m integration
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("psycopg2", reason="psycopg2 not installed")

from dbbuddy_core.db import connect_db  # noqa: E402

PG = dict(
    host=os.getenv("PG_HOST", "127.0.0.1"),
    user=os.getenv("PG_USER", "postgres"),
    password=os.getenv("PG_PASSWORD", "postgres"),
    database=os.getenv("PG_DB", "testdb"),
    port=int(os.getenv("PG_PORT", "5433")),
)


def _connect():
    conn = connect_db(PG["host"], PG["user"], PG["password"], PG["database"],
                      engine="postgresql", port=PG["port"])
    if conn is None:
        pytest.skip(f"no PostgreSQL at {PG['host']}:{PG['port']}")
    return conn


@pytest.fixture
def table():
    name = f"write_probe_{uuid.uuid4().hex[:8]}"
    yield name
    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {name}")
    cur.close()
    conn.close()


def test_autocommit_can_be_enabled_after_connecting():
    # The failing call itself. connect_db must not hand back a connection with a
    # transaction already open on it.
    conn = _connect()
    try:
        conn.autocommit = True                  # raised ProgrammingError
        assert conn.autocommit is True
    finally:
        conn.close()


def test_a_write_survives_the_connection_closing(table):
    # The consequence, and the thing that actually mattered: the row was gone.
    writer = _connect()
    try:
        writer.autocommit = True
        cur = writer.cursor()
        cur.execute(f"CREATE TABLE {table} (id int)")
        cur.execute(f"INSERT INTO {table} VALUES (1)")
        cur.close()
    finally:
        writer.close()

    reader = _connect()
    try:
        cur = reader.cursor()
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == 1
        cur.close()
    finally:
        reader.close()


def test_the_statement_timeout_is_still_in_force():
    # The fix commits the SET rather than dropping it. A timeout that quietly
    # reverted would trade a data-loss bug for an unbounded-query bug — the
    # ceiling this connection is supposed to carry.
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SHOW statement_timeout")
        value = cur.fetchone()[0]
        cur.close()
        assert value not in ("0", 0), "statement_timeout was lost"
    finally:
        conn.close()


def test_the_timeout_survives_switching_to_autocommit():
    # SET without LOCAL is session-scoped, but a SET inside an uncommitted
    # transaction is undone by a rollback. Committing it is what makes it stick.
    conn = _connect()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SHOW statement_timeout")
        value = cur.fetchone()[0]
        cur.close()
        assert value not in ("0", 0), "statement_timeout was lost on autocommit"
    finally:
        conn.close()
