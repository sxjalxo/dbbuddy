"""Microsoft SQL Server dialect — wraps pymssql.

pymssql is used (rather than pyodbc) so the driver installs with a plain
``pip install`` and needs no system ODBC driver configuration.

STATUS: integration-tested against a live SQL Server 2022 instance via
``tests/integration/test_live_engines.py`` (opt-in: ``pytest -m integration``
after ``docker compose -f docker-compose.test.yml up -d``). Verified live:
connection on a mapped port, INFORMATION_SCHEMA introspection
(tables/columns/PKs/FKs), TOP-based row limiting, autocommit, and dict-cursor rows.

**Schemas.** Introspection is scoped to ``SCHEMA_NAME()`` — the connecting user's
own default schema — not to the literal ``dbo``, which would repeat the hardcoded
``public`` mistake the PostgreSQL dialect was fixed for. It previously scoped to
*nothing*, so two same-named tables in different schemas merged into one entry
carrying both tables' columns.

Pointing at some *other* schema is refused rather than half-honoured: T-SQL has no
``search_path``, so introspecting one schema while unqualified names resolve to
another would leave the planner building on tables the query cannot see. Doing it
properly needs schema-qualified identifier emission — see ``docs/ROADMAP.md``.

Still worth exercising before high-stakes production use: collation edge cases,
large result sets, and concurrent writes.
"""

try:
    import pymssql
except ImportError as _err:
    raise ImportError(
        "pymssql is required for SQL Server support. "
        "Install it with:  pip install pymssql"
    ) from _err

from .base import Dialect
from .capabilities import DialectCapabilities
from .schema_meta import ColumnMeta, DatabaseSchema, ForeignKeyMeta, TableMeta


class SQLServerDialect(Dialect):
    engine_name = "sqlserver"

    _capabilities = DialectCapabilities(
        supports_json=False,           # JSON via functions only, no JSON column type
        supports_arrays=False,
        supports_cte=True,
        supports_window_functions=True,
        supports_lateral=False,        # SQL Server uses CROSS/OUTER APPLY, not LATERAL
        supports_full_text=False,      # optional feature, not assumed
        supports_returning=False,      # has OUTPUT, but not RETURNING syntax
        supports_upsert=False,         # has MERGE, but not ON CONFLICT syntax
        supports_regex=False,
        identifier_quote_char='"',     # QUOTED_IDENTIFIER is ON by default under pymssql
    )

    @property
    def capabilities(self) -> DialectCapabilities:
        return self._capabilities

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, host, user, password, database, port=None, db_schema=None):
        """Connect, and refuse a schema this session cannot actually resolve.

        PostgreSQL honours ``db_schema`` by setting ``search_path`` on the session,
        so what is introspected and what an unqualified name resolves to are the
        same thing by construction. **T-SQL has no session-level equivalent** — a
        user's default schema is a property of the user, changed with DDL.

        So the scope is ``SCHEMA_NAME()``, the connecting user's own default
        schema, which gives the same guarantee by the same mechanism. A requested
        schema that is not that one is refused rather than half-honoured:
        introspecting one schema while unqualified names resolve to another is
        precisely the drift the PostgreSQL fix exists to prevent, and it would
        produce a plan built from tables the query cannot see.
        """
        kwargs = dict(server=host.strip(), user=user, password=password, database=database)
        if port:
            kwargs["port"] = int(port)
        conn = pymssql.connect(**kwargs)
        try:
            self.verify_schema(conn, db_schema)
        except Exception:
            conn.close()
            raise
        return conn

    def session_schema(self, raw_conn) -> str | None:
        """The schema that unqualified names in this session resolve to."""
        cur = raw_conn.cursor()
        try:
            cur.execute("SELECT SCHEMA_NAME()")
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            cur.close()

    def verify_schema(self, raw_conn, db_schema: str | None) -> None:
        """Raise unless ``db_schema`` is the session's own schema. No-op if None."""
        if not db_schema:
            return
        current = self.session_schema(raw_conn)
        # SQL Server identifiers are case-insensitive under the usual collations,
        # so refusing "Sales" against "sales" would be a false alarm.
        if current and current.lower() == db_schema.lower():
            return
        raise ValueError(
            f"SQL Server cannot switch the schema a session resolves names against, "
            f"and this connection resolves to {current!r}, not {db_schema!r}. "
            f"Connect as a user whose default schema is {db_schema!r} "
            f"(ALTER USER ... WITH DEFAULT_SCHEMA = {db_schema}), or drop the "
            f"schema setting to use {current!r}."
        )

    def ping(self, raw_conn) -> None:
        cur = raw_conn.cursor()
        cur.execute("SELECT 1")
        cur.close()

    def set_autocommit(self, raw_conn, value: bool) -> None:
        # pymssql exposes autocommit as a method, not an attribute.
        ac = getattr(raw_conn, "autocommit", None)
        if callable(ac):
            ac(value)
        else:
            raw_conn.autocommit = value

    # ── Cursor ────────────────────────────────────────────────────────────────

    def dict_cursor(self, raw_conn):
        return raw_conn.cursor(as_dict=True)

    # ── Schema introspection ──────────────────────────────────────────────────

    def fetch_schema(self, raw_conn) -> dict[str, list[str]]:
        cur = raw_conn.cursor()
        cur.execute(
            """
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
              AND TABLE_SCHEMA = SCHEMA_NAME()
            ORDER BY TABLE_NAME
            """
        )
        tables = [row[0] for row in cur.fetchall()]
        schema: dict[str, list[str]] = {}
        for table in tables:
            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = %s
                  AND TABLE_SCHEMA = SCHEMA_NAME()
                ORDER BY ORDINAL_POSITION
                """,
                (table,),
            )
            schema[table] = [row[0] for row in cur.fetchall()]
        cur.close()
        return schema

    def fetch_schema_rich(self, raw_conn) -> DatabaseSchema:
        cur = raw_conn.cursor()

        cur.execute(
            """
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
              AND TABLE_SCHEMA = SCHEMA_NAME()
            ORDER BY TABLE_NAME
            """
        )
        table_names = [row[0] for row in cur.fetchall()]

        # Primary key columns (table, column) across the database.
        cur.execute(
            """
            SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
             AND tc.TABLE_SCHEMA    = kcu.TABLE_SCHEMA
            WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
              AND tc.TABLE_SCHEMA = SCHEMA_NAME()
            """
        )
        pk_cols: set[tuple[str, str]] = set(cur.fetchall())

        tables: dict[str, TableMeta] = {}
        for table in table_names:
            cur.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = %s
                  AND TABLE_SCHEMA = SCHEMA_NAME()
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
                    is_primary_key=((table, row[0]) in pk_cols),
                )
                for row in cur.fetchall()
            ]

            # Foreign keys for this table (portable INFORMATION_SCHEMA form).
            cur.execute(
                """
                SELECT fk.COLUMN_NAME, pk.TABLE_NAME, pk.COLUMN_NAME
                FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE fk
                  ON rc.CONSTRAINT_NAME = fk.CONSTRAINT_NAME
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE pk
                  ON rc.UNIQUE_CONSTRAINT_NAME = pk.CONSTRAINT_NAME
                 AND fk.ORDINAL_POSITION       = pk.ORDINAL_POSITION
                WHERE fk.TABLE_NAME  = %s
                  -- A constraint name is unique per schema, not per database, so
                  -- joining on name alone can pair this key with another schema's
                  -- constraint and invent a relationship that does not exist.
                  AND fk.TABLE_SCHEMA = SCHEMA_NAME()
                  AND pk.TABLE_SCHEMA = SCHEMA_NAME()
                """,
                (table,),
            )
            fks = [
                ForeignKeyMeta(
                    column=row[0],
                    referenced_table=row[1],
                    referenced_column=row[2],
                )
                for row in cur.fetchall()
            ]
            tables[table] = TableMeta(name=table, columns=columns, foreign_keys=fks)

        cur.close()
        return DatabaseSchema(tables=tables)

    # ── SQL scalar overrides ──────────────────────────────────────────────────

    def current_date(self) -> str:
        return "CAST(GETDATE() AS DATE)"

    def current_timestamp(self) -> str:
        return "GETDATE()"

    def random(self) -> str:
        return "RAND()"

    def boolean_literal(self, value: bool) -> str:
        # SQL Server has no boolean type; BIT uses 1 / 0.
        return "1" if value else "0"

    # ── Date / time ───────────────────────────────────────────────────────────

    def date_sub_interval(self, col: str, n: int, unit: str) -> str:
        return f"DATEADD({unit.lower()}, -{n}, {col})"

    def date_add_interval(self, col: str, n: int, unit: str) -> str:
        return f"DATEADD({unit.lower()}, {n}, {col})"

    def extract_month(self, col: str) -> str:
        return f"MONTH({col})"

    def extract_year(self, col: str) -> str:
        return f"YEAR({col})"

    def date_cast(self, col: str) -> str:
        return f"CAST({col} AS DATE)"

    # ── Casting ───────────────────────────────────────────────────────────────

    def cast(self, expr: str, type_name: str) -> str:
        _type_map = {
            "integer": "INT",
            "int": "INT",
            "signed": "INT",
            "text": "NVARCHAR(MAX)",
            "float": "FLOAT",
        }
        mapped = _type_map.get(type_name.lower(), type_name.upper())
        return f"CAST({expr} AS {mapped})"

    # ── Pagination ────────────────────────────────────────────────────────────

    def limit(self, n: int, offset: int = 0) -> str:
        """SQL Server has no LIMIT. OFFSET/FETCH requires an ORDER BY upstream;
        the compiler prefers ``SELECT TOP n`` for the common no-offset case."""
        if offset:
            return f"OFFSET {offset} ROWS FETCH NEXT {n} ROWS ONLY"
        return f"OFFSET 0 ROWS FETCH NEXT {n} ROWS ONLY"
