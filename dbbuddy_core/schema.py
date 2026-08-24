# ── Schema_Fetcher ──────────────────────────────────────────────────────────
import logging

from dbbuddy_core.dialects.base import DialectConnection


def fetch_schema(conn) -> dict[str, list[str]] | None:
    """Fetch the schema from the database via conn.

    When conn is a DialectConnection the engine-specific introspection SQL
    lives in the dialect. Raw connections (used in some tests) fall back to
    the legacy MySQL path so existing test fixtures keep working.
    """
    logger = logging.getLogger(__name__)
    try:
        if isinstance(conn, DialectConnection):
            return conn.fetch_schema()

        # Legacy path: raw mysql.connector connection (test fixtures / direct use).
        cursor = conn.cursor()
        cursor.execute("SHOW TABLES")
        tables = [row[0] for row in cursor.fetchall()]
        schema = {}
        for table in tables:
            cursor.execute(f"DESCRIBE {table}")
            schema[table] = [row[0] for row in cursor.fetchall()]
        return schema
    except Exception as e:
        logger.error(f"Schema fetch failed: {str(e)}")
        return None
