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

**Size.** Copying is row-by-row over a single connection, which is fine for the
generated sets (`erp`, `hospital`, `legacy`, `tpch`) and unreasonable for the
large imported ones (`employees` at ~4M rows, `airportdb` at up to ~59M). Use
this target for shape, and SQLite for scale.

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
                              batch: int = 1000) -> int:
    """Copy a dataset from its SQLite file into PostgreSQL. Returns the row count.

    The target database is dropped and recreated first, so a run always grades the
    dataset it thinks it is grading rather than whatever a previous run left
    behind. Tables land in ``public`` because that is the only schema the dialect
    introspects.
    """
    from psycopg2.extras import execute_values

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

        for table in ordered:
            cols = ", ".join(
                f"{_quote(name)} {pg_type}" for name, pg_type, _ in columns[table]
            )
            pk = ""
            if pks[table]:
                pk = ", PRIMARY KEY (" + ", ".join(_quote(c) for c in pks[table]) + ")"
            cur.execute(f"CREATE TABLE {_quote(table)} ({cols}{pk})")

        # Foreign keys after every table exists, so a cycle or an out-of-order
        # dependency cannot stop the schema being created.
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

        for table in ordered:
            names = [name for name, _, _ in columns[table]]
            col_list = ", ".join(_quote(n) for n in names)
            insert = f"INSERT INTO {_quote(table)} ({col_list}) VALUES %s"
            cursor = src.execute(f"SELECT {col_list} FROM {_quote(table)}")
            while True:
                rows = cursor.fetchmany(batch)
                if not rows:
                    break
                execute_values(cur, insert, rows)
                total += len(rows)

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
