"""Live-database integration tests for the dialect layer.

Opt-in — excluded from the default test run. To run:

    docker compose -f docker-compose.test.yml up -d
    pip install pymssql
    pytest -m integration

Each engine is prepared (DB created for SQL Server + schema/rows seeded) and any
engine that isn't reachable is skipped, so a partial stack still tests what's up.
Connection params default to the compose file and can be overridden via env vars.
"""

import os
import time

import pytest

from dbbuddy_core.db import connect_db
from dbbuddy_core.execution import compile_sql

pytestmark = pytest.mark.integration

# engine -> connection params (defaults match docker-compose.test.yml)
ENGINES = {
    "mysql": dict(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"), user="root", password="rootpw",
        database="testdb", port=int(os.getenv("MYSQL_PORT", "3307")),
    ),
    "postgresql": dict(
        host=os.getenv("PG_HOST", "127.0.0.1"), user="postgres", password="postgres",
        database="testdb", port=int(os.getenv("PG_PORT", "5433")),
    ),
    "sqlserver": dict(
        host=os.getenv("MSSQL_HOST", "127.0.0.1"), user="sa", password="Str0ng!Passw0rd",
        database="testdb", port=int(os.getenv("MSSQL_PORT", "1433")),
    ),
}

# Idempotent schema: two tables with a primary key + a foreign key.
_SEED_DDL = {
    "mysql": [
        "CREATE TABLE IF NOT EXISTS customers (id INT PRIMARY KEY, name VARCHAR(100))",
        "CREATE TABLE IF NOT EXISTS orders (id INT PRIMARY KEY, customer_id INT, amount INT, "
        "CONSTRAINT fk_orders_cust FOREIGN KEY (customer_id) REFERENCES customers(id))",
    ],
    "postgresql": [
        "CREATE TABLE IF NOT EXISTS customers (id INT PRIMARY KEY, name VARCHAR(100))",
        "CREATE TABLE IF NOT EXISTS orders (id INT PRIMARY KEY, customer_id INT, amount INT, "
        "CONSTRAINT fk_orders_cust FOREIGN KEY (customer_id) REFERENCES customers(id))",
    ],
    "sqlserver": [
        "IF OBJECT_ID('customers','U') IS NULL "
        "CREATE TABLE customers (id INT PRIMARY KEY, name NVARCHAR(100))",
        "IF OBJECT_ID('orders','U') IS NULL "
        "CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT, amount INT, "
        "CONSTRAINT fk_orders_cust FOREIGN KEY (customer_id) REFERENCES customers(id))",
    ],
}

_SEED_ROWS = [
    "DELETE FROM orders",
    "DELETE FROM customers",
    "INSERT INTO customers (id, name) VALUES (1, 'Acme')",
    "INSERT INTO customers (id, name) VALUES (2, 'Globex')",
    "INSERT INTO orders (id, customer_id, amount) VALUES (1, 1, 100)",
    "INSERT INTO orders (id, customer_id, amount) VALUES (2, 1, 200)",
    "INSERT INTO orders (id, customer_id, amount) VALUES (3, 2, 300)",
]


def _connect(engine, params, database=None):
    return connect_db(
        params["host"], params["user"], params["password"],
        database or params["database"], engine=engine, port=params["port"],
    )


def _prepare(engine, params, timeout=180):
    """Wait for the engine, ensure the DB exists, seed schema + rows. Returns
    the params on success, or None if it never became reachable."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # SQL Server: the target DB isn't auto-created — make it via master.
        if engine == "sqlserver":
            master = _connect(engine, params, database="master")
            if master is not None:
                try:
                    master.autocommit = True
                    cur = master.cursor()
                    cur.execute("IF DB_ID('testdb') IS NULL CREATE DATABASE testdb")
                    cur.close()
                finally:
                    master.close()

        conn = _connect(engine, params)
        if conn is not None:
            try:
                conn.autocommit = True
                cur = conn.cursor()
                for stmt in _SEED_DDL[engine] + _SEED_ROWS:
                    cur.execute(stmt)
                cur.close()
            finally:
                conn.close()
            return params
        time.sleep(3)
    return None


@pytest.fixture(scope="session")
def prepared_engines():
    ready = {}
    for engine, params in ENGINES.items():
        prepared = _prepare(engine, params)
        if prepared:
            ready[engine] = prepared
    if not ready:
        pytest.skip("No live databases reachable — run docker-compose.test.yml first")
    return ready


@pytest.fixture(params=list(ENGINES))
def live(request, prepared_engines):
    engine = request.param
    if engine not in prepared_engines:
        pytest.skip(f"{engine} not reachable")
    conn = _connect(engine, prepared_engines[engine])
    assert conn is not None
    conn.autocommit = True
    yield engine, conn
    conn.close()


def test_connection_uses_mapped_port(live):
    _, conn = live
    # Reaching here means connect_db succeeded with the engine's mapped port.
    assert conn is not None


def test_fetch_schema_has_seeded_tables(live):
    _, conn = live
    schema = conn.fetch_schema()
    assert "customers" in schema and "orders" in schema
    assert "name" in schema["customers"]
    assert {"id", "customer_id", "amount"}.issubset(set(schema["orders"]))


def test_rich_schema_pk_and_fk(live):
    _, conn = live
    rich = conn.fetch_schema_rich()

    customers = rich.tables["customers"]
    pk_cols = [c.name for c in customers.columns if c.is_primary_key]
    assert pk_cols == ["id"]

    orders = rich.tables["orders"]
    assert any(
        fk.column == "customer_id"
        and fk.referenced_table == "customers"
        and fk.referenced_column == "id"
        for fk in orders.foreign_keys
    ), f"expected orders.customer_id -> customers.id FK, got {orders.foreign_keys}"


def test_dict_cursor_returns_dicts(live):
    _, conn = live
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, name FROM customers ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    assert rows[0]["name"] == "Acme"
    assert rows[1]["name"] == "Globex"


def test_row_limit_executes_live(live):
    """The engine-appropriate limit (SQL Server TOP vs LIMIT) runs against a
    real server and returns exactly the requested number of rows."""
    engine, conn = live
    plan = {
        "base_table": "orders", "limit": 2,
        "where": [], "joins": [], "group_by": [], "order_by": None, "having": None,
        "select": [{"table": "orders", "column": "id", "alias": "id", "aggregation": None}],
    }
    sql = compile_sql(plan, engine=engine)
    if engine == "sqlserver":
        assert "TOP 2" in sql and "LIMIT" not in sql
    else:
        assert "LIMIT 2" in sql

    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()
    assert len(rows) == 2
