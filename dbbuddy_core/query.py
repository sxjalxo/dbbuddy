import os
import re

import requests

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency for local environments
    OpenAI = None


def is_select_query(sql: str) -> bool:
    return sql.strip().lower().startswith("select")


def is_valid_sql(sql: str) -> bool:
    """Return True if the string contains a recognisable SQL statement.

    Relaxed check — allows minor noise/preamble before the keyword so
    LLM responses like "Here is the query: SELECT ..." still pass.
    Still rejects empty strings, 'unknown', and destructive operations.
    """
    if not sql:
        return False

    sql_lower = sql.strip().lower()

    # Reject known non-SQL tokens
    if sql_lower in ("unknown", "invalid", "none", ""):
        return False

    # Safety: reject destructive operations regardless of position
    if "drop" in sql_lower or "truncate" in sql_lower:
        return False

    # Accept if any DML/DQL keyword appears anywhere in the string
    if any(kw in sql_lower for kw in ("select", "insert", "update", "delete", "with", "show", "explain")):
        return True

    return False


def clean_sql_output(sql: str) -> str:
    """Strip markdown fences, leading labels ('SQL:', 'Answer:'), and
    extract the first valid SQL statement from a noisy model response.
    """
    sql = sql.strip()
    sql = sql.replace("```sql", "").replace("```", "")

    # Extract SQL from explanations — handles no semicolon, uppercase SELECT,
    # and multiline output. Captures from SELECT to end-of-statement or end-of-string.
    match = re.search(r"(select[\s\S]*?)(;|$)", sql, re.IGNORECASE)
    if match:
        return match.group(1).strip() + ";"

    for prefix in ("sql:", "answer:", "query:", "result:", "output:"):
        if sql.lower().startswith(prefix):
            sql = sql[len(prefix):].strip()
    for keyword in ("select", "insert", "update", "delete", "with", "show"):
        idx = sql.lower().find(keyword)
        if idx != -1:
            return sql[idx:].strip()
    return sql.strip()


# ── Schema validation ──────────────────────────────────────────────────────

def _extract_identifiers(sql: str) -> tuple[list[str], list[str], list[dict]]:
    """Extract candidate table and column names from a SQL string.

    Uses simple regex heuristics — good enough for validation purposes.
    Returns (tables, columns, joins) as lowercase lists with duplicates removed.
    """
    sql_lower = sql.lower()

    # Tables: FROM x, JOIN x, UPDATE x, INTO x
    table_pat = re.compile(
        r"(?:from|join|update|into)\s+`?(\w+)`?", re.IGNORECASE
    )
    tables = list({m.group(1).lower() for m in table_pat.finditer(sql)})

    # Columns: SELECT a, b, c  |  SET col = ...  |  WHERE col ...
    # Grab everything between SELECT and FROM, plus SET / WHERE clauses
    col_candidates: set[str] = set()

    select_m = re.search(r"select\s+(.*?)\s+from", sql_lower, re.DOTALL)
    if select_m:
        raw = select_m.group(1)
        # Split on commas, strip aliases (AS x), functions, and wildcards
        for part in raw.split(","):
            part = re.sub(r"\b\w+\s*\(.*?\)", "", part)   # strip function calls
            part = re.sub(r"\bas\s+\w+", "", part, flags=re.IGNORECASE)
            part = re.sub(r"`", "", part)
            token = part.strip().split(".")[-1].strip()    # table.col → col
            if token and token != "*":
                col_candidates.add(token.lower())

    where_m = re.findall(r"(?:where|and|or)\s+`?(\w+)`?\s*[=<>!]", sql_lower)
    col_candidates.update(c.lower() for c in where_m)

    # Extract join conditions for validation
    joins = []
    join_pat = re.compile(
        r"join\s+`?(\w+)`?\s+(?:as\s+\w+\s+)?on\s+([^;]+?)(?:\s+(?:join|where|group|order|limit|;))",
        re.IGNORECASE
    )
    for match in join_pat.finditer(sql):
        join_table = match.group(1).lower()
        join_condition = match.group(2).strip()
        
        # Extract column references from join condition
        join_cols = re.findall(r"`?(\w+)`?\.`?(\w+)`?", join_condition)
        joins.append({
            "table": join_table,
            "condition": join_condition,
            "column_refs": join_cols
        })

    return tables, list(col_candidates), joins


def validate_against_schema(sql: str, schema: dict) -> dict:
    """Check that every table, column, and join referenced in sql exists in schema.

    Args:
        sql:    The generated SQL string.
        schema: Raw schema dict  {table_name: [col1, col2, ...]}

    Returns:
        {"valid": bool, "unknown_tables": [...], "unknown_columns": [...], "invalid_joins": [...]}
    """
    if not schema:
        return {"valid": True, "unknown_tables": [], "unknown_columns": [], "invalid_joins": []}

    known_tables = {t.lower() for t in schema}
    known_columns = {
        col.lower()
        for cols in schema.values()
        for col in cols
    }

    tables_used, cols_used, joins = _extract_identifiers(sql)

    unknown_tables = [t for t in tables_used if t not in known_tables]
    # Only flag columns that don't exist in ANY table (hallucinated names)
    unknown_columns = [c for c in cols_used if c not in known_columns]

    # Validate joins
    invalid_joins = []
    for join in joins:
        join_table = join["table"]
        if join_table not in known_tables:
            invalid_joins.append({
                "table": join_table,
                "reason": "table_not_found",
                "condition": join["condition"]
            })
            continue
        
        # Validate column references in join condition
        for table_ref, col_ref in join["column_refs"]:
            table_ref = table_ref.lower()
            col_ref = col_ref.lower()
            
            if table_ref not in known_tables:
                invalid_joins.append({
                    "table": join_table,
                    "reason": "join_table_not_found",
                    "condition": join["condition"],
                    "invalid_ref": f"{table_ref}.{col_ref}"
                })
            elif table_ref in schema and col_ref not in [c.lower() for c in schema[table_ref]]:
                invalid_joins.append({
                    "table": join_table,
                    "reason": "column_not_found",
                    "condition": join["condition"],
                    "invalid_ref": f"{table_ref}.{col_ref}"
                })

    return {
        "valid": not unknown_tables and not unknown_columns and not invalid_joins,
        "unknown_tables": unknown_tables,
        "unknown_columns": unknown_columns,
        "invalid_joins": invalid_joins,
    }


def build_relationship_graph(schema: dict) -> dict:
    """Build a relationship graph from schema to infer foreign key relationships.
    
    Analyzes column names to detect potential foreign key relationships:
    - users.id → orders.user_id
    - orders.id → order_items.order_id
    
    Args:
        schema: Raw schema dict {table_name: [col1, col2, ...]}
    
    Returns:
        dict: Relationship graph {table: {fk_column: (referenced_table, referenced_column)}}
    """
    relationships = {}
    
    for table, columns in schema.items():
        table_lower = table.lower()
        relationships[table] = {}
        
        for col in columns:
            col_lower = col.lower()
            
            # Skip primary keys (id, pk, etc.)
            if col_lower in ("id", "pk", "primary_key"):
                continue
            
            # Look for foreign key patterns: user_id, order_id, etc.
            if col_lower.endswith("_id"):
                # Extract potential referenced table name
                potential_ref_table = col_lower[:-3]  # Remove "_id"
                
                # Check if this table exists in schema
                for ref_table in schema.keys():
                    ref_table_lower = ref_table.lower()
                    
                    # Match: user_id → users, order_id → orders
                    if (potential_ref_table == ref_table_lower or 
                        potential_ref_table + "s" == ref_table_lower or
                        ref_table_lower + "s" == potential_ref_table):
                        
                        # Check if referenced table has an id column
                        if "id" in [c.lower() for c in schema[ref_table]]:
                            relationships[table][col] = (ref_table, "id")
                            break
    
    return relationships


def compile_sql_from_intent(intent: dict, schema: dict, relationships: dict = None, user_query: str = "") -> str:
    """Compile SQL directly from intent structure (deterministic SQL generation).

    Handles:
    - Single-table SELECT with column projection
    - Multi-table JOINs via BFS (including multi-hop: users→orders→order_items→products)
    - Aggregation (SUM/COUNT + GROUP BY grouped by primary table's id, not secondary)
    - WHERE filters from intent, country/status/time/event keywords
    - Behavioral queries (login events, page views, etc.)

    Returns compiled SQL, or "SELECT * FROM unknown;" on failure.
    """
    if not intent or not intent.get("tables"):
        return "SELECT * FROM unknown;"

    tables = intent["tables"]
    primary_table = tables[0]

    # Resolve primary_table case-insensitively
    matched_table = next((t for t in schema if t.lower() == primary_table.lower()), None)
    if matched_table:
        primary_table = matched_table
        tables[0] = matched_table

    # ── Helper: find column in a table by keyword ─────────────────────────
    def _find_col(table: str, *keywords: str) -> str | None:
        """Return the first column in `table` whose name contains any keyword."""
        if table not in schema:
            return None
        for col in schema[table]:
            if any(kw in col.lower() for kw in keywords):
                return col
        return None

    # Handle primary table not in schema fallback:
    if primary_table not in schema:
        cols = intent.get("columns", [])
        select_clause = ", ".join(cols) if cols else "*"
        where_conditions = []
        for f in intent.get("filters", []):
            f_upper = f.upper()
            if any(op in f_upper for op in ("=", ">", "<", "LIKE", "IN ", "BETWEEN", "IS NULL", "IS NOT NULL")):
                where_conditions.append(f)
        where_part = f" WHERE {' AND '.join(where_conditions)}" if where_conditions else ""
        return f"SELECT {select_clause} FROM {primary_table}{where_part};"

    q = user_query.lower() if user_query else ""

    # ── Aggregation intent ────────────────────────────────────────────────
    COUNT_TERMS = ["count", "number", "how many"]
    SUM_TERMS = ["total", "sum", "revenue", "sales", "generated"]
    AVG_TERMS = ["average", "avg"]
    MAX_TERMS = ["max", "highest", "most"]
    MIN_TERMS = ["min", "lowest", "least"]

    wants_count   = any(kw in q for kw in COUNT_TERMS)
    wants_sum     = any(kw in q for kw in SUM_TERMS)
    wants_avg     = any(kw in q for kw in AVG_TERMS)
    wants_max     = any(kw in q for kw in MAX_TERMS)
    wants_min     = any(kw in q for kw in MIN_TERMS)
    wants_aggregation = any([wants_count, wants_sum, wants_avg, wants_max, wants_min])
    wants_group   = any(kw in q for kw in ("per ", "by user", "by customer", "by product",
                                            "by country", "each", "grouped",
                                            "most", "top ", "bottom "))

    # Guard: aggregation without explicit grouping intent on a join query produces
    # ONLY_FULL_GROUP_BY errors. Strip the aggregation flags so the compiler falls
    # through to a plain projection JOIN instead of a broken GROUP BY query.
    if wants_aggregation and not wants_group and len(tables) > 1:
        wants_aggregation = False
        wants_count = wants_sum = wants_avg = wants_max = wants_min = False

    # ── BFS multi-hop join path finder ────────────────────────────────────
    def bfs_join_path(start: str, targets: list[str], graph: dict) -> list[tuple]:
        """BFS over the relationship graph in a bidirectional (undirected) manner.

        Returns an ORDERED list of (from_t, fk_col, to_t, pk_col) edges that
        connect `start` to every table in `targets`, following any intermediate
        bridge tables.
        """
        if not graph or not targets:
            return []

        # Build undirected graph: adjacency list of table -> list of (neighbor_table, edge_tuple)
        adj = {}
        for tbl in schema.keys():
            adj[tbl.lower()] = []

        # Add directed relationships from graph
        for tbl, fks in graph.items():
            tbl_lower = tbl.lower()
            if tbl_lower not in adj:
                adj[tbl_lower] = []
            for fk_col, (ref_tbl, ref_col) in fks.items():
                ref_tbl_lower = ref_tbl.lower()
                if ref_tbl_lower not in adj:
                    adj[ref_tbl_lower] = []
                # Forward edge: (neighbor_table, edge_tuple)
                adj[tbl_lower].append((ref_tbl_lower, (tbl, fk_col, ref_tbl, ref_col)))
                # Backward edge
                adj[ref_tbl_lower].append((tbl_lower, (tbl, fk_col, ref_tbl, ref_col)))

        needed  = set(t.lower() for t in targets)
        visited = {start.lower()}
        queue   = [(start.lower(), [])]
        result_edges: list[tuple] = []
        covered: set[str] = set()

        while queue:
            current, path = queue.pop(0)

            # Sort neighbors of current to prioritize transactional tables
            def neighbor_priority(item):
                neighbor_lower, edge = item
                if "order_items" in neighbor_lower:
                    return 2
                if "orders" in neighbor_lower:
                    return 1
                return 0

            neighbors = sorted(adj.get(current, []), key=lambda item: (-neighbor_priority(item), item[0]))

            for ref_lower, edge in neighbors:
                if ref_lower in visited:
                    continue
                visited.add(ref_lower)
                new_path = path + [edge]

                if ref_lower in needed:
                    covered.add(ref_lower)
                    for e in new_path:
                        if e not in result_edges:
                            result_edges.append(e)

                queue.append((ref_lower, new_path))

            # Stop early if all targets are reachable
            if covered >= needed:
                break

        return result_edges

    # ── Build JOIN clauses ────────────────────────────────────────────────
    join_clauses: list[str] = []
    all_tables   = [primary_table]   # ordered list of tables in the query
    join_path_found = False

    if len(tables) > 1 and relationships:
        edges = bfs_join_path(primary_table, tables[1:], relationships)
        if edges:
            join_path_found = True
        for from_t, fk_col, to_t, pk_col in edges:
            if to_t not in all_tables:
                join_clauses.append(f"JOIN {to_t} ON {from_t}.{fk_col} = {to_t}.{pk_col}")
                all_tables.append(to_t)
            elif from_t not in all_tables:
                join_clauses.append(f"JOIN {from_t} ON {from_t}.{fk_col} = {to_t}.{pk_col}")
                all_tables.append(from_t)

        # Fallback for any target table BFS still couldn't reach
        for table in tables[1:]:
            if table in all_tables:
                continue
            if table in schema and primary_table in relationships:
                for fk_col, (ref_table, ref_col) in relationships[primary_table].items():
                    if ref_table.lower() == table.lower():
                        join_clauses.append(
                            f"JOIN {table} ON {primary_table}.{fk_col} = {table}.{ref_col}"
                        )
                        all_tables.append(table)
                        join_path_found = True
                        break

    is_multi_table = len(all_tables) > 1

    # ── SELECT projection ─────────────────────────────────────────────────
    # Determine the "grouping anchor" table — the one the user is grouping BY.
    # Default is the primary table, but "per order" / "by product" shifts it.
    group_anchor = primary_table
    for t in all_tables:
        t_lower = t.lower()
        if f"per {t_lower}" in q or f"by {t_lower}" in q or f"each {t_lower}" in q:
            group_anchor = t
            break
        # singular forms: "per user" when table is "users"
        singular = t_lower.rstrip("s")
        if f"per {singular}" in q or f"by {singular}" in q:
            group_anchor = t
            break

    if wants_aggregation and is_multi_table:
        # Find the name/label column from the grouping anchor table
        name_col = _find_col(group_anchor, "name", "username", "title", "email")
        group_id_col = f"{group_anchor}.id"
        name_expr   = f"{group_anchor}.{name_col}" if name_col else group_id_col

        # ── Explicit column targeting per aggregation type ────────────────
        # Prefer known column names over heuristic search to avoid picking
        # wrong columns (e.g. using price instead of total_amount for orders).
        def _best_agg_col(tables_to_search: list[str], *keywords: str) -> str | None:
            """Find the best column for aggregation — check explicit keyword list
            in priority order across the given tables (skipping the group anchor)."""
            for t in tables_to_search:
                if t == group_anchor:
                    continue
                col = _find_col(t, *keywords)
                if col:
                    return f"{t}.{col}"
            return None

        non_anchor = [t for t in all_tables if t != group_anchor]

        if wants_count:
            # COUNT the rows in a non-anchor table using its id column
            count_col = _best_agg_col(non_anchor, "id")
            agg_expr  = f"COUNT({count_col}) AS total_count" if count_col else "COUNT(*) AS total_count"

        elif wants_sum:
            # Explicit priority: total_amount > amount > price > value > revenue
            sum_col = _best_agg_col(non_anchor, "total_amount", "amount", "price", "value", "revenue", "total")
            agg_expr = f"SUM({sum_col}) AS total_revenue" if sum_col else "COUNT(*) AS total_count"

        elif wants_avg:
            avg_col  = _best_agg_col(non_anchor, "total_amount", "amount", "price", "value")
            agg_expr = f"AVG({avg_col}) AS average_value" if avg_col else "COUNT(*) AS total_count"

        elif wants_max:
            max_col  = _best_agg_col(non_anchor, "total_amount", "amount", "price", "value")
            agg_expr = f"MAX({max_col}) AS max_value" if max_col else "COUNT(*) AS total_count"

        elif wants_min:
            min_col  = _best_agg_col(non_anchor, "total_amount", "amount", "price", "value")
            agg_expr = f"MIN({min_col}) AS min_value" if min_col else "COUNT(*) AS total_count"

        else:
            agg_expr = "COUNT(*) AS total_count"

        select_clause = f"{name_expr}, {agg_expr}"

    elif wants_aggregation and not is_multi_table:
        # Single-table aggregation
        if wants_count:
            select_clause = "COUNT(*) AS total_count"
        elif wants_sum:
            col = _find_col(primary_table, "amount", "total", "price", "value")
            select_clause = f"SUM({col}) AS total_revenue" if col else "COUNT(*) AS total_count"
        elif wants_avg:
            col = _find_col(primary_table, "amount", "total", "price", "value")
            select_clause = f"AVG({col}) AS average_value" if col else "COUNT(*) AS total_count"
        elif wants_max:
            col = _find_col(primary_table, "amount", "total", "price", "value")
            select_clause = f"MAX({col}) AS max_value" if col else "COUNT(*) AS total_count"
        elif wants_min:
            col = _find_col(primary_table, "amount", "total", "price", "value")
            select_clause = f"MIN({col}) AS min_value" if col else "COUNT(*) AS total_count"
        else:
            select_clause = "COUNT(*) AS total_count"

    elif is_multi_table:
        # Plain JOIN — project name + amount columns from each table
        parts: list[str] = []
        for t in all_tables:
            name_col   = _find_col(t, "name", "username", "title")
            amount_col = _find_col(t, "amount", "total", "price")
            if name_col:
                parts.append(f"{t}.{name_col}")
            if amount_col:
                parts.append(f"{t}.{amount_col}")
        select_clause = ", ".join(parts) if parts else "*"

    else:
        # Single-table plain SELECT — use intent columns or *
        columns = intent.get("columns", [])
        valid = []
        for c in columns:
            matched_col = next((col for col in schema[primary_table] if col.lower() == c.lower()), None)
            if matched_col:
                valid.append(matched_col)
        select_clause = ", ".join(valid) if valid else "*"

    # ── GROUP BY ──────────────────────────────────────────────────────────
    # Group by every non-aggregated column that appears in SELECT.
    # MySQL requires all non-aggregated SELECT expressions to be in GROUP BY.
    group_by_clause = ""
    if wants_aggregation and wants_group and is_multi_table:
        group_id  = _find_col(group_anchor, "id")
        group_id_expr = f"{group_anchor}.{group_id}" if group_id else f"{group_anchor}.id"

        # Collect all non-aggregated column expressions from select_clause
        # (everything before the first aggregation function)
        non_agg_cols: list[str] = []
        for part in select_clause.split(","):
            part = part.strip()
            # Skip aggregation functions
            if any(fn in part.upper() for fn in ("SUM(", "COUNT(", "AVG(", "MAX(", "MIN(")):
                continue
            # Skip bare aliases like "AS total"
            if part.upper().startswith("AS "):
                continue
            if part:
                non_agg_cols.append(part)

        # Always include the id column; add name/label columns found in SELECT
        group_cols = [group_id_expr]
        for col_expr in non_agg_cols:
            if col_expr != group_id_expr and col_expr not in group_cols:
                group_cols.append(col_expr)

        group_by_clause = "GROUP BY " + ", ".join(group_cols)

    # ── WHERE filters ─────────────────────────────────────────────────────
    where_conditions: list[str] = []

    # 1. LLM-extracted filters from intent
    for f in intent.get("filters", []):
        f_upper = f.upper()
        if any(op in f_upper for op in ("=", ">", "<", "LIKE", "IN ", "BETWEEN", "IS NULL", "IS NOT NULL")):
            where_conditions.append(f)

    # 2. Time filters (generic, not just revenue)
    if "last month" in q or "past month" in q:
        time_col = _find_col(primary_table, "created_at", "created", "date", "timestamp", "time")
        col_ref  = f"{primary_table}.{time_col}" if time_col else "created_at"
        where_conditions.append(f"{col_ref} >= DATE_SUB(CURDATE(), INTERVAL 1 MONTH)")
    elif "this month" in q or "current month" in q:
        time_col = _find_col(primary_table, "created_at", "created", "date", "timestamp", "time")
        col_ref  = f"{primary_table}.{time_col}" if time_col else "created_at"
        where_conditions.append(f"MONTH({col_ref}) = MONTH(CURDATE()) AND YEAR({col_ref}) = YEAR(CURDATE())")
    elif "last week" in q or "past week" in q:
        time_col = _find_col(primary_table, "created_at", "created", "date", "timestamp", "time")
        col_ref  = f"{primary_table}.{time_col}" if time_col else "created_at"
        where_conditions.append(f"{col_ref} >= DATE_SUB(CURDATE(), INTERVAL 1 WEEK)")
    elif "today" in q:
        time_col = _find_col(primary_table, "created_at", "created", "date", "timestamp", "time")
        col_ref  = f"{primary_table}.{time_col}" if time_col else "created_at"
        where_conditions.append(f"DATE({col_ref}) = CURDATE()")

    # 3. Country / location filters
    COUNTRY_MAP = {
        "india": "India",       "us": "US",           "usa": "United States",
        "uk": "United Kingdom", "canada": "Canada",   "australia": "Australia",
        "germany": "Germany",   "france": "France",   "japan": "Japan",
    }
    for kw, val in COUNTRY_MAP.items():
        if f"from {kw}" in q or f" in {kw}" in q:
            country_col = _find_col(primary_table, "country", "location", "region")
            col_ref     = f"{primary_table}.{country_col}" if country_col else "country"
            where_conditions.append(f"{col_ref} = '{val}'")

    # 4. Status filters
    STATUS_MAP = {
        "active": "active", "inactive": "inactive",
        "pending": "pending", "completed": "completed", "cancelled": "cancelled",
    }
    for kw, val in STATUS_MAP.items():
        if kw in q:
            status_col = _find_col(primary_table, "status")
            if status_col:
                where_conditions.append(f"{primary_table}.{status_col} = '{val}'")

    # 5. Behavioral / event-type filters
    # Detect event-type queries: "login events", "page view events", etc.
    EVENT_TYPES = {
        "login":      "login",
        "logout":     "logout",
        "signup":     "signup",
        "purchase":   "purchase",
        "click":      "click",
        "page view":  "page_view",
        "view":       "view",
        "search":     "search",
    }
    matched_event: str | None = None
    for kw, event_val in EVENT_TYPES.items():
        if kw in q:
            matched_event = event_val
            break

    if matched_event:
        # Find the events/log table and its event_type column
        event_table = None
        for t in all_tables:
            event_col = _find_col(t, "event_type", "type", "action", "event")
            if event_col:
                event_table = t
                where_conditions.append(f"{t}.{event_col} = '{matched_event}'")
                # For behavioral aggregations: count events per user
                if wants_group and is_multi_table:
                    id_col   = _find_col(t, "id")
                    agg_part = f"COUNT({t}.{id_col}) AS total_count" if id_col else "COUNT(*) AS total_count"
                    name_col  = _find_col(group_anchor, "name", "username", "email")
                    name_part = f"{group_anchor}.{name_col}" if name_col else f"{group_anchor}.id"
                    select_clause = f"{name_part}, {agg_part}"
                    group_id      = _find_col(group_anchor, "id")
                    group_id_expr = f"{group_anchor}.{group_id}" if group_id else f"{group_anchor}.id"
                    # GROUP BY anchor id + name column (MySQL requires all non-agg cols)
                    group_cols = [group_id_expr]
                    if name_col and f"{group_anchor}.{name_col}" not in group_cols:
                        group_cols.append(f"{group_anchor}.{name_col}")
                    group_by_clause = "GROUP BY " + ", ".join(group_cols)
                break

    # 6. Behavioral semantic column map
    # Maps natural language terms to (table_keyword, column_keyword) so queries like
    # "most used device" resolve to the correct table/column without schema guessing.
    BEHAVIORAL_COL_MAP = {
        "device":    ("event", "device"),
        "browser":   ("event", "browser"),
        "platform":  ("event", "platform"),
        "os":        ("event", "os"),
        "source":    ("event", "source"),
        "channel":   ("event", "channel"),
        "campaign":  ("event", "campaign"),
        "page":      ("event", "page"),
        "url":       ("event", "url"),
    }

    # ── ORDER BY / LIMIT (top-N queries) ──────────────────────────────────
    # Initialize early so behavioral handlers below can set them safely.
    order_by_clause = ""
    limit_clause    = ""

    for term, (table_kw, col_kw) in BEHAVIORAL_COL_MAP.items():
        if term in q:
            # Find the matching table and column in the schema
            for t in schema:
                if table_kw in t.lower():
                    col = _find_col(t, col_kw, term)
                    if col:
                        # Rewrite SELECT to show this column with COUNT.
                        # CRITICAL: the behavioral table must be the FROM table so
                        # "events.device" is valid. Override primary_table and reset
                        # join_clauses so we build FROM events (not FROM users).
                        select_clause = f"{t}.{col}, COUNT(*) AS total_count"
                        group_by_clause = f"GROUP BY {t}.{col}"
                        order_by_clause = f"ORDER BY total_count DESC"
                        if not limit_clause:
                            limit_clause = "LIMIT 10"
                        # Rebuild FROM/JOIN chain with behavioral table as anchor
                        if t != primary_table:
                            # Check if we can join from behavioral table back to primary
                            new_join_clauses = []
                            if relationships and t in relationships:
                                for fk_col, (ref_t, ref_col) in relationships[t].items():
                                    if ref_t.lower() == primary_table.lower():
                                        new_join_clauses.append(
                                            f"JOIN {primary_table} ON {t}.{fk_col} = {primary_table}.{ref_col}"
                                        )
                                        break
                            elif relationships and primary_table in relationships:
                                for fk_col, (ref_t, ref_col) in relationships[primary_table].items():
                                    if ref_t.lower() == t.lower():
                                        new_join_clauses.append(
                                            f"JOIN {primary_table} ON {primary_table}.{fk_col} = {t}.{ref_col}"
                                        )
                                        break
                            # Switch primary table to the behavioral table
                            primary_table = t
                            join_clauses = new_join_clauses
                            all_tables = [t] + [at for at in all_tables if at != t]
                    break
            break

    # 7. Numeric comparison filters
    # Handles: "greater than X", "more than X", "above X", "> X",
    #          "less than X", "below X", "under X", "< X"
    import re as _re2
    NUM_GT_PATTERN = _re2.compile(
        r'(?:greater than|more than|above|over|>)\s*(\d[\d,]*)', _re2.IGNORECASE
    )
    NUM_LT_PATTERN = _re2.compile(
        r'(?:less than|below|under|<)\s*(\d[\d,]*)', _re2.IGNORECASE
    )
    gt_m = NUM_GT_PATTERN.search(q)
    lt_m = NUM_LT_PATTERN.search(q)
    if gt_m or lt_m:
        # Find the best numeric column across all tables
        num_col_ref = None
        for t in all_tables:
            col = _find_col(t, "total_amount", "amount", "price", "value", "total", "revenue")
            if col:
                num_col_ref = f"{t}.{col}"
                break
        if num_col_ref:
            if gt_m:
                val = gt_m.group(1).replace(",", "")
                where_conditions.append(f"{num_col_ref} > {val}")
            if lt_m:
                val = lt_m.group(1).replace(",", "")
                where_conditions.append(f"{num_col_ref} < {val}")

    # ── ORDER BY / LIMIT (top-N queries) — continued ─────────────────────
    # Detect "top N" or "bottom N" intent
    import re as _re
    top_m  = _re.search(r'\btop\s+(\d+)\b', q)
    bot_m  = _re.search(r'\bbottom\s+(\d+)\b', q)
    last_m = _re.search(r'\blast\s+(\d+)\b', q)   # "last 5 orders"

    if top_m or bot_m or last_m:
        n = int((top_m or bot_m or last_m).group(1))
        limit_clause = f"LIMIT {n}"

        if wants_aggregation and group_by_clause:
            # Sort by the aggregation result — find the alias in select_clause
            alias_m = _re.search(r'AS\s+(\w+)\s*$', select_clause, _re.IGNORECASE)
            sort_col = alias_m.group(1) if alias_m else select_clause.split(",")[-1].strip()
            direction = "ASC" if bot_m else "DESC"
            order_by_clause = f"ORDER BY {sort_col} {direction}"
        elif not wants_aggregation:
            # Sort by time for "last N orders"
            time_col = _find_col(primary_table, "created_at", "created", "date", "timestamp")
            if time_col:
                order_by_clause = f"ORDER BY {primary_table}.{time_col} DESC"

    # ── Confidence scoring ──
    confidence = 0.0
    if len(tables) == 1:
        confidence = 1.0  # Base confidence for single table queries is high since they are simple
    elif len(tables) >= 2:
        confidence += 0.4
        if join_path_found:
            confidence += 0.3
        if wants_aggregation:
            confidence += 0.2
        if len(where_conditions) > 0:
            confidence += 0.1

    # Fallback to LLM if multiple tables are requested but confidence is low (e.g. no join path)
    if len(tables) >= 2 and confidence < 0.5:
        return "SELECT * FROM unknown;"

    # ── Assemble final SQL ────────────────────────────────────────────────
    parts_sql: list[str] = [f"SELECT {select_clause} FROM {primary_table}"]
    parts_sql.extend(join_clauses)
    if where_conditions:
        parts_sql.append("WHERE " + " AND ".join(where_conditions))
    if group_by_clause:
        parts_sql.append(group_by_clause)
    if order_by_clause:
        parts_sql.append(order_by_clause)
    if limit_clause:
        parts_sql.append(limit_clause)

    return " ".join(parts_sql) + ";"


# ── Intent planning ────────────────────────────────────────────────────────

def _build_few_shot_examples(schema: dict) -> str:
    """Build 2–3 schema-aware few-shot examples from the actual table names."""
    tables = list(schema.keys())
    if not tables:
        return "User: list all records\nSQL: SELECT * FROM records;"

    examples = []
    t0 = tables[0]
    cols0 = list(schema[t0])[:3]

    # Example 1: simple select
    col_list = ", ".join(cols0) if cols0 else "*"
    examples.append(f"User: list all {t0}\nSQL: SELECT {col_list} FROM {t0};")

    # Example 2: count
    examples.append(f"User: how many {t0} are there\nSQL: SELECT COUNT(*) FROM {t0};")

    # Example 3: two-table join if available
    if len(tables) >= 2:
        t1 = tables[1]
        cols1 = list(schema[t1])[:1]
        fk_candidates = [c for c in list(schema[t1]) if t0.rstrip("s") in c]
        if fk_candidates:
            fk = fk_candidates[0]
            pk = cols0[0] if cols0 else "id"
            examples.append(
                f"User: show {t0} with their {t1}\n"
                f"SQL: SELECT * FROM {t0} JOIN {t1} ON {t0}.{pk} = {t1}.{fk};"
            )
        else:
            # Generic join example without specific foreign key
            pk = cols0[0] if cols0 else "id"
            fk = cols1[0] if cols1 else "id"
            examples.append(
                f"User: show {t0} with their {t1}\n"
                f"SQL: SELECT * FROM {t0} JOIN {t1} ON {t0}.{pk} = {t1}.{fk};"
            )

    return "\n\n".join(examples)


def plan_intent(user_query: str, semantic_layer: dict, provider: str = "local") -> dict:
    """Ask the model to produce a structured intent before generating SQL.

    Returns a dict like:
        {
            "intent": "select",
            "tables": ["users"],
            "columns": ["name", "email"],
            "filters": [],
            "reasoning": "User wants a list of all users with their email addresses."
        }

    Falls back gracefully to an empty intent if the model doesn't cooperate.
    """
    import json as _json

    schema_summary = _schema_context(semantic_layer)
    prompt = f"""
You are a database query planner.

Given a database schema and a user question, output a JSON intent plan.

Schema:
{schema_summary}

User Question: {user_query}

Output ONLY valid JSON in this format:
{{
  "intent": "select",
  "tables": ["table1"],
  "columns": ["col1", "col2"],
  "filters": ["col = value"],
  "reasoning": "one sentence explaining why these tables/columns were chosen"
}}

JSON:""".strip()

    raw = ""
    # Use Qwen Coder as the primary local model
    local_model = os.getenv("LOCAL_MODEL", "qwen2.5-coder:7b")
    
    try:
        if provider in ("local", "hybrid"):
            resp = requests.post(
                "http://127.0.0.1:11434/api/generate",
                json={"model": local_model, "prompt": prompt, "stream": False},
                timeout=30,
            )
            raw = resp.json().get("response", "").strip()
        elif provider == "nemotron" and os.getenv("NEMOTRON_API_KEY"):
            nemotron_endpoint = os.getenv("NEMOTRON_ENDPOINT", "https://integrate.api.nvidia.com/v1/chat/completions")
            resp = requests.post(
                nemotron_endpoint,
                headers={
                    "Authorization": f"Bearer {os.getenv('NEMOTRON_API_KEY')}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "nvidia/nemotron-3-270b-4k-instruct",
                    "messages": [
                        {"role": "system", "content": "You are a database query planner. Return ONLY valid JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 256,
                },
                timeout=30,
            )
            raw = resp.json()["choices"][0]["message"]["content"].strip()

        # Extract JSON block
        json_m = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_m:
            intent = _json.loads(json_m.group(0))
            # If the LLM returned tables, trust it
            if intent.get("tables"):
                return intent
    except Exception:
        pass

    # ── Schema-scan fallback ──────────────────────────────────────────────
    # LLM either failed or returned empty tables. Scan the schema directly:
    # any table whose name appears in the query is a reliable match.
    matched_tables = []
    if semantic_layer:
        q_lower = user_query.lower()
        for table in semantic_layer:
            # Match singular and plural forms (users/user, orders/order)
            variants = {table.lower(), table.lower().rstrip("s"), table.lower() + "s"}
            if any(v in q_lower for v in variants if v):
                matched_tables.append(table)

    return {
        "intent": "select",
        "tables": matched_tables,
        "columns": [],
        "filters": [],
        "reasoning": "schema-scan fallback" if matched_tables else "",
    }


def basic_sql_validation(sql: str) -> dict:
    if not sql or len(sql.strip()) == 0:
        return {"valid": False, "reason": "Empty SQL"}

    if ";" in sql.strip()[:-1]:
        return {"valid": False, "reason": "Multiple statements not allowed"}

    return {"valid": True}


def get_query_type(sql: str) -> str:
    if not sql or sql.strip().lower() in ("unknown", "invalid", ""):
        return "invalid"
    tokens = sql.strip().split()
    return tokens[0].lower() if tokens else "invalid"


def handle_basic_query(user_query: str, schema: dict | None = None) -> str | None:
    """Deterministic SQL for simple single-table queries.

    Runs BEFORE the LLM to guarantee fast, stable results for the most common
    requests. Only fires when the query is unambiguously simple (single table,
    no aggregation, no join keywords).

    Returns a SQL string or None if the query isn't a basic pattern.
    """
    q = user_query.lower()

    # Don't intercept queries that need joins, aggregation, filtering, or are dangerous.
    # "per " covers "per user", "per country", etc.
    # "by " is intentionally space-suffixed to avoid false matches on "by-products".
    complex_signals = (
        "join",
        "group by", "group",
        "count", "sum", "avg", "average",
        "max", "min",
        "total", "per ", "by user", "by country", "by product",
        "revenue", "sales", "amount",
        "where", "filter",
        "last month", "this month", "monthly",
        "from india", "from us",  # value-filter examples
        # Multi-table join signals — presence of another table's concept after "their"
        # indicates the user wants data from a related table.
        # IMPORTANT: "with their emails" is same-table → do NOT add generic "with their"
        # Instead only block when the query mentions 2+ distinct table keywords.
        "they purchased", "they bought", "they ordered",
        # Status/detail qualifiers that imply a join to orders/events
        "order status", "order amount", "order details",
        # Dangerous operations — never generate a safe SELECT for these
        "delete", "drop", "truncate", "update ", "insert ",
        # Numeric comparison — implies filtering which needs the compiler
        "greater than", "more than", "less than", "above", "below", "over ", "under ",
        "> ", "< ",
    )
    if any(sig in q for sig in complex_signals):
        return None

    # ── Multi-table detection ─────────────────────────────────────────────
    # If the query mentions 2+ distinct table keywords, it needs the JOIN compiler.
    table_keywords = ["user", "customer", "order", "product", "event", "payment",
                      "invoice", "item", "review", "category", "supplier"]
    matched_table_kws = [kw for kw in table_keywords if kw in q]
    if len(matched_table_kws) >= 2:
        return None

    # ── Table-specific patterns ───────────────────────────────────────────
    # Resolve actual table names from schema if available, otherwise use
    # common defaults so demos still work without a live connection.

    def _table(keyword: str) -> str:
        """Return the real table name from schema that contains keyword."""
        if schema:
            for t in schema:
                if keyword in t.lower():
                    return t
        return keyword  # fallback to the keyword itself (e.g. "users")

    def _cols(table: str, *want: str) -> str:
        """Return comma-joined columns from the table that match want keywords.
        Falls back to '*' if none found.
        """
        if schema and table in schema:
            matched = [c for c in schema[table]
                       if any(w in c.lower() for w in want)]
            if matched:
                return ", ".join(matched)
        return "*"

    # users / customers
    if "user" in q or "customer" in q:
        keyword = "user" if "user" in q else "customer"
        tbl = _table(keyword)
        if "email" in q:
            cols = _cols(tbl, "name", "email")
            return f"SELECT {cols} FROM {tbl};"
        if "name" in q:
            cols = _cols(tbl, "name")
            return f"SELECT {cols} FROM {tbl};"
        return f"SELECT * FROM {tbl};"

    # products
    if "product" in q:
        tbl = _table("product")
        if "price" in q:
            cols = _cols(tbl, "name", "price")
            return f"SELECT {cols} FROM {tbl};"
        return f"SELECT * FROM {tbl};"

    # orders — only bare "list/show orders", not aggregation queries
    if "order" in q and not any(agg in q for agg in ("amount", "total", "sum")):
        tbl = _table("order")
        return f"SELECT * FROM {tbl};"

    return None


def handle_semantic_query(user_query: str, schema: dict | None = None) -> str | None:
    """Handle high-value business queries with deterministic SQL generation.

    Uses a nested structure (primary keyword → modifier) rather than combinatorial
    AND-chains to keep patterns readable and easy to extend.

    Only covers queries where the correct SQL is unambiguous. Everything else
    goes to the LLM.
    """
    q = user_query.lower()

    if "revenue" in q or "sales" in q:
        # Time-based modifier
        if "last month" in q or "this month" in q or "monthly" in q:
            return (
                "SELECT SUM(total_amount) AS total_revenue "
                "FROM orders "
                "WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 1 MONTH);"
            )
        # Per-user / per-customer modifier
        if "user" in q or "customer" in q or "per " in q or "by " in q:
            return (
                "SELECT users.name, SUM(orders.total_amount) AS total_revenue "
                "FROM users "
                "JOIN orders ON users.id = orders.user_id "
                "GROUP BY users.id, users.name;"
            )
        # Plain revenue / sales total
        return "SELECT SUM(total_amount) AS total_revenue FROM orders;"

    return None


def _schema_context(semantic_layer: dict) -> str:
    """Build a structured, LLM-readable schema context from the semantic layer.

    Output format:
        Table: users
          - id: identifier
          - email: user email
          - created_at: created at
    """
    lines = []
    
    # 🔥 Add semantic term mappings for business queries
    # Build reverse mapping from semantic terms to table/column combinations
    semantic_mappings = {}
    for table, columns in semantic_layer.items():
        for column, info in columns.items():
            term = info.get("term", column)
            if term not in semantic_mappings:
                semantic_mappings[term] = []
            semantic_mappings[term].append(f"{table}.{column}")
    
    # Add business term mappings to schema context
    if semantic_mappings:
        lines.append("Business Term Mappings:")
        for term, columns in sorted(semantic_mappings.items()):
            lines.append(f"  '{term}' → {', '.join(columns)}")
        lines.append("")
    
    for table, columns in semantic_layer.items():
        lines.append(f"Table: {table}")
        for column, info in columns.items():
            term = info.get("term", column)
            lines.append(f"  - {column}: {term}")
    return "\n".join(lines) if lines else "No schema context available."


def generate_sql_local(user_query: str, semantic_layer: dict, schema: dict | None = None, intent: dict | None = None) -> str:
    few_shot = _build_few_shot_examples(schema) if schema else (
        "User: list all users and their emails\nSQL: SELECT name, email FROM users;"
    )

    # Enrich prompt with intent plan if available
    intent_hint = ""
    if intent and (intent.get("tables") or intent.get("reasoning")):
        tables = ", ".join(intent.get("tables", [])) or "unknown"
        columns = ", ".join(intent.get("columns", [])) or "relevant columns"
        reasoning = intent.get("reasoning", "")
        intent_hint = f"""
Intent analysis:
  Target tables: {tables}
  Relevant columns: {columns}
  Reasoning: {reasoning}
"""

    prompt = f"""
You are a SQL generation engine for a MySQL database.

You are given a database schema and a user question. Your only job is to output a valid SQL query.

Schema:
{_schema_context(semantic_layer)}
{intent_hint}
User Question: {user_query}

Rules:
- Use the Business Term Mappings above to translate user terms to actual table/column names
- Only use tables and columns that exist in the schema above
- Prefer SELECT queries
- Do NOT guess or invent column names
- Do NOT return explanations, comments, or markdown
- Output format: SELECT ... FROM ...;
- If unsure, return a best-effort SELECT query using available columns
- When user asks for business terms (e.g., "revenue", "sales"), use the Business Term Mappings to find the correct table/column

Examples:
{few_shot}

SQL:""".strip()

    # Use Qwen Coder as the primary local model
    local_model = os.getenv("LOCAL_MODEL", "qwen2.5-coder:7b")

    try:
        response = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": local_model, "prompt": prompt, "stream": False},
            timeout=30,
        )
        raw = response.json().get("response", "").strip()
        result = clean_sql_output(raw)
        print("DEBUG SQL RAW:", raw)
        print("DEBUG SQL CLEAN:", result)
        if not is_valid_sql(result):
            # Last-resort: if raw output contains SELECT somewhere, use it as-is
            if "select" in result.lower():
                return result.strip() + ";"
            return "unknown"
        return result
    except Exception:
        return "unknown"


def generate_sql_openai(user_query: str, semantic_layer: dict, schema: dict | None = None, intent: dict | None = None) -> str:
    few_shot = _build_few_shot_examples(schema) if schema else (
        "User: list all users and their emails\nSQL: SELECT name, email FROM users;"
    )

    intent_hint = ""
    if intent and (intent.get("tables") or intent.get("reasoning")):
        tables = ", ".join(intent.get("tables", [])) or "unknown"
        columns = ", ".join(intent.get("columns", [])) or "relevant columns"
        reasoning = intent.get("reasoning", "")
        intent_hint = f"""
Intent analysis:
  Target tables: {tables}
  Relevant columns: {columns}
  Reasoning: {reasoning}
"""

    prompt = f"""
You are a database reasoning engine.

Schema:
{_schema_context(semantic_layer)}
{intent_hint}
User Query: {user_query}

Rules:
- Use the Business Term Mappings above to translate user terms to actual table/column names
- Only use tables and columns that exist in the schema above
- Prefer SELECT queries
- Do NOT guess or invent column names
- Return ONLY the SQL query, no explanation, no markdown fences
- When user asks for business terms (e.g., "revenue", "sales"), use the Business Term Mappings to find the correct table/column

Examples:
{few_shot}

SQL:""".strip()

    if OpenAI is None or not os.getenv("OPENAI_API_KEY"):
        return "unknown"

    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": "You are a SQL generation engine. Return only valid SQL queries."},
                {"role": "user", "content": prompt},
            ],
        )
        result = response.choices[0].message.content.strip().replace("```sql", "").replace("```", "").strip()
        result = clean_sql_output(result)
        if not is_valid_sql(result):
            return "unknown"
        return result
    except Exception:
        return "unknown"


def generate_sql_nemotron(user_query: str, semantic_layer: dict, schema: dict | None = None, intent: dict | None = None) -> str:
    """Generate SQL using Nemotron 3 Ultra API with strict SQL-only output."""
    few_shot = _build_few_shot_examples(schema) if schema else (
        "User: list all users and their emails\nSQL: SELECT name, email FROM users;"
    )

    intent_hint = ""
    if intent and (intent.get("tables") or intent.get("reasoning")):
        tables = ", ".join(intent.get("tables", [])) or "unknown"
        columns = ", ".join(intent.get("columns", [])) or "relevant columns"
        reasoning = intent.get("reasoning", "")
        intent_hint = f"""
Intent analysis:
  Target tables: {tables}
  Relevant columns: {columns}
  Reasoning: {reasoning}
"""

    prompt = f"""You are a SQL generation engine for a MySQL database.

Schema:
{_schema_context(semantic_layer)}
{intent_hint}
User Question: {user_query}

Return ONLY SQL.
Do NOT explain.
Do NOT include comments.
Do NOT use markdown fences.

Rules:
- Use the Business Term Mappings above to translate user terms to actual table/column names
- Only use tables and columns that exist in the schema above
- Prefer SELECT queries
- Do NOT guess or invent column names
- Output format: SELECT ... FROM ...;
- When user asks for business terms (e.g., "revenue", "sales"), use the Business Term Mappings to find the correct table/column

Examples:
{few_shot}

SQL:""".strip()

    nemotron_api_key = os.getenv("NEMOTRON_API_KEY")
    nemotron_endpoint = os.getenv("NEMOTRON_ENDPOINT", "https://integrate.api.nvidia.com/v1/chat/completions")

    if not nemotron_api_key:
        return "unknown"

    try:
        response = requests.post(
            nemotron_endpoint,
            headers={
                "Authorization": f"Bearer {nemotron_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "nvidia/nemotron-3-270b-4k-instruct",
                "messages": [
                    {"role": "system", "content": "You are a SQL generation engine. Return ONLY valid SQL queries without explanation or markdown."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 512,
            },
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()["choices"][0]["message"]["content"].strip()
        result = clean_sql_output(result)
        if not is_valid_sql(result):
            return "unknown"
        return result
    except Exception:
        return "unknown"


def generate_sql(user_query: str, semantic_layer: dict, provider: str = "local", schema: dict | None = None) -> tuple[str, str]:
    """Generate SQL with a four-tier hybrid strategy:

    Tier 1 — Basic handler (deterministic, zero latency)
        Simple single-table reads with no aggregation or joins.

    Tier 2 — Semantic handler (deterministic, zero latency)
        High-value business queries: revenue, sales, and known combinations.
        Only fires when intent finds no tables (prevents overriding complex queries).

    Tier 3 — Intent compiler (deterministic, schema-graph driven)
        Joins, filters, and multi-table queries compiled from structured intent.
        Zero hallucination — uses the relationship graph, not the LLM.

    Tier 4 — LLM generator (flexible, higher latency)
        Fallback for queries the compiler can't handle (ambiguous filters,
        free-form aggregations, etc.).

    Returns:
        tuple: (sql_query, model_used)
    """
    # ── Tier 1: Basic deterministic handler ──────────────────────────────
    # Skip the basic handler for dangerous queries — we want the compiler/LLM
    # to generate the actual intended SQL so the user can review and confirm it.
    dangerous_keywords = {"delete", "update ", "drop", "truncate", "insert "}
    is_dangerous_query = any(kw in user_query.lower() for kw in dangerous_keywords)

    if not is_dangerous_query:
        basic_sql = handle_basic_query(user_query, schema)
        if basic_sql:
            return basic_sql, "deterministic"

    # ── Tier 3 prep: Intent planning (needed before semantic decision) ────
    # Run this early so we know whether tables were identified before deciding
    # whether semantic should override.
    intent = plan_intent(user_query, semantic_layer, provider=provider)
    intent_has_tables = bool(intent.get("tables"))

    # ── Tier 1.5: Dangerous query SQL generator ──────────────────────────
    # For DELETE/UPDATE/DROP/TRUNCATE, generate the actual intended SQL so the
    # user can review and confirm the exact statement. The pipeline will hold
    # execution pending confirmation — but it needs real SQL to show.
    if is_dangerous_query:
        q_lower = user_query.lower()
        # Find the primary table from intent or schema scan
        target_table = None
        if intent.get("tables"):
            target_table = intent["tables"][0]
        elif schema:
            for t in schema:
                if t.lower() in q_lower or t.lower().rstrip("s") in q_lower:
                    target_table = t
                    break

        if target_table:
            if "delete" in q_lower:
                # Build DELETE with any filters
                where_parts = []
                for f in intent.get("filters", []):
                    f_upper = f.upper()
                    if any(op in f_upper for op in ("=", ">", "<", "LIKE", "IN ", "BETWEEN")):
                        where_parts.append(f)
                where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

                # Relationship-aware DELETE: find ALL dependent child tables (any depth)
                # and delete them in reverse-dependency order so foreign key
                # constraints are never violated.
                if schema:
                    relationships = build_relationship_graph(schema)

                    def _topological_delete_order(target: str, graph: dict, all_tables: list) -> list[str]:
                        """Return tables in deletion order: deepest dependents first, target last."""
                        # Build a reverse map: parent -> [children]
                        children_of: dict[str, list[str]] = {t: [] for t in all_tables}
                        for tbl, fks in graph.items():
                            for fk_col, (ref_tbl, ref_col) in fks.items():
                                if ref_tbl in children_of:
                                    children_of[ref_tbl].append(tbl)

                        # BFS from target to find all descendants
                        order = []
                        visited = set()
                        queue = [target]
                        while queue:
                            current = queue.pop(0)
                            for child in children_of.get(current, []):
                                if child not in visited and child != target:
                                    visited.add(child)
                                    order.append(child)
                                    queue.append(child)

                        # Deepest tables first (reverse BFS gives leaf-to-root order)
                        # Simple approach: sort by dependency depth
                        order.reverse()
                        return order

                    child_order = _topological_delete_order(
                        target_table, relationships, list(schema.keys())
                    )

                    if child_order:
                        stmts = [f"DELETE FROM {ct};" for ct in child_order]
                        stmts.append(f"DELETE FROM {target_table}{where_sql};")
                        return "\n".join(stmts), "deterministic_dangerous"

                return f"DELETE FROM {target_table}{where_sql};", "deterministic_dangerous"

            elif "update" in q_lower or "set" in q_lower:
                # Try to extract SET clause from intent filters or raw query
                set_parts = []
                # Look for "set column = value" pattern in raw query
                import re as _re_d
                set_m = _re_d.search(r'\bset\s+(\w+)\s*=\s*[\'"]?([^\'" ,]+)[\'"]?', q_lower)
                if set_m:
                    col, val = set_m.group(1), set_m.group(2)
                    # Find exact column name in schema
                    if target_table in (schema or {}):
                        matched_col = next(
                            (c for c in schema[target_table] if c.lower() == col.lower()), col
                        )
                    else:
                        matched_col = col
                    set_parts.append(f"{matched_col} = '{val}'")
                if set_parts:
                    return f"UPDATE {target_table} SET {', '.join(set_parts)};", "deterministic_dangerous"

            elif "drop" in q_lower:
                return f"DROP TABLE {target_table};", "deterministic_dangerous"

            elif "truncate" in q_lower:
                return f"TRUNCATE TABLE {target_table};", "deterministic_dangerous"

    # ── Tier 2: Semantic handler ──────────────────────────────────────────
    # revenue/sales ALWAYS use the semantic handler — the hardcoded SQL is
    # more reliable than anything the intent compiler or LLM produces.
    is_known_semantic = any(kw in user_query.lower() for kw in ("revenue", "sales"))
    if is_known_semantic:
        semantic_sql = handle_semantic_query(user_query, schema)
        if semantic_sql:
            return semantic_sql, "semantic"

    # Semantic fallback: intent found nothing at all
    if not intent_has_tables:
        semantic_sql = handle_semantic_query(user_query, schema)
        if semantic_sql:
            return semantic_sql, "semantic"

    # ── Tier 3: Intent compiler ───────────────────────────────────────────
    # Invoke the compiler aggressively:
    #   - Any query where intent identified 2+ tables → deterministic JOIN problem
    #   - Single-table queries with intent tables → deterministic SELECT
    # Don't let multi-table queries fall to the LLM — the compiler is more reliable.
    if schema and intent_has_tables:
        relationships = build_relationship_graph(schema)
        compiled = compile_sql_from_intent(intent, schema, relationships, user_query=user_query)
        # Reject only the compiler's own failure sentinel or empty output
        if compiled and "FROM unknown" not in compiled and compiled.strip() != ";":
            return compiled, "deterministic_intent"

    # ── Tier 4: LLM fallback ──────────────────────────────────────────────
    if provider == "local":
        sql = generate_sql_local(user_query, semantic_layer, schema=schema, intent=intent)
        return sql, "local" if sql != "unknown" else "unknown"

    if provider == "openai":
        sql = generate_sql_openai(user_query, semantic_layer, schema=schema, intent=intent)
        return sql, "openai" if sql != "unknown" else "unknown"

    if provider == "nemotron":
        sql = generate_sql_nemotron(user_query, semantic_layer, schema=schema, intent=intent)
        return sql, "nemotron" if sql != "unknown" else "unknown"

    if provider == "hybrid":
        # Try local first (Qwen Coder)
        try:
            sql = generate_sql_local(user_query, semantic_layer, schema=schema, intent=intent)
            if is_valid_sql(sql) and sql != "unknown":
                return sql, "local"
        except Exception:
            pass

        # Fallback to Nemotron
        try:
            sql = generate_sql_nemotron(user_query, semantic_layer, schema=schema, intent=intent)
            if is_valid_sql(sql) and sql != "unknown":
                return sql, "nemotron"
        except Exception:
            pass

        return "unknown", "unknown"

    return "unknown", "unknown"


def execute_query(conn, sql: str) -> list:
    """Execute a SQL statement and return results.

    For SELECT/SHOW queries: returns list of row dicts.
    For write queries (INSERT/UPDATE/DELETE): commits and returns
    a synthetic row with rows_affected so callers get a consistent shape.
    """
    cursor = conn.cursor(dictionary=True)

    sql_lower = sql.strip().lower()
    first_word = sql_lower.split()[0] if sql_lower.split() else ""

    if first_word in ("select", "show", "explain", "describe"):
        cursor.execute(sql)
        return cursor.fetchall()

    # Write query — execute each statement, commit once, report rows affected.
    # Multi-statement SQL (e.g. child DELETE + parent DELETE) is split on ";\n".
    statements = [s.strip() for s in sql.split(";\n") if s.strip()]
    if not statements:
        statements = [sql]

    total_affected = 0
    for stmt in statements:
        if not stmt.endswith(";"):
            stmt += ";"
        cursor.execute(stmt)
        total_affected += cursor.rowcount if cursor.rowcount > 0 else 0

    conn.commit()
    return [{"rows_affected": total_affected}]


def safe_execute(conn, sql: str):
    validation = basic_sql_validation(sql)
    if not validation["valid"]:
        return {"success": False, "error": validation["reason"]}

    try:
        return {"success": True, "results": execute_query(conn, sql)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def fix_sql_local(error: str, sql: str, semantic_layer: dict) -> str:
    prompt = f"""
You are a SQL repair engine. Fix the SQL query below so it executes without errors.

Original SQL:
{sql}

Error:
{error}

Schema (use only these tables and columns):
{_schema_context(semantic_layer)}

Rules:
- Return ONLY the corrected SQL, no explanation, no markdown
- Only use tables and columns from the schema above
- Output format: SELECT ... FROM ...;

SQL:""".strip()

    try:
        response = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": "deepseek-coder", "prompt": prompt, "stream": False},
            timeout=30,
        )
        raw = response.json().get("response", "").strip()
        result = clean_sql_output(raw)
        return result if is_valid_sql(result) else sql
    except Exception:
        return sql


def fix_sql_openai(error: str, sql: str, semantic_layer: dict) -> str:
    if OpenAI is None or not os.getenv("OPENAI_API_KEY"):
        return sql

    prompt = f"""
The following SQL query failed:

SQL:
{sql}

Error:
{error}

Database semantic mapping:
{_schema_context(semantic_layer)}

Fix the SQL query.
Rules:
- Only output corrected SQL.
- Prefer SELECT queries.
""".strip()

    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": "You fix SQL queries."},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content.strip().replace("```sql", "").replace("```", "").strip()
    except Exception:
        return sql


def fix_sql_nemotron(error: str, sql: str, semantic_layer: dict) -> str:
    """Fix SQL using Nemotron 3 Ultra API with strict SQL-only output."""
    prompt = f"""You are a SQL repair engine. Fix the SQL query below so it executes without errors.

Original SQL:
{sql}

Error:
{error}

Schema (use only these tables and columns):
{_schema_context(semantic_layer)}

Return ONLY the corrected SQL.
Do NOT explain.
Do NOT include comments.
Do NOT use markdown fences.

Rules:
- Only use tables and columns from the schema above
- Output format: SELECT ... FROM ...;

SQL:""".strip()

    nemotron_api_key = os.getenv("NEMOTRON_API_KEY")
    nemotron_endpoint = os.getenv("NEMOTRON_ENDPOINT", "https://integrate.api.nvidia.com/v1/chat/completions")

    if not nemotron_api_key:
        return sql

    try:
        response = requests.post(
            nemotron_endpoint,
            headers={
                "Authorization": f"Bearer {nemotron_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "nvidia/nemotron-3-270b-4k-instruct",
                "messages": [
                    {"role": "system", "content": "You fix SQL queries. Return ONLY corrected SQL without explanation or markdown."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 512,
            },
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()["choices"][0]["message"]["content"].strip()
        result = clean_sql_output(result)
        return result if is_valid_sql(result) else sql
    except Exception:
        return sql


def fix_sql(error: str, sql: str, semantic_layer: dict, provider: str = "local") -> tuple[str, str]:
    """Fix SQL with intelligent fallback and model tracking.
    
    Returns:
        tuple: (fixed_sql, model_used)
        model_used can be: "local", "nemotron", "openai", "none"
    """
    if provider == "local":
        fixed = fix_sql_local(error, sql, semantic_layer)
        return fixed, "local" if fixed != sql else "none"
    if provider == "openai":
        fixed = fix_sql_openai(error, sql, semantic_layer)
        return fixed, "openai" if fixed != sql else "none"
    if provider == "nemotron":
        fixed = fix_sql_nemotron(error, sql, semantic_layer)
        return fixed, "nemotron" if fixed != sql else "none"
    if provider == "hybrid":
        # Try local first
        fixed = fix_sql_local(error, sql, semantic_layer)
        if fixed and fixed != sql:
            return fixed, "local"
        
        # Fallback to Nemotron
        fixed = fix_sql_nemotron(error, sql, semantic_layer)
        if fixed and fixed != sql:
            return fixed, "nemotron"
        
        return sql, "none"
    return sql, "none"


def fix_sql_with_ai(error: str, sql: str, semantic_layer: dict, provider: str = "local") -> tuple[str, str]:
    return fix_sql(error, sql, semantic_layer, provider=provider)


def is_ollama_running() -> bool:
    try:
        requests.get("http://127.0.0.1:11434", timeout=2)
        return True
    except Exception:
        return False


def estimate_confidence(sql: str, fixed: bool = False) -> str:
    return "medium" if fixed else "high"
