"""Fifth dogfood dataset: Microsoft's AdventureWorks OLTP (2022).

Where `employees` tests **depth** (one HR domain, ~4M rows), AdventureWorks tests
**width and complexity**: 68 tables across 16 business schemas (Sales,
Production, Person, Purchasing, HumanResources, …), 91 declared foreign keys,
self-references, composite keys, and heavy business vocabulary — the structural
shape a real ERP has and the synthetic sets never reach.

It is not shipped as SQLite. This builder converts Microsoft's official OLTP CSV
distribution into SQLite, driven by the Postgres port's DDL
(github.com/lorint/AdventureWorks-for-Postgres `install.sql`) for the schema and
Microsoft's raw bcp-exported CSVs for the data:

* **DDL** — parsed from `install.sql`: column names/types, the declared PRIMARY
  KEYs and the 91 FOREIGN KEYs (both live in `ALTER TABLE` blocks). Schemas are
  flattened to bare table names, which is what the engine reasons over; the FK
  metadata is preserved verbatim because the join graph is built from it.
* **Data** — Microsoft's raw CSVs use bcp terminators, **not** standard CSV:
  field separator ``+|`` and row terminator ``&|\\n``. A text field may contain a
  bare newline (XML columns), so records are split on the row terminator, never
  line-by-line.

Sources live in ``data/adventureworks-src/`` (gitignored); override the DDL and
CSV locations with ``$AW_INSTALL_SQL`` / ``$AW_CSV_DIR``.
"""

from __future__ import annotations

import os
import pathlib
import re
import sqlite3

FIELD_SEP = "+|"
ROW_TERM = "&|"

# Postgres/SQL-Server types → SQLite affinity. Domains (custom types) resolve to
# their base first (see _domain_map), so only base types appear here.
_INT = ("serial", "int", "integer", "smallint", "bigint", "tinyint", "smallserial",
        "bigserial", "boolean", "bit")
_REAL = ("numeric", "decimal", "money", "real", "float", "double")
_BLOB = ("bytea", "image", "varbinary", "binary")


def _sqlite_type(pg_type: str) -> str:
    t = pg_type.lower().split("(")[0].strip().strip('"')
    if t in _INT:
        return "INTEGER"
    if t in _REAL:
        return "REAL"
    if t in _BLOB:
        return "BLOB"
    return "TEXT"   # varchar/char/text/uuid/xml/timestamp/date/name/phone/…


def _src_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2] / "data" / "adventureworks-src"


def _install_sql() -> pathlib.Path:
    env = os.environ.get("AW_INSTALL_SQL")
    if env:
        return pathlib.Path(env)
    return _src_dir() / "AdventureWorks-for-Postgres-master" / "install.sql"


def _csv_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("AW_CSV_DIR") or (_src_dir() / "csv"))


def _domain_map(sql: str) -> dict:
    """CREATE DOMAIN "Name" varchar(50) → {'name': 'varchar'}."""
    out = {}
    for m in re.finditer(r'CREATE DOMAIN\s+"?(\w+)"?\s+(\w+)', sql):
        out[m.group(1).lower()] = m.group(2)
    return out


def _parse_tables(sql: str, domains: dict) -> dict:
    """{table: [(col, sqlite_type), ...]} for every CREATE TABLE block.

    Blocks are nested inside CREATE SCHEMA, each ``  CREATE TABLE Name(`` … ``  )``.
    Constraint/check lines are skipped; a domain-typed column resolves through the
    domain map before mapping to a SQLite affinity.
    """
    tables: dict = {}
    lines = sql.splitlines()
    i, n = 0, len(lines)
    col_re = re.compile(r'^\s*"?(\w+)"?\s+("?[\w]+"?(?:\(\d+(?:,\s*\d+)?\))?)')
    skip = ("constraint", "check", "primary key", "foreign key", "unique")
    while i < n:
        m = re.match(r'\s*CREATE TABLE\s+"?(\w+)"?\s*\(', lines[i])
        if not m:
            i += 1
            continue
        table = m.group(1)
        cols = []
        seen = set()
        i += 1
        while i < n:
            raw = lines[i].split("--")[0].rstrip().rstrip(",")
            stripped = raw.strip()
            low = stripped.lower()
            if stripped.startswith(")"):   # block end (tolerates a trailing comment)
                break
            # Never let a stray CREATE/schema line be read as a column — that is
            # the symptom of a missed block end, and it collides on the word.
            if low and not low.startswith(skip) and not low.startswith(("create", "cluster")):
                cm = col_re.match(raw)
                if cm:
                    col, typ = cm.group(1), cm.group(2)
                    if col.lower() not in seen:
                        seen.add(col.lower())
                        base = domains.get(typ.lower().strip('"'), typ)
                        cols.append((col, _sqlite_type(base)))
            i += 1
        if cols:
            tables[table] = cols
        i += 1
    return tables


def _parse_pks(sql: str) -> dict:
    """{table: [pk_col, ...]} from active ALTER … ADD CONSTRAINT … PRIMARY KEY."""
    out: dict = {}
    for m in re.finditer(
            r'ALTER TABLE\s+\w+\.(\w+)\s+ADD\s+CONSTRAINT\s+"[^"]+"\s+PRIMARY KEY\s*\(([^)]+)\)',
            sql, re.IGNORECASE):
        out[m.group(1)] = [c.strip().strip('"') for c in m.group(2).split(",")]
    return out


def _parse_fks(sql: str) -> list:
    """[(table, [cols], ref_table, [ref_cols]), …] from ALTER … FOREIGN KEY."""
    out = []
    for m in re.finditer(
            r'ALTER TABLE\s+\w+\.(\w+)\s+ADD\s+CONSTRAINT\s+"[^"]+"\s+FOREIGN KEY\s*'
            r'\(([^)]+)\)\s+REFERENCES\s+\w+\.(\w+)\s*\(([^)]+)\)',
            sql, re.IGNORECASE):
        cols = [c.strip().strip('"') for c in m.group(2).split(",")]
        refcols = [c.strip().strip('"') for c in m.group(4).split(",")]
        out.append((m.group(1), cols, m.group(3), refcols))
    return out


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _build_ddl(tables: dict, pks: dict, fks: list) -> str:
    fk_by_table: dict = {}
    for t, cols, rt, rcols in fks:
        if t in tables and rt in tables:
            fk_by_table.setdefault(t, []).append((cols, rt, rcols))
    parts = []
    for table, cols in tables.items():
        lines = [f"  {_q(c)} {t}" for c, t in cols]
        pk = [c for c in pks.get(table, []) if any(c == col for col, _ in cols)]
        if pk:
            lines.append("  PRIMARY KEY (" + ", ".join(_q(c) for c in pk) + ")")
        for fcols, rt, rcols in fk_by_table.get(table, []):
            if all(any(fc == col for col, _ in cols) for fc in fcols):
                lines.append(
                    f"  FOREIGN KEY ({', '.join(_q(c) for c in fcols)}) "
                    f"REFERENCES {_q(rt)} ({', '.join(_q(c) for c in rcols)})")
        parts.append(f"CREATE TABLE {_q(table)} (\n" + ",\n".join(lines) + "\n);")
    return "\n".join(parts)


def _iter_records(text: str):
    """Yield field-lists for one CSV, auto-detecting its format.

    Microsoft's OLTP distribution is **not uniform**: some tables are exported
    with bcp terminators (field ``+|``, row ``&|\\n``) and others are plain
    tab-delimited with newline rows. Detecting per file — bcp when the row
    terminator is present, tab otherwise — is what lets both load; assuming one
    format silently dropped every row of the other (a whole-table zero).
    """
    # Scan the whole text, not a fixed prefix: a table whose first row is a large
    # XML document (Illustration, JobCandidate, ProductReview) pushes the row
    # terminator past any small sniff window, and misreading it as tab-delimited
    # dropped every row.
    if ROW_TERM in text:
        for rec in re.split(ROW_TERM + r"\r?\n", text):
            if rec and rec != "\n":
                yield rec.split(FIELD_SEP)
    else:
        import csv
        import io
        for row in csv.reader(io.StringIO(text), delimiter="\t"):
            if row:
                yield row


def _load_csv(conn, table: str, cols: list, path: pathlib.Path) -> int:
    """Load one CSV (bcp- or tab-delimited, auto-detected) into ``table``."""
    text = path.read_text(encoding="utf-8", errors="replace")
    ncols = len(cols)
    rows = []
    for fields in _iter_records(text):
        if len(fields) != ncols:
            # Ragged line or an unescaped separator; skip rather than misalign
            # every following column.
            if len(fields) < ncols:
                continue
            fields = fields[:ncols]
        rows.append([None if f == "" else f for f in fields])
    if not rows:
        return 0
    placeholders = ",".join("?" * ncols)
    # INSERT OR IGNORE, not plain INSERT: the declared PRIMARY KEYs are kept in the
    # schema (the engine reads them for join/grouping metadata), but a handful of
    # source rows carry quirky key values (SQL Server `hierarchyid` columns export
    # as opaque strings) that collide once coerced. Dropping the rare collision is
    # correct for a correctness harness whose invariants are self-consistent on
    # whatever loaded — far better than failing the whole build over data trivia.
    conn.executemany(
        f"INSERT OR IGNORE INTO {_q(table)} ({','.join(_q(c) for c, _ in cols)}) "
        f"VALUES ({placeholders})", rows)
    return len(rows)


def build(path: str) -> str:
    install = _install_sql()
    csv_dir = _csv_dir()
    if not install.exists():
        raise FileNotFoundError(
            f"AdventureWorks DDL not found at {install}. Fetch "
            f"github.com/lorint/AdventureWorks-for-Postgres and Microsoft's OLTP "
            f"CSVs into data/adventureworks-src/ (or set $AW_INSTALL_SQL/$AW_CSV_DIR).")

    sql = install.read_text(encoding="utf-8", errors="replace")
    domains = _domain_map(sql)
    tables = _parse_tables(sql, domains)
    pks = _parse_pks(sql)
    fks = _parse_fks(sql)

    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.executescript(_build_ddl(tables, pks, fks))
        for table, cols in tables.items():
            csv = csv_dir / f"{table}.csv"
            if csv.exists():
                _load_csv(conn, table, cols, csv)
        conn.commit()
    finally:
        conn.close()
    return path


def stats(path: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        tbls = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: conn.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0] for t in tbls}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "dogfood_adventureworks.db"
    build(target)
    st = stats(target)
    for table, count in sorted(st.items(), key=lambda kv: -kv[1]):
        print(f"{table:>28}  {count:>8,}")
    print(f"\n{len(st)} tables, {sum(st.values()):,} rows")
