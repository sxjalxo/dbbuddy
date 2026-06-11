# ── Schema_Fetcher ──────────────────────────────────────────────────────────
import logging

def fetch_schema(conn) -> dict[str, list[str]] | None:
    logger = logging.getLogger(__name__)
    try:
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
        print(f"[-] Failed to fetch schema: {e}")
        return None
