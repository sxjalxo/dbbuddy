"""PostgreSQL dialect — wraps psycopg2."""

try:
    import psycopg2
    import psycopg2.extras
except ImportError as _err:
    raise ImportError(
        "psycopg2 is required for PostgreSQL support. "
        "Install it with:  pip install psycopg2-binary"
    ) from _err

from dbbuddy_core.logger import get_logger
from .base import Dialect
from .capabilities import DialectCapabilities
from .schema_meta import ColumnMeta, DatabaseSchema, ForeignKeyMeta, TableMeta


logger = get_logger()

class PostgresDialect(Dialect):
    engine_name = "postgresql"

    _capabilities = DialectCapabilities(
        supports_json=True,
        supports_arrays=True,
        supports_cte=True,
        supports_window_functions=True,
        supports_lateral=True,
        supports_full_text=True,
        supports_returning=True,       # INSERT/UPDATE ... RETURNING
        supports_upsert=True,          # INSERT ... ON CONFLICT
        supports_regex=True,
        supports_ilike=True,           # native ILIKE
        identifier_quote_char='"',
        unquoted_identifier_case="lower",
    )

    @property
    def capabilities(self) -> DialectCapabilities:
        return self._capabilities

    # ── Schema scope ──────────────────────────────────────────────────────────
    # Introspection is scoped to ``current_schemas(false)`` — the schemas actually
    # on this connection's ``search_path`` — not to a hardcoded ``public``.
    #
    # With a default connection that resolves to ``public`` and nothing changes.
    # With ``db_schema`` set (see ``connect``) it resolves to that schema, so a
    # database whose tables live in a named schema is visible at all. Before this,
    # such a database reported *no tables*: not an error, just an empty schema and
    # every question failing to ground.
    #
    # Scoping to the search path rather than to a schema name is what keeps
    # discovery and execution consistent: an unqualified identifier in the emitted
    # SQL resolves through the same path the tables were found on.

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, host, user, password, database, port=None, db_schema=None):
        kwargs = dict(host=host, user=user, password=password, dbname=database)
        if port:
            kwargs["port"] = int(port)
        if db_schema:
            # Set the search path for the whole session rather than qualifying
            # every emitted identifier. Introspection reads the same path (see
            # ``_schema_scope``), so what the engine discovers and what an
            # unqualified reference resolves to cannot drift apart.
            #
            # The named schema alone, not "<schema>,public": a user who points DB
            # Buddy at a schema means that schema, and silently unioning `public`
            # would resurface exactly the surprise this setting exists to remove.
            # Built-ins are unaffected — `pg_catalog` is always searched first.
            escaped = db_schema.replace('"', '""')
            kwargs["options"] = f'-c search_path="{escaped}"'
        return psycopg2.connect(**kwargs)

    def ping(self, raw_conn) -> None:
        if raw_conn.closed:
            raise RuntimeError("PostgreSQL connection is closed")
        cur = raw_conn.cursor()
        cur.execute("SELECT 1")
        cur.close()

    def set_autocommit(self, raw_conn, value: bool) -> None:
        raw_conn.autocommit = value

    def apply_statement_timeout(self, raw_conn, seconds: float) -> bool:
        """PostgreSQL ``statement_timeout``, in **milliseconds**. The server aborts
        the statement itself, so this bounds the query rather than merely the
        client's patience — a client-side give-up would leave the query running.

        **Committed, and that is not tidiness.** psycopg2 opens a transaction for
        the ``SET``, and leaving it open broke the next thing every caller does:
        ``conn.autocommit = True`` raised ``set_session cannot be used inside a
        transaction``. Both call sites swallowed that exception, so a write ran
        inside a transaction nobody committed and disappeared when the connection
        closed — while ``/execute`` reported the rows as affected.

        Committing also makes the setting stick. ``SET`` without ``LOCAL`` is
        session-scoped, but a ``SET`` inside an uncommitted transaction is undone
        by a rollback, so the timeout would have been lost exactly when a query
        misbehaved enough to cause one.
        """
        try:
            cur = raw_conn.cursor()
            try:
                cur.execute("SET statement_timeout = %s", (int(seconds * 1000),))
            finally:
                cur.close()
            if not raw_conn.autocommit:
                raw_conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not set a PostgreSQL statement timeout: %s", exc)
            return False

    # ── Cursor ────────────────────────────────────────────────────────────────

    def dict_cursor(self, raw_conn):
        return raw_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── Schema introspection ──────────────────────────────────────────────────

    def fetch_schema(self, raw_conn) -> dict[str, list[str]]:
        cur = raw_conn.cursor()
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = ANY(current_schemas(false))
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        tables = [row[0] for row in cur.fetchall()]
        schema: dict[str, list[str]] = {}
        for table in tables:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s
                  AND table_schema = ANY(current_schemas(false))
                ORDER BY ordinal_position
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
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = ANY(current_schemas(false))
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        table_names = [row[0] for row in cur.fetchall()]
        tables: dict[str, TableMeta] = {}

        # Gather primary key columns once
        cur.execute(
            """
            SELECT kcu.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema   = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = ANY(current_schemas(false))
            """
        )
        pk_cols: set[tuple[str, str]] = set(cur.fetchall())

        for table in table_names:
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = %s
                  AND table_schema = ANY(current_schemas(false))
                ORDER BY ordinal_position
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

            # Foreign keys for this table
            cur.execute(
                """
                SELECT kcu.column_name, ccu.table_name, ccu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema   = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name = tc.constraint_name
                 AND ccu.table_schema   = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema   = ANY(current_schemas(false))
                  AND tc.table_name     = %s
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
        return "CURRENT_DATE"

    def current_timestamp(self) -> str:
        return "CURRENT_TIMESTAMP"

    def random(self) -> str:
        return "RANDOM()"

    def boolean_literal(self, value: bool) -> str:
        return "TRUE" if value else "FALSE"

    # ── Date / time ───────────────────────────────────────────────────────────

    def date_sub_interval(self, col: str, n: int, unit: str) -> str:
        return f"({col} - INTERVAL '{n} {unit.lower()}')"

    def date_add_interval(self, col: str, n: int, unit: str) -> str:
        return f"({col} + INTERVAL '{n} {unit.lower()}')"

    def extract_month(self, col: str) -> str:
        return f"EXTRACT(MONTH FROM {col})"

    def extract_year(self, col: str) -> str:
        return f"EXTRACT(YEAR FROM {col})"

    def date_cast(self, col: str) -> str:
        return f"({col})::date"

    # ── Casting ───────────────────────────────────────────────────────────────

    def cast(self, expr: str, type_name: str) -> str:
        # Postgres preferred shorthand; also supports standard CAST(x AS type)
        _type_map = {"integer": "INTEGER", "text": "TEXT", "float": "FLOAT", "signed": "INTEGER"}
        mapped = _type_map.get(type_name.lower(), type_name.upper())
        return f"{expr}::{mapped}"

    # ── Pattern matching ──────────────────────────────────────────────────────

    def regex_match(self, col: str, pattern: str) -> str:
        return f"{col} ~ {pattern}"

    # ── Pagination ────────────────────────────────────────────────────────────

    def limit(self, n: int, offset: int = 0) -> str:
        if offset:
            return f"LIMIT {n} OFFSET {offset}"
        return f"LIMIT {n}"
