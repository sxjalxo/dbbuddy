"""SQL utilities: schema-aware identifier extraction/validation, relationship
graph inference, safe execution, and query-type helpers.

The legacy string-based / LLM SQL-generation stack (``generate_sql`` and the
per-provider generators, ``compile_sql_from_intent``, the ``fix_sql`` repair
helpers, and their prompt builders) was removed: DB Buddy compiles SQL
deterministically through the query planner → Predicate AST → dialect-aware
compiler (``dbbuddy_core.sql``), and AI is used only for semantic enrichment.
"""

import os
import re

from dbbuddy_core.logger import get_logger

logger = get_logger()


# A single identifier, in any quoting style a dialect may emit, or bare.
#
# ``\w+`` alone is not enough: a quoted identifier may legally contain hyphens
# (``line-item``), spaces (``total amount``) or start with a digit
# (``2024_total``), and matching only its leading word characters made the
# validator read `` `line-item` `` as a table called ``line``. It then reported
# a perfectly valid statement as referencing an unknown table, and the
# orchestrator refused to auto-execute it. Bare ``\w+`` stays last so unquoted
# identifiers still match.
_IDENT = r"(?:`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|\w+)"
_QUOTES = {"`": "`", '"': '"', "[": "]"}


def _unquote(ident: str) -> str:
    """Strip one layer of dialect quoting from a matched identifier."""
    ident = ident.strip()
    if len(ident) >= 2 and _QUOTES.get(ident[0]) == ident[-1]:
        return ident[1:-1]
    return ident


def _extract_identifiers(sql: str) -> tuple[list[str], list[str], list[dict]]:
    """Extract candidate table and column names from a SQL string.

    Uses simple regex heuristics — good enough for validation purposes.
    Returns (tables, columns, joins) as lowercase lists with duplicates removed.
    """
    # Type guard: ensure sql is actually a string
    if not isinstance(sql, str):
        logger.warning(f"_extract_identifiers received non-SQL input: {type(sql)} → {sql}")
        return [], [], []

    sql_lower = sql.lower()

    # Tables: FROM x, JOIN x, UPDATE x, INTO x
    table_pat = re.compile(
        rf"(?:from|join|update|into)\s+({_IDENT})", re.IGNORECASE
    )
    tables = list({_unquote(m.group(1)).lower() for m in table_pat.finditer(sql)})

    # Columns: SELECT a, b, c  |  SET col = ...  |  WHERE col ...
    # Grab everything between SELECT and FROM, plus SET / WHERE clauses
    col_candidates: set[str] = set()

    select_m = re.search(r"select\s+(.*?)\s+from", sql_lower, re.DOTALL)
    if select_m:
        raw = select_m.group(1)
        # Split on commas, strip aliases (AS x), functions, and wildcards
        for part in raw.split(","):
            part = re.sub(r"\b\w+\s*\(.*?\)", "", part)   # strip function calls
            part = re.sub(rf"\bas\s+{_IDENT}", "", part, flags=re.IGNORECASE)
            part = part.strip()
            if not part or part == "*":
                continue
            # Take the trailing identifier of a possibly-qualified reference,
            # matched quote-aware so `t`.`total amount` yields "total amount".
            ref = re.search(rf"(?:{_IDENT}\s*\.\s*)?({_IDENT})\s*$", part)
            if not ref:
                continue
            token = _unquote(ref.group(1))
            if token and token != "*":
                col_candidates.add(token.lower())

    where_m = re.findall(rf"(?:where|and|or)\s+({_IDENT})\s*[=<>!]", sql_lower)
    col_candidates.update(_unquote(c).lower() for c in where_m)

    # Extract join conditions for validation.
    #
    # The clause boundary is a **lookahead** with word boundaries, and end-of-string
    # is one of the accepted boundaries. The earlier pattern got all three of those
    # wrong, and each one silently dropped joins that validate_against_schema was
    # then unable to check:
    #
    #   * It *required* a trailing keyword, so the last JOIN in a statement —
    #     `... JOIN orders ON users.id = orders.user_id` with nothing after it, the
    #     single most common shape — matched nothing at all. A hallucinated join
    #     table in that position was never reported as an invalid join.
    #   * It *consumed* the boundary keyword, so in `A JOIN b ON … JOIN c ON …` the
    #     second `JOIN` was eaten by the first match and clause three was skipped.
    #   * It had no `\b`, so `order` matched inside `orders`: any condition
    #     mentioning an `orders` table truncated there, throwing away the column
    #     references after it.
    joins = []
    join_pat = re.compile(
        rf"\bjoin\s+({_IDENT})\s+"         # joined table
        rf"(?:(?:as\s+)?(?!on\b){_IDENT}\s+)?"  # optional alias (never the ON keyword)
        r"\bon\b\s+"
        r"(.+?)"                          # the condition
        r"(?=\s+\b(?:join|inner|left|right|full|cross|where|group|having|order|"
        r"limit|offset|union|window)\b|\s*;|\s*$)",
        re.IGNORECASE | re.DOTALL,
    )
    aliases = _table_aliases(sql)
    for match in join_pat.finditer(sql):
        join_table = _unquote(match.group(1)).lower()
        join_condition = match.group(2).strip()

        # Column references, with table aliases resolved to real table names.
        # `ON u.id = o.user_id` must be checked against `users`/`orders`, not
        # against tables literally named `u` and `o` — otherwise every aliased
        # join (idiomatic SQL, and what the planner emits) reads as referencing
        # unknown tables. This was invisible while joins were being dropped.
        join_cols = [
            (aliases.get(_unquote(tbl).lower(), _unquote(tbl).lower()),
             _unquote(col).lower())
            for tbl, col in re.findall(rf"({_IDENT})\s*\.\s*({_IDENT})", join_condition)
        ]

        joins.append({
            "table": join_table,
            "condition": join_condition,
            "column_refs": join_cols,
        })

    return tables, list(col_candidates), joins


def _table_aliases(sql: str) -> dict[str, str]:
    """Map ``{alias: real_table}`` for ``FROM t a`` / ``JOIN t AS a`` forms.

    Only used to resolve qualified references before validation. Anything that
    isn't clearly an alias (a reserved word following the table name) is ignored,
    so `FROM users WHERE ...` never registers `where` as an alias for `users`.
    """
    reserved = {
        "on", "as", "where", "group", "having", "order", "limit", "offset",
        "join", "inner", "left", "right", "full", "cross", "union", "set",
        "values", "using", "window", "and", "or", "select", "from",
    }
    out: dict[str, str] = {}
    pat = re.compile(rf"\b(?:from|join)\s+({_IDENT})\s+(?:as\s+)?({_IDENT})",
                     re.IGNORECASE)
    for table, alias in pat.findall(sql):
        alias, table = _unquote(alias), _unquote(table)
        if alias.lower() in reserved:
            continue
        out[alias.lower()] = table.lower()
    return out


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


# The hard ceiling on rows pulled into this process, independent of the display
# limit applied downstream. It has to live *here*, at the cursor, because a
# result set is only bounded once someone stops reading it.
FETCH_CAP = int(os.getenv("DBBUDDY_MAX_FETCH_ROWS", "10000"))
_FETCH_CHUNK = 1000


def _fetch_bounded(cursor, cap: int = None) -> list:
    """Read at most ``cap`` rows, then stop reading.

    ``cursor.fetchall()`` materialises the entire result set before any limit is
    applied. That is survivable on a 300k-row test database and fatal on a real
    one: ``SELECT * FROM booking`` against AirportDB's 54.3M rows built 54.3M
    dicts in memory and took the machine down with it — and the same statement
    against a customer's ERP would do it to the server. The display limit lives
    downstream in ``execution.MAX_ROWS``; this cap is the one that decides how
    much memory a query can cost, so it is deliberately higher (a caller that
    wants more than the display limit still gets a bounded amount) and still
    finite.

    One row beyond the cap is not fetched — the caller learns nothing from it
    that the cap does not already say, and fetching it is another round trip.
    """
    limit = FETCH_CAP if cap is None else cap
    rows = []
    while len(rows) < limit:
        chunk = cursor.fetchmany(min(_FETCH_CHUNK, limit - len(rows)))
        if not chunk:
            return rows
        rows.extend(chunk)
    logger.warning("Result set reached the %d-row fetch cap; stopped reading. "
                   "Add a LIMIT or an aggregate to ask a narrower question.", limit)
    return rows


def execute_query(conn, sql: str, params=None) -> list:
    """Execute a SQL statement and return results.

    For SELECT/SHOW queries: returns list of row dicts.
    For write queries (INSERT/UPDATE/DELETE): commits and returns
    a synthetic row with rows_affected so callers get a consistent shape.

    ``params``, when provided, are bound to ``%s`` placeholders by the driver
    (parameterized query) rather than interpolated into the SQL string.
    """
    cursor = conn.cursor(dictionary=True)

    sql_lower = sql.strip().lower()
    first_word = sql_lower.split()[0] if sql_lower.split() else ""

    if first_word in ("select", "show", "explain", "describe"):
        # Defense in depth: a read must be a single statement. Reject any stacked
        # statement (e.g. "SELECT 1; DELETE FROM users") so a smuggled write can
        # never ride the auto-execute read path, independent of upstream checks.
        if ";" in sql.strip().rstrip(";"):
            raise ValueError("Multiple statements are not allowed in a read query")
        cursor.execute(sql, params or ())
        return _fetch_bounded(cursor)

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


def safe_execute(conn, sql: str, params=None):
    validation = basic_sql_validation(sql)
    if not validation["valid"]:
        return {"success": False, "error": validation["reason"]}

    try:
        return {"success": True, "results": execute_query(conn, sql, params)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


