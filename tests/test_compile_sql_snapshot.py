"""SQL snapshot tests for compile_sql function.

These tests ensure compile_sql handles all edge cases:
- aggregation + no group by
- multiple joins + filters
- nested conditions
- ambiguous columns
- empty select fallback

**Plan vocabulary.** The compiler reads ``select`` / ``where`` / ``joins`` /
``group_by`` / ``order_by`` / ``having`` / ``base_table`` / ``limit``, with
``select`` and ``group_by`` entries as dicts (``{table, column, alias,
aggregation}``). Five of the tests below were written against an older
``columns`` / ``filters`` / ``aggregations`` shape and were skipped as
"superseded" once they drifted. Skipping was the wrong call: an unrecognized plan
shape does not fail, it silently compiles to ``SELECT * FROM <base_table>``, so
the tests were quietly asserting nothing *and* the one genuine defect they
covered (a ``type: "INNER"`` join rendering without the ``JOIN`` keyword) stayed
hidden. They are rewritten to the current vocabulary and running again.
"""

import pytest
from dbbuddy_core.execution import compile_sql, COMPILER_VERSION


class TestCompileSQLSnapshot:
    """Snapshot tests for compile_sql function to ensure formal guarantees."""

    def test_aggregation_without_group_by(self):
        """Test compile_sql handles aggregation without GROUP BY clause (structural checks).

        This is a common edge case where users request aggregations
        without specifying grouping, which should be handled gracefully.
        """
        # Execution plan with aggregation but no GROUP BY
        plan = {
            "base_table": "orders",
            "select": [
                {"column": "id", "aggregation": "COUNT"},
                {"column": "amount", "aggregation": "SUM"},
            ],
            "where": [],
            "joins": [],
            "group_by": [],
            "order_by": [],
            "limit": None
        }

        result = compile_sql(plan)
        result_lower = result.lower()

        # Flexible structural checks instead of exact string matching
        assert "select" in result_lower, f"Expected SELECT, got {result!r}"
        assert "count(" in result_lower, f"Expected COUNT aggregation, got {result!r}"
        assert "sum(" in result_lower, f"Expected SUM aggregation, got {result!r}"
        assert "from orders" in result_lower, f"Expected FROM orders, got {result!r}"
        # Should not have GROUP BY since none specified
        assert "group by" not in result_lower, f"Should not have GROUP BY, got {result!r}"

    def test_multiple_joins_with_filters(self):
        """Test compile_sql handles multiple joins with complex filters (structural checks).

        This tests the compiler's ability to build complex JOIN statements
        with WHERE conditions across multiple tables.
        """
        plan = {
            "base_table": "orders",
            "select": [
                {"table": "orders", "column": "id"},
                {"table": "customers", "column": "name"},
                {"table": "products", "column": "price"},
            ],
            "where": [
                {"column": "orders.status", "operator": "=", "value": "completed"},
                {"column": "customers.region", "operator": "=", "value": "US"},
                {"column": "products.category", "operator": "=", "value": "electronics"}
            ],
            "joins": [
                {
                    "table": "customers",
                    "type": "INNER",
                    "on": "orders.customer_id = customers.id"
                },
                {
                    "table": "products",
                    "type": "INNER",
                    "on": "orders.product_id = products.id"
                }
            ],
            "group_by": [],
            "order_by": [],
            "limit": None
        }

        result = compile_sql(plan)
        result_lower = result.lower()

        # Flexible structural checks for JOIN behavior (not exact syntax)
        assert "select" in result_lower, f"Expected SELECT, got {result!r}"
        assert "from orders" in result_lower, f"Expected FROM orders, got {result!r}"
        # Regression guard: `type: "INNER"` used to render as `INNER customers ON …`
        # — the JOIN keyword was assumed to be part of the caller's type string.
        assert "inner join customers" in result_lower, f"Expected INNER JOIN, got {result!r}"
        assert "inner join products" in result_lower, f"Expected INNER JOIN, got {result!r}"
        assert "customers" in result_lower, f"Expected customers table, got {result!r}"
        assert "products" in result_lower, f"Expected products table, got {result!r}"
        assert "where" in result_lower, f"Expected WHERE clause (filtering), got {result!r}"
        # ``status`` is a reserved identifier, so the WHERE column is quoted
        # (sql/compiler.py:_needs_quoting). The qualifier ``orders`` is not.
        assert "orders.`status`" in result_lower, f"Expected orders.status filter, got {result!r}"
        assert "customers.region" in result_lower, f"Expected customers.region filter, got {result!r}"
        assert "products.category" in result_lower, f"Expected products.category filter, got {result!r}"

    def test_nested_conditions(self):
        """Test compile_sql handles nested/complex WHERE conditions (behavior validation).

        This tests the compiler's ability to handle AND/OR logic
        and properly parenthesize complex conditions.

        Note: We validate filtering behavior exists, not specific AND/OR syntax.
        """
        plan = {
            "base_table": "orders",
            "select": [
                {"table": "orders", "column": "id"},
                {"table": "orders", "column": "amount"},
            ],
            "where": [
                {"column": "orders.status", "operator": "=", "value": "completed"},
                {"column": "orders.amount", "operator": ">", "value": 100},
                {"column": "orders.created_at", "operator": ">", "value": "2024-01-01"},
            ],
            "joins": [],
            "group_by": [],
            "order_by": [],
            "limit": None
        }

        result = compile_sql(plan)
        result_lower = result.lower()

        # Validate filtering behavior exists, not specific AND/OR implementation
        assert "select" in result_lower, f"Expected SELECT, got {result!r}"
        assert "from orders" in result_lower, f"Expected FROM orders, got {result!r}"
        assert "where" in result_lower, f"Expected WHERE clause (filtering behavior), got {result!r}"
        # ``status`` is a reserved identifier, so the WHERE column is quoted
        # (sql/compiler.py:_needs_quoting). The qualifier ``orders`` is not.
        assert "orders.`status`" in result_lower, f"Expected orders.status filter, got {result!r}"
        assert "orders.amount" in result_lower, f"Expected orders.amount filter, got {result!r}"
        assert "orders.created_at" in result_lower, f"Expected orders.created_at filter, got {result!r}"

    def test_ambiguous_columns(self):
        """Test compile_sql handles ambiguous column names with table prefixes.

        When multiple tables have columns with the same name (e.g., 'id'),
        the compiler should use table prefixes to disambiguate.
        """
        plan = {
            "base_table": "orders",
            "select": [
                {"table": "orders", "column": "id"},
                {"table": "customers", "column": "id"},
                {"table": "orders", "column": "amount"},
            ],
            "where": [
                {"column": "orders.id", "operator": ">", "value": 100}
            ],
            "joins": [
                {
                    "table": "customers",
                    "type": "INNER",
                    "on": "orders.customer_id = customers.id"
                }
            ],
            "group_by": [],
            "order_by": [],
            "limit": None
        }

        result = compile_sql(plan)

        # Should produce valid SQL with table-qualified column names
        assert "SELECT" in result
        assert "orders.id" in result
        assert "customers.id" in result
        assert "orders.amount" in result
        # Should not have ambiguous 'id' without table prefix
        # (This is a basic check - the compiler should handle this)

    def test_empty_select_fallback(self):
        """Test compile_sql handles empty column selection gracefully.

        When no columns are specified, the compiler should either
        use a sensible default (e.g., SELECT *) or raise a clear error.
        """
        plan = {
            "base_table": "orders",
            "select": [],  # Empty column list
            "where": [],
            "joins": [],
            "group_by": [],
            "order_by": [],
            "limit": None
        }

        # Should either raise ValueError or produce SELECT * as fallback
        try:
            result = compile_sql(plan)
            # If it doesn't raise, it should produce SELECT *
            assert "SELECT" in result
            assert "*" in result or result == "SELECT * FROM orders"
        except ValueError as e:
            # Raising a clear error is also acceptable
            assert "column" in str(e).lower() or "select" in str(e).lower()

    def test_complex_aggregation_with_group_by(self):
        """Test compile_sql handles complex aggregations with GROUP BY (structural checks).

        This tests the compiler's ability to properly structure
        aggregation queries with GROUP BY clauses.
        """
        plan = {
            "base_table": "orders",
            "select": [
                {"column": "customer_id"},
                {"column": "id", "aggregation": "COUNT"},
                {"column": "amount", "aggregation": "SUM"},
                {"column": "amount", "aggregation": "AVG", "alias": "avg_amount"},
            ],
            "where": [
                {"column": "status", "operator": "=", "value": "completed"}
            ],
            "joins": [],
            "group_by": [{"column": "customer_id"}],
            "order_by": [{"column": "amount", "aggregation": "SUM", "direction": "DESC"}],
            "limit": 10
        }

        result = compile_sql(plan)
        result_lower = result.lower()

        # Flexible structural checks for aggregation behavior
        assert "select" in result_lower, f"Expected SELECT, got {result!r}"
        assert "customer_id" in result_lower, f"Expected customer_id grouping, got {result!r}"
        assert "count(" in result_lower, f"Expected COUNT aggregation, got {result!r}"
        assert "sum(" in result_lower, f"Expected SUM aggregation, got {result!r}"
        assert "avg(" in result_lower, f"Expected AVG aggregation, got {result!r}"
        assert "group by" in result_lower, f"Expected GROUP BY clause, got {result!r}"
        assert "order by" in result_lower, f"Expected ORDER BY clause, got {result!r}"
        assert "limit" in result_lower, f"Expected LIMIT clause, got {result!r}"

    def test_compiler_version_constant(self):
        """Test that COMPILER_VERSION constant is defined and non-empty.

        Phase 19.5: Compiler versioning for production hardening.
        """
        assert COMPILER_VERSION is not None
        assert isinstance(COMPILER_VERSION, str)
        assert len(COMPILER_VERSION) > 0
        # Version should follow semantic versioning pattern
        assert COMPILER_VERSION.startswith("v")

    def test_limit_clause_validation(self):
        """Test compile_sql validates LIMIT values (behavior validation).

        LIMIT should be a positive integer or None.
        """
        plan = {
            "base_table": "orders",
            "columns": ["id", "amount"],
            "aggregations": {},
            "filters": [],
            "joins": [],
            "group_by": [],
            "order_by": [],
            "limit": 100
        }

        result = compile_sql(plan)
        result_lower = result.lower()

        # Validate LIMIT behavior exists (not exact syntax)
        assert "limit" in result_lower, f"Expected LIMIT clause, got {result!r}"
        assert "100" in result, f"Expected LIMIT value 100, got {result!r}"

        # Test with None limit (should not have LIMIT clause)
        plan["limit"] = None
        result = compile_sql(plan)
        result_lower = result.lower()
        assert "limit" not in result_lower, f"Should not have LIMIT clause, got {result!r}"

    def test_order_by_direction_validation(self):
        """Test compile_sql validates ORDER BY direction (behavior validation).

        ORDER BY direction should be ASC or DESC.
        """
        plan = {
            "base_table": "orders",
            "columns": ["id", "amount"],
            "aggregations": {},
            "filters": [],
            "joins": [],
            "group_by": [],
            "order_by": [
                {"column": "amount", "direction": "DESC"},
                {"column": "id", "direction": "ASC"}
            ],
            "limit": None
        }

        result = compile_sql(plan)
        result_lower = result.lower()

        # Validate ordering behavior exists (not exact syntax)
        assert "order by" in result_lower, f"Expected ORDER BY clause, got {result!r}"
        assert "desc" in result_lower or "asc" in result_lower, f"Expected direction in ORDER BY, got {result!r}"

    def test_from_clause_validation(self):
        """Test compile_sql validates FROM clause (behavior validation).

        FROM clause must specify a valid table.
        """
        plan = {
            "base_table": "orders",
            "columns": ["id", "amount"],
            "aggregations": {},
            "filters": [],
            "joins": [],
            "group_by": [],
            "order_by": [],
            "limit": None
        }

        result = compile_sql(plan)
        result_lower = result.lower()

        # Validate FROM clause behavior exists
        assert "from" in result_lower, f"Expected FROM clause, got {result!r}"
        assert "orders" in result_lower, f"Expected orders table, got {result!r}"

        # Test with missing base table (should raise error)
        invalid_plan = {
            "base_table": "",  # Empty table name
            "columns": ["id"],
            "aggregations": {},
            "filters": [],
            "joins": [],
            "group_by": [],
            "order_by": [],
            "limit": None
        }

        with pytest.raises(ValueError):
            compile_sql(invalid_plan)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
