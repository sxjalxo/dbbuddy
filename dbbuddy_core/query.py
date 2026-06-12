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
    """Return True only if sql starts with a recognised DML/DQL keyword.
    Rejects empty strings, 'unknown', plain-text explanations from the model,
    and any response that doesn't open with a SQL verb.
    
    Enhanced with safety checks for destructive operations.
    """
    if not sql:
        return False
    
    sql_lower = sql.strip().lower()
    first = sql_lower.split()[0] if sql_lower else ""
    
    if first not in ("select", "insert", "update", "delete", "with", "show", "explain"):
        return False
    
    # Safety: reject destructive operations
    if "drop" in sql_lower or "truncate" in sql_lower:
        return False
    
    return True


def clean_sql_output(sql: str) -> str:
    """Strip markdown fences, leading labels ('SQL:', 'Answer:'), and
    extract the first valid SQL statement from a noisy model response.
    """
    sql = sql.strip()
    sql = sql.replace("```sql", "").replace("```", "")
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


def compile_sql_from_intent(intent: dict, schema: dict, relationships: dict = None) -> str:
    """Compile SQL directly from intent structure (deterministic SQL generation).
    
    This reduces LLM randomness by using structured intent to drive SQL generation.
    The LLM only fills gaps when intent is incomplete.
    
    Args:
        intent: Intent dict from plan_intent() with tables, columns, filters
        schema: Database schema
        relationships: Optional relationship graph for join inference
    
    Returns:
        str: Compiled SQL query
    """
    if not intent or not intent.get("tables"):
        return "SELECT * FROM unknown;"
    
    # Get primary table
    primary_table = intent["tables"][0]
    if primary_table not in schema:
        return f"SELECT * FROM {primary_table};"  # Fallback
    
    # Get columns to select
    columns = intent.get("columns", [])
    if not columns:
        select_clause = "*"
    else:
        # Filter columns that exist in the table
        valid_columns = [col for col in columns if col in [c.lower() for c in schema[primary_table]]]
        select_clause = ", ".join(valid_columns) if valid_columns else "*"
    
    # Build base query
    sql_parts = [f"SELECT {select_clause} FROM {primary_table}"]
    
    # Handle joins if multiple tables and relationships available
    if len(intent["tables"]) > 1 and relationships:
        for table in intent["tables"][1:]:
            if table in schema and primary_table in relationships:
                # Find join path using relationship graph
                for fk_col, (ref_table, ref_col) in relationships[primary_table].items():
                    if ref_table.lower() == table.lower():
                        sql_parts.append(f"JOIN {table} ON {primary_table}.{fk_col} = {table}.{ref_col}")
                        break
    
    # Handle filters
    filters = intent.get("filters", [])
    if filters:
        where_conditions = []
        for filter_expr in filters:
            # Simple filter handling - can be enhanced
            if "=" in filter_expr:
                where_conditions.append(filter_expr)
        
        if where_conditions:
            sql_parts.append("WHERE " + " AND ".join(where_conditions))
    
    return " ".join(sql_parts) + ";"


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
            return _json.loads(json_m.group(0))
    except Exception:
        pass

    return {
        "intent": "select",
        "tables": [],
        "columns": [],
        "filters": [],
        "reasoning": "",
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


def _schema_context(semantic_layer: dict) -> str:
    """Build a structured, LLM-readable schema context from the semantic layer.

    Output format:
        Table: users
          - id: identifier
          - email: user email
          - created_at: created at
    """
    lines = []
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
- Only use tables and columns that exist in the schema above
- Prefer SELECT queries
- Do NOT guess or invent column names
- Do NOT return explanations, comments, or markdown
- Output format: SELECT ... FROM ...;
- If unsure, return a best-effort SELECT query using available columns

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
        if not is_valid_sql(result):
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
- Only use tables and columns that exist in the schema above
- Prefer SELECT queries
- Do NOT guess or invent column names
- Return ONLY the SQL query, no explanation, no markdown fences

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
- Only use tables and columns that exist in the schema above
- Prefer SELECT queries
- Do NOT guess or invent column names
- Output format: SELECT ... FROM ...;

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
    """Generate SQL with intelligent fallback and model tracking.
    
    Returns:
        tuple: (sql_query, model_used)
        model_used can be: "local", "nemotron", "openai", "unknown"
    """
    # Step 1: Intent planning — ask model to identify tables/columns before generating SQL
    intent = plan_intent(user_query, semantic_layer, provider=provider)

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
