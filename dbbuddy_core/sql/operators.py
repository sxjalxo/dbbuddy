"""First-class SQL WHERE operators.

Each operator knows how to render itself in both forms the compiler needs:

* **parameterized** (execution) — ``%s`` placeholders plus a list of bound values
* **inline** (display / validation) — literals interpolated into the string

Operators implemented: ``=`` and the comparison operators, ``LIKE``,
``IS NULL`` / ``IS NOT NULL``, ``BETWEEN``, ``IN`` / ``NOT IN``, and ``ILIKE``
(dialect-rendered). The subquery predicates — ``EXISTS`` / ``NOT EXISTS`` and
``ANY`` / ``ALL`` — are not registry operators; they are their own AST nodes in
``sql/predicates.py`` because they aren't "column operator value".

The built-in registry is frozen after import: only ``get_operator`` and the
read-only ``OPERATORS`` view are public, so operators can't be mutated at runtime.
An explicit extension point can be added later if plugins ever need to contribute
custom operators.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from dbbuddy_core.sql.exceptions import InvalidConditionError
from dbbuddy_core.sql.renderer import render_literal


class Operator:
    """An operator that knows how to render itself into a SQL fragment.

    ``render`` returns ``(sql_fragment, params)``. In parameterized mode the
    fragment carries ``%s`` placeholders and ``params`` holds the values to bind;
    in inline mode the fragment carries literals and ``params`` is empty.

    ``dialect`` is an optional engine adapter, threaded through for operators whose
    SQL is engine-specific (e.g. ``ILIKE``). Operators that render identical SQL on
    every engine ignore it — so it never changes their output.
    """

    token: str

    def render(self, column: str, value: Any, *, parameterize: bool, dialect=None) -> tuple[str, list]:
        raise NotImplementedError


class BinaryOperator(Operator):
    """Infix operator with a single right-hand value: ``col OP value``.

    Covers ``=``, ``!=``, ``<>``, ``<``, ``>``, ``<=``, ``>=`` and ``LIKE`` — and
    any unrecognized operator token, which is passed through verbatim (preserving
    the compiler's historical behavior of interpolating arbitrary operator strings
    such as ``ILIKE`` or ``REGEXP``).
    """

    def __init__(self, token: str, sql: str | None = None):
        self.token = token
        self.sql = sql or token

    def render(self, column, value, *, parameterize, dialect=None):
        if value is None:
            # A NULL cannot be bound through a comparison; emit a literal NULL and
            # bind nothing. Mirrors the legacy "col OP NULL" fallthrough (=, != and
            # <> are redirected to IS [NOT] NULL during normalization instead).
            return f"{column} {self.sql} NULL", []
        if parameterize:
            return f"{column} {self.sql} %s", [value]
        return f"{column} {self.sql} {render_literal(value)}", []


class UnaryOperator(Operator):
    """Operator with no right-hand value: ``col IS NULL`` / ``col IS NOT NULL``."""

    def __init__(self, token: str, sql: str):
        self.token = token
        self.sql = sql

    def render(self, column, value, *, parameterize, dialect=None):
        return f"{column} {self.sql}", []


class RangeOperator(Operator):
    """Two-bound operator: ``col BETWEEN low AND high``.

    Expects ``value`` to be a ``[low, high]`` pair; anything else is rejected so
    the compiler skips the condition rather than emit invalid SQL.
    """

    def __init__(self, token: str, sql: str):
        self.token = token
        self.sql = sql

    def render(self, column, value, *, parameterize, dialect=None):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise InvalidConditionError(f"{self.token} requires a [low, high] value, got {value!r}")
        low, high = value
        if parameterize:
            return f"{column} {self.sql} %s AND %s", [low, high]
        return f"{column} {self.sql} {render_literal(low)} AND {render_literal(high)}", []


class SetOperator(Operator):
    """Set-membership operator: ``col IN (v1, v2, …)`` / ``col NOT IN (…)``.

    Accepts a list, tuple, or set of values; a scalar is normalized to a single
    element. An empty set renders the equivalent constant predicate — ``1=0`` for
    ``IN`` (matches nothing), ``1=1`` for ``NOT IN`` (matches everything) — instead
    of the invalid ``IN ()``.

    NULL is rejected for ``NOT IN``: SQL's three-valued logic makes
    ``x NOT IN (a, NULL)`` evaluate to UNKNOWN for every row, so it silently
    matches nothing — almost never what the caller intends. Raising surfaces the
    mistake rather than stripping the NULL (which would change the result set) or
    preserving it (which returns zero rows). NULL is allowed in ``IN``, where it
    simply never matches — the expected behavior.
    """

    def __init__(self, token: str, sql: str, *, negated: bool):
        self.token = token
        self.sql = sql
        self.negated = negated
        self._empty_predicate = "1=1" if negated else "1=0"

    def render(self, column, value, *, parameterize, dialect=None):
        values = list(value) if isinstance(value, (list, tuple, set)) else [value]
        if not values:
            return self._empty_predicate, []
        if self.negated and any(v is None for v in values):
            raise InvalidConditionError(
                f"{self.token} does not accept NULL: 'x {self.token} (..., NULL)' "
                f"matches no rows under SQL three-valued logic"
            )
        if parameterize:
            placeholders = ", ".join(["%s"] * len(values))
            return f"{column} {self.sql} ({placeholders})", values
        rendered = ", ".join(render_literal(v) for v in values)
        return f"{column} {self.sql} ({rendered})", []


class ILikeOperator(Operator):
    """Case-insensitive LIKE: ``col ILIKE value``.

    The SQL shape is engine-specific, so this operator delegates it to the
    dialect (``col ILIKE %s`` on Postgres; ``LOWER(col) LIKE LOWER(%s)`` on MySQL
    / SQL Server). The operator owns the ``%s``-vs-literal and param mechanics;
    the dialect owns the fragment via ``dialect.render_ilike(column, rhs)``.

    Without a dialect (no engine context) it uses the portable ``LOWER(...)``
    rewrite, so it never degrades to the unknown-operator passthrough — ``col
    ILIKE %s`` would be a syntax error on MySQL.
    """

    token = "ILIKE"

    def render(self, column, value, *, parameterize, dialect=None):
        if value is None:
            # Mirror the binary NULL fallthrough; a NULL pattern never matches.
            rhs, params = "NULL", []
        elif parameterize:
            rhs, params = "%s", [value]
        else:
            rhs, params = render_literal(value), []
        if dialect is not None:
            fragment = dialect.render_ilike(column, rhs)
        else:
            fragment = f"LOWER({column}) LIKE LOWER({rhs})"
        return fragment, params


# ── Registry ──────────────────────────────────────────────────────────────────
#
# Built once at import from the operators below, then frozen: only ``get_operator``
# and the read-only ``OPERATORS`` view are public, so the operator set can't be
# mutated at runtime. An explicit extension point can be added later if plugins
# ever need to contribute custom operators.

_registry: dict[str, Operator] = {}


def _register(operator: Operator, *tokens: str) -> None:
    """Register ``operator`` under one or more case-insensitive tokens.

    Import-time only (see the freeze below). Defaults to the operator's own
    ``token`` when no aliases are given.
    """
    for tok in (tokens or (operator.token,)):
        _registry[tok.upper().strip()] = operator


# Comparison + pattern operators (generic binary rendering).
for _sym in ("=", "!=", "<>", "<", ">", "<=", ">="):
    _register(BinaryOperator(_sym))
_register(BinaryOperator("LIKE"))
# Case-insensitive LIKE (dialect-rendered).
_register(ILikeOperator())
# Null tests (no bound value).
_register(UnaryOperator("IS NULL", "IS NULL"))
_register(UnaryOperator("IS NOT NULL", "IS NOT NULL"))
# Range.
_register(RangeOperator("BETWEEN", "BETWEEN"))
# Set membership.
_register(SetOperator("IN", "IN", negated=False))
_register(SetOperator("NOT IN", "NOT IN", negated=True))

# Freeze: the built-in registry is complete. Expose only a read-only view.
OPERATORS: MappingProxyType = MappingProxyType(_registry)


def get_operator(token: str) -> Operator:
    """Look up an operator by token.

    Unknown tokens fall back to a generic binary operator that interpolates the
    token verbatim, preserving the compiler's historical pass-through behavior
    for operator strings it was never taught explicitly.
    """
    op = OPERATORS.get(str(token).upper().strip())
    if op is not None:
        return op
    return BinaryOperator(str(token))
