import os

import requests

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency for local environments
    OpenAI = None


def is_select_query(sql: str) -> bool:
    return sql.strip().lower().startswith("select")


def basic_sql_validation(sql: str) -> dict:
    if not sql or len(sql.strip()) == 0:
        return {"valid": False, "reason": "Empty SQL"}

    if ";" in sql.strip()[:-1]:
        return {"valid": False, "reason": "Multiple statements not allowed"}

    return {"valid": True}


def get_query_type(sql: str) -> str:
    tokens = sql.strip().split()
    return tokens[0].lower() if tokens else "select"


def _schema_context(semantic_layer: dict) -> str:
    lines = []
    for table, columns in semantic_layer.items():
        for column, info in columns.items():
            term = info.get("term", "unknown")
            source = info.get("source", "rule")
            lines.append(f"{table}.{column} -> {term} ({source})")
    return "\n".join(lines) if lines else "No schema context available."


def generate_sql_local(user_query: str, semantic_layer: dict) -> str:
    prompt = f"""
You are a SQL expert.

Database semantic mapping:
{_schema_context(semantic_layer)}

User query:
{user_query}

Rules:
- Return ONLY SQL.
- Prefer SELECT queries.
""".strip()

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": "llama3", "prompt": prompt, "stream": False},
            timeout=8,
        )
        return response.json().get("response", "").strip()
    except Exception:
        return "unknown"


def generate_sql_openai(user_query: str, semantic_layer: dict) -> str:
    prompt = f"""
You are a SQL expert.

Database semantic mapping:
{_schema_context(semantic_layer)}

User query:
{user_query}

Rules:
- Prefer SELECT queries.
- Do not generate destructive queries unless absolutely required.
- Return ONLY SQL, with no explanation or markdown fences.
""".strip()

    if OpenAI is None or not os.getenv("OPENAI_API_KEY"):
        return "unknown"

    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": "You generate safe SQL queries."},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content.strip().replace("```sql", "").replace("```", "").strip()
    except Exception:
        return "unknown"


def generate_sql(user_query: str, semantic_layer: dict, provider: str = "local") -> str:
    if provider == "local":
        return generate_sql_local(user_query, semantic_layer)
    if provider == "openai":
        return generate_sql_openai(user_query, semantic_layer)
    if provider == "hybrid":
        sql = generate_sql_local(user_query, semantic_layer)
        if not sql or sql == "unknown":
            return generate_sql_openai(user_query, semantic_layer)
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
Fix this SQL query.

SQL:
{sql}

Error:
{error}

Schema:
{_schema_context(semantic_layer)}

Return ONLY corrected SQL.
""".strip()

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": "llama3", "prompt": prompt, "stream": False},
            timeout=8,
        )
        return response.json().get("response", "").strip()
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
        requests.get("http://localhost:11434", timeout=2)
        return True
    except Exception:
        return False


def estimate_confidence(sql: str, fixed: bool = False) -> str:
    return "medium" if fixed else "high"
