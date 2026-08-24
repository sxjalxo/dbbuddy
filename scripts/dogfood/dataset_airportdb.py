"""Eighth dogfood dataset: MySQL's AirportDB — real data, at real scale.

This is the end of the size ladder: ~55M rows across 14 aviation tables in the
full set, versus 3.9M for `employees` and 300k for the generated benchmarks. It
is loaded from Oracle's published **MySQL Shell dump** (per-table `.sql` DDL, a
`.json` sidecar naming the columns, and zstd-compressed `.tsv.zst` data chunks),
so both the schema and the rows are the real ones — the DDL is translated to
SQLite here, never retyped.

What it adds over the other seven:

* **Scale that changes which plans are acceptable.** A fan-out bug on a 300k-row
  fact is a wrong number; on a 50M-row one it is also a query that never returns.
* **Role-playing dimensions on a real schema** — `flight.from` and `flight.to`
  both reference `airport`, and both are *reserved words* as column names. TPC-DS
  proved the join-role fix on a benchmark; this proves it on a schema that also
  needs quoting to survive.
* **A 1:1 detail table** (`passengerdetails` under `passenger`), which is a
  different join shape from every 1:N in the suite.
* **Time series** (`weatherdata`) and **geography** (`airport_geo`) alongside the
  operational tables.

**Tiers.** Building all 55M rows takes minutes and gigabytes, and most defects
surface at 1% of that, so `build` caps rows per table:

    tier "s"  ~250k per table   — the iteration tier (default)
    tier "m"  ~2M per table     — scale sanity
    tier "l"  no cap            — the full ~55M-row set

Referential integrity is preserved under a cap: tables load parents-first, and a
child row whose foreign key points at a parent row that was capped away is
dropped rather than left dangling. Without that, an inner join legitimately loses
rows and every conservation invariant fails for a reason that is not a defect.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import tarfile
from typing import Dict, Iterable, List, Optional, Set, Tuple

SRC_DIR = pathlib.Path(__file__).resolve().parents[2] / "data" / "airport-db"
ARCHIVE = pathlib.Path(__file__).resolve().parents[2] / "data" / "airport-db.tar.gz"
DOWNLOAD_URL = "https://downloads.mysql.com/docs/airport-db.tar.gz"

TIERS: Dict[str, Optional[int]] = {"s": 250_000, "m": 2_000_000, "l": None}
DEFAULT_TIER = os.environ.get("AIRPORTDB_TIER", "s")

# Rows per executemany batch. Large enough to amortise the round trip, small
# enough that a 50M-row table never materialises in memory.
BATCH = 20_000


# ── Locating the dump ────────────────────────────────────────────────────────

def _extract_if_needed() -> pathlib.Path:
    """Return the directory holding the dump, extracting the tarball once."""
    if SRC_DIR.exists() and any(SRC_DIR.rglob("*.tsv.zst")):
        return _dump_root(SRC_DIR)
    if not ARCHIVE.exists():
        raise FileNotFoundError(
            f"{ARCHIVE} not found. Download it once:\n  curl -sSL -o {ARCHIVE} {DOWNLOAD_URL}")
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ARCHIVE, "r:gz") as tar:
        # Refuse absolute paths and traversal before writing anything.
        for member in tar.getmembers():
            target = (SRC_DIR / member.name).resolve()
            if not str(target).startswith(str(SRC_DIR.resolve())):
                raise ValueError(f"unsafe path in archive: {member.name}")
        tar.extractall(SRC_DIR)
    return _dump_root(SRC_DIR)


def _dump_root(base: pathlib.Path) -> pathlib.Path:
    for candidate in [base, *[p for p in base.iterdir() if p.is_dir()]]:
        if any(candidate.glob("*.tsv.zst")) or any(candidate.glob("@.json")):
            return candidate
    raise FileNotFoundError(f"no MySQL Shell dump found under {base}")


def _tables(root: pathlib.Path) -> Dict[str, dict]:
    """``{table: {"ddl": path, "json": path, "chunks": [paths]}}``.

    MySQL Shell names files ``<schema>@<table>.sql`` and
    ``<schema>@<table>@@<n>.tsv.zst`` (one chunk per file; `booking` ships ~30).
    """
    found: Dict[str, dict] = {}
    for sql_path in root.glob("*@*.sql"):
        name = sql_path.stem
        if name.startswith("@"):
            continue
        table = name.split("@", 1)[1]
        if "@" in table:  # a trigger/routine file, not a table
            continue
        entry = found.setdefault(table, {"chunks": []})
        entry["ddl"] = sql_path
        sidecar = sql_path.with_suffix(".json")
        if sidecar.exists():
            entry["json"] = sidecar
    # Chunk suffixes are not uniform in the published dump: small tables ship
    # ``<schema>@<table>@@0.tsv.zst`` and `booking` ships ``…@booking@9.tsv.zst``.
    # Strip either form rather than assuming one.
    for data_path in sorted(root.glob("*@*.tsv.zst")):
        stem = data_path.name.split(".tsv.zst")[0]
        table = re.sub(r"@+\d+$", "", stem.split("@", 1)[1])
        if table in found:
            found[table]["chunks"].append(data_path)
    return {t: e for t, e in found.items() if e.get("ddl")}


# ── MySQL DDL → SQLite ───────────────────────────────────────────────────────

_TYPE_MAP = [
    (r"\b(tiny|small|medium|big)?int\b(\s*\(\d+\))?(\s+unsigned)?", "INTEGER"),
    (r"\bbool(ean)?\b", "INTEGER"),
    (r"\b(var)?char\s*\(\d+\)", "TEXT"),
    (r"\b(tiny|medium|long)?text\b", "TEXT"),
    (r"\benum\s*\([^)]*\)", "TEXT"),
    (r"\bset\s*\([^)]*\)", "TEXT"),
    (r"\b(datetime|timestamp)\b(\s*\(\d+\))?", "TEXT"),
    (r"\bdate\b", "DATE"),
    (r"\btime\b(\s*\(\d+\))?", "TEXT"),
    (r"\byear\b(\s*\(\d+\))?", "INTEGER"),
    (r"\b(float|double)\b(\s*\(\d+,\s*\d+\))?", "REAL"),
    (r"\b(decimal|numeric)\s*\((\d+),\s*(\d+)\)", r"DECIMAL(\2,\3)"),
    (r"\b(geometry|point|polygon|blob|binary|varbinary)\b(\s*\(\d+\))?", "BLOB"),
]

_DROP_LINE = re.compile(
    r"^\s*(unique\s+)?(key|index|fulltext|spatial)\b", re.I)


def _map_types_outside_identifiers(line: str) -> str:
    """Rewrite MySQL types, leaving quoted identifiers alone.

    AirportDB has columns named `time`, `user` and `comment`; substituting types
    across the whole line turned ``"time" time NOT NULL`` into ``"TEXT" TEXT``
    and the loader then failed with ``table weatherdata has no column named
    time``. A type is only ever outside the quotes, so only those spans are
    touched.
    """
    parts = re.split(r'("[^"]*")', line)
    for index, part in enumerate(parts):
        if part.startswith('"'):
            continue
        for pattern, replacement in _TYPE_MAP:
            part = re.sub(pattern, replacement, part, flags=re.I)
        parts[index] = part
    return "".join(parts)


def translate_ddl(mysql_sql: str, table: str) -> str:
    """Rewrite one MySQL ``CREATE TABLE`` as SQLite, keeping keys and FKs.

    Keys are the point of the exercise: `PRAGMA foreign_key_list` is what feeds
    the relationship graph, so foreign keys and primary keys are preserved
    exactly while secondary indexes (which the engine never reads) are dropped.
    """
    match = re.search(r"CREATE TABLE.*?\((.*)\)[^)]*;", mysql_sql, re.S | re.I)
    if not match:
        raise ValueError(f"no CREATE TABLE found for {table}")
    body = match.group(1)

    lines, depth, current = [], 0, []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            lines.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        lines.append("".join(current).strip())

    out: List[str] = []
    for line in lines:
        if not line or _DROP_LINE.match(line):
            continue
        line = line.replace("`", '"')
        line = re.sub(r"\bCONSTRAINT\s+\"[^\"]+\"\s+", "", line, flags=re.I)
        line = re.sub(r"\s+ON\s+(DELETE|UPDATE)\s+(CASCADE|RESTRICT|SET NULL|NO ACTION)",
                      "", line, flags=re.I)
        line = re.sub(r"\bAUTO_INCREMENT\b", "", line, flags=re.I)
        line = re.sub(r"\bCOMMENT\s+'(?:[^']|'')*'", "", line, flags=re.I)
        line = re.sub(r"\s+/\*.*?\*/", "", line, flags=re.S)
        line = re.sub(r"\bCHARACTER SET \w+|\bCOLLATE \w+", "", line, flags=re.I)
        line = re.sub(r"\bDEFAULT\s+CURRENT_TIMESTAMP(\(\d*\))?(\s+ON UPDATE CURRENT_TIMESTAMP(\(\d*\))?)?",
                      "", line, flags=re.I)
        line = re.sub(r"\bGENERATED ALWAYS AS \(.*?\)( STORED| VIRTUAL)?", "", line, flags=re.I)
        line = _map_types_outside_identifiers(line)
        line = re.sub(r"\s+", " ", line).strip().rstrip(",")
        if line:
            out.append("    " + line)
    return f'CREATE TABLE "{table}" (\n' + ",\n".join(out) + "\n);"


def _columns_of(entry: dict, table: str) -> List[str]:
    """Column order for the TSV, from the dump's own sidecar."""
    sidecar = entry.get("json")
    if sidecar:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        # MySQL Shell nests the field list under "options" alongside the
        # terminators; the TSV column order is this list, not the DDL's.
        cols = meta.get("columns") or (meta.get("options") or {}).get("columns")
        if cols:
            return list(cols)
    raise ValueError(f"no column list for {table}")


def _foreign_keys(conn: sqlite3.Connection, table: str) -> List[Tuple[int, str, str, str]]:
    return [(row[0], row[3], row[2], row[4]) for row in
            conn.execute(f'PRAGMA foreign_key_list("{table}")')]


def _load_order(conn: sqlite3.Connection, tables: Iterable[str]) -> List[str]:
    """Parents before children, so a capped parent is known before its children."""
    pending = list(tables)
    ordered: List[str] = []
    while pending:
        free = [t for t in pending
                if all(ref in ordered or ref == t
                       for _, _, ref, _ in _foreign_keys(conn, t))]
        if not free:
            free = list(pending)  # a cycle; the FK filter simply keeps more rows
        for table in free:
            ordered.append(table)
            pending.remove(table)
    return ordered


# ── Rows ─────────────────────────────────────────────────────────────────────

def _stream_rows(paths: List[pathlib.Path]) -> Iterable[List[Optional[str]]]:
    """Decompress the chunks and yield one list of field strings per row."""
    import zstandard

    decompressor = zstandard.ZstdDecompressor()
    for path in paths:
        with open(path, "rb") as raw, decompressor.stream_reader(raw) as stream:
            trailing = b""
            while True:
                block = stream.read(1 << 20)
                if not block:
                    break
                block = trailing + block
                *complete, trailing = block.split(b"\n")
                for line in complete:
                    if line:
                        yield [None if f == b"\\N" else f.decode("utf-8", "replace")
                               for f in line.split(b"\t")]
            if trailing:
                yield [None if f == b"\\N" else f.decode("utf-8", "replace")
                       for f in trailing.split(b"\t")]


def build(path: str, tier: str = DEFAULT_TIER) -> str:
    cap = TIERS.get(tier, TIERS["s"])
    root = _extract_if_needed()
    tables = _tables(root)
    if not tables:
        raise FileNotFoundError(f"no table dumps under {root}")

    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    # Bulk-load settings. The dump is the source of truth for integrity, and the
    # FK *declarations* (which is what the engine reads) are unaffected.
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")

    for table, entry in tables.items():
        conn.execute(translate_ddl(entry["ddl"].read_text(encoding="utf-8"), table))

    # Keys kept per table only while a cap is in force, so a child can drop rows
    # pointing at a parent that was capped away.
    kept_keys: Dict[str, Set[str]] = {}
    capped: Set[str] = set()

    for table in _load_order(conn, tables):
        entry = tables[table]
        columns = _columns_of(entry, table)
        index_of = {c: i for i, c in enumerate(columns)}
        fks = [(index_of[col], ref_table, ref_col)
               for _, col, ref_table, ref_col in _foreign_keys(conn, table)
               if col in index_of and ref_table in capped]
        pk_columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
                      if row[5]]
        pk_index = [index_of[c] for c in pk_columns if c in index_of]

        placeholders = ",".join("?" * len(columns))
        quoted = ",".join(f'"{c}"' for c in columns)
        statement = f'INSERT OR IGNORE INTO "{table}" ({quoted}) VALUES ({placeholders})'

        batch: List[list] = []
        loaded = 0
        seen: Set[str] = set()
        for row in _stream_rows(entry["chunks"]):
            if cap is not None and loaded >= cap:
                capped.add(table)
                break
            if len(row) != len(columns):
                continue
            # Drop rows orphaned by a parent's cap — a dangling FK would make an
            # inner join lose rows, and every conservation check fail with it.
            if any(row[i] is not None and row[i] not in kept_keys.get(ref, ())
                   for i, ref, _ in fks):
                continue
            batch.append(row)
            loaded += 1
            if pk_index:
                seen.add("\x1f".join(str(row[i]) for i in pk_index))
            if len(batch) >= BATCH:
                conn.executemany(statement, batch)
                batch.clear()
        if batch:
            conn.executemany(statement, batch)
        conn.commit()
        if cap is not None and pk_index:
            kept_keys[table] = seen
        elif table in kept_keys:
            del kept_keys[table]

    conn.close()
    return path


def stats(path: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for t in tables}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "dogfood_airportdb.db"
    tier = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_TIER
    build(target, tier)
    counts = stats(target)
    for table, count in counts.items():
        print(f"{table:>24}  {count:>12,}")
    print(f"{'TOTAL':>24}  {sum(counts.values()):>12,}")
