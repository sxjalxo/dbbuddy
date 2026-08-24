# DB Buddy — Architecture

**An explainable, deterministic, and self-learning natural language query system**

---

## Overview

DB Buddy is a production-grade NL→SQL system that combines:

* deterministic query planning
* multi-engine support (MySQL, PostgreSQL & SQL Server) via a single dialect layer
* semantic understanding via embeddings
* explainable reasoning
* adaptive learning with guardrails

Unlike traditional NL-to-SQL systems, DB Buddy **does not rely on probabilistic SQL generation**.

Instead, it uses a structured pipeline where:

* AI assists in understanding
* the system makes final decisions deterministically

The codebase has settled into four subsystems with distinct responsibilities:

1. **Deterministic query engine** — planning, compilation, execution.
2. **Semantic understanding** — embeddings, learning, AI-assisted labeling.
3. **Explainability** — execution reasoning and query explanations.
4. **Insights Engine** — evidence-bound analysis of executed results.

AI appears in (2) and (4) only, and in both it is constrained to *understanding
and explanation* rather than decision-making or SQL generation. Keeping that
boundary is the single most load-bearing property of the design.

---

## System Architecture

The system follows a multi-stage pipeline:

```
User Query
  ↓
Semantic Enhancer (memory-assisted)
  ↓
Intent Builder
  ↓
Query Planner (deterministic plan)
  ↓
SQL Compiler (Predicate AST)  ──→  Dialect Layer (MySQL / PostgreSQL / SQL Server)
  ↓
Execution Engine (parameterized, safe)
  ↓
Explainability Engine
  ↓
Learning Engine
```

The stages above the dialect layer are **engine-agnostic** — the planner builds
an abstract plan and the SQL compiler renders it via a Predicate AST. Only the
dialect layer knows how to render vendor-specific SQL and run the plan against a
specific engine (see Core Layer 4b). SQL is compiled deterministically; AI is
used only for semantic enrichment, never to generate SQL.

---

## Core Layers

### 1. Semantic Layer (Embeddings + Retrieval)

* Uses embeddings for schema understanding
* ChromaDB (persistent, single shared client) for vector similarity (optional)
* Falls back to in-memory matching if unavailable
* No hardcoded mappings
* Built once per schema and **persisted** — see Context Caching below

---

### 1a. Context Caching & Performance

The expensive setup — DB connection, schema, AI-refined semantic layer, vector
index, and relationship graph — is prepared **once per `(host, database,
schema_hash)`** and reused, rather than recomputed on every request. This is the
single biggest performance lever (a query dropped from ~6–8 s to ~5–15 ms warm).

* **AI labeling (`ai_refine`) runs only on Analyze Schema** and is persisted to
  `.dbbuddy_cache/`. The query path never calls the LLM: it loads the persisted
  AI layer, or uses fast rule-based labels until Analyze runs.
* **Persistence across restarts** — semantic layers (`.dbbuddy_cache/`) and
  embeddings (`chroma_db/` via a process-wide `PersistentClient`) survive
  restarts; the vector collection is reused when already populated.
* **Connection pooling** with ping-reconnect; autocommit on reused connections
  so every SELECT sees fresh data (no stale snapshot).
* **Schema-aware key** — a schema change produces a new key and an automatic
  rebuild, so a stale relationship graph / wrong SQL is never served.
* **Non-blocking** — the heavy build runs outside the cache lock, so a slow
  background Analyze never blocks a fast query.
* **Shared schema resolution** — resolving the context reads the schema to
  resolve its hash (that is what makes the schema-aware key safe). Concurrent
  queries against the same database share one in-flight read instead of each
  issuing their own. Under 8 concurrent queries this took schema resolution from
  ~76 ms to ~8 ms, roughly doubling throughput. Counters: `schema_fetches` /
  `schema_fetches_shared` in `GET /context-metrics`.
* **Short-TTL schema cache** — on a wide ERP schema that read is the single
  largest per-query cost (~65 ms for 465 columns), and it is paid once per chart,
  so a dashboard opening N charts re-reads the same schema N times in a burst.
  The resolved schema is cached for `SCHEMA_CACHE_TTL_SECONDS` (default 5, `0`
  disables) so a burst collapses to one read; a DDL change is still detected
  within the TTL, and Analyze / rebuild / connection edits invalidate it
  immediately. Warm `context_ms` on the 465-column schema drops from ~65 ms to
  ~0.1 ms. Counter: `schema_fetches_cached`.
* **One read serves key and build** — a cold build needs the schema twice: the
  plain `{table: [cols]}` form for the cache key and the rich form (types, PKs,
  FKs) for planning. It now reads the rich schema once and derives the plain form
  from it (byte-identical, so the hash is unchanged), instead of two round trips.
  Rich introspection stays best-effort: if it fails, resolution falls back to the
  plain `fetch_schema` and the build degrades to text-only comparisons.
* **One shared embedding model** — the vector collection is bound to a
  process-wide embedding function. Left to its own devices Chroma constructs one
  per call, and each build a fresh onnxruntime session: ~150 ms before embedding
  a single token, paid by every question not already cached. Shared, an uncached
  schema search costs ~17 ms.
* **Smart invalidation** triggers: schema hash (cache key), AI provider (UI
  re-analyzes on switch — no silent model mixing), and an `EMBEDDINGS_VERSION`
  baked into the collection name. Bumping that constant is how the embedding
  scheme is retired: existing collections are ignored rather than migrated, so
  the first Analyze afterwards re-embeds the schema once (`v2` did this when the
  shared embedding function landed).

Implemented in [dbbuddy_core/context_store.py](../dbbuddy_core/context_store.py).

---

### 2. Intent Builder

Extracts structured query intent:

* aggregation (SUM, COUNT, AVG, etc.)
* grouping (per, by, each)
* filters
* ordering
* limits

**Filter extraction is schema-driven and type-aware** — no hardcoded domain
values or table names, so it generalizes across arbitrary databases. A
**type-handler framework** ([dbbuddy_core/type_handlers.py](../dbbuddy_core/type_handlers.py))
picks a handler per column by its declared SQL type and emits typed
`{column, operator, value}` specs the compiler (4) renders:

* **Numeric** — comparisons (`age > 30`, `amount over 100`, `at least`),
  `BETWEEN`, currency symbols and thousands separators (`$1,000`).
* **Date/Datetime** — explicit ISO comparisons (`created after 2024-01-01`),
  and deterministic **calendar** (`today`, `this month`, `this quarter`) and
  **relative** (`last month`, `past 30 days`) expressions, each resolved to a
  concrete half-open `[start, end)` range in Python (no dialect date functions).
  A **bare** temporal phrase that names no column — "revenue **last month**", where
  "revenue" resolved to `payments.amount` — is attached to the date column of the
  tables the planner already resolved for the query (its *focus tables*), so the
  range lands on `payments.payment_date` instead of being dropped. If a focus
  table has more than one date column the phrase is left off rather than guessed.
  A column counts as temporal by declared type **or** by naming convention
  (`*_date` / `*_at` / `*_time`) when types are present — so ISO dates stored as
  `TEXT` (the SQLite reality) are still recognised; ISO-8601 text compares
  correctly against the emitted bounds. The name-convention branch fires **only
  for text-typed columns**: a column the database types as numeric/boolean is
  never promoted by its name, so a `REAL` column literally called `time` (a
  duration in seconds) or `date` (a serial) does not get an ISO-date range applied
  to it — comparing a float against date strings would silently return the wrong
  rows (`tests/test_type_handlers.py`).
* **Boolean** — `is_paid = true` and adjective phrasing (`paid orders`,
  `unpaid`), guarded so a word used as a value elsewhere doesn't flip the flag.
* **Text** — grammar-aware: `is` / `is not` (→ `!=`) / `in (…)` (→ `IN`) /
  `contains` (→ `LIKE`) / `is null`, before falling back to adjacency.

New column types (enum, uuid, json, array…) are added by **registering a
handler** — the planner never changes. Exclusions come only from fixed grammar
tokens, never from splitting schema identifiers, so planner behavior is
independent of unrelated schema evolution (a determinism guarantee). Named-entity
lookups ("Alice's orders") are grounded on a table's identifier column. The
extractor and its capabilities are validated against a corpus of realistic ERP
schemas in [tests/schema_portability/](../tests/schema_portability/) (SAP, Odoo,
ERPNext, healthcare, …).

Every handler pattern is anchored on the column name (`\b<column>\b …`), so a
column the question never names cannot produce a filter. Extraction therefore
**skips any column whose name (or a match form) is absent from the query text**
before running its regexes — turning an O(all-columns) scan into
O(columns-the-question-mentions). On a wide ERP schema this is the dominant
per-query cost: intent building on the 465-column AdventureWorks schema dropped
from ~210 ms to ~11 ms. Boolean columns are exempt, because they also match
adjective phrasing ("paid", "unpaid") that does not contain the column name. A
substring is a necessary condition for the word-boundary match, so the skip never
drops a filter that would have matched (a determinism-preserving optimization).

---

### 3. Query Planner (Deterministic Core)

* Builds execution plan (not SQL directly)
* Resolves:

  * joins — **graph-based routing over a relationship graph built from the
    database's declared foreign keys** (`fetch_schema_rich().foreign_keys`),
    falling back to an `<x>_id → <x>s` naming heuristic only when the schema
    declares no FKs. Real FK metadata handles cryptic keys (SAP `VBELN`), `_key`
    warehouses, and FK names that differ from the target table (Odoo
    `partner_id → res_partner`) that the heuristic could never resolve. A further
    tier relates tables by their **declared primary keys** — a child column that
    repeats a parent's single-column key (`"line-item"."key" → "order"."key"`, or
    the employees sample's `emp_no`/`dept_no`) joins even when no name convention
    or FK exists. The planner consumes the context's prebuilt graph rather than
    rebuilding a thinner one from the bare `{table:[col]}` schema.
  * aggregation context
  * grouping rules — the COUNT / GROUP BY / ORDER BY / HAVING anchor is the
    table's **declared key** (or a literal `id` when present), never an assumed
    surrogate `id`; a keyless-by-`id` schema (`departments` keyed on `dept_no`)
    would otherwise plan a column that does not exist. The hidden disambiguating
    key is added only when grouping on a `person_name` column (by semantic role,
    not a name list), so a categorical dimension (`gender`, a job `title`)
    collapses as intended instead of splitting per row.
* Enforces:

  * Aggregation ≠ Grouping
  * ORDER BY must reuse aggregation
  * SQL correctness constraints
  * **Determinism** — resolution order (table selection, ambiguous-measure
    binding) is order-preserving, never `list(set(...))`; set iteration follows
    `PYTHONHASHSEED`, which would let the same question compile differently across
    processes. See [SEMANTIC_ROLES.md](SEMANTIC_ROLES.md) for the role vocabulary
    the grouping-key decision consumes.
* **Naming-convention agnostic.** Column/table matching does not assume one
  convention. `intent_builder.split_identifier` tokenizes a name on underscores
  **and** camelCase/PascalCase boundaries and digit runs (`ListPrice` →
  `list price`, `SalesOrderID` → `sales order id`), and
  `semantic_roles.is_identifier_name` recognises a key across `id` / `<x>_id` /
  camelCase `XxxID`. Without these a SQL-Server/.NET schema (AdventureWorks) is
  opaque: `XxxID` keys read as measures (`SUM(SalesOrderID)` for "total sales")
  and multi-word `PascalCase` tables bind to a shorter prefix table. A measure is
  matched to a *numeric* column (via `column_types`), so a text code that merely
  contains a measure word (`SalesOrderNumber`) is never summed.

---

### 4. SQL Compiler (Predicate AST)

The planner's engine-agnostic execution plan is compiled to SQL **deterministically**
by the [dbbuddy_core/sql/](../dbbuddy_core/sql/) subsystem — no LLM is involved in
producing SQL.

* **Predicate AST** — WHERE clauses are first-class objects, not strings. A
  `Predicate` renders itself to `(sql_fragment, bound_params)`:

  * `Condition` (`column operator value`) dispatches to a frozen **operator
    registry**: `=` and the comparison operators, `LIKE`, `IS NULL` /
    `IS NOT NULL`, `BETWEEN`, `IN` / `NOT IN`, and the dialect-rendered `ILIKE`.
  * `ExistsPredicate` (`EXISTS` / `NOT EXISTS`) and `QuantifiedPredicate`
    (`col <cmp> ANY | ALL (subquery)`) wrap a nested, separately-compiled
    subquery plan — never a raw string.
* **Two render modes** — parameterized (`%s` + bound params, for execution) and
  inlined (literals, for display/validation only).
* **Dialect-aware** — operators that differ per engine (e.g. `ILIKE` → native on
  PostgreSQL, `LOWER(col) LIKE LOWER(%s)` on MySQL / SQL Server) delegate to the
  dialect (4b) via capability flags. No engine checks live in the compiler.
* **Fail-closed** — a condition that can't be compiled without changing intent
  (e.g. `NOT IN (…, NULL)`, a malformed `BETWEEN`) raises `InvalidConditionError`
  and aborts compilation rather than silently dropping a predicate and broadening
  the query.

Adding an operator is a one-line registry entry (plus, for engine-specific ones,
a dialect method); the compiler itself does not change.

---

### 4a. Execution Engine

* Validates:

  * table/column existence
  * join correctness
* Handles:

  * SELECT (auto-execute, or held for review when the user disables auto-execute)
  * UPDATE/DELETE (confirmation required)
* **Parameterized execution** — WHERE/HAVING values are bound via `%s`
  placeholders (`cursor.execute(sql, params)`), never interpolated into the SQL
  string, so values cannot inject SQL. A separate inlined string is produced for
  display/validation only.
* **Confirmed-write execution tokens** — a write held for confirmation cannot be
  altered by the client after the user approves it. `/query` mints a single-use,
  short-lived `execution_token` bound to the exact server-stored SQL, the issuing
  user, and a hash of the execution target; `/execute` redeems it and runs the
  *stored* SQL. Raw SQL posted to `/execute` still runs for reads, but a raw
  **write** requires the explicit `query:write:manual` permission — the reviewed
  token flow is otherwise mandatory. Every `/execute` is classified and audited
  with its `source` (`token` / `manual`). See [4c](#4c-confirmed-write-flow).
* Includes dry-run preview
* **Engine-agnostic** — all vendor-specific SQL and connection handling is
  delegated to the dialect layer (4b), so the execution engine itself contains
  no MySQL- or PostgreSQL-specific code.

When a query cannot be grounded in the schema (no matching tables/columns), the
pipeline returns a graceful "couldn't match that to your database" response with
a suggestion — it does not raise, so the API never turns an unrecognized query
into a 500.

---

### 4b. Dialect Layer (Multi-Engine Support)

Every supported SQL engine ships a `Dialect` subclass under
[dbbuddy_core/dialects/](../dbbuddy_core/dialects/). This is the **only** layer that
is engine-aware — there are no `if engine == "postgres"` checks elsewhere.

* **Engines** — MySQL (`mysql`), PostgreSQL (`postgresql`), and SQL Server
  (`sqlserver`), selected per connection via a `DatabaseEngine` enum. A lazy
  registry imports each driver only when its engine is first requested, so a
  missing optional driver does not break the others. Adding an engine is
  documented in `docs/ADDING_DATABASE_DIALECT.md`.
* **`DialectConnection`** — a uniform wrapper around the raw driver connection
  (cursor, dict-cursor, ping/reconnect, autocommit, commit, close). The rest of
  the codebase talks to this wrapper, never the driver.
* **Schema introspection** — `fetch_schema()` returns `{table: [cols]}`;
  `fetch_schema_rich()` returns typed `DatabaseSchema → TableMeta → ColumnMeta`
  with data types, primary keys, and foreign keys for smarter planning.
* **SQL fragment generation** — identifier quoting, row limiting (`LIMIT` vs
  SQL Server `SELECT TOP n`), random, current date/timestamp, date arithmetic,
  cast, concat, regex, boolean literals. The planner calls these methods (e.g.
  `dialect.current_date()`) rather than hardcoding `CURDATE()` vs `CURRENT_DATE`.
* **Capability flags** — `dialect.capabilities.supports_json`,
  `supports_window_functions`, `supports_cte`, `supports_returning`,
  `supports_regex`, `supports_ilike`, etc., so the planner/compiler ask what an
  engine *can do* instead of what it *is* (e.g. `supports_ilike` picks native
  `ILIKE` vs a `LOWER(...)` rewrite).

A shared contract test
([tests/test_dialect_contract.py](../tests/test_dialect_contract.py)) verifies every
dialect behaves identically from the application's perspective; adding an engine
means implementing the contract and passing the suite.

---

### 4c. Confirmed-Write Flow (execution tokens)

Writes must originate from a reviewed plan — a client cannot modify the SQL after
the user confirms it. This is enforced server-side, not by trusting the client.

```
/query  (planner produces a write, held for confirmation)
   │  stores the exact SQL server-side, mints a single-use execution_token
   ▼
returns { sql (display only), warning, dry_run, execution_token }
   │  user reviews and approves in the UI
   ▼
/execute  { connection_id, execution_token }
   │  redeems the token → runs the SERVER-STORED SQL (posted `sql` is ignored)
   ▼
{ write: true, rows_affected }
```

* **Token store** — `execution_tokens` table (migration `0010`), helper
  [backend/app_db/execution_tokens.py](../backend/app_db/execution_tokens.py). Only
  `sha256(token)` is stored (never the raw token, returned to the client once).
  Each row binds the SQL to the issuing user, a `context_hash` of the execution
  target (`engine|host|port|database|user`), and a `safety_category`.
* **Single-use + TTL** — consumed atomically via a guarded `UPDATE ... WHERE
  consumed_at IS NULL AND expires_at > now` (safe across worker processes);
  expires after 5 minutes. A token minted for one connection cannot be replayed
  against another (context-hash mismatch → rejected *without* consuming, so it
  stays valid for its real target).
* **Permission boundary for raw SQL** — `/execute` also accepts raw `sql`:
  a `SELECT` runs for any `query:run` caller (chart re-runs, ad-hoc reads); a raw
  **write** requires the `query:write:manual` permission (granted to the Analyst
  role by default, revocable for a locked-down deployment). The permission — not
  the client — decides whether hand-written writes are allowed.
* **Auditing** — every `/execute` is classified and written to the audit log with
  its `source` (`token` / `manual`), so writes against target databases are
  always attributable. Covered by
  [tests/test_execution_tokens.py](../tests/test_execution_tokens.py).

---

### 5. Explainability Engine (Phase 5)

Generates structured explanations:

* query type
* grouping reasoning
* aggregation decisions
* join logic
* ranking logic
* step-by-step reasoning trace

---

### 6. Learning Engine (Phase 6)

Adaptive semantic learning with guardrails:

* Learns mappings:

  * "revenue" → payments.amount
  * "customers" → users
* Uses:

  * frequency thresholds
  * noise filtering
  * temporal decay
  * capped memory size
* Never overrides deterministic logic

---

## AI Providers & Key Management

AI is used only for **semantic labeling** of the schema — never to generate SQL.
Providers are **configuration, not code**: each is a per-organization *record*, so
supporting a new provider is a settings change, not a code change.

* **Registry + adapters** ([dbbuddy_core/ai_providers.py](../dbbuddy_core/ai_providers.py)):
  an ``AIProvider`` implements only the transport (``generate`` / ``healthcheck``);
  a registry resolves it by ``adapter`` — the single place an adapter name is
  dispatched (everything else branches on declared **capabilities**, like the SQL
  dialect layer). Two adapters ship:
  * **`openai_compatible`** — any OpenAI Chat Completions endpoint (OpenAI, NVIDIA/
    Nemotron, OpenRouter, Groq, Together, Azure, Anthropic/Gemini via their compat
    endpoints). Vendor differences are just `base_url` + `model`.
  * **`ollama`** — a local Ollama server (offline, keyless).
* **Records** (`ai_provider_configs`, org-scoped, `settings:ai`): name, adapter,
  base_url, model, an optional Fernet-encrypted key, `enabled`, a `priority`
  (1 = the org's active default), and an optional `fallback_provider_id`. Managed
  from **Settings → AI Providers** (create / edit / duplicate / delete / activate /
  test). The backend resolves the org's active provider (+ fallback chain) into a
  self-contained runtime config injected into `DBConfig` — the engine never reads
  the app DB.
* **Fallback replaces "hybrid"**: the active provider and its fallback form a chain
  tried in order on a *recoverable* infra error (timeout / connection / 429 / 5xx /
  missing key). Output quality never triggers fallback.
* **Resilience (provider-independent)**: each provider is retried with exponential
  backoff + jitter on *transient* failures (timeout / connection / 429 / 5xx) before
  the chain fails over; a non-transient error (missing key / permanent 4xx) fails
  over immediately. A server `Retry-After` is honored up to a cap (a long hint →
  fail over rather than block). Tunable via `AI_RETRY_*` env vars.
* **Circuit breaker (per provider)**: after `AI_BREAKER_THRESHOLD` consecutive
  *transient* failures a provider trips **OPEN** and is skipped straight to the
  fallback — no retry budget spent — for a cooldown that backs off on repeated
  trips (capped). After the cooldown it goes **HALF-OPEN** and lets one probe
  through: success closes it, failure re-opens it. Non-transient failures never
  trip it. State lives beside the metrics and is shown in the UI.
* **Observability**: every call is timed and tallied in
  [dbbuddy_core/ai_metrics.py](../dbbuddy_core/ai_metrics.py) (calls / successes /
  failovers / retries / rate-limit hits / skips / latency), merged with breaker
  state at `GET /ai-metrics` (`settings:ai`) and shown per-provider in the UI.
* **Keys**: stored encrypted at rest (Fernet, like ERP connection passwords), never
  returned; `credentials_ok` flags a record whose key no longer decrypts. Legacy
  keyring/env keys (`NEMOTRON_API_KEY`/`OPENAI_API_KEY`) are migrated into records
  on startup and remain a deprecated compatibility path (see the 3-phase retirement).
* **Honest provenance**: each mapping records `source` (`ai` or `rule`) and the
  `provider` that produced it. If the whole chain is unavailable, labeling falls
  back to deterministic rules and the UI reports this instead of claiming AI.

---

## Contracts & Strict Mode

Internal stage boundaries (e.g. "the planner returns a dict execution plan") are
recovered leniently in production. Setting `DBBUDDY_STRICT=1` turns those silent
recoveries into loud `ContractViolation` errors — recommended for tests/CI to
catch contract regressions early.

---

## Testing — Behavioral Test Suite

Regression testing is **schema-adaptive**. Instead of hard-coded table names, the
`BehavioralTestSuite` classifies any connected schema into semantic roles
(entity, monetary, temporal, event, quantity) and validates the engine's
*intent* using the schema's own names — e.g. "monetary aggregate grouped by
entity". The same behaviors therefore pass across completely different schemas.

---

## System Properties

### Deterministic

* No hallucinated SQL
* Fully predictable execution

### Explainable

* Every query has reasoning trace

### Safe

* Execution validation layer
* Controlled mutation queries (confirmation gate for writes)
* Confirmed writes run server-stored SQL via single-use execution tokens; raw
  writes gated by the `query:write:manual` permission (see [4c](#4c-confirmed-write-flow))
* Parameterized queries (no SQL injection via values)
* Secrets stored in the OS keyring, never echoed back

### Adaptive

* Improves over time
* No corruption risk (guardrails)

---

## Performance

* Accuracy: ~87%
* Execution Success: ~92%
* Confidence: ~82%
* Learning improvement: +12%

### Latency (per-DB context caching)

| Step | Latency |
| ---- | ------- |
| Analyze Schema (one-time) | ~5–6 s |
| Cold query (first; loads from disk) | ~150–250 ms (narrow) · ~500–600 ms (wide, 465-col) |
| Warm query | ~5–15 ms (narrow) · ~25 ms (wide, 465-col) |

The deterministic stages (intent → plan → compile) total only a few ms; the old
~6–8 s/query cost was the per-request LLM labeling, now paid once at Analyze.
Latency scales with schema width — the figures above bracket a narrow schema and
a 465-column ERP schema. On the wide schema the two levers that keep the warm
path flat are the column-mention gate in intent extraction and the short-TTL
schema cache (above); both landed together and took the wide-schema warm query
from ~300 ms to ~25 ms.

---

## API — Context Management

Beyond `/query` and `/execute`:

| Endpoint | Purpose |
| -------- | ------- |
| `GET /engines` | List supported database engines (`mysql`, `postgresql`, `sqlserver`) |
| `POST /analyze` | Analyze schema: AI-label, persist, index (one-time heavy build) |
| `POST /rebuild-context` | Force a rebuild (manual invalidation) |
| `POST /analyze-status` | Readiness checklist (connected / analyzed / semantic + vector ready / cached) |
| `GET /context-metrics` | Cache hit rate, build time, cold/warm latency, schema-fetch counts (`schema_fetches` / `schema_fetches_shared` / `schema_fetches_cached`) and `schema_cache_ttl_s` |

Connection requests carry an `engine` field (`mysql` / `postgresql` / `sqlserver`).
Connection failures return a descriptive error (cause included), not a silent empty success.

---

## Query response — what travels

Two fields on a `/query` response exist to keep the payload proportionate:

* **`semantic_layer` is a slice**, not the whole layer — only the columns the
  generated SQL touches. The full layer holds one entry per column in the
  *database*; on a real ERP schema that was the bulk of the response body, and it
  was serialized, cached in Redis, and pushed over the wire on every query.
* **`ai_labeled`** is a boolean computed over the **whole** layer. It exists
  because the slice cannot answer the question it looks like it can: a query
  touching only rule-based columns of an AI-refined database would otherwise read
  as "not AI-labeled" and flip the UI badge (and the CLI's fallback note) based on
  which columns the question happened to hit.

`meta.stage_timings` covers every stage including **`context_ms`** — resolving the
prepared context, which re-reads the schema and is the largest per-query block on
a wide schema. It was the one significant stage the timings did not account for,
which made it invisible in exactly the contended case where it mattered most.

Every terminal path past compilation is recorded to the query log. That was not
always true: the log call sat after the execute branch, which returns from inside
itself, so auto-executed `SELECT`s — the most common operation the system serves —
were never logged at all.

---

## Frontend UX Principles

> Expose decisions, not internals.

* **Fast mode → AI-enhanced mode** — queries are usable instantly (rule-based
  labels); a background Analyze upgrades them to AI labels and a header badge
  reflects the current mode.
* **Transient progress indicator** animates the analyze steps, then fades away
  once ready (not a persistent panel).
* **One execution badge** per result — `AI-enhanced · Nms` or `Deterministic ·
  Nms` — using the honest backend query latency (`meta.latency_ms`), not
  wall-clock that includes setup. Per-stage timings live under a collapsed
  "Technical details" section.
* Non-blocking toast when a query runs while analysis is still in flight.

---

## Integration Notes

* MySQL / MariaDB → `mysql-connector-python`
* PostgreSQL → `psycopg2` (imported lazily; absent driver doesn't break MySQL)
* SQL Server → `pymssql` (imported lazily; optional install)
* Redis → optional caching (prompt→SQL, retrieval, plan). Optional means
  optional: the client also parks itself after `REDIS_ERROR_THRESHOLD`
  consecutive errors, so a server that dies *mid-process* costs one timeout per
  `REDIS_RECOVERY_SECONDS` cooldown rather than one per call. Decode failures and
  unserializable values are excluded from that count — a bad value says nothing
  about the server's health, and folding the two together would hide real bugs.
* ChromaDB → optional vector retrieval (persistent, single shared client **and a
  single shared embedding function**; see the context-store notes above)
* Per-DB context cached in-process and on disk (`.dbbuddy_cache/`); the cache key
  includes the engine, so the same host/database on different engines is never
  conflated
* AI labeling → optional; a local **Ollama** server or any **OpenAI-compatible**
  cloud endpoint, configured as per-org provider records (see "AI provider
  management" above). Active provider + fallback chain; keys encrypted at rest.
  Absent/unreachable → deterministic rule-based labels (honest `AI used: No`).

---

## Design Philosophy

DB Buddy follows a strict principle:

> AI assists. The system decides.

This ensures:

* reliability
* explainability
* production safety

### Ranking: down-weight, don't delete

Any component that scores and then trims results — semantic matching, relation
inference, retrieval — follows one ordering:

> **score everything → rank → truncate**

never

> ~~discard → rank what's left~~

Filtering before ranking creates a **discontinuity at the threshold**, and users
read a discontinuity as the system breaking rather than as a tuning choice. The
relation graph learned this the hard way: a hard cutoff on over-common entity
tokens meant 8 databases sharing an entity produced a full graph and a 9th
produced *none*, so connecting one more database silently blanked the view (QA
finding #12). Scoring the same tokens with a taper instead lets weak links fade
below strong ones, and the ranking sorts them out on its own.

Two corollaries:

* **A threshold kept for performance is not part of the scoring model.** Where a
  hard bound is genuinely needed to bound work (e.g. `_MIN_COMPUTE_WEIGHT`
  bounding quadratic pair generation), place it where scores are already
  negligible, name it for what it is, and record what would let it be removed —
  so it is never mistaken for a statement about the data.
* **Test the transition, not two points either side of it.** Tests placed on
  opposite sides of a cliff *encode* it instead of catching it: the pre-fix suite
  asserted N=4 works and N=12 is empty, and passed for the entire life of the
  bug. Assert the property — continuity, monotonicity — across the boundary.

---

## Status

* Production-ready (~95%)
* Research-level system
* Actively evolving with adaptive learning

---

## Platform Layer (auth, RBAC, multi-tenancy, publishing, audit, MFA, jobs)

This document covers the **NL→SQL engine**. Wrapped around it is a multi-user
platform (`backend/app_db/` + `frontend/`) backed by a dedicated PostgreSQL
**application database** — kept strictly separate from connected business databases,
which remain query targets only:

* **Auth + RBAC** — JWT/Argon2; roles admin / org_admin / analyst / user with
  granular permissions; every endpoint gated. Writes against target databases
  are governed by the confirmed-write flow ([4c](#4c-confirmed-write-flow)):
  planner writes run via single-use execution tokens, and raw write SQL needs the
  `query:write:manual` permission.
* **Organizations** — multi-tenant; platform-admin vs org-admin scoping.
* **Publishing** — draft → customize → publish → clients view read-only,
  live-re-queried reports (no SQL/credential exposure).
* **Chart customization** — a saved chart carries a `chart_type` and an opaque
  `config` JSON blob (palette + per-series / per-category color overrides,
  stamped with a schema `version`). The server owns only the **envelope**:
  it validates type, structure (depth/size), and version, and otherwise never
  interprets the config — publishing passes it through untouched. The frontend
  `ChartRenderer` is the **single** interpreter and the only renderer, shared by
  the analyst preview and the published client report, so a chart looks identical
  everywhere and swapping the charting library touches one file. Editable from
  the web app (Infographics) or the CLI (`dbbuddy charts customize`), both
  through `PATCH /charts/{id}`.
* **User lifecycle** — an admin can deactivate a user (reversible suspension, via
  `PATCH`) or **permanently delete** them (`DELETE /admin/users/{id}`). A delete
  purges every row the user owns — connections, schema snapshots, saved and
  published charts, query history, API keys, scheduled jobs and their runs,
  execution tokens, notifications — explicitly and in FK-dependency order, rather
  than relying on DB-level `ondelete`, because cascade semantics are not portable
  between SQLite (tests, FKs off by default → silent orphans) and PostgreSQL
  (prod, where `published_reports.published_by` is `SET NULL` on a NOT NULL
  column → raises). Audit rows are **kept** with their actor nulled, so the
  compliance trail outlives the account. Scope follows the rest of the router: an
  org admin may only delete non-privileged members of their own org, and nobody
  may delete themselves — so the last admin can never be removed.
* **Relation graph** — analyst-only map of how databases and tables interconnect.
  See [Relation Graph](#relation-graph-infographics--relations) below.
* **Audit** — (actor, org, entity, action) events with correlation ids; admin
  dashboard.
* **MFA** — TOTP + recovery codes + challenge flow + `amr` claim.
* **Personal API keys** — long-lived CLI/automation credentials (`/auth/keys`),
  exchanged for JWTs so they carry the owner's live permissions; managed from the
  CLI or the web app (Settings → Personal API Keys). See **[CLI.md](CLI.md)**.
* **Background jobs** — in-process APScheduler: scheduled report refreshes /
  context rebuilds, run-now, history, notifications.

Schema is versioned with **Alembic** (`backend/migrations/`, revisions 0001–0013;
applied automatically on startup). Auth adds stateless refresh-token revocation
(`users.token_version`, migration 0009) bumped on logout / MFA-disable /
deactivation. Migration 0010 adds the `execution_tokens` table backing the
confirmed-write flow, 0011 the `ai_provider_configs` table, 0012 the
`saved_charts.config` column holding chart customization, and 0013 the
`schema_snapshots` table backing the relation graph. For setup,
configuration, and a production security checklist, see
**[DEPLOYMENT.md](DEPLOYMENT.md)** and **[SECURITY.md](SECURITY.md)**.

---

## Relation Graph (Infographics → Relations)

An analyst-only view of how databases and their tables interconnect. Gated on
`schema:analyze`, which only the `analyst` role holds — enforced server-side in
`routers/relations.py`, not merely hidden in the UI.

### Snapshots, not live introspection

The graph is built entirely from **stored schema snapshots**
(`schema_snapshots`, migration 0013), never from live connections. A schema is
introspected once via `fetch_schema_rich()` and persisted as JSON
(`{"tables": [{name, columns[{name,type,pk}], foreign_keys[…]}]}`); every graph
view is then a single app-DB read plus an in-memory build. That is what lets the
overview cover a whole fleet without opening hundreds of connections. Exactly one
snapshot per connection (`connection_id` unique); refreshing overwrites it, and a
`fingerprint` (SHA-256 of the canonical JSON) records the structural shape.

Snapshots are captured on demand:

| Endpoint | Purpose |
| --- | --- |
| `POST /relations/snapshot/{connection_id}` | Introspect one live database, (over)write its snapshot |
| `POST /relations/snapshot` | Refresh every connection the caller owns (best-effort; per-connection failures reported in `failed`) |
| `GET /relations/overview` | Database-level graph across all owned connections |
| `GET /relations/detail/{connection_id}` | Table-level graph for one database (`409` if never snapshotted) |

Snapshots are owned through their parent connection: deleting a connection, using
"Clear saved databases", or hard-deleting a user removes them explicitly (SQLite
does not cascade — see the user-lifecycle note above).

### Two levels

**Detail** (`build_detail_graph`) — one node per table in a single database. Edges
come from the schema's **declared foreign keys** when it has any (authoritative and
directed, `kind: "fk"`); only when a schema declares none does it fall back to the
`<x>_id → <x>s` naming heuristic (`kind: "heuristic"`, reusing
`dbbuddy_core.relationship_graph.build_relationship_graph`). The UI draws declared
FKs solid and inferred ones dashed, so a real constraint is never presented as a
guess. FKs pointing outside the snapshot are dropped.

**Overview** (`build_overview_graph`) — one node per database. Real foreign keys
never cross a database boundary, so cross-DB links can only ever be **inferred**.
Inference works on *entity tokens*: each table name is normalized (lowercased,
schema prefix stripped, singularized) into an entity the DB **defines**, and each
FK target plus each `<x>_id` column into an entity it **refers** to. An inverted
index maps token → databases, and a shared token emits an edge scored by signal
strength:

| Signal | Confidence |
| --- | --- |
| One DB defines the entity table, the other references it | `0.9` |
| Both define it (a shared dimension) | `0.7` |
| Both merely reference it (no owner seen) | `0.5` |

Two mechanisms keep the result meaningful, and both **degrade rather than
delete**. A **stopword list** drops attribute-ish tokens (`status`, `type`,
`code`, …) that would otherwise wire every database to every other through a
shared `status_id`. Then an **IDF-style taper** (`_token_weight`) scales each
token's contribution by how generic it is: full weight up to a fan-out of 8
databases, decaying as `8 / fan-out` above that. A pair's confidence is the best
score any single shared token earns it, so a link backed by one specific entity
keeps its strength even when the same pair also shares generic ones. Output is
sorted highest-confidence first and capped at 1000 edges.

Because that cap keeps the *top* of a confidence sort, truncation removes whole
weaker tiers rather than thinning evenly — so it is reported rather than hidden.
`stats` carries both `links` (what this payload contains) and `total_links` (what
exists), plus a `truncated` flag; the UI renders "1,000 of 2,347 inferred links"
and a notice that only the strongest are drawn. A count that silently doubles as
a floor is exactly the kind of dishonesty the graph is supposed to avoid.

The taper is deliberate: an earlier hard cutoff meant a token shared by 8
databases scored full confidence while the same token shared by 9 produced **no
edges at all**, so connecting one more database silently emptied the graph. Now
common entities fade below specific ones and the ranking sorts them out on its
own (QA finding #12).

> One bound remains, for **compute rather than presentation**: pair emission is
> quadratic in fan-out, so tokens whose weight falls below `_MIN_COMPUTE_WEIGHT`
> (fan-out ≳ 160, confidence ≲ 0.04) are skipped. It does not say those databases
> are unrelated — it says the token is too uninformative to spend CPU enumerating
> every pair among its holders. Placed where edges are already near-invisible, and
> revisit whether it is needed at all once **#13** replaces pair generation with
> something that doesn't explode on high-fan-out tokens; the taper may then
> suffice alone. See [QA_CHECKLIST.md](QA_CHECKLIST.md) and
> [down-weight, don't delete](#ranking-down-weight-dont-delete).

### Rendering

`frontend/src/components/RelationGraph.tsx` renders in **real 3D** (WebGL, via
`react-force-graph-3d` → three.js): a 3D force-directed layout the analyst can
orbit, zoom and pan, with node labels shown on hover. Depth is what makes it
readable at scale — nodes spread through three dimensions instead of piling into
one crowded plane, so a large schema no longer collapses into a flat hairball of
overlapping labels. All graph *data*, including every inference, still comes from
the backend; the client only lays out and draws.

The layout runs inside the library (`d3-force-3d`), stepped incrementally per
animation frame rather than as one synchronous pass, which retires the old
hand-rolled Fruchterman-Reingold layout and the main-thread O(n²) ceiling that
bounded graph size (former QA finding **R4** — superseded). Three practical
details the component owns:

* **Theme colours** — three.js cannot parse a CSS custom property, and the token
  palette is authored in `oklch()`, which its colour parser also rejects. Each
  token is resolved to a plain sRGB `rgb()` string (rasterised through a 1×1
  canvas) and re-resolved when the theme flips, so the scene follows light/dark.
* **SSR-safe mount** — the WebGL canvas is gated behind a client-side size
  measurement (a callback ref), so it never renders during server-side rendering
  and never initialises at 0×0.
* **Clean teardown** — the library disposes its renderer/scene on unmount, so
  repeatedly opening Relations does not leak WebGL contexts.

Adds two frontend dependencies (`react-force-graph-3d`, `three`); backend and
Python requirements are unchanged.

---

## Dashboards (Infographics → Dashboards)

A dashboard is a **collection of saved charts with narrative** — a report in
everything but name. The term is the client's, and it is used end to end rather
than translated at the UI boundary, because a name that only holds in one layer
stops holding the first time someone reads the other layer.

Infographics has three sub-tabs: **Charts**, **Dashboards**, **Relations**.

### Two things an analyst can do with a customized chart

* **Publish** — send that chart to clients as-is (existing flow, `published_reports`).
* **Pin to dashboard** — place it inside a dashboard, where it gains a position
  and an optional description.

They are independent, not a choice: a chart can be published *and* pinned,
because they answer different questions ("show clients this chart" vs "this chart
is part of this story"). Pinning prompts for an existing dashboard or creates one
inline.

### Data model

| Table | Holds |
| --- | --- |
| `dashboards` | title, optional description, draft/published status, owner |
| `dashboard_items` | one pinned chart: `chart_id`, `description`, `description_source`, `position` |
| `published_dashboards` | a publication *record* — same shape as `published_reports` |

The description belongs to the **pin, not the chart**: the same chart pinned to
two dashboards carries a different narrative in each. `(dashboard_id, chart_id)`
is unique, so re-pinning updates rather than stacking a duplicate — pinning twice
is a correction, not a request for two copies.

A dashboard stores **no data**. It references charts, and charts re-run live, so
editing a pinned chart updates every dashboard containing it with no sync step.

### Publishing — the same versioning model as charts

`PublishedDashboard` mirrors `PublishedReport` exactly: the dashboard stays the
editable draft, the publication is a record rather than a copy, clients always
render from the live draft, re-publishing reactivates the same record instead of
duplicating it, and unpublish sets `status = "revoked"` rather than deleting (the
history survives). Client visibility is resolved *through the publication record*,
not `Dashboard.status`, so revoking hides it immediately even if the draft still
says "published".

### Refresh — parallel first, cache second

Opening a dashboard re-runs every chart (`app_db/chart_runtime.py`, shared with
published reports so read-only enforcement and the "needs attention" contract
exist once rather than in two places that drift).

The dominant cost of a dashboard is **N round-trips, not N queries**, so charts
run concurrently on a bounded pool (`DASHBOARD_MAX_PARALLEL_QUERIES`, default 6 —
bounded so one wide dashboard cannot flood a target database). This is the large
win and it costs no freshness.

A **short-TTL Redis cache** (`DASHBOARD_CACHE_TTL_SECONDS`, default 45) sits
behind that, keyed on the execution target plus the exact SQL — not on chart id,
so two charts with identical SQL share a result and editing SQL misses
immediately. The username is part of the key because row-level permissions can
make the same query return different rows for different database users.

The cache is deliberately short-lived and **its age is reported**. A hit is by
definition not "now", and a dashboard that silently showed minute-old data while
claiming to be live would be the same dishonesty the relation graph's truncation
disclosure exists to avoid. Every chart carries `fetched_at` and `cached`; the UI
renders "Data as of 12s ago" and a Refresh that bypasses the cache. Redis is
optional — with no server every chart simply runs live, which is slower and
strictly *more* correct.

**Everything is bounded, and every bound is disclosed.** A chart is a *visual*,
but nothing stops a saved `SELECT * FROM huge` from returning 200 000 rows —
measured at **47 MB for one chart**, which a dashboard would then multiply by its
chart count. Three caps, each at the point where the growth happens:

| Cap | Default | Why there |
| --- | --- | --- |
| `CHART_MAX_ROWS` | 5000 | Applied before serialization, caching, or the response, so a runaway query costs a bounded amount of memory. `row_count` + `truncated` carry the true total — a chart plotted from a slice must never look like the whole dataset. |
| `CHART_MAX_CACHEABLE_ROWS` | 2000 | Larger results are returned but not cached. Redis is a latency optimization, not a blob store; a few huge entries would evict everything useful. |
| `DASHBOARD_MAX_CHARTS` | 40 | A dashboard's chart count *is* its cost against the target database on every open. Capped where a dashboard grows, not discovered later as a slow request. |

**Failures are per-chart.** A chart whose connection is gone, whose credentials no
longer decrypt, or whose query no longer matches the schema comes back
`needs_attention` and renders in place, while its siblings display normally. One
dead connection must not blank an eleven-chart dashboard.

**Thread safety:** `chart_runtime` never touches the ORM. The router resolves
charts and decrypts credentials on the request thread and hands over plain
`ChartJob` values, because a SQLAlchemy `Session` is not safe to share across
threads and a lazy-load inside a worker would be a latent, load-dependent bug.

### AI descriptions

An analyst can write a description or ask the AI for one. "Ask AI" runs the chart
and hands the **result** to the Insights Engine, returning its summary as a
*suggestion* — `POST /dashboards/{id}/items/{item}/describe` deliberately **does
not save**. An AI-written sentence appearing in a published dashboard without
anyone reading it is exactly the failure the engine exists to prevent. Accepting
it stores `description_source = "ai"` for provenance; editing it flips back to
`"manual"`.

The descriptions inherit every Insights guardrail, so a description is either
grounded in the chart's data or says it cannot determine one — a dashboard cannot
narrate a cause the data does not contain.

---

## Protecting the Target Database

DB Buddy queries databases it does not own. Two mechanisms exist specifically to
keep it a well-behaved participant — both protect the **target database**, not DB Buddy.

### Statement timeouts

`connect_db` applies `Dialect.apply_statement_timeout()` to every connection
(`ERP_STATEMENT_TIMEOUT`, default 60 s) — PostgreSQL `statement_timeout`, MySQL
`MAX_EXECUTION_TIME`. Without it a lock wait or a scan of a 400-million-row table
holds a worker *and* a pool slot until the database decides to answer. The AI path
had timeouts, retries, and a circuit breaker; the data path — the one users
actually wait on — had no ceiling at all.

Applied as a **session setting after connect**, never as a connect argument, so an
older server loses the ceiling rather than losing the connection. The method
returns a boolean so callers can distinguish a real bound from a best-effort one;
SQL Server has no session-level equivalent and honestly returns `False`.

### Backpressure

`dbbuddy_core/erp_concurrency.py` caps concurrent queries **per target database**
(`ERP_MAX_CONCURRENT_QUERIES`, default 10). Eighty analysts opening dashboards at
9 a.m. is a foreseeable Monday, and with everything else working correctly it
still lands as hundreds of simultaneous queries on a production ERP that has a
business to run. An analytics tool taking down a target database because the
tool got popular is the worst failure mode available to this product.

* Keyed on engine+host+port+database, deliberately **not** on the user — two
  analysts with separate credentials still contend for the same machine.
* Targets are independent: a saturated ERP A never starves ERP B.
* Waiting is bounded (`ERP_QUEUE_TIMEOUT`); past it a caller fails with `ERPBusy`
  rather than holding a worker behind a queue that is not moving.
* **Busy is not broken.** A saturated database reads as "handling too many
  requests right now", not as a broken chart — the operator response to those two
  is completely different.
* `DASHBOARD_MAX_PARALLEL_QUERIES` is **derived** from this ceiling rather than
  configured independently, so a dashboard can never fan out wider than its target
  allows and queue against itself.

**Where the slot is taken.** Every path that reaches a target database now holds
one:

| Path | Where |
|---|---|
| `/query`, `/analyze`, `/rebuild-context` (pooled pipeline) | `DBContext.connection()` |
| `/execute` (unpooled, single statement) | `backend/main.py` |
| Dashboard / chart refresh | `chart_runtime.execute_chart` |

Only the last of these used to. The others bypassed the ceiling entirely, which
made the mechanism largely decorative: the dashboard path was protected while the
*primary* query path — the one users actually wait on — was not.

The pooled path needs it most, and for a non-obvious reason.
`_ConnectionPool(maxsize=POOL_SIZE)` bounds only how many connections are kept
**idle**; `acquire()` opens a *new* connection whenever the idle queue is empty
and closes the surplus on release. So the pool never limited concurrency — N
simultaneous requests opened N sessions against the ERP, unbounded, which is
precisely the failure this section exists to prevent. The semaphore is the actual
ceiling; taking it *before* the pool also means live connections stay bounded and
the idle pool becomes meaningful instead of a churn buffer.

Callers see `503` + `Retry-After` for a saturated target, distinct from `502`
(unreachable/broken) and `500` (our bug) — three different operator responses.

**Per process**, like the rest of the runtime's shared state: with N workers the
effective ceiling is N×; set `ERP_MAX_CONCURRENT_QUERIES` to
`desired_total / worker_count`. A true global limit needs Redis — see
[PRE_DEPLOYMENT_REVIEW.md](PRE_DEPLOYMENT_REVIEW.md).

### Request rate limiting

`dbbuddy_core/rate_limiter.py` caps queries per caller (default 10/s) in Redis.
The limiter is keyed on the `user_id` the caller threads in; the API layer passes
the authenticated subject (`payload["sub"]`) from `/query`.

Without that the key fell back to the literal string `"default"` for every
authenticated request, so the ceiling was **global rather than per-user**: one
client's burst throttled the whole deployment, and no individual caller could be
limited. Recording a request now costs one Redis round-trip instead of three
(`LPUSH`/`LTRIM`/`EXPIRE` are pipelined).

It **fails open** when Redis is absent, which is the common single-node
deployment — so treat it as a fairness mechanism, not an abuse control.

**Authentication throttling is deliberately the opposite.** `app_db/login_guard.py`
keeps its sliding window in Redis (a sorted set per `(ip, identity)`), shared
across workers, and **degrades closed**: if Redis is unreachable the in-process
window still decides, with its cap divided by `LOGIN_GUARD_WORKERS`, so the
limiter becomes stricter rather than vanishing.

The asymmetry is intentional and worth stating, because "use the same limiter
everywhere" is the tempting simplification:

| | Query rate limiter | Login throttle |
| --- | --- | --- |
| Redis absent | Fails **open** — queries run, the ERP runs warmer | Degrades **closed** — stricter local window |
| Cost of being wrong | Some extra load on a target database | Unlimited password attempts |

A throughput limiter protecting a resource can afford to yield. A control
protecting authentication cannot disappear the moment its dependency does — that
is when it is most needed. See
[PRE_DEPLOYMENT_REVIEW.md](PRE_DEPLOYMENT_REVIEW.md#authentication-throttling-shared-degrading-closed).

---

## Semantic memory is per database

`learning_engine` records term → column mappings learned from successful queries,
and `semantic_enhancer` injects them into later questions. Both are **partitioned
by `host|database|engine`** (`memory_scope(config)`, matching
`context_store._db_key`).

The partition is the point. A learned mapping is not globally true — database
semantics are local. One flat store produced three failures at once, only the
first of which schema validation can catch:

* the same identifier meaning different things in two schemas (`products.price`
  exists in both, and means something different in each);
* **frequency counts pooling**, so a term learned 50× on one database crossed the
  learning threshold instantly on every other — arriving "already trusted";
* per-term pruning evicting a correct mapping because an unrelated, busier
  database had a stronger one.

Scoped on the *logical* database, deliberately **not** on the schema hash:
semantic understanding should survive an `ALTER TABLE`. Adding a column tomorrow
must not erase what "customer" means.

`save_memory` is a read-modify-write of one scope, so concurrent learning against
A cannot erase B. The v1 flat file is **discarded** on upgrade rather than
migrated: it records no owner, so assigning one would be speculation of exactly
the kind being fixed.

A second check remains inside the enhancer: an injected `table.column` or table
token absent from the active schema is dropped. Scoping is the boundary;
validation covers memory written before scoping existed.

---

## Identifier quoting

`compile_sql` quotes identifiers through the active `Dialect.quote_identifier()`,
**only when needed** — the identifier is a SQL keyword, contains anything outside
`[A-Za-z0-9_]` (space, hyphen, accent, CJK), or starts with a digit.

Until this existed the compiler emitted bare identifiers, so any customer schema
containing a table called `order`, a column called `"total amount"`, or a
non-ASCII name was unqueryable — `SELECT order.group FROM order` is a syntax
error, not a wrong answer. Every dialect already exposed the quoting primitive;
only the compiler never called it.

Quoting selectively rather than universally is a deliberate trade. Universal
quoting is also correct, but it rewrites every statement the product emits —
including the SQL users read, copy into their own tools, and paste into
documentation. Targeted quoting leaves ordinary output byte-identical.

Qualified references are split before quoting: `competitor_prices.price` becomes
`"competitor_prices"."price"`, never `"competitor_prices.price"` — a single
column whose name contains a dot, which no database has.

**Every clause quotes, not just the visible ones.** Quoting must cover *all* the
places an identifier reaches the SQL, not only `SELECT`/`FROM`. Two clauses were
later found still emitting bare qualifiers and are now fixed
(`tests/test_reserved_word_columns.py`):

* **`WHERE` / `HAVING` conditions** — the operator framework
  (`sql/operators.py`) interpolates a condition's `column` verbatim, so a filter
  on a reserved-word table compiled to `... FROM "order" WHERE order.status = %s`
  — quoted in `FROM`, bare in `WHERE`, a syntax error. `compile_sql` now quotes
  each predicate's `column`/`column_ref` (`_quote_condition_columns`) before
  rendering, covering `=`, the comparisons, `LIKE`, `ILIKE`, `BETWEEN`,
  `IN`/`NOT IN`, `IS NULL`, and column-to-column refs.
* **`ORDER BY`** — only the *aggregate* ordering path routed through the quoting
  helper; the simple-column paths emitted the raw column (`ORDER BY group DESC`)
  and dropped the table qualifier (ambiguous on a join). All ordering paths now
  quote and keep the qualifier.

The lesson is that "the compiler quotes identifiers" is a claim about a *set of
clauses*, and the set was incomplete until each rendering site was checked
individually — `SELECT`, `FROM`, `JOIN`, `WHERE`, `GROUP BY`, `HAVING`,
`ORDER BY`. See [STRESS_ASSESSMENT.md](STRESS_ASSESSMENT.md) (2026-07-28).

---

## Insights Engine (evidence-bound analysis)

Conversational explanations of an **executed** query result. Analyst-only, gated
on `schema:analyze` in `routers/insights.py` — the same server-side boundary the
relation graph uses.

Deliberately **not** a chatbot. A chatbot answers anything; an insights engine
answers only from evidence. That distinction drives the prompts, the validators,
and the fact that the engine never receives a database — only the result set it
is explaining. It never generates SQL: the deterministic planner → Predicate AST
→ dialect compiler path remains the only producer of SQL, and no field of an
insights request reaches it.

### One AI runtime, not two

`dbbuddy_core/insights/` calls the same provider chain that column labeling does,
through `ai_providers.generate_with_chain()` — extracted from `classify_columns`
so both share one resilience path (per-provider retries, circuit breaker,
metrics, failover). Adding an AI feature should never mean adding a transport
stack.

```
column labeling ─┐
                 ├──► generate_with_chain()  ──► AIProvider registry
Insights Engine ─┘      retry · breaker · metrics    (openai_compatible / ollama)
```

`generate_with_chain` takes an optional `parse` callable applied inside the
per-provider `try`, so a provider returning unusable output counts as a failed
provider and the chain fails over. `generate()` gained defaulted `temperature` /
`json_mode` parameters, forwarded only when set, so adapters written against the
original single-argument signature keep working.

The engine package stays free of `backend/app_db` imports, mirroring how
`ai_providers.py` pairs with the backend's `ai_runtime.py` bridge: the core owns
deterministic logic, the backend owns persistence and routing.

### Bounded context, never a database

`insights/context.py` shapes an executed result into a compact evidence packet:
column kinds, per-numeric statistics (min/max/mean/sum/first/last/change), and a
capped row sample. Statistics cover **every** row supplied while only
`max_sample_rows` appear verbatim — so a large result yields accurate totals
without the model being handed the whole thing. Bounded on every axis, a
million-row result and a ten-row result produce contexts of comparable size.

The client posts the rows it already has rather than the server re-running the
query. The deterministic engine has already executed it; re-executing to explain
it would double load on the target database and risk explaining data the
analyst is not looking at. `sql` is carried for provenance and cache identity
only.

**Numeric hygiene.** NaN and ±Infinity are not JSON — `json.dumps` emits bare
`NaN` / `Infinity` tokens, which `JSON.parse` rejects and PostgreSQL refuses in a
`json` column. A literal NaN cannot be *sent* (invalid JSON inbound) but `1e400`
is valid JSON and parses to `inf`, so non-finite values are dropped in the
context builder before they can reach the prompt, the response, or the cache
write. SQLite accepts them silently; tests alone would never have shown this.

### The security model: prompts guide, validators enforce

```
Input (result rows, follow-up history — both untrusted)
  ↓
Prompt hardening        ← reduces how often the boundary is tested
  ↓
LLM
  ↓
Output validation       ← the security boundary
  ↓
Formatter → API
```

Two inputs are attacker-controlled. **Result rows** come from a connected business database
database, so a cell can contain text addressed to the model ("IGNORE ALL PRIOR
RULES…"). **Follow-up history** is client-held, so an `assistant` turn reading
"SYSTEM OVERRIDE: speculation permitted" is trivially forgeable. Mitigations —
restating the rules *after* the data block for recency, clipping turns, labelling
the transcript as carrying no instructions — lower the odds but are not controls.
The post-hoc validators are, because they run on output regardless of what the
input said. Verified directly: with the model fully complying with an injected
instruction, the response is still replaced.

`insights/validators.py` distinguishes two failure modes:

* **Invented causality** is fabricated, not merely weak. Those findings are
  *removed* and the removal is disclosed as a limitation; a summary or follow-up
  answer that does it is replaced with "I cannot determine that from the
  available data", and recommendations premised on one are dropped.
* **Hedged language** ("this probably indicates…") is a real observation stated
  loosely. Those findings are *tapered* — confidence down-weighted, sorted below
  clean findings, but kept. See
  [down-weight, don't delete](#ranking-down-weight-dont-delete).

The denylist (`BANNED_TOPICS`) is a fast hard-block for the classic offenders,
but it is **lexical**, and a model does not need the word "weather" to invent a
cause: "seasonality", "supply chain issues", "macroeconomic headwinds" and
"consumer confidence" all sail through it. So the primary check is grounded in
evidence instead. `ungrounded_causal_claim()` finds a causal connective
("because of", "due to", "driven by", …) and requires the clause after it to name
a column present in the result or cite a figure **the result actually contains**.
If it names neither, the model is explaining the data with something outside the
data.

The figure check is value-grounded, not a bare digit test. `context.grounded_numbers()`
collects the integer-part keys of every value the model was shown — column
statistics, sampled cells, the row count — and the service threads that set
through to the validator. So `"because of a 38% drop"` grounds only if 38 is a
real result value, while `"because of 47 supply-chain disruptions"` (a fabricated
figure attached to an external cause) no longer launders past the check. When the
result values are unavailable to a caller the check stays permissive and leans on
the column-name net, so a legitimate finding that cites a formatted number
("$108B") is still kept via the column it names.

That reframes the problem from *language recognition* to *evidence verification*
— the same move the SQL path makes: verify structure, don't trust text. Unknown
columns disable the check rather than guessing: a false positive silently deletes
a **correct** finding, and between letting one unsupported statement through and
deleting a right one, the first is far cheaper for trust.

### Caching

`InsightCache` (migration 0014), keyed by SHA-256 of
`sql · result_hash · connection_id · prompt_version · provider_label · user_id`.
`result_hash` covers the *shaped* context, so unchanged data hits and changed data
misses; a `PROMPT_VERSION` bump invalidates every entry by construction rather
than needing a purge. Entries older than `INSIGHTS_CACHE_TTL_HOURS` regenerate in
place (the overwrite restarts the clock). A bundle no provider produced is never
cached — that would pin a transient outage in place.

`user_id` is in the key because the **lookup** is user-scoped. Whenever a cache
key and its lookup range over different identities, one of two things follows:
leakage, or an integrity failure. Here the lookup was the narrower of the two, so
there was no leakage — but two analysts in one org running the same query with no
saved connection derived the same key, each missed the other's row, and the second
insert violated the unique constraint (**QA #19**). Key scope and read scope must
match.

### Configuration

`INSIGHTS_ENABLED` · `INSIGHTS_TEMPERATURE` (0.2 — analysis, not prose) ·
`INSIGHTS_MAX_SAMPLE_ROWS` · `INSIGHTS_MAX_COLUMNS` · `INSIGHTS_MAX_CELL_CHARS` ·
`INSIGHTS_MAX_HISTORY_TURNS` · `INSIGHTS_MAX_TURN_CHARS` ·
`INSIGHTS_CACHE_TTL_HOURS` (≤0 disables expiry). Provider selection is
deliberately *not* an insights setting — insights use the org's existing AI
provider chain.

### Endpoints & rendering

| Endpoint | Purpose |
| --- | --- |
| `POST /insights/generate` | Analyze an executed result (cache-backed; `regenerate` overwrites) |
| `POST /insights/ask` | Answer a follow-up against the same evidence packet (not cached) |
| `GET /insights/settings` | What the panel needs to render; reports provider identity, never secrets |

`frontend/src/components/InsightsPanel.tsx` renders summary, findings with inline
evidence, recommendations, limitations, a conversation box, suggested questions
(derived deterministically from the context shape, so they cannot name a column
that does not exist), Copy, and Regenerate. Generation is explicit — it costs a
provider call, so scrolling through results never triggers one.

Cost note: 5000 rows × 40 columns builds in ~142 ms and hashes in ~1 ms, but
produces a ~18k-token prompt. A model with a smaller context returns a 4xx, which
is a `RecoverableProviderError` → failover → honest bundle, so correctness is
unaffected; a prompt-budget pass is future work.

---

## Future Work

* **structured evidence objects** in findings — replacing prose the validator
  inspects (`does this mention a known column?`) with
  `{"type": "statistic", "field": "revenue_change", "value": -18.2}` it can
  *verify*: the field exists, the statistic was computed, the value matches, and
  every recommendation cites at least one evidence object. Findings become
  verifiable objects rather than prose that happens to mention data, which is the
  same structured-IR + deterministic-validation shape the SQL path already has.
  It also unlocks "show supporting data" / clickable evidence in the UI without
  another API change.
* prompt-budget pass for the Insights Engine (token cost + small-context models)
* plan visualization (graph view)
* proactive ambiguity detection
* additional SQL engines (SQLite, Oracle) on the existing dialect layer
* real-time learning feedback loops
* notification email delivery (SMTP) + an external job queue if scale requires it
