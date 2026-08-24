"""Execution module for SQL compilation and execution.

This module handles SQL compilation from execution plans, query execution,
and result processing.

Compiler versioning for production hardening.
Add query budget/guardrails for production safety.
Move validate_aggregation and get_dry_run_estimate to break circular import.
"""

import re
import time

from dbbuddy_core.logger import get_logger
from dbbuddy_core.query import safe_execute, get_query_type, validate_against_schema

# SQL compilation now lives in the dbbuddy_core.sql package. Re-exported here so
# existing importers keep working unchanged:
#     from dbbuddy_core.execution import compile_sql, compile_parameterized_sql
from dbbuddy_core.sql.compiler import compile_parameterized_sql, compile_sql  # noqa: F401

logger = get_logger()

# Compiler versioning for production hardening
# This version tracks the SQL compiler implementation for observability
# and rollback capabilities, and it keys the response cache (see
# orchestrator._QUERY_CACHE_PREFIX) — bump it whenever the emitted SQL changes
# for an unchanged plan, or cached responses will keep serving the old SQL.
#
# v2: ORDER BY assembly no longer quotes an aggregate expression as an
#     identifier. See tests/test_order_by_compilation.py.
COMPILER_VERSION = "v2"

# Query budget/guardrails for production safety
# These limits prevent DB overload and ensure system stability
MAX_ROWS = 1000  # Maximum rows to return from a query
MAX_EXECUTION_TIME = 2.0  # Maximum execution time in seconds


def _extract_count(result: dict) -> int | None:
    """Pull the scalar ``COUNT(*)`` out of a dry-run result row.

    The column name a driver returns for ``COUNT(*)`` varies by engine (MySQL
    ``count(*)``, PostgreSQL/SQL Server ``count`` or an anonymous column), so read
    the row's single value positionally instead of by a hardcoded key. Returns
    None when the count query produced no usable row.
    """
    if not isinstance(result, dict) or not result.get("success"):
        return None
    rows = result.get("results") or []
    if not rows or not isinstance(rows[0], dict) or not rows[0]:
        return None
    try:
        return int(next(iter(rows[0].values())))
    except (TypeError, ValueError):
        return None


def execute_query_safely(conn, sql: str, params=None) -> dict:
    """Execute SQL query with safety checks and guardrails.

    Add query budget/guardrails for production safety.

    Args:
        conn: Database connection
        sql: SQL query string (may contain %s placeholders)
        params: Optional bound values for the placeholders

    Returns:
        Dict with execution results:
        {
            "success": bool,
            "results": list,
            "error": str (if failed),
            "truncated": bool (if results were truncated),
            "execution_time_ms": float (execution time in milliseconds)
        }
    """
    # Phase 7.2: Wrap entire function to catch crashes anywhere
    try:
        start_time = time.time()

        # Execute the query
        execution_result = safe_execute(conn, sql, params)

        execution_time = (time.time() - start_time) * 1000  # Convert to milliseconds

        # Phase 7.2: Guard against non-dict execution results
        if not isinstance(execution_result, dict):
            logger.warning(f"safe_execute returned unexpected type: {type(execution_result)}")
            execution_result = {
                "success": False,
                "error": str(execution_result) if execution_result else "Unknown error",
                "results": []
            }

        # Normalize execution result to dict (defensive programming)
        if isinstance(execution_result, list):
            logger.debug("safe_execute returned list, normalizing to dict")
            execution_result = {
                "success": True,
                "results": execution_result
            }
        elif not isinstance(execution_result, dict):
            logger.warning(f"safe_execute returned unexpected type: {type(execution_result)}")
            execution_result = {
                "success": False,
                "results": [],
                "error": f"Unexpected execution result type: {type(execution_result)}"
            }

        # Check execution time guardrail
        if execution_time > MAX_EXECUTION_TIME * 1000:
            logger.warning(f"Query exceeded execution time limit: {execution_time:.2f}ms > {MAX_EXECUTION_TIME * 1000}ms")

        # Apply row limit guardrail if query succeeded
        if execution_result.get("success") is True:
            results = execution_result.get("results", [])
            fetched = len(results)

            # `fetched` is no longer "how many rows the query has" — reads are
            # bounded at the cursor and the planner also sends a `LIMIT
            # MAX_ROWS + 1`, so a full result set is never materialised. What one
            # extra row proves is only that *more existed*, which is all the
            # caller needs to say "truncated". Reporting the true total would
            # cost a second COUNT query; claiming `fetched` as the total would be
            # a lie, so it is no longer reported at all.
            if fetched > MAX_ROWS:
                logger.warning("Query returned more than %d rows; truncating for display",
                               MAX_ROWS)
                execution_result["results"] = results[:MAX_ROWS]
                execution_result["truncated"] = True
            else:
                execution_result["truncated"] = False

        execution_result["execution_time_ms"] = round(execution_time, 2)

        return execution_result

    except Exception as e:
        import traceback
        logger.debug("\nEXECUTE_QUERY_SAFELY CRASH")
        logger.debug("%s %s", "ERROR:", str(e))
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e),
            "results": [],
            "execution_time_ms": 0
        }


def validate_query(sql: str, schema: dict) -> dict:
    """Validate SQL query against schema.

    Args:
        sql: SQL query string
        schema: Database schema

    Returns:
        Dict with validation results:
        {
            "valid": bool,
            "unknown_tables": list,
            "unknown_columns": list,
            "invalid_joins": list
        }
    """
    # Type guard: ensure sql is actually a string
    if not isinstance(sql, str):
        logger.error(f"Validation received non-SQL input: {type(sql)} → {sql}")
        return {
            "valid": False,
            "unknown_tables": [],
            "unknown_columns": [],
            "invalid_joins": []
        }

    query_type = get_query_type(sql)
    if query_type == "invalid":
        return {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}

    return validate_against_schema(sql, schema)


def validate_aggregation(sql: str) -> dict:
    """Validate GROUP BY aggregation rules.

    Ensures that all non-aggregated columns in SELECT are either:
    - In the GROUP BY clause, OR
    - Used with an aggregate function (SUM, COUNT, AVG, MAX, MIN, etc.)

    Args:
        sql: SQL statement to validate

    Returns:
        dict with 'valid' (bool), 'error' (str if invalid), and 'violations' (list of invalid columns)
    """
    sql_lower = sql.lower().strip()

    # Extract SELECT clause
    select_match = re.search(r'select\s+(.+?)\s+from', sql_lower, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return {"valid": True, "error": None, "violations": []}

    select_clause = select_match.group(1)

    # Extract GROUP BY clause
    group_by_match = re.search(r'group by\s+(.+?)(?:\s+having|\s+order by|\s+limit|$)', sql_lower, re.IGNORECASE)
    group_by_columns = []
    if group_by_match:
        group_by_clause = group_by_match.group(1)
        # Split by comma and clean up
        group_by_columns = [col.strip() for col in group_by_clause.split(',')]

    # Aggregate functions to detect
    aggregate_functions = ['sum', 'count', 'avg', 'max', 'min', 'stddev', 'variance', 'group_concat']

    # Extract columns from SELECT clause
    # Remove subqueries, handle aliases
    select_columns = []
    for col in select_clause.split(','):
        col = col.strip()
        # Remove aliases (AS or space before alias)
        col = re.sub(r'\s+as\s+.*$', '', col, flags=re.IGNORECASE)
        col = re.sub(r'\s+\w+\s*$', '', col)  # Remove trailing alias
        col = col.strip()

        # Check if it's an aggregate function
        is_aggregate = any(func in col for func in aggregate_functions)

        if not is_aggregate and col and col != '*':
            select_columns.append(col)

    # Check for violations
    violations = []
    for col in select_columns:
        # Clean up column name (remove table prefix, functions, etc.)
        col_clean = col

        # Remove table prefix (e.g., "u.name" -> "name")
        if '.' in col_clean:
            col_clean = col_clean.split('.')[-1]

        # Remove any remaining function calls or expressions
        col_clean = re.sub(r'\([^)]*\)', '', col_clean)
        col_clean = col_clean.strip()

        # Check if this column is in GROUP BY
        in_group_by = any(
            col_clean in group_col or group_col.endswith(f'.{col_clean}') or group_col == col
            for group_col in group_by_columns
        )

        if not in_group_by:
            violations.append(col)

    if violations:
        return {
            "valid": False,
            "error": f"GROUP BY violation: Non-aggregated columns not in GROUP BY clause: {', '.join(violations)}",
            "violations": violations
        }

    return {"valid": True, "error": None, "violations": []}


def get_dry_run_estimate(sql: str, conn) -> dict:
    """Execute dry run count query to estimate impact.

    Args:
        sql: SQL statement to estimate
        conn: Database connection

    Returns:
        dict with estimated row count and affected columns
    """
    try:
        # Determine query type
        query_type = get_query_type(sql)

        if query_type == "delete":
            # Convert "DELETE FROM t [WHERE ...]" to "SELECT COUNT(*) FROM t [WHERE ...]".
            # The WHERE clause MUST be preserved so the estimate reflects only the
            # rows the DELETE will actually remove — not the entire table. (Dropping
            # it here previously inflated the pre-confirmation blast-radius warning.)
            count_sql = re.sub(
                r'^\s*DELETE\s+FROM', 'SELECT COUNT(*) FROM', sql, count=1, flags=re.IGNORECASE
            ).strip()

            result = safe_execute(conn, count_sql)
            estimated_rows = _extract_count(result)
            if estimated_rows is not None:
                return {
                    "estimated_rows": estimated_rows,
                    "affected_columns": []
                }

        elif query_type == "update":
            # Extract table and WHERE clause
            table_match = re.search(r'UPDATE\s+(\w+)', sql, re.IGNORECASE)
            if table_match:
                table = table_match.group(1)
                where_match = re.search(r'WHERE\s+(.+?)(?:\s+LIMIT|$)', sql, re.IGNORECASE)

                if where_match:
                    where_clause = where_match.group(1)
                    count_sql = f"SELECT COUNT(*) FROM {table} WHERE {where_clause}"
                else:
                    count_sql = f"SELECT COUNT(*) FROM {table}"

                result = safe_execute(conn, count_sql)
                estimated_rows = _extract_count(result)
                if estimated_rows is not None:
                    # Extract affected columns
                    set_match = re.search(r'SET\s+(.+?)\s+WHERE', sql, re.IGNORECASE)
                    affected_columns = []
                    if set_match:
                        set_clause = set_match.group(1)
                        for col in set_clause.split(','):
                            col = col.strip().split('=')[0].strip()
                            affected_columns.append(col)

                    return {
                        "estimated_rows": estimated_rows,
                        "affected_columns": affected_columns
                    }

        return None

    except Exception as e:
        logger.error(f"Dry run estimation failed: {e}")
        return None
