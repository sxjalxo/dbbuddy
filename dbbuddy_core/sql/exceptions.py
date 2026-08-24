"""Typed exceptions for SQL compilation.

Explicit error types (instead of bare ``ValueError``) so the compiler can be
precise about *why* it refused to generate SQL, and so callers can catch a
category rather than string-matching messages. As the compiler grows (EXISTS,
ANY / ALL, dialect-specific operators), these become the vocabulary for its
intended **fail-closed philosophy**: if intent cannot be confidently preserved,
raise rather than emit SQL that quietly means something else.

``InvalidConditionError`` intentionally also subclasses ``ValueError`` for
backward compatibility — callers that catch ``ValueError`` keep working.
``render_conditions`` is now **fail-closed**: it lets these propagate so
``compile_sql`` aborts rather than silently dropping a predicate and broadening
the query.
"""


class SQLCompilationError(Exception):
    """Base class for every failure raised while compiling an execution plan to SQL."""


class InvalidConditionError(SQLCompilationError, ValueError):
    """A WHERE condition is well-formed but cannot be compiled without changing its
    meaning — e.g. ``NOT IN (…, NULL)`` (matches no rows under three-valued logic)
    or ``BETWEEN`` without a ``[low, high]`` pair."""


class UnsupportedOperatorError(SQLCompilationError):
    """An operator is not supported — by the compiler, or by the active dialect."""


class DialectCapabilityError(SQLCompilationError):
    """The active dialect lacks a capability required to render a construct
    (e.g. a native ``ILIKE``)."""
