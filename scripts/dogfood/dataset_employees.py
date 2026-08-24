"""Fourth dogfood dataset: the real MySQL ``employees`` sample DB.

Unlike the first three (all synthesized in Python), this loads the canonical
``employees`` test database published by Giuseppe Maxia
(https://github.com/datacharmer/test_db) — ~4M rows across six tables — from its
MySQL dump files into SQLite. It exists to exercise the engine at *scale* and on
a schema shaped the way real operational databases are, not toy stars:

* **~2.8M-row fact table** (``salaries``) 1:N under a 300k-row dimension
  (``employees``). The breakdown-sums-to-total invariant on this is the whole
  point — a plan that joins before aggregating inflates the sum by millions.
* **Composite primary keys, no surrogate ``id`` anywhere** — ``salaries`` keys on
  ``(emp_no, from_date)``, ``dept_emp`` / ``dept_manager`` on ``(emp_no,
  dept_no)``, ``titles`` on ``(emp_no, title, from_date)``. Nothing to default
  ``id`` grouping onto.
* **Declared foreign keys named ``_no``, not ``_id``** — ``emp_no`` ->
  ``employees``, ``dept_no`` -> ``departments``. The naming heuristic cannot see
  these; only the declared-FK / declared-PK graph tiers relate the tables.
* **A ``CHAR(4)`` string key** (``dept_no`` = ``'d001'``) and an **``ENUM``
  gender** ('M'/'F') — a low-cardinality dimension bare literals should ground on.
* **Two foreign keys out of one table** (``dept_emp`` -> employees *and*
  departments), the fan-out shape that trips join-path selection.

The dump files are not in the repo (165MB); ``build`` reads them from
``data/employees/dumps`` by default, override with ``$EMPLOYEES_DUMP_DIR``.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

# SQLite-adapted schema. Types simplified from the MySQL original (ENUM -> TEXT,
# CHAR/VARCHAR -> TEXT) but the **keys and foreign keys are preserved verbatim**,
# because they are exactly what the engine is being tested against. FKs are what
# ``PRAGMA foreign_key_list`` reads back into the relationship graph.
DDL = """
CREATE TABLE employees (
    emp_no      INTEGER NOT NULL,
    birth_date  DATE    NOT NULL,
    first_name  TEXT    NOT NULL,
    last_name   TEXT    NOT NULL,
    gender      TEXT    NOT NULL,
    hire_date   DATE    NOT NULL,
    PRIMARY KEY (emp_no)
);

CREATE TABLE departments (
    dept_no     TEXT NOT NULL,
    dept_name   TEXT NOT NULL,
    PRIMARY KEY (dept_no),
    UNIQUE (dept_name)
);

CREATE TABLE dept_manager (
    emp_no    INTEGER NOT NULL,
    dept_no   TEXT    NOT NULL,
    from_date DATE    NOT NULL,
    to_date   DATE    NOT NULL,
    PRIMARY KEY (emp_no, dept_no),
    FOREIGN KEY (emp_no)  REFERENCES employees   (emp_no),
    FOREIGN KEY (dept_no) REFERENCES departments (dept_no)
);

CREATE TABLE dept_emp (
    emp_no    INTEGER NOT NULL,
    dept_no   TEXT    NOT NULL,
    from_date DATE    NOT NULL,
    to_date   DATE    NOT NULL,
    PRIMARY KEY (emp_no, dept_no),
    FOREIGN KEY (emp_no)  REFERENCES employees   (emp_no),
    FOREIGN KEY (dept_no) REFERENCES departments (dept_no)
);

CREATE TABLE titles (
    emp_no    INTEGER NOT NULL,
    title     TEXT    NOT NULL,
    from_date DATE    NOT NULL,
    to_date   DATE,
    PRIMARY KEY (emp_no, title, from_date),
    FOREIGN KEY (emp_no) REFERENCES employees (emp_no)
);

CREATE TABLE salaries (
    emp_no    INTEGER NOT NULL,
    salary    INTEGER NOT NULL,
    from_date DATE    NOT NULL,
    to_date   DATE    NOT NULL,
    PRIMARY KEY (emp_no, from_date),
    FOREIGN KEY (emp_no) REFERENCES employees (emp_no)
);
"""

# One dump file per table, applied in FK-safe order.
DUMPS = [
    ("departments",  "load_departments.dump"),
    ("employees",    "load_employees.dump"),
    ("dept_manager", "load_dept_manager.dump"),
    ("dept_emp",     "load_dept_emp.dump"),
    ("titles",       "load_titles.dump"),
    ("salaries1",    "load_salaries1.dump"),
    ("salaries2",    "load_salaries2.dump"),
    ("salaries3",    "load_salaries3.dump"),
]


def _dump_dir() -> pathlib.Path:
    env = os.environ.get("EMPLOYEES_DUMP_DIR")
    if env:
        return pathlib.Path(env)
    return pathlib.Path(__file__).resolve().parents[2] / "data" / "employees" / "dumps"


def build(path: str) -> str:
    dump_dir = _dump_dir()
    missing = [f for _, f in DUMPS if not (dump_dir / f).exists()]
    if missing:
        raise FileNotFoundError(
            f"employees dump files not found in {dump_dir}: {missing}. "
            f"Download from https://github.com/datacharmer/test_db (load_*.dump) "
            f"or set $EMPLOYEES_DUMP_DIR.")

    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    try:
        # FKs off during bulk load: the dumps are ordered but the ON-CASCADE
        # constraints add nothing to a one-shot load and only slow it down. The
        # declared FKs still live in the schema, which is what the engine reads.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.executescript(DDL)

        for _, fname in DUMPS:
            # Each dump is a single multi-row ``INSERT INTO `t` VALUES (...),...;``
            # statement — backtick-quoted, which SQLite accepts. executescript
            # loads the whole statement in one shot.
            sql = (dump_dir / fname).read_text(encoding="utf-8", errors="replace")
            conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()
    return path


def stats(path: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "dogfood_employees.db"
    build(target)
    for table, count in stats(target).items():
        print(f"{table:>14}  {count:>10,}")
