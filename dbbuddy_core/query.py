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
    - Multi-table JOINs via BFS on the relationship graph (including multi-hop)
    - Aggregation (SUM/COUNT + GROUP BY) detected from query keywords
    - WHERE filters extracted from intent and from query text

    Args:
        intent:      Intent dict from plan_intent()
        schema:      Raw schema dict {table: [col, ...]}
        relationships: Relationship graph from build_relationship_graph()
        user_query:  Original query string used for keyword detection

    Returns:
        Compiled SQL string, or "SELECT * FROM unknown;" on failure.
    """
    if not intent or not intent.get("tables"):
        return "SELECT * FROM unknown;"

    tables = intent["tables"]
    primary_table = tables[0]
    if primary_table not in schema:
        return "SELECT * FROM unknown;"

    q = user_query.lower() if user_query else ""

    # ── Aggregation intent detection ──────────────────────────────────────
    wants_aggregation = any(kw in q for kw in ("total", "sum", "count", "average", "avg", "max", "min"))
    wants_group = any(kw in q for kw in ("per ", "by user", "by customer", "by product", "by country", "each"))

    # ── BFS join path finder ──────────────────────────────────────────────
    def bfs_join_path(start: str, targets: list[str], graph: dict) -> list[tuple]:
        """Return list of (from_table, fk_col, to_table, pk_col) edges connecting
        start to all targets via BFS over the relationship graph.
        Returns empty list if any target is unreachable.
        """
        if not graph or not targets:
            return []

        needed = set(t.lower() for t in targets)
        visited = {start.lower()}
        # queue: (current_table, path_so_far)
        queue = [(start, [])]
        joined = set()
        result_edges = []

        while queue and needed - joined:
            current, path = queue.pop(0)
            current_lower = current.lower()

            for fk_col, (ref_table, ref_col) in graph.get(current, {}).items():
                ref_lower = ref_table.lower()
                if ref_lower in visited:
                    continue
                visited.add(ref_lower)
                edge = (current, fk_col, ref_table, ref_col)
                new_path = path + [edge]

                if ref_lower in needed:
                    result_edges.extend(new_path)
                    joined.add(ref_lower)

                queue.append((ref_table, new_path))

        # Return edges only if all targets were reached
        return result_edges if needed == joined else []

    # ── Build JOIN clauses ────────────────────────────────────────────────
    join_clauses = []
    all_tables = [primary_table]  # tables actually in the query

    if len(tables) > 1 and relationships:
        edges = bfs_join_path(primary_table, tables[1:], relationships)
        for from_t, fk_col, to_t, pk_col in edges:
            join_clauses.append(f"JOIN {to_t} ON {from_t}.{fk_col} = {to_t}.{pk_col}")
            if to_t not in all_tables:
                all_tables.append(to_t)

        # Fallback: direct relationship check for tables BFS couldn't path to
        already_joined = {t for clause in join_clauses for t in all_tables}
        for table in tables[1:]:
            if table in already_joined or table in all_tables:
                continue
            if table in schema and primary_table in relationships:
                for fk_col, (ref_table, ref_col) in relationships[primary_table].items():
                    if ref_table.lower() == table.lower():
                        join_clauses.append(f"JOIN {table} ON {primary_table}.{fk_col} = {table}.{ref_col}")
                        all_tables.append(table)
                        break

    # ── SELECT projection ─────────────────────────────────────────────────
    def _find_col(table: str, *keywords: str) -> str | None:
        """Return first column in table whose name contains any keyword."""
        if table not in schema:
            return None
        for col in schema[table]:
            if any(kw in col.lower() for kw in keywords):
                return col
        return None

    is_multi_table = len(all_tables) > 1

    if wants_aggregation and is_multi_table:
        # e.g. "total order amount per user" → users.name, SUM(orders.total_amount)
        # Find the name column from the primary table
        name_col = _find_col(primary_table, "name", "username", "email")
        # Find the value column from secondary tables
        value_col = None
        agg_func = "COUNT(*)"
        for t in all_tables[1:]:
            col = _find_col(t, "amount", "total", "price", "value", "revenue")
            if col:
                if any(kw in q for kw in ("count", "how many")):
                    agg_func = f"COUNT({t}.{col})"
                else:
                    agg_func = f"SUM({t}.{col})"
                value_col = f"{t}.{col}"
                break
        if name_col:
            select_clause = f"{primary_table}.{name_col}, {agg_func} AS total"
        else:
            select_clause = f"{primary_table}.id, {agg_func} AS total"

    elif wants_aggregation and not is_multi_table:
        # Single-table aggregation
        col = _find_col(primary_table, "amount", "total", "price", "value")
        if col:
            agg_func = "SUM" if any(kw in q for kw in ("sum", "total")) else "COUNT"
            select_clause = f"{agg_func}({col}) AS total"
        else:
            select_clause = "COUNT(*) AS total"

    elif is_multi_table:
        # Multi-table join — project meaningful columns instead of *
        parts = []
        for t in all_tables:
            name_col = _find_col(t, "name", "username", "title")
            amount_col = _find_col(t, "amount", "total", "price")
            if name_col:
                parts.append(f"{t}.{name_col}")
            if amount_col:
                parts.append(f"{t}.{amount_col}")
        select_clause = ", ".join(parts) if parts else "*"

    else:
        # Single-table — use intent columns if available, else *
        columns = intent.get("columns", [])
        valid = [c for c in columns if c in [col.lower() for col in schema[primary_table]]]
        select_clause = ", ".join(valid) if valid else "*"

    # ── GROUP BY ──────────────────────────────────────────────────────────
    group_by_clause = ""
    if wants_aggregation and wants_group and is_multi_table:
        group_by_clause = f"GROUP BY {primary_table}.id"

    # ── WHERE filters ─────────────────────────────────────────────────────
    # 1. From intent filters (LLM-extracted)
    where_conditions = []
    for f in intent.get("filters", []):
        if "=" in f:
            where_conditions.append(f)

    # 2. Keyword-based WHERE extraction from query text
    # country/location filters
    COUNTRY_KEYWORDS = {
        "india": "India", "us": "US", "usa": "United States",
        "uk": "United Kingdom", "canada": "Canada", "australia": "Australia",
        "germany": "Germany", "france": "France",
    }
    for kw, val in COUNTRY_KEYWORDS.items():
        if f"from {kw}" in q or f"in {kw}" in q:
            country_col = _find_col(primary_table, "country", "location", "region")
            if country_col:
                where_conditions.append(f"{primary_table}.{country_col} = '{val}'")
            else:
                where_conditions.append(f"country = '{val}'")
            break

    # status filters
    STATUS_KEYWORDS = {"active": "active", "inactive": "inactive",
                        "pending": "pending", "completed": "completed"}
    for kw, val in STATUS_KEYWORDS.items():
        if kw in q:
            status_col = _find_col(primary_table, "status")
            if status_col:
                where_conditions.append(f"{primary_table}.{status_col} = '{val}'")
            break

    # ── Assemble final SQL ────────────────────────────────────────────────
    parts = [f"SELECT {select_clause} FROM {primary_table}"]
    parts.extend(join_clauses)
    if where_conditions:
        parts.append("WHERE " + " AND ".join(where_conditions))
    if group_by_clause:
        parts.append(group_by_clause)

    return " ".join(parts) + ";"


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

    # Don't intercept queries that need joins, aggregation, or filtering.
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
    )
    if any(sig in q for sig in complex_signals):
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
    basic_sql = handle_basic_query(user_query, schema)
    if basic_sql:
        return basic_sql, "deterministic"

    # ── Tier 3 prep: Intent planning (needed before semantic decision) ────
    # Run this early so we know whether tables were identified before deciding
    # whether semantic should override.
    intent = plan_intent(user_query, semantic_layer, provider=provider)
    intent_has_tables = bool(intent.get("tables"))

    # ── Tier 2: Semantic handler ──────────────────────────────────────────
    # Only fires when intent has no tables OR query is a known business term
    # without a specific table target — prevents overriding complex join queries
    # where the LLM/compiler already knows the right tables.
    is_known_semantic = (
        ("revenue" in user_query.lower() or "sales" in user_query.lower())
        and not intent_has_tables
    )
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
    # Build relationship graph and attempt deterministic SQL compilation.
    # This handles joins and filters without LLM involvement.
    if intent_has_tables and schema:
        relationships = build_relationship_graph(schema)
        compiled = compile_sql_from_intent(intent, schema, relationships, user_query=user_query)
        # Only accept the compiled result if it looks like real SQL
        # (compile_sql_from_intent returns "SELECT * FROM unknown;" on failure)
        if compiled and "unknown" not in compiled and is_valid_sql(compiled):
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


def execute_query(conn, sql: str):
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql)
    return cursor.fetchall()


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
