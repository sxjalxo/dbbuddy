"""Central registry mapping engine names to Dialect singletons.

Dialects are instantiated lazily on first use so the absence of an optional
driver (e.g. psycopg2 for Postgres) only raises an error when that engine is
actually requested — not at import time.
"""

from .engine import DatabaseEngine
from .mysql import MySQLDialect

# Canonical names exposed to the API and the frontend dropdown.
SUPPORTED_ENGINES: list[str] = [e.value for e in DatabaseEngine]

# Lazy-loaded singletons keyed by canonical engine value string.
_cache: dict[str, object] = {}

_FACTORIES: dict[str, type] = {
    DatabaseEngine.MYSQL: MySQLDialect,
}


def _postgres_factory():
    from .postgres import PostgresDialect  # deferred — requires psycopg2
    return PostgresDialect


def _sqlserver_factory():
    from .sqlserver import SQLServerDialect  # deferred — requires pymssql
    return SQLServerDialect


_DEFERRED_FACTORIES: dict[str, object] = {
    DatabaseEngine.POSTGRES: _postgres_factory,
    DatabaseEngine.SQLSERVER: _sqlserver_factory,
}


def get_dialect(engine: "str | DatabaseEngine"):
    """Return the Dialect singleton for *engine*. Raises ValueError for unknown engines."""
    if isinstance(engine, DatabaseEngine):
        db_engine = engine
    else:
        db_engine = DatabaseEngine.normalize(engine or "mysql")

    key = db_engine.value
    if key not in _cache:
        if db_engine in _FACTORIES:
            _cache[key] = _FACTORIES[db_engine]()
        elif db_engine in _DEFERRED_FACTORIES:
            _cache[key] = _DEFERRED_FACTORIES[db_engine]()()
        else:
            raise ValueError(
                f"Unsupported database engine: {engine!r}. "
                f"Supported: {SUPPORTED_ENGINES}"
            )

    return _cache[key]
