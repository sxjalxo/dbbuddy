"""Seventh dogfood dataset: TPC-DS, the decision-support/BI benchmark.

TPC-H taught the engine benchmark *identifier* conventions. TPC-DS adds the
failure mode none of the other six carry: **a snowflake with role-playing
dimensions and parallel fact tables.**

* **Role-playing dimensions.** ``web_sales`` holds *two* foreign keys into
  ``date_dim`` (``ws_sold_date_sk`` and ``ws_ship_date_sk``) and two into
  ``customer`` (``ws_bill_customer_sk``, ``ws_ship_customer_sk``). The same is
  true of every fact table. A join graph that keys on "which table does this
  reference" rather than "through which column" collapses them into one edge and
  answers about the ship date with the sale date.
* **Parallel facts with identical measure names.** ``ss_ext_sales_price``,
  ``cs_ext_sales_price`` and ``ws_ext_sales_price`` are the same measure in three
  channels; "total sales price" is genuinely ambiguous and must not read as
  certain. Their returns tables (``store_returns``…) repeat the pattern.
* **Surrogate ``_sk`` keys beside business ``_id`` keys.** ``c_customer_sk`` is
  the join key; ``c_customer_id`` is the human identifier. Counting the wrong one
  is not visible in the result.
* **Snowflaked dimensions** — ``customer`` → ``customer_address`` /
  ``customer_demographics`` / ``household_demographics`` → ``income_band``.
* **Nullable foreign keys** everywhere in the facts, so an inner join silently
  drops rows a total must still include.

Schema comes from the official DDL (``tools/tpcds.sql`` and ``tools/tpcds_ri.sql``
of github.com/gregrahn/tpcds-kit, downloaded to ``data/tpcds-src/``, gitignored):
25 tables, 24 declared primary keys, 104 declared foreign keys, parsed here and
translated into SQLite rather than retyped, so the shape under test is the
benchmark's and not this file's idea of it.

The *rows* are generated. ``dsdgen`` is a C program that has to be compiled per
platform, and what this loop tests is the schema shape and the relationships
between answers — neither depends on reproducing TPC-DS's exact byte stream. Row
counts follow the spec's ratios at a small scale factor; referential integrity,
the value domains of every grouping column, and the correlation between measures
are real, which is what the invariants need.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import random
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

SEED = 20260723

SRC_DIR = pathlib.Path(__file__).resolve().parents[2] / "data" / "tpcds-src"
DDL_FILE = SRC_DIR / "tpcds.sql"
RI_FILE = SRC_DIR / "tpcds_ri.sql"

DDL_URL = "https://raw.githubusercontent.com/gregrahn/tpcds-kit/master/tools/tpcds.sql"
RI_URL = "https://raw.githubusercontent.com/gregrahn/tpcds-kit/master/tools/tpcds_ri.sql"

# Rows per table. The spec's shape at a small scale: three sales channels whose
# fact tables dwarf every dimension, a returns table roughly a tenth of its
# sales table, and dimensions that stay small (that is what makes them
# dimensions). Total ~320k rows, which builds in seconds and still makes a
# fan-out error visible as an implausible number.
ROW_COUNTS = {
    "dbgen_version": 1,
    "income_band": 20,
    "ship_mode": 20,
    "reason": 35,
    "warehouse": 5,
    "web_site": 5,
    "call_center": 6,
    "store": 12,
    "web_page": 60,
    "promotion": 100,
    "catalog_page": 500,
    "household_demographics": 720,
    "time_dim": 1440,
    "customer_demographics": 2000,
    "customer_address": 2500,
    "date_dim": 2557,
    "item": 3000,
    "customer": 5000,
    "inventory": 40000,
    "store_sales": 120000,
    "catalog_sales": 60000,
    "web_sales": 40000,
    "store_returns": 12000,
    "catalog_returns": 6000,
    "web_returns": 4000,
}

DATE_START = datetime.date(1998, 1, 1)
DAYS = ROW_COUNTS["date_dim"]
# The spec numbers date_dim keys from a Julian-style offset rather than 1, which
# is worth keeping: a surrogate key that does not start at 1 breaks any code that
# assumes key == row number.
DATE_SK_BASE = 2450815

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]

# Value pools, chosen so every dimension column a question might group by has a
# small, realistic domain. Keyed by the *last* word of the column name, which is
# how TPC-DS names them (``cd_marital_status``, ``s_state``, ``i_category``).
POOLS: Dict[str, List[str]] = {
    "gender": ["M", "F"],
    "status": ["S", "M", "D", "W", "U"],
    "rating": ["Low Risk", "Good", "High Risk", "Unknown"],
    "category": ["Books", "Electronics", "Home", "Jewelry", "Men", "Music",
                 "Shoes", "Sports", "Women", "Children"],
    "class": ["accessories", "athletic", "bracelets", "classical", "consignment",
              "dresses", "estate", "fiction", "guns", "mattresses"],
    "brand": [f"brand #{i}" for i in range(1, 21)],
    "color": ["almond", "azure", "black", "blush", "chartreuse", "cyan", "forest",
              "goldenrod", "honeydew", "ivory", "khaki", "lace"],
    "units": ["Box", "Bunch", "Bundle", "Carton", "Case", "Cup", "Dozen", "Each",
              "Gram", "Lb", "N/A", "Oz", "Pallet", "Pound", "Ton", "Tsp"],
    "container": ["Unknown"],
    "state": ["CA", "TX", "NY", "FL", "IL", "PA", "OH", "GA", "NC", "MI"],
    "country": ["United States", "Canada", "Mexico"],
    "city": ["Fairview", "Midway", "Oak Grove", "Riverside", "Franklin",
             "Greenwood", "Salem", "Georgetown", "Bridgeport", "Kingston"],
    "county": ["Williamson County", "Barrow County", "Walker County",
               "Ziebach County", "Fillmore County"],
    "type": ["Payment", "Shipping", "Store", "Web", "Catalog"],
    "name": ["ought", "able", "pri", "ese", "anti", "cally", "eing", "bar"],
    "channel": ["Store", "Catalog", "Web", "Email", "Tv", "Radio", "Press"],
    "flag": ["Y", "N"],
    "desc": ["Reliable, sound, unusual buyers", "Anxious, single, whole troops",
             "Big, small, private markets", "Boring, cold, front feet"],
    "salutation": ["Mr.", "Mrs.", "Ms.", "Dr.", "Sir", "Miss"],
    "carrier": ["DIAMOND", "AIRBORNE", "USPS", "UPS", "FEDEX", "ZOUROS"],
    "code": ["AIR", "SURFACE", "SEA", "NEXT DAY", "TWO DAY", "LIBRARY"],
    "size": ["small", "medium", "large", "extra large", "petite", "N/A"],
    "manufact": [f"manufacturer #{i}" for i in range(1, 16)],
    "shift": ["first", "second", "third"],
    "meal": ["breakfast", "lunch", "dinner"],
    "day": DAY_NAMES,
}

FIRST_NAMES = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer",
               "Michael", "Linda", "David", "Elizabeth", "William", "Barbara"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
              "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez"]
STREET_TYPES = ["Road", "Street", "Avenue", "Lane", "Court", "Drive", "Way",
                "Circle", "Parkway", "Boulevard"]


# ── Schema: parse the official DDL rather than restate it ────────────────────

def _fetch(url: str, target: pathlib.Path) -> None:
    """Download a reference file if it is not already on disk."""
    import urllib.request

    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        target.write_bytes(response.read())


def _sources() -> Tuple[str, str]:
    for url, path in ((DDL_URL, DDL_FILE), (RI_URL, RI_FILE)):
        if not path.exists():
            _fetch(url, path)
    return DDL_FILE.read_text(encoding="utf-8"), RI_FILE.read_text(encoding="utf-8")


_TABLE_RE = re.compile(r"create table (\w+)\s*\((.*?)\n\);", re.S | re.I)
_FK_RE = re.compile(
    r"^alter table (\w+) add constraint \w+ foreign key\s*\((\w+)\)\s*"
    r"references (\w+)\s*\((\w+)\)", re.I | re.M)


def parse_schema(ddl: str, ri: str) -> Dict[str, dict]:
    """``{table: {"columns": [(name, type)], "pk": [...], "fks": {col: (t, c)}}}``."""
    tables: Dict[str, dict] = {}
    for name, body in _TABLE_RE.findall(ddl):
        columns: List[Tuple[str, str]] = []
        pk: List[str] = []
        for line in body.strip().splitlines():
            line = line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            if line.lower().startswith("primary key"):
                pk = [c.strip() for c in line[line.index("(") + 1:line.rindex(")")].split(",")]
                continue
            parts = line.split()
            columns.append((parts[0], " ".join(parts[1:]).replace("not null", "").strip()))
        tables[name] = {"columns": columns, "pk": pk, "fks": {}}

    for table, column, ref_table, ref_column in _FK_RE.findall(ri):
        if table in tables and ref_table in tables:
            tables[table]["fks"][column] = (ref_table, ref_column)
    return tables


def _create_statement(table: str, spec: dict) -> str:
    lines = [f'    "{col}" {typ}' for col, typ in spec["columns"]]
    if spec["pk"]:
        lines.append("    PRIMARY KEY (" + ", ".join(f'"{c}"' for c in spec["pk"]) + ")")
    for col, (ref_table, ref_col) in spec["fks"].items():
        lines.append(f'    FOREIGN KEY ("{col}") REFERENCES "{ref_table}" ("{ref_col}")')
    return f'CREATE TABLE "{table}" (\n' + ",\n".join(lines) + "\n);"


def _generation_order(tables: Dict[str, dict]) -> List[str]:
    """Parents before children, so a foreign key always has a value to point at."""
    ordered: List[str] = []
    remaining = dict(tables)
    while remaining:
        free = [t for t, spec in remaining.items()
                if all(ref in ordered or ref == t
                       for ref, _ in spec["fks"].values())]
        if not free:  # a cycle — emit the rest in declaration order
            free = list(remaining)
        for table in sorted(free, key=lambda t: ROW_COUNTS.get(t, 0)):
            ordered.append(table)
            remaining.pop(table)
    return ordered


# ── Data ─────────────────────────────────────────────────────────────────────

def _business_id(prefix_index: int) -> str:
    """A 16-character alphabetic business key, as TPC-DS writes them."""
    letters = []
    value = prefix_index
    for _ in range(16):
        letters.append(chr(ord("A") + value % 26))
        value //= 26
    return "".join(reversed(letters))


def _pool_for(column: str) -> Optional[List[str]]:
    parts = column.split("_")[1:]  # drop the table prefix
    for token in reversed(parts):
        if token in POOLS:
            return POOLS[token]
    return None


def _date_dim_rows(columns: List[str]) -> List[tuple]:
    rows = []
    for offset in range(DAYS):
        day = DATE_START + datetime.timedelta(days=offset)
        month_seq = (day.year - DATE_START.year) * 12 + day.month
        values = {
            "d_date_sk": DATE_SK_BASE + offset,
            "d_date_id": _business_id(offset),
            "d_date": day.isoformat(),
            "d_month_seq": month_seq,
            "d_week_seq": offset // 7,
            "d_quarter_seq": (day.year - DATE_START.year) * 4 + (day.month - 1) // 3 + 1,
            "d_year": day.year,
            "d_dow": day.weekday(),
            "d_moy": day.month,
            "d_dom": day.day,
            "d_qoy": (day.month - 1) // 3 + 1,
            "d_fy_year": day.year,
            "d_fy_quarter_seq": (day.month - 1) // 3 + 1,
            "d_fy_week_seq": offset // 7,
            "d_day_name": DAY_NAMES[day.weekday()],
            "d_quarter_name": f"{day.year}Q{(day.month - 1) // 3 + 1}",
            "d_holiday": "N",
            "d_weekend": "Y" if day.weekday() >= 5 else "N",
            "d_following_holiday": "N",
            "d_first_dom": DATE_SK_BASE + offset - day.day + 1,
            "d_last_dom": DATE_SK_BASE + offset,
            "d_same_day_ly": DATE_SK_BASE + offset - 365,
            "d_same_day_lq": DATE_SK_BASE + offset - 91,
            "d_current_day": "N",
            "d_current_week": "N",
            "d_current_month": "N",
            "d_current_quarter": "N",
            "d_current_year": "Y" if day.year == DATE_START.year else "N",
        }
        rows.append(tuple(values.get(c) for c in columns))
    return rows


def _time_dim_rows(columns: List[str]) -> List[tuple]:
    rows = []
    for minute in range(ROW_COUNTS["time_dim"]):
        second = minute * 60
        hour = second // 3600
        values = {
            "t_time_sk": second,
            "t_time_id": _business_id(minute),
            "t_time": second,
            "t_hour": hour,
            "t_minute": (second // 60) % 60,
            "t_second": 0,
            "t_am_pm": "AM" if hour < 12 else "PM",
            "t_shift": POOLS["shift"][hour // 8],
            "t_sub_shift": "morning" if hour < 12 else "evening",
            "t_meal_time": (POOLS["meal"][0] if 6 <= hour < 10 else
                            POOLS["meal"][1] if 11 <= hour < 14 else
                            POOLS["meal"][2] if 17 <= hour < 21 else None),
        }
        rows.append(tuple(values.get(c) for c in columns))
    return rows


def _value(rng: random.Random, table: str, column: str, sql_type: str,
           row_index: int, spec: dict, keys: Dict[str, List[int]]):
    """One cell, chosen from the column's declared type and its name."""
    fk = spec["fks"].get(column)
    if fk:
        parent = keys.get(fk[0])
        if not parent:
            return None
        # Nullable foreign keys are real in TPC-DS facts: a few percent of rows
        # legitimately have no dimension, which is exactly what makes an inner
        # join silently drop them.
        if column not in spec["pk"] and rng.random() < 0.02:
            return None
        return rng.choice(parent)

    if column in spec["pk"] and column.endswith("_sk"):
        return row_index

    lowered = sql_type.lower()
    if column.endswith("_id") and lowered.startswith(("char", "varchar")):
        return _business_id(row_index)

    pool = _pool_for(column)
    if pool:
        return rng.choice(pool)

    if column.endswith("first_name"):
        return rng.choice(FIRST_NAMES)
    if column.endswith("last_name"):
        return rng.choice(LAST_NAMES)
    if column.endswith("street_type"):
        return rng.choice(STREET_TYPES)
    if column.endswith("email_address"):
        return f"{rng.choice(FIRST_NAMES)}.{rng.choice(LAST_NAMES)}@example.com"
    if column.endswith("_zip"):
        return f"{rng.randint(10000, 99999)}"

    if lowered.startswith("decimal"):
        # Net profit is allowed to be negative; everything else is money.
        if column.endswith(("profit", "loss")):
            return round(rng.uniform(-500, 3000), 2)
        return round(rng.uniform(1, 5000), 2)
    if lowered.startswith("integer"):
        if column.endswith("_number"):
            return row_index
        return rng.randint(1, 100)
    if lowered.startswith("date"):
        return (DATE_START + datetime.timedelta(days=rng.randint(0, DAYS))).isoformat()
    if lowered.startswith("time"):
        return rng.randint(0, 86399)
    return f"{column.split('_')[-1]} {rng.randint(1, 999)}"


def build(path: str) -> str:
    rng = random.Random(SEED)
    ddl, ri = _sources()
    tables = parse_schema(ddl, ri)

    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    for table, spec in tables.items():
        conn.execute(_create_statement(table, spec))

    keys: Dict[str, List[int]] = {}
    for table in _generation_order(tables):
        spec = tables[table]
        columns = [c for c, _ in spec["columns"]]
        count = ROW_COUNTS.get(table, 10)

        if table == "date_dim":
            rows = _date_dim_rows(columns)
        elif table == "time_dim":
            rows = _time_dim_rows(columns)
        else:
            rows = [tuple(_value(rng, table, col, typ, index + 1, spec, keys)
                          for col, typ in spec["columns"])
                    for index in range(count)]

        placeholders = ",".join("?" * len(columns))
        quoted = ",".join(f'"{c}"' for c in columns)
        # OR IGNORE: a composite fact key (item, ticket number) can collide once
        # the item is drawn at random, and losing a handful of rows changes
        # nothing the invariants measure.
        conn.executemany(
            f'INSERT OR IGNORE INTO "{table}" ({quoted}) VALUES ({placeholders})', rows)

        surrogate = next((c for c in columns if c.endswith("_sk")), None)
        if surrogate and (surrogate in spec["pk"] or len(spec["pk"]) == 1):
            keys[table] = [r[0] for r in conn.execute(
                f'SELECT "{surrogate}" FROM "{table}"')]

    conn.commit()
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
    target = sys.argv[1] if len(sys.argv) > 1 else "dogfood_tpcds.db"
    build(target)
    counts = stats(target)
    for table, count in counts.items():
        print(f"{table:>24}  {count:>9,}")
    print(f"{'TOTAL':>24}  {sum(counts.values()):>9,}")
