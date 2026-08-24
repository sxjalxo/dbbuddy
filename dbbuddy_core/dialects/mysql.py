"""MySQL dialect — wraps mysql-connector-python."""

import mysql.connector

from dbbuddy_core.logger import get_logger
from .base import Dialect
from .capabilities import DialectCapabilities
from .schema_meta import ColumnMeta, DatabaseSchema, ForeignKeyMeta, TableMeta


logger = get_logger()

class MySQLDialect(Dialect):
    engine_name = "mysql"

    _capabilities = DialectCapabilities(
        supports_json=True,
        supports_arrays=False,
        supports_cte=True,           # MySQL 8+
        supports_window_functions=True,  # MySQL 8+
        supports_lateral=True,       # MySQL 8.0.14+
        supports_full_text=True,
        supports_returning=False,
        supports_upsert=True,        # INSERT ... ON DUPLICATE KEY UPDATE
        supports_regex=True,
        identifier_quote_char="`",
    )

    @property
    def capabilities(self) -> DialectCapabilities:
        return self._capabilities

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, host, user, password, database, port=None, db_schema=None):
        # MySQL has no namespace below the database — CREATE SCHEMA is an alias
        # for CREATE DATABASE — so ``database`` already carries this. Accepted and
        # ignored so callers need not special-case the engine.
        kwargs = dict(host=host, user=user, password=password, database=database)
        if port:
            kwargs["port"] = int(port)
        conn = mysql.connector.connect(**kwargs)
        if not conn.is_connected():
            raise RuntimeError("mysql.connector.connect returned a disconnected object")
        return conn

    def ping(self, raw_conn) -> None:
        raw_conn.ping(reconnect=True, attempts=2, delay=1)

    def apply_statement_timeout(self, raw_conn, seconds: float) -> bool:
        """MySQL 5.7.8+ — ``MAX_EXECUTION_TIME`` is in **milliseconds** and applies
        to read-only SELECTs, which is exactly the traffic that needs bounding.
        Older servers reject the variable; that is logged and reported, not raised."""
        try:
            cur = raw_conn.cursor()
            try:
                cur.execute("SET SESSION MAX_EXECUTION_TIME = %s", (int(seconds * 1000),))
            finally:
                cur.close()
            return True
        except Exception as exc:  # noqa: BLE001 — unsupported server, not a failure
            logger.warning("Could not set a MySQL statement timeout: %s", exc)
            return False

    # ── Cursor ────────────────────────────────────────────────────────────────

    def dict_cursor(self, raw_conn):
        return raw_conn.cursor(dictionary=True)

    # ── Schema introspection ──────────────────────────────────────────────────

    def fetch_schema(self, raw_conn) -> dict[str, list[str]]:
        cursor = raw_conn.cursor()
        cursor.execute("SHOW TABLES")
        tables = [row[0] for row in cursor.fetchall()]
        schema: dict[str, list[str]] = {}
        for table in tables:
            cursor.execute(f"DESCRIBE {table}")
            schema[table] = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return schema

    def fetch_schema_rich(self, raw_conn) -> DatabaseSchema:
        cursor = raw_conn.cursor()
        cursor.execute("SHOW TABLES")
        table_names = [row[0] for row in cursor.fetchall()]
        tables: dict[str, TableMeta] = {}

        for table in table_names:
            # Columns + key info from information_schema
            cursor.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (table,),
            )
            columns = [
                ColumnMeta(
                    name=row[0],
                    data_type=row[1],
                    nullable=(row[2] == "YES"),
                    default=row[3],
                    is_primary_key=(row[4] == "PRI"),
                    is_unique=(row[4] in ("PRI", "UNI")),
                )
                for row in cursor.fetchall()
            ]

            # Foreign keys
            cursor.execute(
                """
                SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = %s
                  AND REFERENCED_TABLE_NAME IS NOT NULL
                """,
                (table,),
            )
            fks = [
                ForeignKeyMeta(
                    column=row[0],
                    referenced_table=row[1],
                    referenced_column=row[2],
                )
                for row in cursor.fetchall()
            ]
            tables[table] = TableMeta(name=table, columns=columns, foreign_keys=fks)

        cursor.close()
        return DatabaseSchema(tables=tables)

    # ── SQL scalar overrides ──────────────────────────────────────────────────

    def current_date(self) -> str:
        return "CURDATE()"

    def current_timestamp(self) -> str:
        return "NOW()"

    def random(self) -> str:
        return "RAND()"

    def boolean_literal(self, value: bool) -> str:
        return "1" if value else "0"

    # ── Date / time ───────────────────────────────────────────────────────────

    def date_sub_interval(self, col: str, n: int, unit: str) -> str:
        return f"DATE_SUB({col}, INTERVAL {n} {unit.upper()})"

    def date_add_interval(self, col: str, n: int, unit: str) -> str:
        return f"DATE_ADD({col}, INTERVAL {n} {unit.upper()})"

    def extract_month(self, col: str) -> str:
        return f"MONTH({col})"

    def extract_year(self, col: str) -> str:
        return f"YEAR({col})"

    def date_cast(self, col: str) -> str:
        return f"DATE({col})"

    # ── Casting ───────────────────────────────────────────────────────────────

    def cast(self, expr: str, type_name: str) -> str:
        # MySQL uses SIGNED/UNSIGNED instead of INTEGER
        _type_map = {"integer": "SIGNED", "text": "CHAR", "float": "DECIMAL"}
        mapped = _type_map.get(type_name.lower(), type_name.upper())
        return f"CAST({expr} AS {mapped})"

    # ── Pattern matching ──────────────────────────────────────────────────────

    def regex_match(self, col: str, pattern: str) -> str:
        return f"{col} REGEXP {pattern}"

    # ── Pagination ────────────────────────────────────────────────────────────

    def limit(self, n: int, offset: int = 0) -> str:
        if offset:
            return f"LIMIT {n} OFFSET {offset}"
        return f"LIMIT {n}"
