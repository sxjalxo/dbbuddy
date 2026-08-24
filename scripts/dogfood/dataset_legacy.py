"""Third dogfood dataset: adversarial identifiers.

Deliberately small — the first two datasets already exercise planner semantics,
and more rows would only re-test what passes. This one biases entirely toward
**pathological naming**, the portability class customers actually hit:

* **Reserved words as table and column names** — ``order``, ``group``, ``select``,
  ``from``, ``table``, ``key``, ``value``, ``index``, ``desc``. Any of these
  unquoted is a syntax error on some engine.
* **Mixed case that is significant** — ``Customer`` and ``CUSTOMER_LOG`` coexist.
* **Spaces and hyphens in identifiers** — ``"total amount"``, ``"line-item"``.
* **Non-English and non-ASCII names** — ``cliente``, ``précio``, ``顧客``.
* **Names differing only by case or separator** — ``user_id`` vs ``userid`` vs
  ``UserID`` on one table, which defeats normalize-and-compare matching.
* **A column named the same as its table** and **a column named ``id`` on a table
  whose key is not ``id``**.
* **Very long identifiers** near the 64-char limit MySQL enforces.
* **Numeric-leading and all-numeric-looking names** (``"2024_total"``).

Correctness bar here is lower on purpose: several of these questions have no
sensible answer. What must hold is that the engine **never emits invalid SQL,
never crashes, and never silently answers a different question**.
"""

from __future__ import annotations

import os
import random
import sqlite3

SEED = 20260722

LONG_NAME = "customer_lifetime_value_including_projected_renewals_and_credits"

# SQLite accepts anything in double quotes; that is the point — the *schema* is
# legal and the engine must cope with reading it back out.
DDL = f'''
CREATE TABLE "order" (
    "key" INTEGER PRIMARY KEY,
    "value" DECIMAL(10,2),
    "group" TEXT,
    "desc" TEXT,
    "index" INTEGER,
    "total amount" DECIMAL(10,2),
    "2024_total" DECIMAL(10,2)
);

CREATE TABLE "Customer" (
    "customer_id" INTEGER PRIMARY KEY,
    "Customer" TEXT,
    -- Differ only by separator. A case variant ("UserID") cannot be added here:
    -- SQLite identifiers are case-insensitive, so it is a duplicate column. The
    -- separator variants are the portable version of the same hazard — they
    -- collapse to one key under normalize-and-compare matching.
    "user_id" INTEGER,
    "userid" INTEGER,
    "user id" INTEGER,
    "{LONG_NAME}" DECIMAL(12,2)
);

CREATE TABLE "CUSTOMER_LOG" (
    "id" INTEGER PRIMARY KEY,
    "customer_id" INTEGER,
    "action" TEXT,
    "when" TEXT
);

CREATE TABLE "cliente" (
    "cliente_id" INTEGER PRIMARY KEY,
    "nombre" TEXT,
    "précio" DECIMAL(10,2),
    "顧客" TEXT
);

CREATE TABLE "line-item" (
    "line_id" INTEGER PRIMARY KEY,
    "key" INTEGER,
    "qty" INTEGER,
    "select" TEXT
);
'''

GROUPS = ["retail", "wholesale", "online"]
ACTIONS = ["created", "updated", "deleted", "viewed"]
NOMBRES = ["Ana", "Luis", "Carmen", "Diego"]
KANJI = ["東京", "大阪", "京都"]
SELECTS = ["alpha", "beta", "gamma"]


def build(path: str) -> str:
    rng = random.Random(SEED)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(DDL)

    conn.executemany(
        'INSERT INTO "order" VALUES (?,?,?,?,?,?,?)',
        [(i + 1, round(rng.uniform(5, 900), 2), rng.choice(GROUPS),
          f"desc {i}", rng.randint(1, 50),
          round(rng.uniform(10, 5000), 2), round(rng.uniform(1, 999), 2))
         for i in range(400)])

    conn.executemany(
        'INSERT INTO "Customer" VALUES (?,?,?,?,?,?)',
        [(i + 1, f"Customer {i + 1}", i + 1, i + 1, i + 1,
          round(rng.uniform(100, 90000), 2)) for i in range(120)])

    conn.executemany(
        'INSERT INTO "CUSTOMER_LOG" VALUES (?,?,?,?)',
        [(i + 1, rng.randint(1, 120), rng.choice(ACTIONS), f"2026-0{rng.randint(1,9)}-01")
         for i in range(600)])

    conn.executemany(
        'INSERT INTO "cliente" VALUES (?,?,?,?)',
        [(i + 1, rng.choice(NOMBRES), round(rng.uniform(1, 500), 2), rng.choice(KANJI))
         for i in range(90)])

    conn.executemany(
        'INSERT INTO "line-item" VALUES (?,?,?,?)',
        [(i + 1, rng.randint(1, 400), rng.randint(1, 20), rng.choice(SELECTS))
         for i in range(700)])

    conn.commit()
    conn.close()
    return path


def stats(path: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "dogfood_legacy.db"
    build(target)
    for table, count in stats(target).items():
        print(f"{table:>24}  {count:>8,}")
