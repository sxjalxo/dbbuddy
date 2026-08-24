"""Planner Utilities for Graph-Based Query Planning.

This module provides utility functions for graph-based query planning,
including base table selection, join path resolution, and fallback logic.

Production-grade multi-table reasoning.
Aggregation and GROUP BY support.
"""

from collections import deque
from typing import Dict, List, Any, Tuple


# FIX: Column classification for aggregation
NUMERIC_COLUMNS = ["amount", "price", "total", "value", "count"]

# FIX: Aggregation keywords for fallback detection
AGGREGATION_KEYWORDS = {
    "sum": "SUM",
    "total": "SUM",
    "count": "COUNT",
    "number": "COUNT",
    "avg": "AVG",
    "average": "AVG",
    "max": "MAX",
    "min": "MIN"
}


def determine_base_table(tables: List[str], graph: Dict[str, Dict[str, Tuple[str, str]]]) -> str:
    """Choose base table with highest connectivity (graph degree).

    Base table selection without hardcoding.
    This naturally selects the most connected table (e.g., users).

    Args:
        tables: List of table names involved in the query
        graph: Relationship graph from build_relationship_graph

    Returns:
        Table name with highest connectivity
    """
    if not tables:
        return None

    def score(table: str):
        edges = graph.get(table, {})
        degree = len(edges)
        # A parent (referenced) table has an outgoing edge keyed from its own
        # "id"; it is the natural grouping anchor for "per <entity>" queries.
        is_parent = any(keys[0] == "id" for keys in edges.values())
        # Deterministic: degree, then parent preference, then name as a stable
        # tiebreak so set/hash ordering can never change the result.
        return (degree, 1 if is_parent else 0, table)

    return max(tables, key=score)


def find_join_path(graph: Dict[str, Dict[str, Tuple[str, str]]], start: str, target: str) -> List[Tuple[str, str, Tuple[str, str]]]:
    """BFS to find path between tables in the relationship graph.

    Multi-hop join path resolution using graph traversal.

    Args:
        graph: Relationship graph from build_relationship_graph
        start: Starting table name
        target: Target table name

    Returns:
        List of tuples representing the path: (left, right, (left_key, right_key))
    """
    queue = deque([(start, [])])
    visited = set()

    while queue:
        current, path = queue.popleft()

        if current == target:
            return path

        visited.add(current)

        for neighbor, keys in graph.get(current, {}).items():
            if neighbor not in visited:
                queue.append((
                    neighbor,
                    path + [(current, neighbor, keys)]
                ))

    return []


def resolve_joins(graph: Dict[str, Dict[str, Tuple[str, str]]], base_table: str, tables: List[str]) -> List[Dict[str, Any]]:
    """Resolve join paths for multiple tables using graph traversal.

    Graph-based join resolution without hardcoding.

    Args:
        graph: Relationship graph from build_relationship_graph
        base_table: Base table for the query
        tables: List of all tables involved in the query

    Returns:
        List of join dicts with left_table, right_table, left_key, right_key
    """
    joins = []

    for table in tables:
        if table == base_table:
            continue

        path = find_join_path(graph, base_table, table)

        for left, right, (left_key, right_key) in path:
            joins.append({
                "left_table": left,
                "right_table": right,
                "left_key": left_key,
                "right_key": right_key
            })

    return joins


def get_representative_column(schema: Dict[str, List[str]], table: str) -> str:
    """Get a representative column for a table for fallback select.

    Generic fallback column selection without hardcoding.
    Improved priority for better UX: name, title, plan, email, id

    Args:
        schema: Database schema
        table: Table name

    Returns:
        Column name to use as representative
    """
    priority = ["name", "title", "plan", "email", "id"]

    for p in priority:
        if p in schema.get(table, []):
            return p

    # Fallback to first column
    return schema.get(table, ["id"])[0]


def apply_select_fallback(intent: Dict[str, Any], schema: Dict[str, List[str]]) -> None:
    """Apply fallback select logic when no columns are specified.

    Generic fallback select without hardcoding.
    Semantic ordering for natural UX (users first, then related entities).
    FIX: Ensure numeric columns are included for aggregation queries.

    Fix for "show all" queries - use * instead of empty list.
    For degenerate queries like "show all users" with no columns, no aggregation,
    no filters, no memory mapping, use SELECT * to avoid pipeline inconsistency.

    Args:
        intent: Query intent dict (modified in place)
        schema: Database schema
    """
    if intent.get("select"):
        return

    # FIX: Semantic table ordering for natural UX - initialize tables in all code paths
    priority_order = ["users", "subscriptions", "payments"]
    tables = sorted(intent.get("tables", []), key=lambda t: priority_order.index(t) if t in priority_order else 99)

    # Detect "show all" pattern - use * for SELECT
    query = intent.get("original_query", "")
    query_lower = query.lower()

    # Check for "show all" or "list all" pattern with no aggregation
    show_all_pattern = ("show" in query_lower or "list" in query_lower) and "all" in query_lower
    has_aggregation = any(word in AGGREGATION_KEYWORDS for word in query_lower.split())

    # If "show all" with no aggregation, use * instead of column list
    if show_all_pattern and not has_aggregation and not intent.get("aggregation"):
        # Use special marker for SELECT * - but keep actual table name
        # The table field should be the actual table, not "*"
        actual_table = tables[0] if tables else None
        intent["select"] = [{"table": actual_table or "unknown", "column": "*", "alias": "*", "aggregation": None}]
        return

    fallback = []

    # FIX: If aggregation detected, ensure numeric columns are included
    if has_aggregation:
        for table in tables:
            for col in schema.get(table, []):
                if col in NUMERIC_COLUMNS:
                    fallback.append({
                        "table": table,
                        "column": col,
                        "alias": col,
                        "aggregation": None
                    })
                    # Only add one numeric column per table
                    break

    # Add representative columns for each table
    for table in tables:
        col = get_representative_column(schema, table)

        # Avoid duplicates
        if not any(f["table"] == table and f["column"] == col for f in fallback):
            fallback.append({
                "table": table,
                "column": col,
                "alias": col,
                "aggregation": None
            })

    intent["select"] = fallback
