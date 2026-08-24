"""Point the engine at a SQLite file as if it were a customer ERP database.

Three seams are patched, matching the three things the engine asks a database
for. Patching lower (e.g. driving the planner directly) would skip the pipeline
being tested; patching higher would need a live MySQL, which makes the loop slow
and non-reproducible.

  ``dbbuddy_core.db.connect_db``      -> a DialectConnection over sqlite3
  ``dbbuddy_core.schema.fetch_schema`` -> ``{table: [col, ...]}``
  ``Dialect.fetch_schema_rich``        -> column types **and declared FKs**

The third matters most: without it ``context_store`` falls back to ``{}`` FKs and
the join graph silently drops to name guessing — so the harness would grade the
fallback path while believing it graded the real one.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

from dbbuddy_core.dialects.schema_meta import (
    ColumnMeta, DatabaseSchema, ForeignKeyMeta, TableMeta,
)


class _Cursor:
    """dict rows + ``%s`` -> ``?`` placeholder translation.

    The compiler emits MySQL-style placeholders and the engine reads dict rows;
    sqlite3 does neither.
    """

    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    def execute(self, sql: str, params=None):
        sql = sql.replace("%s", "?")
        # The engine under test is declared as MySQL, so quoted identifiers come
        # back in backticks. SQLite rejects those; it wants double quotes. Without
        # this every identifier the compiler *correctly* quoted became a syntax
        # error at execution and read as the engine refusing its own SQL.
        sql = sql.replace("`", '"')
        return self._cursor.execute(sql, params) if params else self._cursor.execute(sql)

    def _dict(self, row):
        cols = [d[0] for d in self._cursor.description or []]
        return dict(zip(cols, row))

    def fetchall(self):
        return [self._dict(r) for r in self._cursor.fetchall()]

    def fetchone(self):
        row = self._cursor.fetchone()
        return self._dict(row) if row is not None else None

    def fetchmany(self, size=None):
        # The value-index sampler pulls a bounded page rather than fetchall, and
        # its failures are swallowed by design ("sampling must never block
        # Analyze"). Omitting this made every dimension column raise
        # AttributeError, silently produce an empty index, and present as the
        # *product* being unable to ground a literal.
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._dict(r) for r in rows]

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def close(self):
        self._cursor.close()


class _Connection:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.autocommit = True

    def cursor(self, dictionary: bool = False):
        cur = self._conn.cursor()
        return _Cursor(cur) if dictionary else cur

    def ping(self, reconnect: bool = True):
        self._conn.execute("SELECT 1")

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def raw(self) -> sqlite3.Connection:
        return self._conn


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _q(identifier: str) -> str:
    """Double-quote an identifier for PRAGMA. Reserved words (`order`, `group`),
    hyphens and non-ASCII are all legal table names, and an unquoted PRAGMA on one
    fails with `near "order": syntax error` — which surfaces as the *engine*
    being unable to read the schema."""
    return '"' + identifier.replace('"', '""') + '"'


def fetch_schema(conn) -> dict[str, list[str]]:
    raw = conn.raw() if isinstance(conn, _Connection) else conn
    return {t: [r[1] for r in raw.execute(f"PRAGMA table_info({_q(t)})")]
            for t in _tables(raw)}


def fetch_schema_rich(conn) -> DatabaseSchema:
    raw = conn.raw() if isinstance(conn, _Connection) else conn
    schema = DatabaseSchema()
    for table in _tables(raw):
        columns = [
            ColumnMeta(name=r[1], data_type=r[2] or "TEXT",
                       nullable=not r[3], is_primary_key=bool(r[5]))
            for r in raw.execute(f"PRAGMA table_info({_q(table)})")
        ]
        fks = [
            # PRAGMA foreign_key_list: (id, seq, table, from, to, ...)
            ForeignKeyMeta(column=r[3], referenced_table=r[2],
                           referenced_column=r[4] or "id")
            for r in raw.execute(f"PRAGMA foreign_key_list({_q(table)})")
        ]
        schema.tables[table] = TableMeta(name=table, columns=columns, foreign_keys=fks)
    return schema


@contextmanager
def engine_pointed_at(db_path: str):
    """Run the block with the engine talking to ``db_path``."""

    def _connect(host, user, password, database, engine="mysql", port=None):
        raw = sqlite3.connect(db_path, check_same_thread=False)
        return _Connection(raw)

    # Patch every *concrete* dialect, not the base class: each engine overrides
    # fetch_schema_rich, so patching Dialect alone silently changes nothing and
    # context_store falls back to empty FKs — which downgrades the join graph to
    # name guessing while the harness believes it is grading declared FKs.
    from dbbuddy_core.dialects import registry

    dialect_classes = {type(d) for d in _known_dialects(registry)}
    patches = [
        patch("dbbuddy_core.db.connect_db", _connect),
        patch("dbbuddy_core.schema.fetch_schema", fetch_schema),
    ]
    patches += [
        patch.object(cls, "fetch_schema_rich", lambda self, conn: fetch_schema_rich(conn))
        for cls in dialect_classes
    ]
    started = [p.start() for p in patches]
    try:
        assert started is not None
        yield
    finally:
        for p in reversed(patches):
            p.stop()


def _known_dialects(registry) -> list:
    """Every dialect instance the registry can hand out, best-effort."""
    out = []
    for name in ("mysql", "postgresql", "sqlserver"):
        try:
            out.append(registry.get_dialect(name))
        except Exception:  # noqa: BLE001 — an optional driver may be absent
            continue
    return out


def query(db_path: str, sql: str) -> list[dict]:
    """Run SQL directly, for computing an invariant's expected side."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()
