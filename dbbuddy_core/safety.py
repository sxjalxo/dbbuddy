"""Safety module for query classification and warning generation.

This module handles safety classification of SQL queries and generates
intelligent warnings for write operations, including FK-safe DELETE warnings.

Phase 18.5 of the pipeline refactoring.
"""

import re

from dbbuddy_core.logger import get_logger

logger = get_logger()


# Query categories for safety
READ_QUERIES = {"select", "show", "describe", "explain"}
WRITE_QUERIES = {"insert", "update", "delete", "drop", "alter", "truncate", "grant", "revoke", "create"}


def _strip_literals_and_comments(sql: str) -> str:
    """Blank out string literals and comments so their contents (``;``, ``DROP``,
    …) can't fool statement/keyword detection. Errs on the side of removing text,
    which only makes classification more conservative (safer)."""
    s = re.sub(r"'(?:[^']|'')*'", "''", sql)      # single-quoted literals
    s = re.sub(r'"(?:[^"]|"")*"', '""', s)         # double-quoted literals
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.DOTALL)  # /* block */ comments
    s = re.sub(r"--[^\n]*", " ", s)                # -- line comments
    s = re.sub(r"#[^\n]*", " ", s)                 # # line comments (MySQL)
    return s


def classify_query_safety(sql: str) -> tuple[str, bool]:
    """Classify query as READ or WRITE and whether it requires confirmation.

    Reads (``SELECT``/``SHOW``/``DESCRIBE``/``EXPLAIN``) don't require
    confirmation; everything else does. This is a defense-in-depth gate, so it
    fails safe: anything ambiguous is treated as a write.

    Hardening beyond a plain first-keyword check:
      * String literals and comments are stripped first, so a ``;`` or write
        keyword hiding inside them can't change the verdict.
      * **Stacked/multi-statement** SQL (e.g. ``SELECT 1; DROP TABLE t``) is
        always treated as a write — a legitimate NL-derived query is a single
        statement, so multiple statements are inherently suspicious.
      * ``EXPLAIN`` wrapping a write keyword (e.g. Postgres
        ``EXPLAIN ANALYZE DELETE …``, which actually executes) is a write.

    Args:
        sql: SQL query string

    Returns:
        tuple: (category, requires_confirmation)
        category: "read" or "write"
        requires_confirmation: bool
    """
    if not isinstance(sql, str):
        return "write", True  # Fail safe

    cleaned = _strip_literals_and_comments(sql)
    statements = [s.strip() for s in cleaned.split(";") if s.strip()]

    if not statements:
        return "write", True  # empty / whitespace / comment-only — fail safe

    # Stacked statements are never a plain read.
    if len(statements) > 1:
        return "write", True

    words = statements[0].lower().split()
    first_word = words[0] if words else ""

    if first_word in READ_QUERIES:
        # EXPLAIN can execute a wrapped write on some engines — inspect its body.
        if first_word == "explain" and any(w in WRITE_QUERIES for w in words):
            return "write", True
        return "read", False
    elif first_word in WRITE_QUERIES:
        return "write", True
    else:
        # Default to write for safety
        return "write", True


def generate_warning(sql: str, relationship_graph: dict | None = None) -> str:
    """Generate intelligent warning for write operations.

    When relationship_graph is provided, DELETE warnings include the names of
    dependent child tables so the user understands the full blast radius.

    Args:
        sql: SQL query string
        relationship_graph: Optional relationship graph for FK-aware warnings

    Returns:
        Warning message string
    """
    sql_lower = sql.lower()

    # Extract table name for more specific warnings
    table_match = re.search(r'from\s+(\w+)|into\s+(\w+)|table\s+(\w+)', sql_lower)
    table_name = table_match.group(1) or table_match.group(2) or table_match.group(3) if table_match else "the database"

    if "drop" in sql_lower and "table" in sql_lower:
        return f"This query will DROP the table '{table_name}'. All data will be permanently lost and cannot be recovered."
    elif "drop" in sql_lower:
        return f"This query will DROP '{table_name}'. This action cannot be undone."
    elif "delete" in sql_lower:
        base = f"This query will DELETE rows from '{table_name}'. This action cannot be undone."
        # Add dependent-table context when graph is available
        if relationship_graph:
            child_tables = [
                tbl for tbl, fks in relationship_graph.items()
                if tbl.lower() != table_name.lower()
                and any(ref_tbl.lower() == table_name.lower() for _, (ref_tbl, _) in fks.items())
            ]
            if child_tables:
                base += (
                    f"\nRelated records in {', '.join(child_tables)} will also be deleted "
                    f"(child rows are removed first to satisfy foreign key constraints)."
                )
        return base
    elif "truncate" in sql_lower:
        return f"This query will TRUNCATE the table '{table_name}', removing all data instantly."
    elif "update" in sql_lower:
        return f"This query will UPDATE existing records in '{table_name}'. Ensure conditions are correct to avoid unintended changes."
    elif "insert" in sql_lower:
        return f"This query will INSERT new data into '{table_name}'."
    elif "alter" in sql_lower:
        return f"This query will ALTER the structure of '{table_name}'. This may affect existing data and applications."
    elif "grant" in sql_lower or "revoke" in sql_lower:
        return "This query will modify access permissions. This affects database security."
    elif "create" in sql_lower:
        if "table" in sql_lower:
            return f"This query will CREATE a new table '{table_name}'."
        else:
            return "This query will CREATE a new database object."
    else:
        return "This query may modify data. Please review before execution."
