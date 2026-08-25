"""The dogfood COPY loader against a real PostgreSQL server.

The encoder is unit-tested without a server (`tests/test_dogfood_postgres_copy.py`);
this is the other half — that what the encoder produces is what PostgreSQL stores.
The two halves catch different things. An escaping bug shows up in the unit tests;
a *format* bug — the wrong NULL marker, a bytea that COPY accepts and stores as
the literal text `\\x6162` — only shows up when a server parses it.

Values here are chosen to be the ones that break a naive loader: embedded tabs and
newlines (the COPY delimiters), the literal characters `\\` and `N`, an empty
string that must not become NULL, and non-ASCII text.

Opt-in like the rest of `tests/integration/`:

    docker compose -f docker-compose.test.yml up -d
    pytest -m integration
"""

import os
import sqlite3

import pytest

pytestmark = pytest.mark.integration

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

from scripts.dogfood.postgres_target import (  # noqa: E402
    _connect, load_sqlite_into_postgres,
)

BACKSLASH = chr(92)

# Its own database name, so a run of this test can never touch the database a
# dogfood run is using.
PARAMS = {
    "host": os.getenv("DOGFOOD_PG_HOST", "127.0.0.1"),
    "port": int(os.getenv("DOGFOOD_PG_PORT", "5442")),
    "user": os.getenv("DOGFOOD_PG_USER", "dbbuddy"),
    "password": os.getenv("DOGFOOD_PG_PASSWORD", "dbbuddy"),
    "database": "dbbuddy_copy_loader_test",
}

AWKWARD = [
    (1, "plain", ""),
    (2, "with\ttab", "x"),
    (3, "with\nnewline", "y"),
    (4, BACKSLASH + "N", "z"),               # the literal NULL marker as data
    (5, BACKSLASH + BACKSLASH, "w"),
    (6, "café — 顧客", "v"),
    (7, None, "u"),                          # a real NULL
]


@pytest.fixture
def dataset(tmp_path):
    """A small SQLite file with a parent, a child, and hostile text."""
    path = tmp_path / "copy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, label TEXT, tag TEXT)")
    conn.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER, "
        "amount REAL, flag BOOLEAN, payload BLOB, "
        "FOREIGN KEY (parent_id) REFERENCES parent(id))"
    )
    conn.executemany("INSERT INTO parent VALUES (?, ?, ?)", AWKWARD)
    conn.executemany(
        "INSERT INTO child VALUES (?, ?, ?, ?, ?)",
        [(1, 1, 1.5, 1, b"ab"), (2, 7, None, 0, None)],
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def loaded(dataset):
    try:
        rows = load_sqlite_into_postgres(dataset, PARAMS)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no PostgreSQL at {PARAMS['host']}:{PARAMS['port']} ({exc})")
    conn = _connect(PARAMS)
    yield rows, conn
    conn.close()


def _rows(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()


def test_every_row_arrives(loaded):
    rows, conn = loaded
    assert rows == len(AWKWARD) + 2
    assert _rows(conn, "SELECT count(*) FROM parent")[0][0] == len(AWKWARD)
    assert _rows(conn, "SELECT count(*) FROM child")[0][0] == 2


def test_text_survives_the_round_trip(loaded):
    _, conn = loaded
    stored = dict(_rows(conn, "SELECT id, label FROM parent ORDER BY id"))
    for pk, label, _ in AWKWARD:
        assert stored[pk] == label, f"row {pk} did not round-trip"


def test_null_and_empty_string_stay_distinct(loaded):
    _, conn = loaded
    # Row 7's label is NULL; row 1's tag is the empty string. A CSV-shaped loader
    # collapses these into each other and nothing complains.
    assert _rows(conn, "SELECT label FROM parent WHERE id = 7")[0][0] is None
    assert _rows(conn, "SELECT tag FROM parent WHERE id = 1")[0][0] == ""


def test_the_literal_null_marker_is_not_read_as_null(loaded):
    _, conn = loaded
    assert _rows(conn, "SELECT label FROM parent WHERE id = 4")[0][0] == BACKSLASH + "N"


def test_numbers_bools_and_bytea(loaded):
    _, conn = loaded
    rows = _rows(conn, "SELECT amount, flag, payload FROM child ORDER BY id")
    assert rows[0][0] == 1.5
    assert rows[0][1] is True
    assert bytes(rows[0][2]) == b"ab"
    assert rows[1][0] is None
    assert rows[1][1] is False
    assert rows[1][2] is None


def test_primary_keys_are_created_after_the_load(loaded):
    _, conn = loaded
    # The engine reads declared keys from the target, so a loader that skipped
    # them would change how every plan is built — silently, and only on this
    # target.
    keys = _rows(conn, """
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
        ORDER BY tc.table_name
    """)
    assert ("parent", "id") in keys
    assert ("child", "id") in keys


def test_foreign_keys_survive(loaded):
    _, conn = loaded
    fks = _rows(conn, """
        SELECT tc.table_name
        FROM information_schema.table_constraints tc
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
    """)
    assert ("child",) in fks


def test_a_reload_replaces_rather_than_appends(dataset):
    # The database is dropped and recreated per load, so a second run grades the
    # dataset it thinks it is grading.
    try:
        first = load_sqlite_into_postgres(dataset, PARAMS)
        second = load_sqlite_into_postgres(dataset, PARAMS)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no PostgreSQL at {PARAMS['host']}:{PARAMS['port']} ({exc})")
    assert first == second
    conn = _connect(PARAMS)
    try:
        assert _rows(conn, "SELECT count(*) FROM parent")[0][0] == len(AWKWARD)
    finally:
        conn.close()
