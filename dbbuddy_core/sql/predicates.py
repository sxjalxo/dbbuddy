"""Predicate AST for WHERE clauses.

A ``Predicate`` is anything that renders to a boolean SQL fragment. ``Condition``
(``column operator value``) is the common leaf; this module adds the *subquery*
predicates that don't fit that shape:

    Predicate
    ├── Condition            (column operator value — in conditions.py)
    ├── ExistsPredicate      EXISTS / NOT EXISTS (subquery)
    └── QuantifiedPredicate  column <cmp> ANY | ALL (subquery)

Keeping these as their own AST nodes — rather than twisting ``Condition`` — leaves
room for grouped / nested predicates later.

Subquery operands are always a nested **execution plan** compiled by our own
compiler (never a raw string), so values inside them are parameterized/escaped
exactly like the top-level query.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from dbbuddy_core.sql.exceptions import InvalidConditionError

# Comparators permitted in a quantified comparison (whitelist → safe to interpolate).
_COMPARATORS = {"=", "!=", "<>", "<", ">", "<=", ">="}
_QUANTIFIERS = {"ANY", "ALL"}


class Predicate(ABC):
    """Anything that renders to a boolean SQL fragment plus its bound params."""

    @abstractmethod
    def render(self, *, parameterize: bool, dialect=None) -> tuple[str, list]:
        ...


def _compile_subquery(subquery, *, parameterize: bool, dialect, keyword: str) -> tuple[str, list]:
    """Compile a nested execution plan into ``(sql, params)``.

    The operand MUST be an execution-plan dict — never a raw string — so the
    subquery runs through the same safe, parameterized compiler as everything
    else. A non-dict operand is a fail-closed error.
    """
    if not isinstance(subquery, dict) or not subquery:
        raise InvalidConditionError(
            f"{keyword} requires a subquery execution plan (a dict), got {subquery!r}"
        )
    # Lazy import: predicates is imported by conditions, which is imported by the
    # compiler — importing the compiler here at module load would cycle.
    from dbbuddy_core.sql.compiler import compile_sql

    engine = getattr(dialect, "engine_name", None) or "mysql"
    if parameterize:
        sub_sql, sub_params = compile_sql(subquery, parameterize=True, engine=engine)
        return sub_sql, list(sub_params)
    return compile_sql(subquery, parameterize=False, engine=engine), []


class ExistsPredicate(Predicate):
    """``EXISTS (subquery)`` / ``NOT EXISTS (subquery)`` — no left-hand column."""

    def __init__(self, subquery, *, negated: bool = False):
        self.subquery = subquery
        self.negated = negated

    def render(self, *, parameterize, dialect=None):
        keyword = "NOT EXISTS" if self.negated else "EXISTS"
        sub_sql, params = _compile_subquery(
            self.subquery, parameterize=parameterize, dialect=dialect, keyword=keyword
        )
        return f"{keyword} ({sub_sql})", params


class QuantifiedPredicate(Predicate):
    """``column <comparator> ANY | ALL (subquery)`` — a quantified comparison.

    Scope: the standard SQL *subquery* form (portable across engines). Postgres's
    array form (``= ANY (ARRAY[...])``) is a future extension gated on
    ``supports_arrays``.
    """

    def __init__(self, column: str, comparator: str, quantifier: str, subquery):
        self.column = column
        self.comparator = comparator
        self.quantifier = quantifier
        self.subquery = subquery

    def render(self, *, parameterize, dialect=None):
        comparator = str(self.comparator).strip()
        quantifier = str(self.quantifier).upper().strip()
        if not self.column:
            raise InvalidConditionError("ANY/ALL requires a left-hand column")
        if comparator not in _COMPARATORS:
            raise InvalidConditionError(
                f"ANY/ALL comparator must be one of {sorted(_COMPARATORS)}, got {comparator!r}"
            )
        if quantifier not in _QUANTIFIERS:
            raise InvalidConditionError(f"quantifier must be ANY or ALL, got {self.quantifier!r}")
        sub_sql, params = _compile_subquery(
            self.subquery, parameterize=parameterize, dialect=dialect, keyword=quantifier
        )
        return f"{self.column} {comparator} {quantifier} ({sub_sql})", params
