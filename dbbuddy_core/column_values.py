"""Dimension value index (Phase C) — data-driven literal grounding.

Roles (Phase A/B) tell the planner that ``city`` is a *dimension* but not that
``Pune`` *lives in* ``city``. To decide ``WHERE city = 'Pune'`` over
``WHERE name = 'Pune'`` you need data, not grammar. This module samples the
distinct values of low-cardinality dimension columns at Analyze-Schema time and
builds a ``value -> [(table, column)]`` index. At query time an ungrounded
literal that exactly matches an indexed value binds to that column, and only an
unmatched literal falls through to the person-name grounding.

Deliberately bounded — it runs one ``SELECT DISTINCT`` per dimension column at
analyze time (never in the query path), caps cardinality, value length, and the
number of columns sampled, and swallows any per-column error. A failure yields a
smaller (or empty) index and degrades to Phase B behavior; it never blocks a
query. See ``docs/SEMANTIC_ROLES.md``.
"""

import logging
from typing import Callable, Dict, List, Optional, Tuple

from dbbuddy_core.semantic_roles import DIMENSION

logger = logging.getLogger(__name__)

# Bounds. A dimension is only indexed if its distinct count is at or below the
# cap (else it is not really categorical). Values longer than the length cap, and
# purely numeric tokens (which the numeric filter path owns), are not indexed.
MAX_DISTINCT = 200
MAX_COLUMNS = 80
MAX_VALUE_LEN = 60


def _dimension_columns(
    schema: Dict[str, List[str]],
    column_roles: Optional[Dict[str, Dict[str, str]]],
) -> List[Tuple[str, str]]:
    """(table, column) pairs classified as dimensions, capped at MAX_COLUMNS."""
    if not column_roles:
        return []
    out: List[Tuple[str, str]] = []
    for table, cols in schema.items():
        roles = column_roles.get(table, {})
        for col in cols:
            if roles.get(col) == DIMENSION:
                out.append((table, col))
                if len(out) >= MAX_COLUMNS:
                    return out
    return out


def _indexable(value) -> Optional[str]:
    """Return the string form of a value worth indexing, or None.

    Skips NULLs, over-long values, and purely numeric tokens (owned by the
    type-aware numeric filter path, and prone to false collisions).
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or len(s) > MAX_VALUE_LEN or s.isdigit():
        return None
    return s


def build_value_index(
    connect: Callable,
    schema: Dict[str, List[str]],
    column_roles: Optional[Dict[str, Dict[str, str]]],
    dialect,
) -> Dict[str, List[List[str]]]:
    """Sample distinct dimension values into a ``{value_lower: [[table, col], …]}`` index.

    ``connect`` is a zero-arg context manager yielding a live DB connection (the
    context store's pooled ``connection``). Best-effort throughout: a column that
    errors or exceeds the cardinality cap is skipped, never fatal.
    """
    index: Dict[str, List[List[str]]] = {}
    columns = _dimension_columns(schema, column_roles)
    if not columns:
        return index

    def _quote(name: str) -> str:
        try:
            return dialect.quote_identifier(name)
        except Exception:
            return name

    for table, col in columns:
        # No LIMIT clause (engine-agnostic): DISTINCT dedups server-side and we
        # pull at most cap+1 rows to detect "too high cardinality" and stop.
        sql = f"SELECT DISTINCT {_quote(col)} AS v FROM {_quote(table)}"
        try:
            with connect() as conn:
                cur = conn.cursor(dictionary=True)
                cur.execute(sql)
                rows = cur.fetchmany(MAX_DISTINCT + 1)
                try:
                    cur.close()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001 — sampling is best-effort
            logger.debug("value-index sample skipped for %s.%s: %s", table, col, exc)
            continue

        if len(rows) > MAX_DISTINCT:
            continue  # high-cardinality → not a categorical dimension

        for row in rows:
            s = _indexable(row.get("v"))
            if s is None:
                continue
            index.setdefault(s.lower(), [])
            pair = [table, col]
            if pair not in index[s.lower()]:
                index[s.lower()].append(pair)

    return index


def resolve_value(
    literal: str,
    value_index: Optional[Dict[str, List[List[str]]]],
    tables: Optional[List[str]] = None,
) -> Optional[Tuple[str, str]]:
    """Return (table, column) a literal exactly matches in the value index, or None.

    Case-insensitive full-value match (high precision — ``Pune`` equals a city
    value, ``Alice`` matches nothing). On a collision (a value present in several
    dimension columns) prefer a column whose table is already in play, else the
    first indexed occurrence.
    """
    if not value_index or not literal:
        return None
    hits = value_index.get(literal.strip().lower())
    if not hits:
        return None
    in_play = set(tables or [])
    for table, col in hits:
        if table in in_play:
            return table, col
    table, col = hits[0]
    return table, col
