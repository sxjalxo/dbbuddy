"""Point the engine at a real PostgreSQL database instead of the SQLite shim.

The SQLite target is fast and needs no server, and it graded the engine's answers
well for a long time. What it cannot grade is the SQL itself. It declares the
engine as MySQL and then rewrites the statement on its way to SQLite — ``%s`` to
``?``, backticks to double quotes — so a statement that PostgreSQL would reject
outright can execute happily and return rows.

That is not hypothetical. ``ORDER BY `SUM(payments.amount)` `` became
``ORDER BY "SUM(payments.amount)"``, which SQLite reads as a *string constant*:
no error, no sorting, plausible rows. On PostgreSQL:

    ERROR:  column "SUM(payments.amount)" does not exist

"Top N X by Y" was broken on the documented production engine while eight dogfood
datasets stayed green.

So this module runs the same suites against a real PostgreSQL server, with the
engine declared as ``postgresql`` and **nothing rewritten**. No patching either:
the real dialect does its own introspection, which means the FK metadata and the
join graph come from PostgreSQL rather than from a shim that might disagree with
it.

The dataset builders stay untouched. They know how to produce a SQLite file, so
that file is copied into PostgreSQL — schema, declared types, primary keys,
foreign keys and rows — and the suites then run against the copy.

**Size.** Rows go in through ``COPY … FROM STDIN``, streamed straight out of the
SQLite cursor, so the large imported datasets (`employees` at ~4M rows,
`airportdb` at up to ~59M) load in bounded memory and at bulk-load speed rather
than one ``INSERT`` batch at a time. Constraints follow the data — tables are
created bare, rows are copied, and the primary keys and foreign keys are added
afterwards — because maintaining a unique index per row is most of the cost of
loading a large table, and PostgreSQL builds the same index far faster in one
pass at the end.

A constraint that only fails *after* the load is a deliberate trade. The failure
is louder that way (``ALTER TABLE`` names the constraint and the offending row)
rather than surfacing as a mid-copy error on row 4 million.

**Why a whole database rather than a schema.** The PostgreSQL dialect scopes every
introspection query to ``table_schema = 'public'``, so a dataset loaded into a named
schema is invisible to the engine — it reports no tables at all. That is a real
product limitation (a PostgreSQL ERP using named schemas cannot be queried today),
tracked separately; here it just means the harness gets its own database and loads
into ``public``, which it drops and recreates per run.

Configuration comes from the environment, defaulting to the demo stack's server:

    DOGFOOD_PG_HOST  DOGFOOD_PG_PORT  DOGFOOD_PG_USER
    DOGFOOD_PG_PASSWORD  DOGFOOD_PG_DB  DOGFOOD_PG_ADMIN_DB
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Dict, List, Tuple

# A database of its own, dropped and recreated per run, so a run always grades the
# dataset it thinks it is grading and can never collide with the application
# database or with a previous run's leftovers.
def pg_params() -> Dict[str, object]:
    """Connection parameters for the target database."""
    return {
        "host": os.getenv("DOGFOOD_PG_HOST", "127.0.0.1"),
        "port": int(os.getenv("DOGFOOD_PG_PORT", "5442")),
        "user": os.getenv("DOGFOOD_PG_USER", "dbbuddy"),
        "password": os.getenv("DOGFOOD_PG_PASSWORD", "dbbuddy"),
        "database": os.getenv("DOGFOOD_PG_DB", "dbbuddy_dogfood"),
    }


def _admin_params() -> Dict[str, object]:
    """Same server, a database we are not about to drop."""
    params = pg_params()
    params["database"] = os.getenv("DOGFOOD_PG_ADMIN_DB", "postgres")
    return params


def recreate_database(params: Dict[str, object] | None = None) -> None:
    """Drop and recreate the dogfood database. Destructive, by design.

    Only ever touches the database named by ``DOGFOOD_PG_DB`` — which defaults to
    a dedicated name, not the application database.
    """
    params = params or pg_params()
    target = params["database"]
    conn = _connect(_admin_params())
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (target,),
        )
        cur.execute(f"DROP DATABASE IF EXISTS {_quote(str(target))}")
        cur.execute(f"CREATE DATABASE {_quote(str(target))}")
    finally:
        conn.close()


def _connect(params: Dict[str, object]):
    import psycopg2

    return psycopg2.connect(
        host=params["host"], port=params["port"], user=params["user"],
        password=params["password"], dbname=params["database"],
    )


# SQLite's declared types are advisory, but the dataset builders do declare them,
# and they are the only type information available. Anything unrecognised becomes
# TEXT: a wrong-but-permissive type produces a copy that loads, where a wrong-and-
# strict one produces a copy that fails to load and looks like an engine defect.
_TYPE_MAP: List[Tuple[Tuple[str, ...], str]] = [
    (("INT",), "BIGINT"),
    (("CHAR", "CLOB", "TEXT"), "TEXT"),
    (("BLOB",), "BYTEA"),
    (("REAL", "FLOA", "DOUB"), "DOUBLE PRECISION"),
    (("DECIMAL", "NUMERIC"), "NUMERIC"),
    (("BOOL",), "BOOLEAN"),
    (("DATETIME", "TIMESTAMP"), "TIMESTAMP"),
    (("DATE",), "DATE"),
    # TIME maps to TEXT, not to PostgreSQL's TIME. SQLite has no time type, so a
    # builder that declares one is describing intent rather than storage — TPC-DS
    # declares `dv_create_time TIME` and stores an integer number of seconds,
    # which PostgreSQL rejects outright. TEXT accepts whatever is actually there;
    # the alternative is a copy that will not load and reads as an engine defect.
    (("TIME",), "TEXT"),
]


# How many rows to inspect before trusting a declared type. SQLite's typing is
# dynamic, so a builder can (and TPC-DS does) declare `INT` and store "brand #3",
# or declare `TIME` and store an integer. PostgreSQL rejects both. Sampling is
# cheap, deterministic, and turns "the copy failed halfway" into "that column is
# text after all".
_TYPE_SAMPLE_ROWS = 500


def _is_compatible(value, pg_type: str) -> bool:
    """Whether one value can be stored in a column of ``pg_type``."""
    if value is None:
        return True
    if pg_type == "TEXT":
        return True
    if pg_type in ("BIGINT", "DOUBLE PRECISION", "NUMERIC"):
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        try:
            float(str(value))
            return True
        except (TypeError, ValueError):
            return False
    if pg_type == "BOOLEAN":
        return isinstance(value, bool) or str(value).lower() in {
            "0", "1", "true", "false", "t", "f", "yes", "no",
        }
    if pg_type in ("DATE", "TIMESTAMP"):
        # ISO text or an epoch-ish number; anything else is not a date to PostgreSQL.
        text = str(value)
        return len(text) >= 8 and text[:4].isdigit() and not text.isdigit()
    if pg_type == "BYTEA":
        return isinstance(value, (bytes, bytearray, memoryview))
    return True


def _verified_types(src, table: str,
                    columns: "List[Tuple[str, str, bool]]") -> "List[Tuple[str, str, bool]]":
    """Downgrade any column whose data contradicts its declared type to TEXT."""
    names = [name for name, _, _ in columns]
    if not names:
        return columns
    col_list = ", ".join(_quote(n) for n in names)
    rows = src.execute(
        f"SELECT {col_list} FROM {_quote(table)} LIMIT {_TYPE_SAMPLE_ROWS}"
    ).fetchall()
    if not rows:
        return columns

    verified = []
    for index, (name, pg_type, pk) in enumerate(columns):
        if pg_type != "TEXT" and not all(_is_compatible(row[index], pg_type) for row in rows):
            pg_type = "TEXT"
        verified.append((name, pg_type, pk))
    return verified


def _pg_type(declared: str) -> str:
    d = (declared or "").upper()
    # DATETIME before DATE, and NUMERIC before INT, so a longer name is not
    # shadowed by a shorter substring match.
    for needles, pg in _TYPE_MAP:
        if any(n in d for n in needles):
            return pg
    return "TEXT"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


# ── COPY encoding ─────────────────────────────────────────────────────────────
#
# COPY's default text format is tab-separated, newline-terminated, with backslash
# escapes and ``\N`` for NULL. It is faster than CSV to produce and to parse, and
# unlike CSV it distinguishes NULL from the empty string without any quoting
# rules at all.
#
# Every escape below is load-bearing on real data. `legacy` stores non-ASCII and
# separator-only-different identifiers, and the AdventureWorks CSVs carry embedded
# tabs; an unescaped tab does not raise, it shifts every later column by one and
# lands a dataset that grades wrong. That is the failure this format makes easy to
# get wrong and cheap to get right.

_ESCAPES = (
    # Backslash first, always: escaping it after the others would double the
    # backslash they just introduced, turning a real newline into the literal
    # two characters and vice versa.
    (chr(92), chr(92) * 2),
    ("\b", chr(92) + "b"),
    ("\f", chr(92) + "f"),
    ("\n", chr(92) + "n"),
    ("\r", chr(92) + "r"),
    ("\t", chr(92) + "t"),
    ("\v", chr(92) + "v"),
)

_NULL = chr(92) + "N"


def _copy_encode(value) -> str:
    """One SQLite value as a COPY text-format field."""
    if value is None:
        return _NULL
    # bool before int: bool subclasses int, and PostgreSQL will not accept "1"
    # for a BOOLEAN column from COPY.
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, (bytes, bytearray, memoryview)):
        # bytea hex input is \x…, and that backslash is itself escaped.
        return chr(92) * 2 + "x" + bytes(value).hex()
    if not isinstance(value, str):
        return str(value)
    for char, escape in _ESCAPES:
        value = value.replace(char, escape)
    return value


def _copy_row(row) -> str:
    """One SQLite row as a COPY text-format line, terminator included."""
    return "\t".join(_copy_encode(v) for v in row) + "\n"


class _CopyStream:
    """A read()-able view over a SQLite cursor, for ``copy_expert``.

    psycopg2 pulls from this rather than being handed a buffer, which is what
    keeps memory flat: at any moment it holds one ``fetchmany`` batch plus one
    partial line, whether the table has a thousand rows or fifty-nine million.
    """

    def __init__(self, cursor, batch: int = 10_000):
        self._cursor = cursor
        self._batch = batch
        self._buffer = ""
        self._exhausted = False
        self.rows = 0

    def _fill(self) -> bool:
        """Pull one batch into the buffer. False when the cursor is spent."""
        if self._exhausted:
            return False
        rows = self._cursor.fetchmany(self._batch)
        if not rows:
            self._exhausted = True
            return False
        self._buffer += "".join(_copy_row(row) for row in rows)
        self.rows += len(rows)
        return True

    def read(self, size: int = -1) -> str:
        if size is None or size < 0:
            while self._fill():
                pass
            out, self._buffer = self._buffer, ""
            return out
        while len(self._buffer) < size and self._fill():
            pass
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out

    def readline(self, size: int = -1) -> str:
        while "\n" not in self._buffer and self._fill():
            pass
        if not self._buffer:
            return ""
        index = self._buffer.find("\n")
        if index < 0:                           # no terminator left; return the rest
            out, self._buffer = self._buffer, ""
            return out
        out, self._buffer = self._buffer[:index + 1], self._buffer[index + 1:]
        return out


def _sqlite_tables(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _order_by_dependency(tables: List[str], fks: Dict[str, List[str]]) -> List[str]:
    """Parents before children, so foreign keys can be created with the tables.

    A cycle (rare, but legal) falls back to source order; the FK is added anyway
    and PostgreSQL will reject it only if the data genuinely violates it.
    """
    ordered: List[str] = []
    remaining = list(tables)
    while remaining:
        progressed = False
        for table in list(remaining):
            parents = [p for p in fks.get(table, []) if p in remaining and p != table]
            if not parents:
                ordered.append(table)
                remaining.remove(table)
                progressed = True
        if not progressed:                      # cycle
            ordered.extend(remaining)
            break
    return ordered


def load_sqlite_into_postgres(sqlite_path: str, params: Dict[str, object] | None = None,
                              batch: int = 10_000) -> int:
    """Copy a dataset from its SQLite file into PostgreSQL. Returns the row count.

    The target database is dropped and recreated first, so a run always grades the
    dataset it thinks it is grading rather than whatever a previous run left
    behind. Tables land in ``public`` because that is the only schema the dialect
    introspects.

    Order is bare tables → ``COPY`` → primary keys → foreign keys. Rows arrive
    with no index to maintain and no constraint to check per row, and PostgreSQL
    then builds each index in one pass. ``batch`` is the SQLite fetch size, not a
    statement size: one ``COPY`` per table streams the whole thing.
    """
    params = params or pg_params()
    recreate_database(params)
    src = sqlite3.connect(sqlite_path)
    src.row_factory = None

    tables = _sqlite_tables(src)
    columns: Dict[str, List[Tuple[str, str, bool]]] = {}
    pks: Dict[str, List[str]] = {}
    fk_defs: Dict[str, List[Tuple[str, str, str]]] = {}
    fk_parents: Dict[str, List[str]] = {}

    for table in tables:
        info = src.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
        declared = [(row[1], _pg_type(row[2]), bool(row[5])) for row in info]
        columns[table] = _verified_types(src, table, declared)
        pks[table] = [row[1] for row in info if row[5]]
        fks = src.execute(f"PRAGMA foreign_key_list({_quote(table)})").fetchall()
        # (id, seq, table, from, to, on_update, on_delete, match)
        fk_defs[table] = [(row[3], row[2], row[4]) for row in fks]
        fk_parents[table] = [row[2] for row in fks]

    ordered = _order_by_dependency(tables, fk_parents)

    dest = _connect(params)
    dest.autocommit = False
    total = 0
    try:
        cur = dest.cursor()

        # Bare tables: no primary key yet, so COPY has no unique index to
        # maintain per row. The keys are added once the data is in.
        for table in ordered:
            cols = ", ".join(
                f"{_quote(name)} {pg_type}" for name, pg_type, _ in columns[table]
            )
            cur.execute(f"CREATE TABLE {_quote(table)} ({cols})")

        for table in ordered:
            names = [name for name, _, _ in columns[table]]
            col_list = ", ".join(_quote(n) for n in names)
            cursor = src.execute(f"SELECT {col_list} FROM {_quote(table)}")
            stream = _CopyStream(cursor, batch=batch)
            cur.copy_expert(f"COPY {_quote(table)} ({col_list}) FROM STDIN", stream)
            total += stream.rows

        # Primary keys after the rows. ADD PRIMARY KEY implies NOT NULL, which
        # SQLite does not enforce on a non-INTEGER key column — so a file with a
        # NULL in a declared key fails here rather than at row 4,000,000, and the
        # error names the table.
        for table in ordered:
            if not pks[table]:
                continue
            key = ", ".join(_quote(c) for c in pks[table])
            cur.execute(
                f"ALTER TABLE {_quote(table)} ADD CONSTRAINT "
                f"{_quote(f'pk_{table}')} PRIMARY KEY ({key})"
            )

        # Foreign keys last: every table exists and is populated, so a cycle or an
        # out-of-order dependency cannot stop the schema being created, and each
        # constraint is validated in one pass instead of row by row.
        for table in ordered:
            for i, (from_col, parent, to_col) in enumerate(fk_defs[table]):
                if parent not in columns:
                    continue                    # references a table the file lacks
                target = to_col or (pks.get(parent) or [None])[0]
                if not target:
                    continue
                cur.execute(
                    f"ALTER TABLE {_quote(table)} ADD CONSTRAINT "
                    f"{_quote(f'fk_{table}_{i}')} FOREIGN KEY ({_quote(from_col)}) "
                    f"REFERENCES {_quote(parent)} ({_quote(target)})"
                )

        dest.commit()
    except Exception:
        dest.rollback()
        raise
    finally:
        src.close()
        dest.close()

    return total


@contextmanager
def engine_pointed_at_postgres(params: Dict[str, object] | None = None):
    """Run the block with the engine talking to the copied dataset.

    Nothing is patched, and that is the whole point: the *real* PostgreSQL dialect
    connects, introspects and executes. A shim intercepting any of those would
    reintroduce exactly the blind spot this target exists to remove.

    It is a context manager for symmetry with ``engine_pointed_at`` — the SQLite
    target has teardown to do, this one does not.
    """
    yield


def query(sql: str, params_: Dict[str, object] | None = None) -> List[Dict]:
    """Run a statement directly, for the invariant checks."""
    conn = _connect(params_ or pg_params())
    try:
        cur = conn.cursor()
        cur.execute(sql)
        names = [d[0] for d in cur.description or []]
        return [dict(zip(names, row)) for row in cur.fetchall()]
    finally:
        conn.close()
