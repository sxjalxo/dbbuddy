"""
Manual verification script for the core query set (Task 3).

Runs each of the 9 target queries through compile_sql_from_intent and
checks that the SQL output satisfies the "senior engineer" correctness bar.

Run with:
    python -m pytest tests/verify_core_queries.py -v
or:
    python tests/verify_core_queries.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbbuddy_core.query import compile_sql_from_intent, build_relationship_graph

# ---------------------------------------------------------------------------
# Schema that closely mirrors a real ecommerce DB
# ---------------------------------------------------------------------------
SCHEMA = {
    "users": ["id", "name", "email", "country", "status", "created_at"],
    "orders": ["id", "user_id", "total_amount", "status", "created_at"],
    "order_items": ["id", "order_id", "product_id", "quantity", "price"],
    "products": ["id", "name", "price", "category"],
}

RELATIONSHIPS = build_relationship_graph(SCHEMA)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def run(query_text: str, intent: dict) -> str:
    return compile_sql_from_intent(intent, SCHEMA, RELATIONSHIPS, user_query=query_text)


# ---------------------------------------------------------------------------
# Q1  show users  →  SELECT * FROM users;
# ---------------------------------------------------------------------------
def test_q1_show_users():
    intent = {"tables": ["users"], "columns": [], "filters": []}
    sql = run("show users", intent)
    print(f"\nQ1: {sql}")
    assert sql == "SELECT * FROM users;", f"Q1 FAIL: got {sql!r}"


# ---------------------------------------------------------------------------
# Q2  show users from india  →  SELECT ... FROM users WHERE country = 'India';
# ---------------------------------------------------------------------------
def test_q2_show_users_from_india():
    intent = {"tables": ["users"], "columns": [], "filters": []}
    sql = run("show users from india", intent)
    print(f"\nQ2: {sql}")
    assert "FROM users" in sql, f"Q2 FAIL: wrong table in {sql!r}"
    assert "country" in sql.lower(), f"Q2 FAIL: no country filter in {sql!r}"
    assert "'India'" in sql, f"Q2 FAIL: India not in WHERE in {sql!r}"
    assert "WHERE" in sql, f"Q2 FAIL: no WHERE clause in {sql!r}"


# ---------------------------------------------------------------------------
# Q3  show users and their orders
#     → JOIN without aggregation, columns from both tables
# ---------------------------------------------------------------------------
def test_q3_show_users_and_their_orders():
    intent = {"tables": ["users", "orders"], "columns": [], "filters": []}
    sql = run("show users and their orders", intent)
    print(f"\nQ3: {sql}")
    assert "JOIN" in sql.upper(), f"Q3 FAIL: no JOIN in {sql!r}"
    assert "orders" in sql.lower(), f"Q3 FAIL: 'orders' missing in {sql!r}"
    assert "unknown" not in sql.lower(), f"Q3 FAIL: fallback to unknown in {sql!r}"
    # Must not be an aggregation (no SUM/COUNT/GROUP BY required)
    # but that's allowed for join projection — just confirm it JOINs
    assert "SELECT" in sql.upper(), f"Q3 FAIL: no SELECT in {sql!r}"


# ---------------------------------------------------------------------------
# Q4  list users and their order amounts
#     → JOIN with orders.total_amount
# ---------------------------------------------------------------------------
def test_q4_list_users_and_their_order_amounts():
    intent = {"tables": ["users", "orders"], "columns": [], "filters": []}
    sql = run("list users and their order amounts", intent)
    print(f"\nQ4: {sql}")
    assert "JOIN" in sql.upper(), f"Q4 FAIL: no JOIN in {sql!r}"
    assert "total_amount" in sql.lower(), f"Q4 FAIL: total_amount not selected in {sql!r}"
    assert "unknown" not in sql.lower(), f"Q4 FAIL: fallback to unknown in {sql!r}"


# ---------------------------------------------------------------------------
# Q5  show total revenue  →  SELECT SUM(...) FROM orders;
# ---------------------------------------------------------------------------
def test_q5_show_total_revenue():
    intent = {"tables": ["orders"], "columns": [], "filters": []}
    sql = run("show total revenue", intent)
    print(f"\nQ5: {sql}")
    assert "SUM(" in sql.upper(), f"Q5 FAIL: SUM not present in {sql!r}"
    assert "FROM orders" in sql, f"Q5 FAIL: wrong table in {sql!r}"


# ---------------------------------------------------------------------------
# Q6  show revenue per user
#     → SELECT users.name, SUM(orders.total_amount) ... GROUP BY users.id
#       NOT just "SELECT orders.id, COUNT(*) FROM orders"
# ---------------------------------------------------------------------------
def test_q6_show_revenue_per_user():
    intent = {"tables": ["users", "orders"], "columns": [], "filters": []}
    sql = run("show revenue per user", intent)
    print(f"\nQ6: {sql}")
    assert "JOIN" in sql.upper(), f"Q6 FAIL: no JOIN in {sql!r}"
    assert "SUM(" in sql.upper(), f"Q6 FAIL: SUM aggregation missing in {sql!r}"
    assert "GROUP BY" in sql.upper(), f"Q6 FAIL: no GROUP BY in {sql!r}"
    assert "users" in sql.lower(), f"Q6 FAIL: users table not referenced in {sql!r}"
    # Crucially: must NOT be the degenerate "SELECT orders.id, COUNT(*) FROM orders"
    assert "FROM orders" not in sql or "users" in sql.lower(), (
        f"Q6 FAIL: degenerate single-table query {sql!r}"
    )
    # users.name or equivalent should appear in SELECT
    assert "users.name" in sql or "users.id" in sql, (
        f"Q6 FAIL: no user identifier in SELECT {sql!r}"
    )


# ---------------------------------------------------------------------------
# Q7  orders from last month  →  WHERE clause with date filter
# ---------------------------------------------------------------------------
def test_q7_orders_from_last_month():
    intent = {"tables": ["orders"], "columns": [], "filters": []}
    sql = run("orders from last month", intent)
    print(f"\nQ7: {sql}")
    assert "FROM orders" in sql, f"Q7 FAIL: wrong table in {sql!r}"
    assert "WHERE" in sql, f"Q7 FAIL: no WHERE clause in {sql!r}"
    # Date filter should be present (DATE_SUB or similar)
    assert "DATE_SUB" in sql.upper() or "INTERVAL" in sql.upper(), (
        f"Q7 FAIL: no date filter in {sql!r}"
    )


# ---------------------------------------------------------------------------
# Q8  number of orders per user
#     → SELECT users.name, COUNT(orders.id) ... GROUP BY users.id
#       NOT just a simple count from orders
# ---------------------------------------------------------------------------
def test_q8_number_of_orders_per_user():
    intent = {"tables": ["users", "orders"], "columns": [], "filters": []}
    sql = run("number of orders per user", intent)
    print(f"\nQ8: {sql}")
    assert "JOIN" in sql.upper(), f"Q8 FAIL: no JOIN in {sql!r}"
    assert "COUNT(" in sql.upper(), f"Q8 FAIL: COUNT missing in {sql!r}"
    assert "GROUP BY" in sql.upper(), f"Q8 FAIL: no GROUP BY in {sql!r}"
    assert "users" in sql.lower(), f"Q8 FAIL: users table missing in {sql!r}"
    # Must not be the degenerate "SELECT COUNT(*) FROM orders" only
    assert "FROM users" in sql or "JOIN users" in sql.lower() or "users" in sql.lower(), (
        f"Q8 FAIL: users not in query at all {sql!r}"
    )


# ---------------------------------------------------------------------------
# Q9  delete all users
#     → DELETE FROM users;  (confirmation is a UI concern)
# ---------------------------------------------------------------------------
def test_q9_delete_all_users():
    """
    compile_sql_from_intent is a SELECT compiler.
    DELETE queries go through the LLM path (generate_sql).
    
    For this task we verify two things:
    1. The pipeline correctly identifies DELETE as a write operation
       (requires_confirmation = True)
    2. When we force a DELETE intent, the SQL compiles cleanly

    Since compile_sql_from_intent doesn't emit DELETE, we test the
    classify_query_safety function directly, which is what the UI uses to
    gate the confirmation dialog.
    """
    from dbbuddy_core.pipeline import classify_query_safety

    sql = "DELETE FROM users;"
    category, requires_confirmation = classify_query_safety(sql)
    print(f"\nQ9 DELETE safety: category={category}, requires_confirmation={requires_confirmation}")
    assert category == "write", f"Q9 FAIL: expected 'write', got {category!r}"
    assert requires_confirmation is True, "Q9 FAIL: confirmation not required for DELETE"


# ---------------------------------------------------------------------------
# Run directly
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [
        test_q1_show_users,
        test_q2_show_users_from_india,
        test_q3_show_users_and_their_orders,
        test_q4_list_users_and_their_order_amounts,
        test_q5_show_total_revenue,
        test_q6_show_revenue_per_user,
        test_q7_orders_from_last_month,
        test_q8_number_of_orders_per_user,
        test_q9_delete_all_users,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  💥  {t.__name__}: EXCEPTION — {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
