"""Capability flags for a SQL dialect.

The query planner asks "can this engine do X?" rather than "is this Postgres?",
keeping all engine-specific branching inside the dialect layer.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DialectCapabilities:
    # Data types
    supports_json: bool = False
    supports_arrays: bool = False

    # Query features
    supports_cte: bool = True          # WITH ... AS (...)
    supports_window_functions: bool = True
    supports_lateral: bool = False     # LATERAL JOIN

    # Full-text search
    supports_full_text: bool = False

    # Write features
    supports_returning: bool = False   # INSERT/UPDATE ... RETURNING
    supports_upsert: bool = False      # ON CONFLICT / ON DUPLICATE KEY UPDATE

    # Pattern matching
    supports_regex: bool = False       # native regex operator (REGEXP / ~)
    supports_ilike: bool = False       # native case-insensitive LIKE (ILIKE)

    # Identifier quoting character (`` ` `` for MySQL, ``"`` for Postgres/ANSI)
    identifier_quote_char: str = '"'
