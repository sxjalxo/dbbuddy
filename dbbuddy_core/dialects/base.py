"""Base dialect abstraction for SQL engine support.

Every SQL engine ships its own Dialect subclass. The rest of the codebase
calls through DialectConnection — it never checks which engine is in use.

Design rules
------------
* The planner/compiler never imports engine names or driver modules.
* Engine-specific SQL lives ONLY here — in Dialect methods.
* Adding a new engine = subclassing Dialect + adding it to the registry.
"""

from abc import ABC, abstractmethod
from typing import Any

from .capabilities import DialectCapabilities
from .schema_meta import DatabaseSchema


class Dialect(ABC):
    """Per-engine adapter.

    Covers: connection lifecycle, cursor, schema introspection (simple + rich),
    SQL fragment generation, and a capabilities manifest.
    """

    engine_name: str = "unknown"

    # ── Capabilities ─────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def capabilities(self) -> DialectCapabilities:
        """Immutable capability flags for this engine."""
        ...

    # ── Connection lifecycle ──────────────────────────────────────────────────

    @abstractmethod
    def connect(self, host: str, user: str, password: str, database: str, port: int | None = None) -> Any:
        """Open a raw driver connection. Raise on failure.

        ``port`` is optional; when None the engine's default port is used.
        """
        ...

    @abstractmethod
    def ping(self, raw_conn) -> None:
        """Revalidate a pooled connection. Raise if the connection is dead."""
        ...

    def set_autocommit(self, raw_conn, value: bool) -> None:
        raw_conn.autocommit = value

    def apply_statement_timeout(self, raw_conn, seconds: float) -> bool:
        """Bound how long any single statement on this connection may run.

        Without this a pathological query — a lock wait, a scan of a 400-million-row
        table — holds a worker and a pool slot until the database decides to answer.
        The AI path has timeouts, retries, and a circuit breaker; the *data* path,
        which is the one users actually wait on, otherwise has no ceiling at all.

        Applied as a session setting after connect rather than as a connect
        argument, so a driver that does not support it degrades to "no timeout"
        instead of failing to connect. Returns True when the ceiling is in force,
        so callers can tell a real bound from a best-effort one.

        The base implementation is a no-op: a dialect that cannot express this
        must say so rather than pretend.
        """
        return False

    # ── Cursor ───────────────────────────────────────────────────────────────

    @abstractmethod
    def dict_cursor(self, raw_conn):
        """Return a cursor whose fetchall()/fetchone() yield plain dicts."""
        ...

    # ── Schema introspection ─────────────────────────────────────────────────

    @abstractmethod
    def fetch_schema(self, raw_conn) -> dict[str, list[str]]:
        """Return {table: [col, ...]} for every user table in the database."""
        ...

    @abstractmethod
    def fetch_schema_rich(self, raw_conn) -> DatabaseSchema:
        """Return full schema metadata including types, constraints, and FKs."""
        ...

    # ── Identifier quoting ────────────────────────────────────────────────────

    def quote_identifier(self, name: str) -> str:
        """Wrap an identifier in engine-specific quotes to avoid keyword clashes."""
        q = self.capabilities.identifier_quote_char
        escaped = name.replace(q, q + q)
        return f"{q}{escaped}{q}"

    # ── SQL scalar fragments ──────────────────────────────────────────────────
    # Override these in each dialect. The planner calls these methods and
    # never hard-codes engine-specific syntax.

    def current_date(self) -> str:
        """Today's date (no time component)."""
        return "CURDATE()"

    def current_timestamp(self) -> str:
        """Current date and time."""
        return "NOW()"

    def random(self) -> str:
        """A random float in [0, 1)."""
        return "RAND()"

    def boolean_literal(self, value: bool) -> str:
        """Engine-appropriate boolean literal."""
        return "TRUE" if value else "FALSE"

    # ── Date / time fragments ─────────────────────────────────────────────────

    def date_sub_interval(self, col: str, n: int, unit: str) -> str:
        """``col`` minus *n* *unit*s (e.g. 1 month ago)."""
        return f"DATE_SUB({col}, INTERVAL {n} {unit.upper()})"

    def date_add_interval(self, col: str, n: int, unit: str) -> str:
        """``col`` plus *n* *unit*s."""
        return f"DATE_ADD({col}, INTERVAL {n} {unit.upper()})"

    def extract_month(self, col: str) -> str:
        return f"MONTH({col})"

    def extract_year(self, col: str) -> str:
        return f"YEAR({col})"

    def date_cast(self, col: str) -> str:
        """Truncate a timestamp to a date."""
        return f"DATE({col})"

    # ── Type casting ──────────────────────────────────────────────────────────

    def cast(self, expr: str, type_name: str) -> str:
        """``CAST(expr AS type_name)`` — override if type names differ."""
        return f"CAST({expr} AS {type_name})"

    # ── String functions ──────────────────────────────────────────────────────

    def concat(self, *args: str) -> str:
        """Concatenate expressions."""
        return f"CONCAT({', '.join(args)})"

    # ── Pattern matching ──────────────────────────────────────────────────────

    def regex_match(self, col: str, pattern: str) -> str:
        """Case-sensitive regex match.  Override per engine."""
        raise NotImplementedError(
            f"{self.engine_name} regex syntax not implemented"
        )

    def render_ilike(self, column: str, rhs: str) -> str:
        """Case-insensitive LIKE.

        Native ``ILIKE`` when the engine supports it (``supports_ilike``),
        otherwise the portable ``LOWER(col) LIKE LOWER(rhs)`` rewrite. ``rhs`` is
        the already-rendered right-hand side — a ``%s`` placeholder (parameterized
        path) or a quoted literal (inline path). The operator owns which; the
        dialect owns the SQL shape.
        """
        if self.capabilities.supports_ilike:
            return f"{column} ILIKE {rhs}"
        return f"LOWER({column}) LIKE LOWER({rhs})"

    # ── Pagination ────────────────────────────────────────────────────────────

    def limit(self, n: int, offset: int = 0) -> str:
        """LIMIT / OFFSET clause."""
        if offset:
            return f"LIMIT {n} OFFSET {offset}"
        return f"LIMIT {n}"


class DialectConnection:
    """Normalises any raw driver connection behind a uniform interface.

    All code in the codebase that used to touch ``mysql.connector`` objects
    directly now talks to this wrapper. The wrapper delegates to the dialect
    for every driver-specific operation so no other module needs to branch on
    the engine type.
    """

    __slots__ = ("_raw", "dialect")

    def __init__(self, raw_conn: Any, dialect: Dialect):
        self._raw = raw_conn
        self.dialect = dialect

    # ── Cursor ───────────────────────────────────────────────────────────────

    def cursor(self, dictionary: bool = False):
        if dictionary:
            return self.dialect.dict_cursor(self._raw)
        return self._raw.cursor()

    # ── Connection management ─────────────────────────────────────────────────

    @property
    def autocommit(self) -> bool:
        return getattr(self._raw, "autocommit", False)

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self.dialect.set_autocommit(self._raw, value)

    def ping(self, **_kwargs) -> None:
        """Revalidate the connection (kwargs ignored — dialect handles strategy)."""
        self.dialect.ping(self._raw)

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()

    # ── Schema ────────────────────────────────────────────────────────────────

    def fetch_schema(self) -> dict[str, list[str]]:
        return self.dialect.fetch_schema(self._raw)

    def fetch_schema_rich(self) -> DatabaseSchema:
        return self.dialect.fetch_schema_rich(self._raw)
