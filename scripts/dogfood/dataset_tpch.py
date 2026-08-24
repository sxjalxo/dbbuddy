"""Sixth dogfood dataset: TPC-H, the analytical/warehouse benchmark.

Where AdventureWorks tests **width** (68 tables, many domains) and employees tests
**depth** (4M rows in one domain), TPC-H tests the **star/snowflake analytics
shape** — a 300k-row `lineitem` fact hanging off `orders`, `part` and `supplier`,
with `nation`/`region` as small dimensions reached only by a 3-hop join. Every
question is an aggregation over a join, which is exactly where fan-out
double-counting hides.

It also carries a naming convention none of the other five have, and one that is
extremely common in warehouse and mainframe-derived schemas:

* **Every column is prefixed with its table's initial** — ``l_extendedprice``,
  ``o_orderdate``, ``ps_supplycost``, ``c_mktsegment``. Splitting on the
  underscore yields a junk one-letter token plus one *concatenated* word, so a
  user saying "extended price" and a column named ``l_extendedprice`` never meet
  under word-level matching.
* **Keys end in ``key``, not ``id``** — ``l_orderkey``, ``c_custkey``,
  ``ps_partkey``. Nothing in the schema is named ``id``, and a key that reads as
  a measure gets summed (the AdventureWorks ``XxxID`` failure in a new dress).
* **Concatenated table names** — ``lineitem``, ``partsupp``: no separator at all.
* **Composite primary keys** — ``partsupp(ps_partkey, ps_suppkey)`` and
  ``lineitem(l_orderkey, l_linenumber)``; no surrogate key anywhere.
* **Coded single-character dimensions** — ``l_returnflag`` R/A/N,
  ``l_linestatus`` O/F, ``o_orderstatus`` O/F/P.
* **A ``comment`` column on all eight tables**, so the most ambiguous possible
  column name is present everywhere (a calibration probe, not a correctness one).

The data is generated rather than produced by ``dbgen``: the C generator is not
buildable here, and the properties under test are the *schema shape* and the
*join/aggregation relationships between answers*, neither of which depends on
reproducing TPC-H's exact byte stream. The row-count ratios, value domains,
distributions and referential structure follow the spec at scale factor
``SCALE_FACTOR``; the invariant tier needs internal consistency, not the
official numbers.
"""

from __future__ import annotations

import datetime
import os
import random
import sqlite3

SEED = 20260723

# TPC-H scale factor. Row counts are the spec's: customer 150k*SF, supplier
# 10k*SF, part 200k*SF, partsupp 4 per part, orders 1.5M*SF, lineitem 1-7 per
# order. 0.05 gives a ~300k-row fact table — big enough that a fan-out bug shows
# as an implausible number, small enough to build in under a minute.
SCALE_FACTOR = 0.05

DDL = '''
CREATE TABLE region (
    r_regionkey INTEGER PRIMARY KEY,
    r_name      CHAR(25) NOT NULL,
    r_comment   VARCHAR(152)
);

CREATE TABLE nation (
    n_nationkey INTEGER PRIMARY KEY,
    n_name      CHAR(25) NOT NULL,
    n_regionkey INTEGER NOT NULL,
    n_comment   VARCHAR(152),
    FOREIGN KEY (n_regionkey) REFERENCES region(r_regionkey)
);

CREATE TABLE part (
    p_partkey     INTEGER PRIMARY KEY,
    p_name        VARCHAR(55) NOT NULL,
    p_mfgr        CHAR(25) NOT NULL,
    p_brand       CHAR(10) NOT NULL,
    p_type        VARCHAR(25) NOT NULL,
    p_size        INTEGER NOT NULL,
    p_container   CHAR(10) NOT NULL,
    p_retailprice DECIMAL(15,2) NOT NULL,
    p_comment     VARCHAR(23)
);

CREATE TABLE supplier (
    s_suppkey   INTEGER PRIMARY KEY,
    s_name      CHAR(25) NOT NULL,
    s_address   VARCHAR(40) NOT NULL,
    s_nationkey INTEGER NOT NULL,
    s_phone     CHAR(15) NOT NULL,
    s_acctbal   DECIMAL(15,2) NOT NULL,
    s_comment   VARCHAR(101),
    FOREIGN KEY (s_nationkey) REFERENCES nation(n_nationkey)
);

CREATE TABLE partsupp (
    ps_partkey    INTEGER NOT NULL,
    ps_suppkey    INTEGER NOT NULL,
    ps_availqty   INTEGER NOT NULL,
    ps_supplycost DECIMAL(15,2) NOT NULL,
    ps_comment    VARCHAR(199),
    PRIMARY KEY (ps_partkey, ps_suppkey),
    FOREIGN KEY (ps_partkey) REFERENCES part(p_partkey),
    FOREIGN KEY (ps_suppkey) REFERENCES supplier(s_suppkey)
);

CREATE TABLE customer (
    c_custkey    INTEGER PRIMARY KEY,
    c_name       VARCHAR(25) NOT NULL,
    c_address    VARCHAR(40) NOT NULL,
    c_nationkey  INTEGER NOT NULL,
    c_phone      CHAR(15) NOT NULL,
    c_acctbal    DECIMAL(15,2) NOT NULL,
    c_mktsegment CHAR(10) NOT NULL,
    c_comment    VARCHAR(117),
    FOREIGN KEY (c_nationkey) REFERENCES nation(n_nationkey)
);

CREATE TABLE orders (
    o_orderkey      INTEGER PRIMARY KEY,
    o_custkey       INTEGER NOT NULL,
    o_orderstatus   CHAR(1) NOT NULL,
    o_totalprice    DECIMAL(15,2) NOT NULL,
    o_orderdate     DATE NOT NULL,
    o_orderpriority CHAR(15) NOT NULL,
    o_clerk         CHAR(15) NOT NULL,
    o_shippriority  INTEGER NOT NULL,
    o_comment       VARCHAR(79),
    FOREIGN KEY (o_custkey) REFERENCES customer(c_custkey)
);

CREATE TABLE lineitem (
    l_orderkey      INTEGER NOT NULL,
    l_partkey       INTEGER NOT NULL,
    l_suppkey       INTEGER NOT NULL,
    l_linenumber    INTEGER NOT NULL,
    l_quantity      DECIMAL(15,2) NOT NULL,
    l_extendedprice DECIMAL(15,2) NOT NULL,
    l_discount      DECIMAL(15,2) NOT NULL,
    l_tax           DECIMAL(15,2) NOT NULL,
    l_returnflag    CHAR(1) NOT NULL,
    l_linestatus    CHAR(1) NOT NULL,
    l_shipdate      DATE NOT NULL,
    l_commitdate    DATE NOT NULL,
    l_receiptdate   DATE NOT NULL,
    l_shipinstruct  CHAR(25) NOT NULL,
    l_shipmode      CHAR(10) NOT NULL,
    l_comment       VARCHAR(44),
    PRIMARY KEY (l_orderkey, l_linenumber),
    FOREIGN KEY (l_orderkey) REFERENCES orders(o_orderkey),
    FOREIGN KEY (l_partkey) REFERENCES part(p_partkey),
    FOREIGN KEY (l_suppkey) REFERENCES supplier(s_suppkey)
);
'''

# The spec's fixed dimension contents.
REGIONS = ["AFRICA", "AMERICA", "ASIA", "EUROPE", "MIDDLE EAST"]
NATIONS = [
    ("ALGERIA", 0), ("ARGENTINA", 1), ("BRAZIL", 1), ("CANADA", 1), ("EGYPT", 4),
    ("ETHIOPIA", 0), ("FRANCE", 3), ("GERMANY", 3), ("INDIA", 2), ("INDONESIA", 2),
    ("IRAN", 4), ("IRAQ", 4), ("JAPAN", 2), ("JORDAN", 4), ("KENYA", 0),
    ("MOROCCO", 0), ("MOZAMBIQUE", 0), ("PERU", 1), ("CHINA", 2), ("ROMANIA", 3),
    ("SAUDI ARABIA", 4), ("VIETNAM", 2), ("RUSSIA", 3), ("UNITED KINGDOM", 3),
    ("UNITED STATES", 1),
]

MKTSEGMENTS = ["AUTOMOBILE", "BUILDING", "FURNITURE", "MACHINERY", "HOUSEHOLD"]
PRIORITIES = ["1-URGENT", "2-HIGH", "3-MEDIUM", "4-NOT SPECIFIED", "5-LOW"]
SHIPMODES = ["REG AIR", "AIR", "RAIL", "SHIP", "TRUCK", "MAIL", "FOB"]
SHIPINSTRUCT = ["DELIVER IN PERSON", "COLLECT COD", "NONE", "TAKE BACK RETURN"]
CONTAINERS = [f"{a} {b}" for a in ("SM", "LG", "MED", "JUMBO", "WRAP")
              for b in ("CASE", "BOX", "BAG", "JAR", "PKG", "PACK", "CAN", "DRUM")]
TYPES = [f"{a} {b} {c}"
         for a in ("STANDARD", "SMALL", "MEDIUM", "LARGE", "ECONOMY", "PROMO")
         for b in ("ANODIZED", "BURNISHED", "PLATED", "POLISHED", "BRUSHED")
         for c in ("TIN", "NICKEL", "BRASS", "STEEL", "COPPER")]
COLOURS = ["almond", "antique", "aquamarine", "azure", "beige", "bisque", "black",
           "blanched", "blue", "blush", "brown", "burlywood", "burnished", "chartreuse",
           "chiffon", "chocolate", "coral", "cornflower", "cornsilk", "cream", "cyan",
           "dark", "deep", "dim", "dodger", "drab", "firebrick", "floral", "forest",
           "frosted", "gainsboro", "ghost", "goldenrod", "green", "grey", "honeydew"]
WORDS = ["furiously", "carefully", "blithely", "quickly", "slyly", "regular", "final",
         "special", "express", "pending", "bold", "ironic", "silent", "even", "busy",
         "packages", "requests", "accounts", "deposits", "instructions", "theodolites",
         "dependencies", "excuses", "frays", "asymptotes", "platelets", "warthogs"]

DATE_START = datetime.date(1992, 1, 1)
DATE_END = datetime.date(1998, 8, 2)
_SPAN = (DATE_END - DATE_START).days


def _text(rng: random.Random, max_len: int) -> str:
    out = []
    length = 0
    while length < max_len - 8:
        w = rng.choice(WORDS)
        out.append(w)
        length += len(w) + 1
    return " ".join(out)[:max_len]


def _date(rng: random.Random, offset_days: int = 0) -> str:
    d = DATE_START + datetime.timedelta(days=rng.randint(0, _SPAN) + offset_days)
    return d.isoformat()


def build(path: str, scale: float = SCALE_FACTOR) -> str:
    rng = random.Random(SEED)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(DDL)

    n_part = max(1, int(200_000 * scale))
    n_supp = max(1, int(10_000 * scale))
    n_cust = max(1, int(150_000 * scale))
    n_order = max(1, int(1_500_000 * scale))

    conn.executemany("INSERT INTO region VALUES (?,?,?)",
                     [(i, name, _text(rng, 60)) for i, name in enumerate(REGIONS)])
    conn.executemany("INSERT INTO nation VALUES (?,?,?,?)",
                     [(i, name, rkey, _text(rng, 60))
                      for i, (name, rkey) in enumerate(NATIONS)])

    conn.executemany(
        "INSERT INTO part VALUES (?,?,?,?,?,?,?,?,?)",
        [(k,
          " ".join(rng.sample(COLOURS, 5)),
          f"Manufacturer#{rng.randint(1, 5)}",
          # Brand encodes the manufacturer, as in the spec: Brand#<M><N>.
          f"Brand#{rng.randint(1, 5)}{rng.randint(1, 5)}",
          rng.choice(TYPES),
          rng.randint(1, 50),
          rng.choice(CONTAINERS),
          # Spec formula: (90000 + ((key/10) mod 20001) + 100 * (key mod 1000)) / 100
          round((90000 + ((k // 10) % 20001) + 100 * (k % 1000)) / 100.0, 2),
          _text(rng, 22))
         for k in range(1, n_part + 1)])

    conn.executemany(
        "INSERT INTO supplier VALUES (?,?,?,?,?,?,?)",
        [(k, f"Supplier#{k:09d}", _text(rng, 25), rng.randrange(len(NATIONS)),
          f"{rng.randint(10, 34)}-{rng.randint(100, 999)}-"
          f"{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
          round(rng.uniform(-999.99, 9999.99), 2), _text(rng, 100))
         for k in range(1, n_supp + 1)])

    # 4 suppliers per part, spread deterministically across the supplier table.
    conn.executemany(
        "INSERT INTO partsupp VALUES (?,?,?,?,?)",
        [(p, ((p + i * (n_supp // 4)) % n_supp) + 1,
          rng.randint(1, 9999), round(rng.uniform(1.0, 1000.0), 2), _text(rng, 120))
         for p in range(1, n_part + 1) for i in range(4)])

    conn.executemany(
        "INSERT INTO customer VALUES (?,?,?,?,?,?,?,?)",
        [(k, f"Customer#{k:09d}", _text(rng, 40), rng.randrange(len(NATIONS)),
          f"{rng.randint(10, 34)}-{rng.randint(100, 999)}-"
          f"{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
          round(rng.uniform(-999.99, 9999.99), 2), rng.choice(MKTSEGMENTS),
          _text(rng, 116))
         for k in range(1, n_cust + 1)])

    # Orders and their lineitems are built together so o_totalprice is the true
    # sum of its lines — the fan-out invariants depend on that identity holding.
    orders_rows = []
    line_rows = []
    for okey in range(1, n_order + 1):
        odate = DATE_START + datetime.timedelta(days=rng.randint(0, _SPAN))
        n_lines = rng.randint(1, 7)
        total = 0.0
        for line in range(1, n_lines + 1):
            partkey = rng.randint(1, n_part)
            suppkey = rng.randint(1, n_supp)
            qty = float(rng.randint(1, 50))
            # Extended price tracks quantity and the part's price band, as the
            # spec does; the exact constant does not matter, the correlation does.
            ext = round(qty * (900.0 + (partkey % 2000) / 10.0), 2)
            discount = round(rng.randint(0, 10) / 100.0, 2)
            tax = round(rng.randint(0, 8) / 100.0, 2)
            ship = odate + datetime.timedelta(days=rng.randint(1, 121))
            commit = odate + datetime.timedelta(days=rng.randint(30, 90))
            receipt = ship + datetime.timedelta(days=rng.randint(1, 30))
            # Returned/fulfilled status follows the ship date, as in the spec:
            # everything shipped before the cutoff is closed.
            if receipt <= datetime.date(1995, 6, 17):
                flag, status = rng.choice(["R", "A"]), "F"
            else:
                flag, status = "N", "O"
            total += ext * (1 - discount) * (1 + tax)
            line_rows.append((okey, partkey, suppkey, line, qty, ext, discount, tax,
                              flag, status, ship.isoformat(), commit.isoformat(),
                              receipt.isoformat(), rng.choice(SHIPINSTRUCT),
                              rng.choice(SHIPMODES), _text(rng, 43)))
        ostatus = "F" if all(r[9] == "F" for r in line_rows[-n_lines:]) else (
            "O" if all(r[9] == "O" for r in line_rows[-n_lines:]) else "P")
        orders_rows.append((okey, rng.randint(1, n_cust), ostatus, round(total, 2),
                            odate.isoformat(), rng.choice(PRIORITIES),
                            f"Clerk#{rng.randint(1, 1000):09d}", 0, _text(rng, 78)))
        if len(line_rows) >= 50_000:
            conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", orders_rows)
            conn.executemany(
                "INSERT INTO lineitem VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", line_rows)
            orders_rows, line_rows = [], []
    if orders_rows:
        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", orders_rows)
    if line_rows:
        conn.executemany(
            "INSERT INTO lineitem VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", line_rows)

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
    target = sys.argv[1] if len(sys.argv) > 1 else "dogfood_tpch.db"
    build(target)
    for table, count in stats(target).items():
        print(f"{table:>12}  {count:>9,}")
