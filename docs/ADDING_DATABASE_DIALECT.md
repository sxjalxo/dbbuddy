# Adding a Database Dialect

DB Buddy talks to every SQL engine through a single **dialect layer**. The
planner, compiler, and execution path are engine-agnostic — there are no
`if engine == "postgres"` branches scattered through the code. Adding a new
engine (Oracle, SQLite, ClickHouse, …) means implementing one contract and
registering it — not editing the pipeline.

Today three engines ship: **MySQL**, **PostgreSQL**, and **SQL Server**. Use any
of them as a reference:

- [`dbbuddy_core/dialects/mysql.py`](../dbbuddy_core/dialects/mysql.py) — eager driver
- [`dbbuddy_core/dialects/postgres.py`](../dbbuddy_core/dialects/postgres.py) — deferred driver
- [`dbbuddy_core/dialects/sqlserver.py`](../dbbuddy_core/dialects/sqlserver.py) — deferred driver + `TOP` pagination

---

## Overview: the moving parts

| File | What you add |
| ---- | ------------ |
| `dbbuddy_core/dialects/engine.py` | Enum member + normalization aliases |
| `dbbuddy_core/dialects/<engine>.py` | The `Dialect` subclass (new file) |
| `dbbuddy_core/dialects/registry.py` | A factory entry (eager or deferred) |
| `dbbuddy_core/execution.py` | Pagination handling **if** the engine has no `LIMIT` |
| `requirements.txt` | The driver package |
| `backend/app_db/routers/connections.py` | Already engine-agnostic (canonicalizes via `normalize`) — usually no change |
| `frontend/src/routes/app.tsx` | Add the engine to the connect picker + `prettyEngine` label map |
| `tests/test_dialect_contract.py` | Register the dialect in the contract suite |
| `tests/test_<engine>_dialect.py` | Engine-specific unit tests (mocked driver) |
| `tests/integration/test_live_engines.py` + `docker-compose.test.yml` | Live integration coverage |

There's a copy-paste [checklist](#checklist) at the bottom.

---

## 1. Register the engine name and its aliases

Add a member to the `DatabaseEngine` enum in
[`dbbuddy_core/dialects/engine.py`](../dbbuddy_core/dialects/engine.py). The enum
subclasses `(str, Enum)`, so members compare equal to their string value and
serialize transparently.

```python
class DatabaseEngine(str, Enum):
    MYSQL = "mysql"
    POSTGRES = "postgresql"
    SQLSERVER = "sqlserver"
    ORACLE = "oracle"          # ← new: the canonical value
```

Add any user-facing spellings to `normalize()` so `"Oracle DB"`, `"oracledb"`,
etc. all resolve to the canonical member. `normalize()` is what the API and
frontend go through, so this is where you make the engine forgiving about input:

```python
_aliases = {
    "postgres": cls.POSTGRES,
    "mssql": cls.SQLSERVER,
    "sql server": cls.SQLSERVER,
    "oracledb": cls.ORACLE,     # ← new
}
```

`SUPPORTED_ENGINES` (exposed at `GET /engines` and used by the frontend) is
derived automatically from the enum — no separate list to maintain.

---

## 2. Implement the `Dialect` contract

Create `dbbuddy_core/dialects/<engine>.py` subclassing
[`Dialect`](../dbbuddy_core/dialects/base.py). Import the driver at module top and
raise a helpful `ImportError` if it's missing (deferred registration means this
only fires when the engine is actually used):

```python
try:
    import oracledb
except ImportError as _err:
    raise ImportError("oracledb is required for Oracle support. pip install oracledb") from _err
```

### Required (abstract) methods

| Method | Contract |
| ------ | -------- |
| `capabilities` (property) | Return an immutable `DialectCapabilities` (see §3). |
| `connect(host, user, password, database, port=None)` | Open a raw driver connection; raise on failure. `port=None` ⇒ engine default. |
| `ping(raw_conn)` | Revalidate a pooled connection; raise if dead (e.g. run `SELECT 1`). |
| `dict_cursor(raw_conn)` | Return a cursor whose `fetchall()/fetchone()` yield plain **dicts**. |
| `fetch_schema(raw_conn) -> dict[str, list[str]]` | `{table: [col, ...]}` for every user table. |
| `fetch_schema_rich(raw_conn) -> DatabaseSchema` | Full typed metadata incl. PKs + FKs (see §4). |

### Override when the driver differs from the MySQL defaults

- `set_autocommit(raw_conn, value)` — the base sets `raw_conn.autocommit = value`.
  Override if the driver uses a **method** (e.g. pymssql: `raw_conn.autocommit(value)`).
- SQL scalar/fragment methods — the base returns MySQL syntax. Override the ones
  that differ:

| Method | MySQL (base) | PostgreSQL | SQL Server |
| ------ | ------------ | ---------- | ---------- |
| `current_date()` | `CURDATE()` | `CURRENT_DATE` | `CAST(GETDATE() AS DATE)` |
| `current_timestamp()` | `NOW()` | `CURRENT_TIMESTAMP` | `GETDATE()` |
| `random()` | `RAND()` | `RANDOM()` | `RAND()` |
| `boolean_literal(v)` | `TRUE`/`FALSE` | `TRUE`/`FALSE` | `1`/`0` (BIT) |
| `date_sub_interval(col,n,u)` | `DATE_SUB(col, INTERVAL n U)` | `(col - INTERVAL 'n u')` | `DATEADD(u, -n, col)` |
| `date_add_interval(col,n,u)` | `DATE_ADD(...)` | `(col + INTERVAL ...)` | `DATEADD(u, n, col)` |
| `extract_month(col)` / `extract_year(col)` | `MONTH()` / `YEAR()` | `EXTRACT(... FROM col)` | `MONTH()` / `YEAR()` |
| `date_cast(col)` | `DATE(col)` | `(col)::date` | `CAST(col AS DATE)` |
| `cast(expr, type)` | `CAST(expr AS type)` | `expr::type` | `CAST(expr AS type)` (map `text`→`NVARCHAR(MAX)`) |
| `concat(*args)` | `CONCAT(...)` | `CONCAT(...)` | `CONCAT(...)` |
| `regex_match(col, pat)` | `NotImplementedError` | `col ~ pat` | `NotImplementedError` |
| `limit(n, offset)` | `LIMIT n [OFFSET o]` | `LIMIT n [OFFSET o]` | `OFFSET o ROWS FETCH NEXT n ROWS ONLY` |

`quote_identifier()` is provided by the base and uses
`capabilities.identifier_quote_char` (doubling it to escape). Set that flag
rather than overriding the method. Use `"` where the engine supports
ANSI-quoted identifiers.

---

## 3. Declare capabilities

`DialectCapabilities`
([capabilities.py](../dbbuddy_core/dialects/capabilities.py)) is a frozen
dataclass of boolean flags. The planner asks *"can this engine do X?"* rather
than *"is this Postgres?"*, so **be conservative**: only advertise a capability
if the engine supports it with syntax the compiler actually emits.

```python
_capabilities = DialectCapabilities(
    supports_json=False,
    supports_arrays=False,
    supports_cte=True,
    supports_window_functions=True,
    supports_lateral=False,
    supports_full_text=False,
    supports_returning=False,   # e.g. SQL Server has OUTPUT, not RETURNING → False
    supports_upsert=False,      # different syntax per engine → False unless compiled
    supports_regex=False,
    identifier_quote_char='"',
)
```

---

## 4. Introspection requirements

Both introspection methods must return the same **shape** regardless of engine —
that's what keeps the semantic layer and relationship graph engine-agnostic.

- **`fetch_schema`** → `{table_name: [column_name, ...]}` for user tables, in
  ordinal order. Most engines can do this from `INFORMATION_SCHEMA.TABLES` /
  `INFORMATION_SCHEMA.COLUMNS` (MySQL, Postgres, SQL Server all do). Exclude
  system tables/schemas.

- **`fetch_schema_rich`** → a `DatabaseSchema` built from the typed metadata in
  [schema_meta.py](../dbbuddy_core/dialects/schema_meta.py):

  ```
  DatabaseSchema(tables={name: TableMeta})
    TableMeta(name, columns=[ColumnMeta], foreign_keys=[ForeignKeyMeta])
      ColumnMeta(name, data_type, nullable, default, is_primary_key)
      ForeignKeyMeta(column, referenced_table, referenced_column)
  ```

  **Primary keys and foreign keys are required** — the join planner relies on FK
  metadata. If the engine can't express FKs via `INFORMATION_SCHEMA`, use its
  system catalogs (e.g. SQL Server's `REFERENTIAL_CONSTRAINTS` join, or Oracle's
  `ALL_CONSTRAINTS`/`ALL_CONS_COLUMNS`).

Use the driver's parameter placeholder for the table filter (`%s` for
mysql-connector / psycopg2 / pymssql; `:1` for oracledb) — never string-format
identifiers or values into introspection queries.

---

## 5. Pagination differences (important)

There is **no single `LIMIT` syntax** across engines, and the primary compiler
does not route through `dialect.limit()`. Row limiting is emitted in
[`execution.py::compile_sql`](../dbbuddy_core/execution.py), which takes an
`engine` argument (threaded from `orchestrator.py` via `config.engine`):

- **MySQL / PostgreSQL** — a trailing `LIMIT n`.
- **SQL Server** — `SELECT TOP n …` (injected after `SELECT`), because SQL
  Server has no `LIMIT` and `OFFSET/FETCH` requires an `ORDER BY` that may be
  absent.

If your new engine also lacks `LIMIT`, extend the branch in `compile_sql`
(mirror the `_is_sqlserver(engine)` check) rather than only overriding
`dialect.limit()`. Keep the `dialect.limit()` override too — it documents the
engine's native form and is used by the secondary intent compiler in
`query.py`.

> Rule of thumb: `dialect.limit()` describes the engine; `compile_sql`'s
> `engine` branch is what the deterministic pipeline actually emits.

---

## 6. Register the dialect

Add a factory in
[`registry.py`](../dbbuddy_core/dialects/registry.py). Prefer **deferred**
registration for any engine whose driver is an optional dependency, so a missing
driver only errors when that engine is requested — not at import time:

```python
def _oracle_factory():
    from .oracle import OracleDialect   # deferred — requires oracledb
    return OracleDialect

_DEFERRED_FACTORIES = {
    DatabaseEngine.POSTGRES: _postgres_factory,
    DatabaseEngine.SQLSERVER: _sqlserver_factory,
    DatabaseEngine.ORACLE: _oracle_factory,     # ← new
}
```

Only put an engine in the eager `_FACTORIES` map if its driver is a hard
dependency (MySQL is, because it's the default).

Add the driver to [`requirements.txt`](../requirements.txt) with a comment.

---

## 7. Wire the UI + API (usually trivial)

- **Backend** — `connections.py` already canonicalizes the engine through
  `DatabaseEngine.normalize(...)`, and `main.py` threads `engine`/`port` for all
  engines. No change needed unless your engine has special connection semantics.
- **Frontend** — add the display label to the connect picker in
  `frontend/src/routes/app.tsx` (`["MySQL", "PostgreSQL", "SQL Server"]`) and to
  the `prettyEngine` label map. The label is lowercased and normalized on the
  backend, so `"SQL Server"` → `"sqlserver"` works automatically.

---

## 8. Required tests

Three layers, in increasing cost:

1. **Contract test (required).** Register the dialect in
   [`tests/test_dialect_contract.py`](../tests/test_dialect_contract.py) by
   adding a factory `pytest.param`. Guard optional drivers with
   `pytest.importorskip`:

   ```python
   def _make_oracle_dialect():
       pytest.importorskip("oracledb", reason="oracledb not installed")
       from dbbuddy_core.dialects.oracle import OracleDialect
       return OracleDialect()

   DIALECT_FACTORIES = [..., pytest.param(_make_oracle_dialect, id="oracle")]
   ```

   The contract suite asserts every dialect returns non-empty SQL fragments,
   valid boolean literals, a single-char quote char, all-boolean capabilities,
   and date fragments that reference the column.

2. **Engine unit tests (recommended).** Add `tests/test_<engine>_dialect.py`
   mirroring `test_postgres_dialect.py` — mock the driver and assert connection
   kwargs (including `port` passthrough), cursor behavior, and the SQL fragments.

3. **Live integration test (required before "production-ready").** Unit +
   contract tests don't catch real-database quirks (collation, identifier
   casing, datetime types, FK catalog differences). Add the engine to:
   - [`docker-compose.test.yml`](../docker-compose.test.yml) — a container with a
     seeded `testdb` (watch for host **port conflicts** with locally-running DBs;
     MySQL/Postgres are remapped to 3307/5433 for that reason).
   - [`tests/integration/test_live_engines.py`](../tests/integration/test_live_engines.py) —
     add connection params + engine-specific seed DDL. The suite verifies
     connection on a mapped port, `INFORMATION_SCHEMA` introspection (tables /
     PKs / FKs), a dict cursor, and live row-limiting.

   These are marked `@pytest.mark.integration` and **excluded from the default
   run** (`addopts = -m 'not integration'` in `pyproject.toml`). Run them with:

   ```bash
   docker compose -f docker-compose.test.yml up -d
   pip install <driver>
   pytest -m integration
   docker compose -f docker-compose.test.yml down -v
   ```

> Note: there is a second, older live-test gate — some real-execution tests in
> `tests/` are skipped unless `DBBUDDY_LIVE_DB_TESTS=1` (see `tests/conftest.py`).
> New dialect work should use the `-m integration` + docker path above.

---

## Checklist

```
[ ] engine.py       — add DatabaseEngine.<X> + normalize() aliases
[ ] <engine>.py     — Dialect subclass: capabilities, connect(port), ping,
                      set_autocommit (if method-based), dict_cursor,
                      fetch_schema, fetch_schema_rich (PK + FK!), SQL fragments
[ ] registry.py     — deferred (optional driver) or eager factory
[ ] execution.py    — extend the pagination branch if the engine lacks LIMIT
[ ] requirements.txt — add the driver
[ ] app.tsx         — connect picker + prettyEngine label
[ ] test_dialect_contract.py — register factory (importorskip)
[ ] test_<engine>_dialect.py — mocked unit tests (incl. port passthrough)
[ ] docker-compose.test.yml + test_live_engines.py — live integration (mind port clashes)
[ ] README.md       — add the engine to the drivers table + engine lists
```

When every box is checked and `pytest -m integration` is green against a live
instance, the engine is genuinely supported — not just compiled.
