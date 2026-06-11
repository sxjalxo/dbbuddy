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
    """
    if not sql:
        return False
    first = sql.strip().lower().split()[0] if sql.strip() else ""
    return first in ("select", "insert", "update", "delete", "with", "show", "explain")


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

def _extract_identifiers(sql: str) -> tuple[list[str], list[str]]:
    """Extract candidate table and column names from a SQL string.

    Uses simple regex heuristics — good enough for validation purposes.
    Returns (tables, columns) as lowercase lists with duplicates removed.
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

    return tables, list(col_candidates)


def validate_against_schema(sql: str, schema: dict) -> dict:
    """Check that every table and column referenced in sql exists in schema.

    Args:
        sql:    The generated SQL string.
        schema: Raw schema dict  {table_name: [col1, col2, ...]}

    Returns:
        {"valid": bool, "unknown_tables": [...], "unknown_columns": [...]}
    """
    if not schema:
        return {"valid": True, "unknown_tables": [], "unknown_columns": []}

    known_tables = {t.lower() for t in schema}
    known_columns = {
        col.lower()
        for cols in schema.values()
        for col in cols
    }

    tables_used, cols_used = _extract_identifiers(sql)

    unknown_tables = [t for t in tables_used if t not in known_tables]
    # Only flag columns that don't exist in ANY table (hallucinated names)
    unknown_columns = [c for c in cols_used if c not in known_columns]

    return {
        "valid": not unknown_tables and not unknown_columns,
        "unknown_tables": unknown_tables,
        "unknown_columns": unknown_columns,
    }


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
    try:
        if provider in ("local", "hybrid"):
            resp = requests.post(
                "http://127.0.0.1:11434/api/generate",
                json={"model": "deepseek-coder", "prompt": prompt, "stream": False},
                timeout=30,
            )
            raw = resp.json().get("response", "").strip()
        elif provider == "openai" and OpenAI and os.getenv("OPENAI_API_KEY"):
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.choices[0].message.content.strip()

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

    try:
        response = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": "deepseek-coder", "prompt": prompt, "stream": False},
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


def generate_sql(user_query: str, semantic_layer: dict, provider: str = "local", schema: dict | None = None) -> str:
    # Step 1: Intent planning — ask model to identify tables/columns before generating SQL
    intent = plan_intent(user_query, semantic_layer, provider=provider)

    if provider == "local":
        return generate_sql_local(user_query, semantic_layer, schema=schema, intent=intent)
    if provider == "openai":
        return generate_sql_openai(user_query, semantic_layer, schema=schema, intent=intent)
    if provider == "hybrid":
        sql = generate_sql_local(user_query, semantic_layer, schema=schema, intent=intent)
        if not is_valid_sql(sql):
            return generate_sql_openai(user_query, semantic_layer, schema=schema, intent=intent)
        return sql
    return "unknown"


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


def fix_sql(error: str, sql: str, semantic_layer: dict, provider: str = "local") -> str:
    if provider == "local":
        return fix_sql_local(error, sql, semantic_layer)
    if provider == "openai":
        return fix_sql_openai(error, sql, semantic_layer)
    if provider == "hybrid":
        fixed = fix_sql_local(error, sql, semantic_layer)
        if not fixed or fixed == sql:
            return fix_sql_openai(error, sql, semantic_layer)
        return fixed
    return sql


def fix_sql_with_ai(error: str, sql: str, semantic_layer: dict, provider: str = "local") -> str:
    return fix_sql(error, sql, semantic_layer, provider=provider)


def is_ollama_running() -> bool:
    try:
        requests.get("http://127.0.0.1:11434", timeout=2)
        return True
    except Exception:
        return False


def estimate_confidence(sql: str, fixed: bool = False) -> str:
    return "medium" if fixed else "high"
