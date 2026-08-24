"""
Test database adapter for E2E testing without MySQL dependency.

This module provides SQLite-compatible versions of db.py and schema.py
functions to enable true end-to-end testing without requiring MySQL setup.
"""

import sqlite3
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SqliteDictCursor:
    """Cursor adapter: dict rows + %s→? placeholder translation.

    The engine compiles parameterized SQL with MySQL-style ``%s`` placeholders
    and reads rows as dicts (``cursor(dictionary=True)``); sqlite3 supports
    neither, so this shim translates both.
    """

    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    def execute(self, sql: str, params=None):
        sql = sql.replace("%s", "?")
        if params:
            return self._cursor.execute(sql, params)
        return self._cursor.execute(sql)

    def _row_to_dict(self, row):
        columns = [d[0] for d in self._cursor.description or []]
        return dict(zip(columns, row))

    def fetchall(self):
        return [self._row_to_dict(r) for r in self._cursor.fetchall()]

    def fetchmany(self, size: int = 1):
        # Reads are bounded at the cursor now (query._fetch_bounded), so the
        # adapter has to offer the DB-API's batched read like a real driver.
        return [self._row_to_dict(r) for r in self._cursor.fetchmany(size)]

    def fetchone(self):
        row = self._cursor.fetchone()
        return self._row_to_dict(row) if row is not None else None

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def close(self):
        self._cursor.close()


class SqliteDialectConnection:
    """Adapts a sqlite3 connection to the DialectConnection contract the engine
    expects from ``connect_db`` (``cursor(dictionary=True)``, commit/close)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def cursor(self, dictionary: bool = False):
        cur = self._conn.cursor()
        return SqliteDictCursor(cur) if dictionary else cur

    def ping(self, reconnect: bool = True):
        # The connection pool revalidates reused connections with ping();
        # sqlite is in-process, so reachability is a no-op check.
        self._conn.execute("SELECT 1")

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def connect_db_sqlite(host: str, user: str, password: str, database: str) -> Optional[sqlite3.Connection]:
    """
    SQLite-compatible database connection for testing.

    Args:
        host: Ignored for SQLite (kept for interface compatibility)
        user: Ignored for SQLite (kept for interface compatibility)
        password: Ignored for SQLite (kept for interface compatibility)
        database: For SQLite, this can be ":memory:" or a file path

    Returns:
        SQLite connection object or None on failure
    """
    try:
        conn = sqlite3.connect(database)
        logger.info(f"[+] Connected to SQLite database: {database}")
        return conn
    except Exception as e:
        logger.error(f"[-] SQLite connection failed: {e}")
        return None


def fetch_schema_sqlite(conn: sqlite3.Connection) -> dict[str, list[str]] | None:
    """
    SQLite-compatible schema fetching for testing.

    Uses PRAGMA table_info instead of MySQL's DESCRIBE command.

    Args:
        conn: SQLite connection object

    Returns:
        Dictionary mapping table names to column lists, or None on failure
    """
    try:
        cursor = conn.cursor()

        # Get all tables (SQLite-specific)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        schema = {}
        for table in tables:
            # Get column info using PRAGMA (SQLite-specific)
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in cursor.fetchall()]  # row[1] is column name
            schema[table] = columns

        logger.info(f"[+] Fetched schema with {len(schema)} tables")
        return schema

    except Exception as e:
        logger.error(f"[-] Schema fetch failed: {e}")
        return None


def create_test_schema(conn: sqlite3.Connection) -> bool:
    """
    Create test schema for E2E testing.

    Args:
        conn: SQLite connection object

    Returns:
        True if successful, False otherwise
    """
    try:
        cursor = conn.cursor()

        # Create test tables
        cursor.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT COLLATE NOCASE,
                email TEXT COLLATE NOCASE,
                country TEXT COLLATE NOCASE,
                status TEXT COLLATE NOCASE,
                created_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                total_amount REAL,
                status TEXT COLLATE NOCASE,
                created_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                name TEXT COLLATE NOCASE,
                price REAL,
                category TEXT COLLATE NOCASE
            )
        """)

        # Insert test data
        cursor.execute("INSERT INTO users VALUES (1, 'Alice', 'alice@example.com', 'USA', 'active', '2024-01-01')")
        cursor.execute("INSERT INTO users VALUES (2, 'Bob', 'bob@example.com', 'India', 'active', '2024-01-02')")
        cursor.execute("INSERT INTO users VALUES (3, 'Charlie', 'charlie@example.com', 'India', 'inactive', '2024-01-03')")

        cursor.execute("INSERT INTO orders VALUES (1, 1, 100.0, 'completed', '2024-01-01')")
        cursor.execute("INSERT INTO orders VALUES (2, 2, 200.0, 'pending', '2024-01-02')")
        cursor.execute("INSERT INTO orders VALUES (3, 1, 150.0, 'completed', '2024-01-03')")

        cursor.execute("INSERT INTO products VALUES (1, 'Laptop', 999.99, 'electronics')")
        cursor.execute("INSERT INTO products VALUES (2, 'Mouse', 29.99, 'electronics')")

        conn.commit()
        logger.info("[+] Created test schema with sample data")
        return True

    except Exception as e:
        logger.error(f"[-] Failed to create test schema: {e}")
        return False


def execute_sql_sqlite(conn: sqlite3.Connection, sql: str) -> dict:
    """
    Execute SQL query on SQLite for testing.

    Args:
        conn: SQLite connection object
        sql: SQL query to execute

    Returns:
        Dictionary with success status and results
    """
    try:
        cursor = conn.cursor()
        cursor.execute(sql)

        # Determine if it's a SELECT query or write operation
        sql_lower = sql.lower().strip()
        if sql_lower.startswith("select"):
            results = cursor.fetchall()
            # Convert to list of dicts for consistency
            columns = [desc[0] for desc in cursor.description]
            dict_results = [dict(zip(columns, row)) for row in results]
            return {"success": True, "results": dict_results}
        else:
            # Write operation
            conn.commit()
            return {"success": True, "rows_affected": cursor.rowcount}

    except Exception as e:
        logger.error(f"[-] SQL execution failed: {e}")
        return {"success": False, "error": str(e)}
