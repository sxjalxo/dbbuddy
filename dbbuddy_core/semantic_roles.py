"""The semantic identity of schema columns — what each column *means*.

A column's *role* is its meaning to the query planner, independent of its name in
any one database: is it a person's name, a technical id, a measure to aggregate,
a date, a low-cardinality category? Grounding a bare literal ("Alice") onto a
column by sentence position is brittle; knowing that ``users.name`` holds a
person name lets the planner pick the right column regardless of word order or
which of several name-ish columns a table has.

This module owns that identity end to end:

* the closed role vocabulary the planner branches on,
* the **deterministic prior** (:func:`classify_roles`) — roles from column names
  and declared SQL types only, no AI, no data sampling, no network,
* the **AI overlay** (:func:`overlay_ai_roles`) — roles derived from the schema
  classification the semantic layer already produced, applied only where the
  prior was a weak default,
* and the ranked lookup the planner uses to resolve a table's human identifier
  (:func:`identifier_columns_for`).

Roles are computed at Analyze-Schema time and cached on ``DBContext.column_roles``.
Every layer degrades to the one below it, so a missing or failed enrichment always
yields a real answer rather than an error. Sampled *values* for dimension columns
live next door in :mod:`dbbuddy_core.column_values`, which keys off the roles set here.

See ``docs/SEMANTIC_ROLES.md`` for the full design.
"""

from typing import Dict, List, Optional

# Closed role vocabulary. The planner branches on these, so the set is fixed —
# an open vocabulary would reintroduce the guessing roles exist to remove.
IDENTIFIER = "identifier"
PERSON_NAME = "person_name"
LABEL = "label"
MEASURE = "measure"
TEMPORAL = "temporal"
DIMENSION = "dimension"
FREE_TEXT = "free_text"

ROLES = frozenset({IDENTIFIER, PERSON_NAME, LABEL, MEASURE, TEMPORAL, DIMENSION, FREE_TEXT})

# Human-identifier column names, in priority order. A person's name gets the
# PERSON_NAME role; a non-person human label (product title, city label) gets
# LABEL. Kept aligned with intent_builder._IDENTIFIER_COLUMNS.
_PERSON_NAME_EXACT = ("name", "full_name", "fullname", "display_name", "username", "user_name")
_LABEL_EXACT = ("title", "label")

# SQL type-name fragments (lowercased, substring-matched) for coarse type roles.
_TEMPORAL_TYPES = ("date", "time", "timestamp", "datetime", "year")
_NUMERIC_TYPES = (
    "int", "decimal", "numeric", "float", "double", "real", "money", "number", "bigint", "smallint",
)
# Numeric columns whose *name* marks them as an identifier/quantity-key, never a
# measure to SUM/AVG (e.g. a foreign key that happens to be an integer).
_NON_MEASURE_NAME_HINTS = ("id", "code", "number", "no", "zip", "postal", "phone", "year")


def is_identifier_name(name: str) -> bool:
    """Whether a column *name* denotes a technical key, across naming conventions.

    Catches ``id``/``pk``/``uuid``/``guid``, the snake_case ``<x>_id`` key, **and**
    the camelCase ``XxxID`` key that SQL Server, .NET/EF and AdventureWorks use
    everywhere (``CustomerID``, ``SalesOrderID``). The camelCase form is the one
    that matters here: lowercasing it to ``salesorderid`` destroys the boundary a
    plain ``endswith("_id")`` needs, so every key in such a schema read as a
    *measure* and a "total sales" question summed an id. The check uses the
    original casing — an uppercase ``ID`` preceded by a lowercase letter is a key
    boundary, while all-caps words (``GRID``, ``PAID``, ``RFID``) are not.
    """
    if not name:
        return False
    lo = name.lower()
    if lo in ("id", "pk", "uuid", "guid"):
        return True
    if lo.endswith("_id"):
        return True
    return len(name) > 2 and name.endswith("ID") and name[-3].islower()


def _type_of(column_types: Optional[Dict[str, Dict[str, str]]], table: str, col: str) -> str:
    if not column_types:
        return ""
    return (column_types.get(table, {}).get(col) or "").lower()


def _is_numeric(sql_type: str) -> bool:
    return any(frag in sql_type for frag in _NUMERIC_TYPES)


def _is_temporal(sql_type: str) -> bool:
    return any(frag in sql_type for frag in _TEMPORAL_TYPES)


def classify_column(col: str, sql_type: str) -> str:
    """Role for one column from its name + declared SQL type (deterministic).

    Priority: identifier keys → person name / label → temporal → measure →
    free_text / dimension. Type is advisory; when it is unavailable (empty) the
    name heuristics still yield a useful role, defaulting to ``dimension``.
    """
    cl = col.lower()

    # Technical identifiers: id/uuid/guid, a snake_case "<x>_id", or a camelCase
    # "XxxID" key (the convention across SQL Server / .NET schemas).
    if is_identifier_name(col):
        return IDENTIFIER

    # Human identifiers.
    if cl in _PERSON_NAME_EXACT or cl.endswith("_name"):
        return PERSON_NAME
    if cl in _LABEL_EXACT or cl.endswith("_title") or cl.endswith("_label"):
        return LABEL

    # Temporal by type, or by an obvious name when the type is unknown.
    if _is_temporal(sql_type) or cl.endswith("_at") or cl.endswith("_date") or cl.endswith("_time"):
        return TEMPORAL

    # Numeric → measure, unless the name marks it as a key/quantity code.
    if _is_numeric(sql_type):
        if any(hint in cl for hint in _NON_MEASURE_NAME_HINTS):
            return IDENTIFIER if ("id" in cl or "code" in cl or cl.endswith("no")) else DIMENSION
        return MEASURE

    # Long-form prose columns are free text, not filterable dimensions.
    if cl in ("description", "notes", "comment", "comments", "body", "content", "bio", "summary"):
        return FREE_TEXT

    # Default for a text/unknown column: a categorical dimension. (Phase C may
    # downgrade high-cardinality ones to free_text using sampled distinct counts.)
    return DIMENSION


def classify_roles(
    schema: Dict[str, List[str]],
    column_types: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, Dict[str, str]]:
    """Return ``{table: {col: role}}`` for a whole schema (deterministic prior)."""
    roles: Dict[str, Dict[str, str]] = {}
    for table, cols in schema.items():
        roles[table] = {
            col: classify_column(col, _type_of(column_types, table, col))
            for col in cols
        }
    return roles


# Within the person_name tier, a real display name is a likelier match for a
# queried literal than a login handle, so handle-like columns rank last.
_HANDLE_HINTS = ("username", "user_name", "handle", "login", "slug")


# ── Phase B: AI-classification term → role ───────────────────────────────────
# ai_refine already classifies each column into a one-word category from a
# role-like vocabulary ("value, quantity, name, date, identifier, status,
# description") and stores it as the column's `term`. That AI signal — which has
# the whole schema as context — maps onto the role taxonomy, so no extra model
# call is needed. "value" is intentionally omitted: too ambiguous (numeric value
# vs a text "value" column) to promote safely; the deterministic prior decides it.
_AI_TERM_TO_ROLE = {
    "identifier": IDENTIFIER,
    "name": PERSON_NAME,
    "date": TEMPORAL,
    "datetime": TEMPORAL,
    "timestamp": TEMPORAL,
    "quantity": MEASURE,
    "measure": MEASURE,
    "amount": MEASURE,
    "status": DIMENSION,
    "category": DIMENSION,
    "description": FREE_TEXT,
    "text": FREE_TEXT,
}

# Prior roles the AI is allowed to override. Only the two "weak default" roles —
# where the name/type heuristics could not commit — get upgraded by the AI. A
# confident structural signal (id, `_name`, a date type, a numeric measure) is
# never overridden, so the AI can *fill gaps* (promote an unrecognized text
# column like "borrower" to person_name) but never *fight* the prior.
_AI_OVERRIDABLE = frozenset({DIMENSION, FREE_TEXT})


def ai_role_from_term(term: Optional[str]) -> Optional[str]:
    """Map an AI classification term to a role, or None if it isn't a known category."""
    if not term:
        return None
    return _AI_TERM_TO_ROLE.get(term.strip().lower())


def overlay_ai_roles(
    prior: Dict[str, Dict[str, str]],
    semantic: Dict[str, Dict[str, dict]],
) -> Dict[str, Dict[str, str]]:
    """Overlay AI-derived roles onto the deterministic prior (Phase B), in place.

    For each column the AI actually labeled (``source == "ai"``) whose term maps
    to a role, upgrade the prior only when the prior was a weak default
    (``dimension``/``free_text``). Returns the same ``prior`` dict for chaining.
    Degrades to the pure prior when ``semantic`` carries no AI labels.
    """
    for table, cols in (semantic or {}).items():
        for col, info in (cols or {}).items():
            if not isinstance(info, dict) or info.get("source") != "ai":
                continue
            ai_role = ai_role_from_term(info.get("term"))
            if ai_role is None:
                continue
            current = prior.get(table, {}).get(col)
            if current in _AI_OVERRIDABLE and ai_role != current:
                prior.setdefault(table, {})[col] = ai_role
    return prior


def _person_rank(col: str) -> int:
    cl = col.lower()
    if any(h in cl for h in _HANDLE_HINTS):
        return 2          # login handle — last resort within person_name
    if cl.startswith("last"):
        return 1          # a surname is a weaker match than a given/full name
    return 0              # name / full_name / display_name / first_name …


def identifier_columns_for(
    table: str,
    schema: Dict[str, List[str]],
    column_roles: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[str]:
    """Human-identifier columns for a table, best first (person_name before label).

    Role-driven when ``column_roles`` is available; otherwise returns [] so the
    caller falls back to its own name heuristic. Within the person_name tier, a
    real display name outranks a login handle; ordering is otherwise stable
    (schema order), so results are deterministic.
    """
    if not column_roles:
        return []
    table_roles = column_roles.get(table, {})
    cols = schema.get(table, [])
    persons = [c for c in cols if table_roles.get(c) == PERSON_NAME]
    persons.sort(key=_person_rank)  # stable: ties keep schema order
    labels = [c for c in cols if table_roles.get(c) == LABEL]
    return persons + labels
