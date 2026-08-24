"""A large, populated ERP-shaped SQLite database for dogfooding.

Generated rather than downloaded so the loop is reproducible and needs no
network, no Docker, and no license. The *shape* is what matters, and it is
deliberately hostile in the ways real ERP schemas are:

* **1:N chains** — ``customers -> orders -> order_items`` — so any plan that
  joins before aggregating double-counts. This is what the breakdown-sums-to-total
  invariant hunts.
* **Two FKs to one table** — ``orders.billing_address_id`` and
  ``shipping_address_id`` both point at ``addresses``.
* **FK name != table name** — ``orders.placed_by_id -> customers``, the Odoo
  pattern that defeats name-only inference.
* **Singular and plural table names in one schema** — ``person``, ``customers``.
* **A self-referencing FK** — ``employees.manager_id -> employees``.
* **A junction table** — ``product_tags`` — so many-to-many fan-out is reachable.
* **NULLs in a measure and in a FK**, because COUNT/SUM/JOIN all treat them
  differently and generated SQL routinely forgets which.
* **Mixed types** — DECIMAL money, REAL, INTEGER, DATE-as-TEXT.

Row counts are large enough that a fan-out bug produces an obviously wrong
number rather than an ambiguous one.
"""

from __future__ import annotations

import os
import random
import sqlite3
from datetime import date, timedelta

# Deterministic: the same database every run, so a finding reproduces.
SEED = 20260720

SCALE = int(os.getenv("DOGFOOD_SCALE", "1"))
N_CUSTOMERS = 800 * SCALE
N_ORDERS = 6_000 * SCALE
N_PRODUCTS = 400 * SCALE
N_EMPLOYEES = 120 * SCALE
N_ADDRESSES = 900 * SCALE

DDL = """
CREATE TABLE regions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT NOT NULL
);

CREATE TABLE addresses (
    id INTEGER PRIMARY KEY,
    city TEXT NOT NULL,
    postcode TEXT,
    region_id INTEGER REFERENCES regions(id)
);

-- Singular table name on purpose; "person" and "customers" coexist.
CREATE TABLE person (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT
);

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    person_id INTEGER REFERENCES person(id),
    segment TEXT NOT NULL,
    credit_limit DECIMAL(12,2),
    signed_up_on TEXT NOT NULL,
    region_id INTEGER REFERENCES regions(id)
);

CREATE TABLE employees (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    department TEXT NOT NULL,
    manager_id INTEGER REFERENCES employees(id),
    salary DECIMAL(12,2)
);

CREATE TABLE categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id),
    unit_price DECIMAL(10,2) NOT NULL,
    weight_kg REAL,
    discontinued INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE tags (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL
);

-- Junction: many-to-many, the other classic fan-out source.
CREATE TABLE product_tags (
    product_id INTEGER NOT NULL REFERENCES products(id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (product_id, tag_id)
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    -- FK name does not contain the target table name (Odoo pattern).
    placed_by_id INTEGER NOT NULL REFERENCES customers(id),
    -- Two FKs to one table.
    billing_address_id INTEGER REFERENCES addresses(id),
    shipping_address_id INTEGER REFERENCES addresses(id),
    sales_rep_id INTEGER REFERENCES employees(id),
    status TEXT NOT NULL,
    order_date TEXT NOT NULL,
    freight DECIMAL(10,2)
);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    discount REAL NOT NULL DEFAULT 0
);

CREATE TABLE payments (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount DECIMAL(12,2) NOT NULL,
    paid_on TEXT NOT NULL,
    method TEXT NOT NULL
);

CREATE INDEX idx_orders_customer ON orders(placed_by_id);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_payments_order ON payments(order_id);
"""

REGIONS = [("North", "US"), ("South", "US"), ("West", "US"), ("East", "US"),
           ("Bavaria", "DE"), ("Catalonia", "ES"), ("Maharashtra", "IN"),
           ("Kanto", "JP")]
SEGMENTS = ["enterprise", "midmarket", "smb", "public sector"]
STATUSES = ["pending", "shipped", "delivered", "cancelled", "returned"]
DEPARTMENTS = ["sales", "support", "logistics", "finance"]
METHODS = ["card", "transfer", "cheque"]
CATEGORIES = ["hardware", "software", "services", "consumables", "licences"]
TAGS = ["bestseller", "clearance", "new", "fragile", "bulk", "restricted"]
CITIES = ["Springfield", "Riverton", "Ashford", "Munich", "Barcelona",
          "Pune", "Yokohama", "Fairview", "Lakeside"]


def build(path: str) -> str:
    """Create (or recreate) the dogfood database at ``path``. Returns the path."""
    rng = random.Random(SEED)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(DDL)

    conn.executemany("INSERT INTO regions (id, name, country) VALUES (?,?,?)",
                     [(i + 1, n, c) for i, (n, c) in enumerate(REGIONS)])
    conn.executemany("INSERT INTO categories (id, name) VALUES (?,?)",
                     [(i + 1, n) for i, n in enumerate(CATEGORIES)])
    conn.executemany("INSERT INTO tags (id, label) VALUES (?,?)",
                     [(i + 1, t) for i, t in enumerate(TAGS)])

    conn.executemany(
        "INSERT INTO addresses (id, city, postcode, region_id) VALUES (?,?,?,?)",
        [(i + 1, rng.choice(CITIES), f"{rng.randint(10000, 99999)}",
          rng.randint(1, len(REGIONS))) for i in range(N_ADDRESSES)])

    conn.executemany(
        "INSERT INTO person (id, full_name, email) VALUES (?,?,?)",
        [(i + 1, f"Person {i + 1}",
          None if i % 37 == 0 else f"person{i + 1}@example.com")
         for i in range(N_CUSTOMERS)])

    start = date(2023, 1, 1)
    conn.executemany(
        "INSERT INTO customers (id, person_id, segment, credit_limit, signed_up_on, region_id) "
        "VALUES (?,?,?,?,?,?)",
        [(i + 1, i + 1, rng.choice(SEGMENTS),
          # NULL credit limit for some: SUM and AVG must skip these.
          None if i % 23 == 0 else round(rng.uniform(1000, 250000), 2),
          (start + timedelta(days=rng.randint(0, 900))).isoformat(),
          rng.randint(1, len(REGIONS))) for i in range(N_CUSTOMERS)])

    employees = []
    for i in range(N_EMPLOYEES):
        # Self-referencing FK; the first employee in each department has no manager.
        manager = None if i < 4 else rng.randint(1, max(1, i))
        employees.append((i + 1, f"Employee {i + 1}", rng.choice(DEPARTMENTS),
                          manager, round(rng.uniform(35000, 190000), 2)))
    conn.executemany(
        "INSERT INTO employees (id, full_name, department, manager_id, salary) "
        "VALUES (?,?,?,?,?)", employees)

    conn.executemany(
        "INSERT INTO products (id, name, category_id, unit_price, weight_kg, discontinued) "
        "VALUES (?,?,?,?,?,?)",
        [(i + 1, f"Product {i + 1}", rng.randint(1, len(CATEGORIES)),
          round(rng.uniform(5, 4000), 2),
          None if i % 31 == 0 else round(rng.uniform(0.1, 50), 3),
          1 if i % 17 == 0 else 0) for i in range(N_PRODUCTS)])

    product_tags = set()
    for pid in range(1, N_PRODUCTS + 1):
        for _ in range(rng.randint(0, 3)):
            product_tags.add((pid, rng.randint(1, len(TAGS))))
    conn.executemany("INSERT INTO product_tags (product_id, tag_id) VALUES (?,?)",
                     sorted(product_tags))

    orders = []
    for i in range(N_ORDERS):
        orders.append((
            i + 1,
            rng.randint(1, N_CUSTOMERS),
            rng.randint(1, N_ADDRESSES),
            # Some orders ship to the billing address; some have none recorded.
            None if i % 19 == 0 else rng.randint(1, N_ADDRESSES),
            None if i % 29 == 0 else rng.randint(1, N_EMPLOYEES),
            rng.choice(STATUSES),
            (start + timedelta(days=rng.randint(0, 900))).isoformat(),
            round(rng.uniform(0, 250), 2),
        ))
    conn.executemany(
        "INSERT INTO orders (id, placed_by_id, billing_address_id, shipping_address_id, "
        "sales_rep_id, status, order_date, freight) VALUES (?,?,?,?,?,?,?,?)", orders)

    items, item_id = [], 1
    for oid in range(1, N_ORDERS + 1):
        # 1..6 lines per order — this is the fan-out multiplier.
        for _ in range(rng.randint(1, 6)):
            items.append((item_id, oid, rng.randint(1, N_PRODUCTS),
                          rng.randint(1, 25), round(rng.uniform(5, 4000), 2),
                          round(rng.choice([0, 0, 0, 0.05, 0.1, 0.2]), 2)))
            item_id += 1
    conn.executemany(
        "INSERT INTO order_items (id, order_id, product_id, quantity, unit_price, discount) "
        "VALUES (?,?,?,?,?,?)", items)

    payments, pid = [], 1
    for oid in range(1, N_ORDERS + 1):
        for _ in range(rng.randint(0, 2)):   # some orders unpaid, some part-paid
            payments.append((pid, oid, round(rng.uniform(10, 9000), 2),
                             (start + timedelta(days=rng.randint(0, 950))).isoformat(),
                             rng.choice(METHODS)))
            pid += 1
    conn.executemany(
        "INSERT INTO payments (id, order_id, amount, paid_on, method) VALUES (?,?,?,?,?)",
        payments)

    conn.commit()
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
    target = sys.argv[1] if len(sys.argv) > 1 else "dogfood.db"
    build(target)
    for table, count in stats(target).items():
        print(f"{table:>16}  {count:>8,}")
