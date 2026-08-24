"""Every plan the compiler emits must actually run on every engine.

This is the test that would have caught the `ORDER BY "SUM(payments.amount)"`
defect, and it exists because nothing else could. The unit suite asserts on plan
structure; the dogfood suites execute, but against SQLite — which accepts a quoted
unknown identifier as a *string constant*, so the broken SQL returned rows, in the
wrong order, with no error. PostgreSQL rejects it outright:

    ERROR:  column "SUM(payments.amount)" does not exist

"Top N X by Y" — the most common analytical question there is — was therefore
broken on the documented production engine while every test stayed green.

The check here is deliberately shallow and broad: compile a representative plan,
execute it, and require the database to accept it. It asserts nothing about the
*answer* — dogfood owns correctness — only that the statement is legal SQL for
this engine. That is the axis SQLite cannot cover.

Opt-in like the rest of `tests/integration/`:

    docker compose -f docker-compose.test.yml up -d
    pytest -m integration
"""

import os

import pytest

from dbbuddy_core.db import connect_db
from dbbuddy_core.execution import compile_sql

pytestmark = pytest.mark.integration

# Defaults match docker-compose.test.yml. Host, port *and* credentials are all
# overridable, so this can be pointed at any reachable instance (the demo stack's
# Postgres, for example) without editing the file.
ENGINES = {
    "mysql": dict(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", "rootpw"),
        database=os.getenv("MYSQL_DB", "testdb"),
        port=int(os.getenv("MYSQL_PORT", "3307")),
    ),
    "postgresql": dict(
        host=os.getenv("PG_HOST", "127.0.0.1"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASSWORD", "postgres"),
        database=os.getenv("PG_DB", "testdb"),
        port=int(os.getenv("PG_PORT", "5433")),
    ),
    "sqlserver": dict(
        host=os.getenv("MSSQL_HOST", "127.0.0.1"),
        user=os.getenv("MSSQL_USER", "sa"),
        password=os.getenv("MSSQL_PASSWORD", "Str0ng!Passw0rd"),
        database=os.getenv("MSSQL_DB", "testdb"),
        port=int(os.getenv("MSSQL_PORT", "1433")),
    ),
}

_DDL = {
    "mysql": [
        "CREATE TABLE IF NOT EXISTS gsx_customers (id INT PRIMARY KEY, name VARCHAR(100))",
        "CREATE TABLE IF NOT EXISTS gsx_payments (id INT PRIMARY KEY, customer_id INT, "
        "amount DECIMAL(12,2), paid_at DATE, "
        "CONSTRAINT fk_gsx FOREIGN KEY (customer_id) REFERENCES gsx_customers(id))",
    ],
    "postgresql": [
        "CREATE TABLE IF NOT EXISTS gsx_customers (id INT PRIMARY KEY, name VARCHAR(100))",
        "CREATE TABLE IF NOT EXISTS gsx_payments (id INT PRIMARY KEY, customer_id INT, "
        "amount NUMERIC(12,2), paid_at DATE, "
        "CONSTRAINT fk_gsx FOREIGN KEY (customer_id) REFERENCES gsx_customers(id))",
    ],
    "sqlserver": [
        "IF OBJECT_ID('gsx_customers','U') IS NULL "
        "CREATE TABLE gsx_customers (id INT PRIMARY KEY, name NVARCHAR(100))",
        "IF OBJECT_ID('gsx_payments','U') IS NULL "
        "CREATE TABLE gsx_payments (id INT PRIMARY KEY, customer_id INT, "
        "amount DECIMAL(12,2), paid_at DATE, "
        "CONSTRAINT fk_gsx FOREIGN KEY (customer_id) REFERENCES gsx_customers(id))",
    ],
}

SCHEMA = {
    "gsx_customers": ["id", "name"],
    "gsx_payments": ["id", "customer_id", "amount", "paid_at"],
}

_JOIN = {
    "table": "gsx_payments",
    "on": "gsx_customers.id = gsx_payments.customer_id",
    "type": "INNER",
}
_AGG = {"function": "SUM", "column": {"table": "gsx_payments", "column": "amount"}}


def _ranked_plan(order_by):
    """The shape behind "top N customers by total payments"."""
    return {
        "base_table": "gsx_customers",
        "select": [
            {"table": "gsx_customers", "column": "name"},
            {"table": "gsx_payments", "column": "amount", "aggregation": "SUM"},
        ],
        "joins": [_JOIN],
        "group_by": [
            {"table": "gsx_customers", "column": "name"},
            {"table": "gsx_customers", "column": "id"},
        ],
        "aggregation": _AGG,
        "order_by": order_by,
        "limit": 5,
    }


# Each entry is a plan shape that has broken on at least one engine, or that
# exercises a clause the compiler renders differently per dialect.
PLANS = {
    # The regression this module was written for.
    "ranked-aggregate-preformatted": _ranked_plan(
        {"column": "SUM(gsx_payments.amount)", "direction": "DESC"}
    ),
    "ranked-aggregate-structured": _ranked_plan(
        {"table": "gsx_payments", "column": "amount", "aggregation": "SUM", "direction": "DESC"}
    ),
    "ranked-aggregate-as-list": _ranked_plan(
        [{"column": "SUM(gsx_payments.amount)", "direction": "DESC"}]
    ),
    "grouped-aggregate": {
        "base_table": "gsx_customers",
        "select": [
            {"table": "gsx_customers", "column": "name"},
            {"table": "gsx_payments", "column": "amount", "aggregation": "SUM"},
        ],
        "joins": [_JOIN],
        "group_by": [{"table": "gsx_customers", "column": "name"}],
        "aggregation": _AGG,
    },
    "filtered-aggregate": {
        "base_table": "gsx_payments",
        "select": [{"table": "gsx_payments", "column": "amount", "aggregation": "SUM"}],
        "aggregation": _AGG,
        "where": [
            {"column": "gsx_payments.paid_at", "operator": ">=", "value": "2020-01-01"},
            {"column": "gsx_payments.paid_at", "operator": "<", "value": "2030-01-01"},
        ],
    },
    "having-threshold": {
        "base_table": "gsx_customers",
        "select": [
            {"table": "gsx_customers", "column": "name"},
            {"table": "gsx_payments", "column": "amount", "aggregation": "SUM"},
        ],
        "joins": [_JOIN],
        "group_by": [{"table": "gsx_customers", "column": "name"}],
        "aggregation": _AGG,
        "having": {"column": "amount", "operator": ">", "value": 0, "aggregation": "SUM"},
    },
    "plain-select-with-limit": {
        "base_table": "gsx_customers",
        "select": [{"table": "gsx_customers", "column": "name"}],
        "order_by": {"table": "gsx_customers", "column": "name", "direction": "ASC"},
        "limit": 10,
    },
}


@pytest.fixture(scope="module", params=sorted(ENGINES))
def live(request):
    engine = request.param
    params = ENGINES[engine]
    try:
        # connect_db reports failure by *returning None*, not by raising, so both
        # have to be treated as "engine not available" rather than as a failure.
        conn = connect_db(
            params["host"], params["user"], params["password"], params["database"],
            engine=engine, port=params["port"],
        )
    except Exception as exc:                      # noqa: BLE001 — availability, not a failure
        pytest.skip(f"{engine} not reachable: {exc}")
    if conn is None:
        pytest.skip(f"{engine} not reachable at {params['host']}:{params['port']}")
    cursor = conn.cursor()
    for statement in _DDL[engine]:
        cursor.execute(statement)
    try:
        conn.commit()
    except Exception:                             # noqa: BLE001 — autocommit dialects
        pass
    yield engine, conn
    try:
        conn.close()
    except Exception:                             # noqa: BLE001
        pass


@pytest.mark.parametrize("plan_name", sorted(PLANS))
def test_generated_sql_is_accepted_by_the_database(live, plan_name):
    engine, conn = live
    sql = compile_sql(PLANS[plan_name], engine=engine)

    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        cursor.fetchall()
    except Exception as exc:                      # noqa: BLE001 — the point of the test
        pytest.fail(f"{engine} rejected the generated SQL for {plan_name!r}:\n{sql}\n\n{exc}")


def test_an_aggregate_in_order_by_is_never_quoted_as_an_identifier(live):
    """Pin the specific shape, so a failure names the cause rather than the symptom.

    On SQLite the quoted form is a string constant and executes happily, so the
    execution check above cannot be the only guard.
    """
    engine, _ = live
    sql = compile_sql(
        _ranked_plan({"column": "SUM(gsx_payments.amount)", "direction": "DESC"}),
        engine=engine,
    )
    assert '"SUM(' not in sql and "`SUM(" not in sql and "[SUM(" not in sql, sql


def test_the_broken_form_really_is_rejected(live):
    """Pin the premise: the SQL this module guards against is genuinely invalid.

    Without this, a future refactor could quietly make the guard vacuous — and the
    guard's whole value is that it fails where SQLite cannot. Asserting the
    database *rejects* the known-bad form keeps the module honest about what it is
    protecting.

    Skipped on engines that tolerate the form; the point is the engines that do not.
    """
    engine, conn = live
    broken = (
        "SELECT gsx_customers.name, SUM(gsx_payments.amount) AS sum_amount "
        "FROM gsx_customers "
        "JOIN gsx_payments ON gsx_customers.id = gsx_payments.customer_id "
        "GROUP BY gsx_customers.name "
        'ORDER BY "SUM(gsx_payments.amount)" DESC'
    )
    if engine != "postgresql":
        pytest.skip("quoting rules differ; PostgreSQL is the engine that rejects this")

    cursor = conn.cursor()
    with pytest.raises(Exception) as caught:
        cursor.execute(broken)
        cursor.fetchall()
    assert "SUM(gsx_payments.amount)" in str(caught.value)
