# db.py
import logging
import os

from dbbuddy_core.dialects import get_dialect
from dbbuddy_core.dialects.base import DialectConnection

logger = logging.getLogger(__name__)

# Wall-clock ceiling on any single statement against a customer database, applied
# per connection. Generous enough for a legitimately heavy analytical query, short
# enough that a runaway one surfaces as an error instead of a worker that never
# returns. Set to 0 to disable (not recommended outside debugging).
STATEMENT_TIMEOUT_SECONDS = float(os.getenv("ERP_STATEMENT_TIMEOUT", "60"))


class DatabaseUnavailableError(RuntimeError):
    """The target ERP database could not be reached or used (unreachable host,
    bad credentials, unknown database, schema fetch failed).

    It signals a problem with an *upstream* dependency, not a bug in this server —
    the API layer maps it to HTTP 502 Bad Gateway rather than 500.
    """


def connect_db(host: str, user: str, password: str, database: str, engine: str = "mysql", port: int | None = None, db_schema: str | None = None) -> DialectConnection | None:
    """Open a DialectConnection for the given engine.

    Returns a DialectConnection on success, None on failure. Callers are
    engine-agnostic — they work with the DialectConnection interface and
    never touch the underlying driver directly. ``port`` is optional; when None
    the engine's default port is used.
    """
    try:
        dialect = get_dialect(engine)
        raw_conn = dialect.connect(host, user, password, database, port=port,
                                   db_schema=db_schema)
        # Bound every statement on this connection. Without it a lock wait or a
        # scan of a huge table holds a worker and a pool slot until the database
        # decides to answer — the data path is the one users actually wait on and
        # was the only path in the system with no ceiling.
        if STATEMENT_TIMEOUT_SECONDS > 0:
            dialect.apply_statement_timeout(raw_conn, STATEMENT_TIMEOUT_SECONDS)
        logger.info("Connected to %s database %r on %s", engine, database, host)
        return DialectConnection(raw_conn, dialect)
    except Exception as e:
        logger.warning("Connection to %s database %r on %s failed: %s", engine, database, host, e)
        return None
