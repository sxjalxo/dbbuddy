# Developer Guide

Contributor guide for DB Buddy setup, codebase orientation, tests, and engineering conventions.

## Project layout

- `dbbuddy_core/` — the deterministic NL→SQL engine (planner, compiler,
  execution, **dialect layer** under `dbbuddy_core/dialects/`).
- `backend/` — FastAPI app + application database (`app_db/`, Alembic migrations).
- `frontend/` — TanStack Start / React UI.
- `tests/` — pytest suite (unit + gated integration under `tests/integration/`).
- `docs/` — this documentation.
- `scripts/` — ad-hoc developer utilities (see [../scripts/README.md](../scripts/README.md)).

## Codebase map (where things live)

A quick orientation for the next developer -- the questions you'll actually ask:

| Question | Where |
| -------- | ----- |
| **Where is authentication implemented?** | [`backend/app_db/routers/auth.py`](../backend/app_db/routers/auth.py) (login / refresh / logout / MFA) and [`backend/app_db/security.py`](../backend/app_db/security.py) (Argon2, JWT, Fernet, TOTP). See [SECURITY.md](SECURITY.md). |
| **Where are permissions enforced?** | [`backend/app_db/deps.py`](../backend/app_db/deps.py) — `require_permission()` / `require_token_permission()`; a user's grants come from `User.permission_names()` in [`backend/app_db/models.py`](../backend/app_db/models.py). |
| **How do I add a new role or permission?** | [`backend/app_db/seed.py`](../backend/app_db/seed.py) — add to the `PERMISSIONS` catalogue and the role→permission mapping; `seed_roles_and_permissions()` syncs it on startup. Then gate endpoints with `require_permission("<perm>")` and the token will carry it. |
| **How do I add a new database dialect?** | [ADDING_DATABASE_DIALECT.md](ADDING_DATABASE_DIALECT.md). |
| **How do I run migrations?** | Alembic in [`backend/migrations/`](../backend/migrations/). The app runs `alembic upgrade head` on startup; manually: `APP_DATABASE_URL=... python -m alembic upgrade head`. See [DEPLOYMENT.md](DEPLOYMENT.md). |
| **How do I seed users?** | [`backend/seed_test_accounts.py`](../backend/seed_test_accounts.py) — creates admin/org_admin/analyst/client (see [Setup](#setup)). |
| **How do I deploy?** | [DEPLOYMENT.md](DEPLOYMENT.md). |
| **How do I check a change didn't slow things down?** | [`scripts/benchmarks/`](../scripts/benchmarks/) — one benchmark per subsystem plus an end-to-end pipeline run, on a synthetic SQLite schema (no live DB). `run_all.py --check` gates against the committed `baseline.json`; `--update` re-records it. Every metric declares a kind (`gate` / `trend` / `diagnostic` / `experimental`) — see [Performance benchmarks](#performance-benchmarks). |
| **How do I check a change didn't return the wrong answer?** | [`scripts/dogfood/`](../scripts/dogfood/) — populated-database correctness suites for `erp`, `hospital`, `legacy`, the real MySQL `employees` sample (depth, ~4M rows), Microsoft's `adventureworks` OLTP (width — 68 tables, 16 schemas), and generated `tpch` / `tpcds` (warehouse shape — prefixed/concatenated column names and keys named `key`; star schema with role-playing dimensions). They run the full pipeline and check invariants/golden values rather than schema shape alone; see [Correctness loop](#correctness-loop-dogfooding). |
| **How do I troubleshoot?** | [DEPLOYMENT.md](DEPLOYMENT.md) troubleshooting notes; runtime logs in `logs/`; run `DBBUDDY_STRICT=1 pytest` to surface silently-recovered errors. |
| **Where are scheduled jobs configured?** | [`backend/app_db/jobs.py`](../backend/app_db/jobs.py) — `JobScheduler` (APScheduler, in-process), `execute_job`, executors `_run_report_refresh` / `_run_context_rebuild`, `SCHEDULE_KINDS`. Started in [`backend/main.py`](../backend/main.py) (disable with `DBBUDDY_DISABLE_SCHEDULER=1`); CRUD API in `routers/jobs.py`. |
| **Where are notifications generated?** | Created in [`backend/app_db/jobs.py`](../backend/app_db/jobs.py) (job outcomes → `Notification(...)`); model in [`backend/app_db/models.py`](../backend/app_db/models.py); read/mark API in `routers/notifications.py`. |
| **Where does the relation graph live?** | Pure builders in [`backend/app_db/relations_service.py`](../backend/app_db/relations_service.py) (`build_overview_graph` / `build_detail_graph` / `snapshot_from_rich` — plain dicts in, plain dicts out, no DB), HTTP + snapshot capture in [`routers/relations.py`](../backend/app_db/routers/relations.py), storage in the `SchemaSnapshot` model (migration `0013`), rendering in [`frontend/src/components/RelationGraph.tsx`](../frontend/src/components/RelationGraph.tsx). Tune inference by editing the module constants (`_STOPWORD_ENTITIES`, `_MAX_TOKEN_DB_FRACTION`, `_SMALL_FLEET`, `_MAX_EDGES`, `_CONF_*`); tests are `tests/test_relations_service.py` (unit) and `tests/test_relations_api.py` (API + RBAC). See [ARCHITECTURE.md](ARCHITECTURE.md#relation-graph-infographics--relations). |
| **Where does the Insights Engine live?** | Pure logic in [`dbbuddy_core/insights/`](../dbbuddy_core/insights/) — `context.py` (evidence packet + `result_hash` + grounded result numbers), `prompts.py` (`PROMPT_VERSION`, guardrail text), `validators.py` (the enforcement boundary), `formatter.py`, `service.py`. **No `backend/app_db` imports** — HTTP, RBAC, and the `InsightCache` (migration `0014`) live in [`routers/insights.py`](../backend/app_db/routers/insights.py); UI in [`InsightsPanel.tsx`](../frontend/src/components/InsightsPanel.tsx). It calls the same provider chain as labeling via `ai_providers.generate_with_chain()` — **do not add a second transport stack.** Changing anything in `prompts.py` means bumping `PROMPT_VERSION`, which invalidates every cache entry by construction. Tests: `tests/test_insights_engine.py`, `tests/test_insights_dogfood.py`, and `tests/test_insights_api.py`. See [ARCHITECTURE.md](ARCHITECTURE.md#insights-engine-evidence-bound-analysis). |
| **How do I add another AI-powered feature?** | Consume `ai_providers.generate_with_chain(prompt, chain, parse=…)` — it already owns retries, the circuit breaker, metrics, and failover. Resolve the org's chain with `ai_runtime.resolve_active_provider_chain(org_id)`. If the feature interprets model output as anything a user acts on, validate that output *after* generation: prompts are guidance, validators are enforcement (see [SECURITY.md](SECURITY.md#ai-output-validation-prompt-injection)). |
| **Where do dashboards live?** | Routing + authoring in [`routers/dashboards.py`](../backend/app_db/routers/dashboards.py); live chart execution in [`app_db/chart_runtime.py`](../backend/app_db/chart_runtime.py) (**shared with published reports** — don't add a second execution path); models `Dashboard` / `DashboardItem` / `PublishedDashboard` (migration `0015`); UI in [`DashboardsPanel.tsx`](../frontend/src/components/DashboardsPanel.tsx) and [`PinToDashboard.tsx`](../frontend/src/components/PinToDashboard.tsx). `chart_runtime` runs queries on a thread pool and **must never touch the ORM** — resolve charts and decrypt credentials on the request thread and pass plain `ChartJob` values. Tests: `tests/test_dashboards.py`. See [ARCHITECTURE.md](ARCHITECTURE.md#dashboards-infographics--dashboards). |
| **How do I protect a connected ERP/database from DB Buddy?** | Two ceilings, both in the engine. Statement timeouts: `Dialect.apply_statement_timeout()` per dialect, applied in [`dbbuddy_core/db.py`](../dbbuddy_core/db.py)`.connect_db` (`ERP_STATEMENT_TIMEOUT`). Backpressure: [`dbbuddy_core/erp_concurrency.py`](../dbbuddy_core/erp_concurrency.py) — a semaphore per target database (`ERP_MAX_CONCURRENT_QUERIES`), held in `DBContext.connection()` (pooled pipeline), `backend/main.py` (`/execute`), and `chart_runtime` (dashboards). **Any new code path that queries a target database MUST hold a `query_slot()`** — for a while only the dashboard path did, which made the ceiling decorative. Note the pool does *not* bound concurrency: `_ConnectionPool(maxsize=…)` caps only *idle* connections and `acquire()` opens a new one whenever the queue is empty, so the semaphore is the only real limit on live target sessions. Tests: `tests/test_erp_backpressure.py`, `tests/test_hardening.py`. |
| **How is a session revoked?** | One integer: `User.token_version`, embedded in refresh tokens *and* in the access token's `tv` claim. Bump it (logout / MFA disable / deactivation) and **also call `deps.invalidate_revocation(user_id)`**, or the change waits out the cache TTL. The claims-only hot path (`get_token_payload`) checks it against a short-lived, LRU-bounded, single-flight in-process cache — process-local, so multi-worker convergence is bounded by `AUTH_REVOCATION_CACHE_TTL`. See [SECURITY.md](SECURITY.md#session-revocation-stateless-via-token_version). |
| **Where do I validate an operator-supplied outbound URL?** | [`backend/app_db/url_guard.py`](../backend/app_db/url_guard.py) — `validate_outbound_url()`. Anything the *server* fetches at a caller's direction is an SSRF primitive; today that is an AI provider's `base_url`, validated on create and patch. Link-local (cloud instance metadata) and non-`http(s)` schemes are always refused; private/loopback behind `AI_PROVIDER_BLOCK_PRIVATE_NETWORKS`. Add new outbound-URL fields to this guard rather than re-checking inline. |
| **Which limiter fails open, and which doesn't?** | Deliberately different. [`dbbuddy_core/rate_limiter.py`](../dbbuddy_core/rate_limiter.py) (queries, keyed on the authenticated `sub`) **fails open** — no Redis, queries still run. [`backend/app_db/login_guard.py`](../backend/app_db/login_guard.py) (auth) uses a **shared Redis window and degrades closed** — no Redis, the local window still decides with its cap divided by `LOGIN_GUARD_WORKERS`. A throughput limiter can yield; a control protecting authentication cannot vanish when its dependency does. Don't merge them. |
| **How do I delete a user and everything they own?** | [`backend/app_db/routers/admin.py`](../backend/app_db/routers/admin.py) — `_purge_user_owned_data()` unwinds ownership **explicitly, in FK-dependency order**, because cascade behavior differs between SQLite (tests) and PostgreSQL (prod). **Adding a new user-owned table means adding a delete here**, or a hard delete will orphan or 500 on it. |
| **Where is the CLI, and how does it stay in parity with the web app?** | [`dbbuddy/main.py`](../dbbuddy/main.py) (argparse commands → `cmd_*`), [`dbbuddy/session.py`](../dbbuddy/session.py) (authenticated HTTP to the platform). Every data command has two modes: platform (calls the same REST endpoints the browser does) and `--local` (runs `dbbuddy_core` in-process, no app DB). When you add a web feature that should be scriptable, add a `cmd_*` + a `session.py` method for platform mode and a direct-`dbbuddy_core` path for `--local`; `dbbuddy insights` (AI Insights) is the worked example. Tests: `tests/test_cli_*.py`. |
| **How do I add a chart type or palette?** | [`frontend/src/components/ChartRenderer.tsx`](../frontend/src/components/ChartRenderer.tsx) — the **only** renderer and the only interpreter of a chart's `config` (add to `CHART_TYPES` / `PALETTES` + a render branch). Then allow the type in `VALID_CHART_TYPES` ([`backend/app_db/schemas.py`](../backend/app_db/schemas.py)) and, for CLI parity, `CHART_TYPES` / `CHART_PALETTES` in [`dbbuddy/main.py`](../dbbuddy/main.py). The server never interprets colors — keep it that way. |

## Setup

**Backend / engine (Python 3.10+):**

Runtime deps (chromadb, DB drivers, etc.) live in the project **`.venv`**. Use
that interpreter — the system Python won't have them.

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python  -m pip install -r requirements.txt        # POSIX
```

**App database:** SQLite is the local-dev default; production uses PostgreSQL via
`APP_DATABASE_URL`. Seed demo accounts (admin/org_admin/analyst/client) with:

```bash
cd backend
APP_DATABASE_URL=sqlite:///./dbbuddy_app.db PYTHONPATH=.. ../.venv/Scripts/python.exe seed_test_accounts.py
```

**Run the backend:**

```bash
cd backend
JWT_SECRET=dev-secret APP_DATABASE_URL=sqlite:///./dbbuddy_app.db PYTHONPATH=.. \
  ../.venv/Scripts/python.exe -m uvicorn main:app --port 8000
```

**Frontend:**

```bash
cd frontend
npm install
npm run dev        # vite dev server
```

## Tests

```bash
.venv/Scripts/python.exe -m pytest            # default suite (unit; excludes integration)
.venv/Scripts/python.exe -m pytest -m integration   # live-DB dialect suite (see below)
.venv/Scripts/python.exe -m pytest --random-order    # order-dependence hunt (one seed at a time)
DBBUDDY_STRICT=1 .venv/Scripts/python.exe -m pytest # fail on silently-recovered errors (recommended in CI)
```

- **One run at a time, and a file at a time while iterating.** The suite imports
  `chromadb` + `onnxruntime`, so a single run holds hundreds of MB; several in
  parallel, or a sweep of `--random-order` seeds back to back, will take a
  developer machine down. Run `pytest <file>` while working and the full suite
  once at the end.

- **Live-DB integration tests** are opt-in (`addopts = -m 'not integration'` in
  `pyproject.toml`). Start the engines, then run them:

  ```bash
  docker compose -f docker-compose.test.yml up -d
  pip install pymssql
  pytest -m integration
  docker compose -f docker-compose.test.yml down -v
  ```

- **Use a file-based (or Postgres) app DB for tests — never `:memory:`.**
  Per-connection in-memory SQLite breaks multi-connection fixtures and produces
  spurious errors (e.g. the whole RBAC suite). Use `sqlite:///<tmpfile>.db`.
- A second gate: the real-execution tests are skipped unless
  `DBBUDDY_LIVE_DB_TESTS=1` (see [`tests/conftest.py`](../tests/conftest.py)).
  That, and `-m integration`, are the **only** skips in a default run.

### Global state must be reset between tests

The runtime keeps process-global state for speed and Redis-global state for
sharing. Both leak across tests, and the failures they cause are order-dependent
and environment-dependent — the worst kind to debug. `conftest.py` has autouse
fixtures clearing all of it: prepared DB contexts, AI metrics, the AI circuit
breaker, the login throttle, the ERP concurrency semaphores, the auth revocation
cache, and the Redis chart-result cache.

**When you add shared state, add its reset here.** The cost of not doing so is
concrete: the dashboard backpressure test passed alone and failed in a full run,
*but only on a machine with Redis up* — the chart cache keys on
`(org, target, SQL)`, outlives the process, and every dashboard test collides on
one key, so a later test silently read an earlier one's rows and never reached
the code it was written to exercise.

### On skipping tests

There is no skip-list for "superseded" tests, deliberately. There used to be:
seven entries, all justified as pre-refactor contracts. All seven were
re-examined and none were drift — between them they were hiding dropped SQL
joins, unresolved table aliases, and a compiler emitting syntactically invalid
`INNER customers ON …`. A failing test is a claim that something is wrong.
Retiring one means showing the **product** is right, not filing the test away.

**Schema-portability corpus.** The planner is validated against realistic ERP /
domain schemas (SAP, Odoo, ERPNext, healthcare, e-commerce, banking) under
[`tests/schema_portability/`](../tests/schema_portability/), driven by
[`tests/test_schema_portability.py`](../tests/test_schema_portability.py). When
you change filter extraction, type handling, or the join graph:

- **Add a schema** by dropping a `<name>.json` file in that folder
  (`{"tables": {t: {col: sql_type}}, "foreign_keys": {...}}`).
- **Add a regression** by appending a case to `CASES` (or a dedicated test).
  Every fix should arrive with a case here.
- `scripts/exercise_schemas.py` is the exploratory harness (run it to eyeball the
  capability matrix across all schemas; not a pass/fail test).

**Frontend checks:**

```bash
cd frontend
npm run lint       # eslint
npm run format     # prettier --write
```

## Correctness loop (dogfooding)

[`scripts/dogfood/`](../scripts/dogfood/) runs the engine against **populated**
databases and scores the answers. Distinct from the benchmarks (speed) and from
`tests/schema_portability/` (schema shape, no rows): the failure this catches is
SQL that runs fine and returns the wrong number.

```bash
.venv/Scripts/python.exe scripts/dogfood/run.py --dataset erp        # or hospital | legacy | employees | adventureworks | tpch | tpcds | airportdb
.venv/Scripts/python.exe scripts/dogfood/run.py --dataset erp --rebuild -v
.venv/Scripts/python.exe scripts/dogfood/run.py --dataset erp --suite fanout
.venv/Scripts/python.exe scripts/dogfood/run.py --dataset employees --suite fanout having
.venv/Scripts/python.exe scripts/dogfood/run.py --dataset employees --shuffle   # randomize suite order
```

**One lesson per dataset.** The suite is chosen for *different failure modes*,
not for volume — each database taught the engine something the others could not:

| Dataset | Primary lesson |
| --- | --- |
| `erp` | Planner correctness |
| `hospital` | Schema portability |
| `legacy` | Pathological identifiers |
| `employees` | Production modeling assumptions |
| `adventureworks` | Enterprise naming conventions |
| `tpch` | Benchmark identifier conventions and analytical workloads |
| `tpcds` | Star-schema structure — role-playing dimensions and parallel facts |
| `airportdb` | Real data at real scale — 55M rows, reserved-word join keys |

Each one introduced a genuinely different failure mode, which is the evidence
that the suite is well chosen rather than redundant. A ninth dataset earns its
place the same way: name the failure mode it adds before adding it.

The `employees` dataset is not synthetic — it is the real MySQL `employees`
sample (~4M rows, github.com/datacharmer/test_db) loaded into SQLite. Its dumps
are large and gitignored (`data/employees/`); pass
`--db data/employees/employees.db` so a run reuses the built file instead of
rebuilding. It exercises the engine at scale on a real HR schema: a 2.8M-row
`salaries` fact, composite primary keys with **no surrogate `id` anywhere**, and
declared foreign keys named `_no` (not `_id`).

**Invariants, not expected answers.** Judging a thousand generated answers by
hand does not scale, so the suite scores *relationships between answers* —
`sum(grouped) == ungrouped total`, `filter narrows`, `paraphrases agree`. These
need no golden value, so a new database costs nothing to add, and they are what
catches wrong-but-valid SQL. The canonical case: a plan joined
`customers → orders → payments` and then summed `credit_limit`, returning 701M
instead of 94M. Valid SQL, plausible figure, every validator approves — only the
invariant disagreed. Golden values are reserved for the few questions with one
right answer.

**Bias new datasets toward pathological, not larger.** `erp` (declared FKs,
plural tables) and `hospital` (no declared FKs, singular tables, four-hop
snowflake, composite key) already cover most planner semantics. `legacy` is
small and exists purely for adversarial identifiers — reserved words, spaces,
hyphens, non-ASCII, separator-only-different columns. `employees` is the
exception to "not larger": it is there for **scale and real-schema shape**, and
it earned its keep — it surfaced an assumed-`id` crash across GROUP BY / ORDER BY
/ COUNT / HAVING, a dropped `GROUP BY <column>`, and a genuinely production-grade
non-determinism bug (`list(set(...))` for table order gave the *same question a
different answer* under a different `PYTHONHASHSEED`). `adventureworks` is the
**width** counterpart — 68 tables across 16 business domains, 91 declared FKs, and
SQL-Server/.NET naming (camelCase `XxxID` keys, PascalCase tables). It surfaced a
whole class of naming-convention assumptions: `XxxID` keys read as measures
(`SUM(SalesOrderID)`), camelCase measures/tables unmatchable by word, and extremum
on a multi-word measure falling to a COUNT-ranking. Fixes are schema-driven
(`semantic_roles.is_identifier_name`, `intent_builder.split_identifier`), never a
name list. Its dumps are gitignored; build once and pass
`--db data/adventureworks.db`.

`tpch` is the **analytics/warehouse** shape: a 300k-row `lineitem` fact joined to
`orders`/`part`/`supplier`, with `nation`/`region` two and three hops out, so
every question is an aggregate over a join. It is generated
(`scripts/dogfood/dataset_tpch.py`, scale factor 0.05, no dump to download) and
carries the naming convention neither of the real datasets has: **every column is
prefixed with its table's initial and its words run together** —
`l_extendedprice`, `o_orderdate`, `ps_supplycost` — and **keys end in `key`, with
no column named `id` anywhere**. That defeated word matching in both directions:
splitting a column cannot recover a boundary that was never written, so
`intent_builder.expand_query_tokens` re-joins adjacent *query* words and
`segment_token` splits a run-together column token using the query's own words as
its only dictionary (so `mktsegment` correctly fails to match "market segment"
rather than being guessed at), while `uniform_column_prefix` derives the noise
prefix from the table's own columns.

`tpcds` is the **star-schema structure** counterpart, and the ambiguity it adds is
structural rather than lexical. Its 25 tables come from the official DDL
(`tools/tpcds.sql` + `tools/tpcds_ri.sql` of github.com/gregrahn/tpcds-kit,
downloaded to the gitignored `data/tpcds-src/`), so the 24 primary keys and 104
foreign keys under test are the benchmark's own. Rows are generated. What it
exercises that nothing else does: **role-playing dimensions** — one fact reaches
one dimension through several foreign keys (`ws_sold_date_sk` *and*
`ws_ship_date_sk` → `date_dim`), so "which table" no longer identifies a join;
**parallel facts** whose measures share a name across three sales channels;
**surrogate `_sk` keys beside business `_id` keys**; and **nullable foreign keys**,
which decide whether a COUNT sees every row. The graph now keeps every foreign key
between a pair (`relationship_graph.JoinKeys.alternates`) and the planner picks the
one whose role the question named (`query_planner._apply_join_roles`).

`airportdb` is the top of the size ladder and the only dataset whose rows *and*
schema are both real and both large: Oracle's published MySQL Shell dump
(626 MB, gitignored at `data/airport-db.tar.gz`), 14 aviation tables, ~55M rows
in full. `scripts/dogfood/dataset_airportdb.py` translates the MySQL DDL to
SQLite, keeping every declared key, and streams the zstd-compressed TSV chunks
in. It builds in **tiers** — `AIRPORTDB_TIER=s` (~250k rows/table, the default
and the one to iterate on), `m` (~2M), `l` (uncapped, a ~10 GB SQLite file) —
and a capped build stays referentially intact by dropping child rows whose
parent was capped away, since a dangling foreign key makes an inner join lose
rows and every conservation invariant fail for a reason that is not a defect.
Its distinctive hazard: `flight.from` and `flight.to` are both foreign keys to
`airport` **and both reserved words**, so the schema needs role selection and
quoting at the same time.

**Two ceilings bound a read, and both matter.** `execution.MAX_ROWS` is the
*display* limit and it is not a safety mechanism — it trims a list that has
already been built. The ones that bound memory are `query.FETCH_CAP` (the cursor
stops reading after 10k rows; `$DBBUDDY_MAX_FETCH_ROWS`) and
`query_planner._bound_unlimited_read`, which sends `LIMIT MAX_ROWS + 1` with any
row-returning plan the question did not limit, so the database stops scanning
instead of producing every row for us to throw away. A read that hits the limit
reports `truncated` and deliberately reports **no total** — counting the rest
would mean scanning the table the bound just avoided. Anything that adds a new
execution path must go through `execute_query`, not a bare `fetchall`.

**Hunt order dependence with `--shuffle [seed]`.** Suites share the process
context, caches and semantic memory, so a state leak from one query into the next
only shows when the order changes. `--shuffle` randomizes suite order and prints
the seed for replay; a green run means the answer does not depend on which
question was asked first. Randomize the *dataset* order too by looping the eight
datasets in a shuffled sequence — one at a time (see below).

**Run one at a time.** Every dogfood process imports `chromadb` + `onnxruntime`
and builds the embedder before the first question, so each run costs hundreds of
MB of RSS on its own; on top of that `tpch` scans a 300k-row fact table (~3
minutes), `employees` 3.9M rows and `adventureworks` 759k. Two of these
concurrently — or a loop over the full pytest suite — will exhaust a laptop, and
has. While iterating, use `--suite <name>` and stay on the cheap datasets
(`erp` / `hospital` / `legacy` cover most planner semantics in about 4 seconds
each); keep the large ones for the single confirming run at the end.

The built databases live in the system temp dir (`dbbuddy_dogfood_*.db`, ~114 MB
for the generated sets) and are caches — deleting them costs a rebuild of a few
seconds, except `employees` / `adventureworks`, which reload from their dumps and
are worth passing `--db` to instead.

**Record cost as well as correctness** with
[`scripts/dogfood/benchmark.py`](../scripts/dogfood/benchmark.py) — for a
representative set of questions per dataset it reports the per-stage latency
breakdown (from the pipeline's own `meta.stage_timings`), the confidence band,
and the SQL shape (tables, joins, predicates, grouping). It asserts nothing by
default; `--max-total-ms N` turns total latency into a CI gate, `--json` writes
the full record. Distinct from `scripts/benchmarks/` (subsystem micro-benchmarks
on a synthetic schema) — this measures whole-pipeline cost on the real datasets.

**Treat the harness as production software.** Several harness bugs so far
presented as product bugs, each costing an iteration: patching a base class the
concrete dialects override, a missing `fetchmany` whose failure the sampler
swallows by design, skipping Analyze, unquoted `PRAGMA`, a stale `PLAN_VERSION`,
a half-built database silently reused after a failed build, and a golden check
comparing a row *count* against a probe that returns the rows themselves. Before
believing a finding, check whether the harness caused it. (Windows note: run with
`PYTHONIOENCODING=utf-8` — the box-drawing separators crash a cp1252 console.)

## Performance benchmarks

[`scripts/benchmarks/`](../scripts/benchmarks/) is one benchmark per subsystem
(embeddings, schema resolution, cache) plus an end-to-end pipeline run, on a
synthetic SQLite schema — no live DB, no Redis, no network. The split is the
point: a single number tells you something got slower, not what.

```bash
.venv/Scripts/python.exe scripts/benchmarks/run_all.py            # run + compare to baseline
.venv/Scripts/python.exe scripts/benchmarks/run_all.py --check    # exit 1 on a gated regression
.venv/Scripts/python.exe scripts/benchmarks/run_all.py --update   # re-record baseline.json
.venv/Scripts/python.exe scripts/benchmarks/bench_cache.py        # one subsystem, standalone
```

Every metric declares a **kind** — `gate` (fails `--check`), `trend` (compared and
reported, never blocks), `diagnostic` (context only), `experimental` (kept out of
the baseline until it earns a place). Run `run_all.py --list-metrics --kind gate`
to see what actually blocks. Full rules in
[`scripts/README.md`](../scripts/README.md).

Run `--check` when you touch the query path, the cache, or the vector store, and
`--update` when a number moves on purpose. Adding a new metric? Start it
`experimental`.

**Before believing a regression, check the machine.** These are wall-clock
timings, so a baseline is only meaningful against comparable hardware.
`baseline.json` records an `_recorded_on` fingerprint (host, platform, CPU count,
Python, timestamp) and `run_all.py` prints the mismatch alongside any regression
block. If the baseline predates that fingerprint it says so instead of guessing.
When in doubt, `git stash` and re-run: a regression that survives your changes
being removed is the hardware, not you.

**Dead-code sweep.** `vulture` alone is noisy on this codebase (FastAPI route
handlers and dialect-contract methods read as unused). Scan it *including*
`tests/`, then confirm anything it flags is unreferenced from the frontend and
docs too before deleting:

```bash
.venv/Scripts/python.exe -m vulture dbbuddy_core dbbuddy backend scripts tests --min-confidence 90
```

## Coding conventions

**Python**
- Formatted with **Black**, line length **100** (`pyproject.toml`).
- **Dialect abstraction is a hard rule:** engine-specific SQL, drivers, and
  connection handling live **only** in `dbbuddy_core/dialects/`. There must be no
  `if engine == "postgres"` branches in the planner/compiler/execution. Adding an
  engine follows [ADDING_DATABASE_DIALECT.md](ADDING_DATABASE_DIALECT.md).
- Parameterize user-derived values in SQL — never string-interpolate them.
- Prefer keyword construction for dataclasses like `DBConfig` (fields are added
  over time; positional construction is brittle).

**Frontend**
- Keep to the existing Tailwind + component patterns; match surrounding style.
- `npm run lint` (eslint) and `npm run format` (prettier) must pass. The tree is
  error-clean; only a few `react-refresh/only-export-components` warnings remain
  in mixed-export files by design — don't add new errors.
- **Line endings are LF.** Prettier enforces LF and [`.gitattributes`](../.gitattributes)
  normalizes the repo, so `core.autocrlf` on Windows won't produce spurious
  `Delete ␍` lint failures. Don't reintroduce CRLF.

**General:** match the conventions of the file you're editing (naming, comment
density, structure) rather than introducing a new style.

## Database migrations

Application-DB schema changes go through **Alembic** (`backend/migrations/`). Add
a migration for any model change; the app runs `alembic upgrade head` on startup.

## Change Hygiene

- Keep commits focused; separate refactors from behavior changes.
- Keep the default `pytest` suite green. If you touched the **dialect layer**, also run `pytest -m integration` against the Docker engines.
- Add or adjust tests with behavior changes; add an Alembic migration if you changed a model.
- Update the relevant docs in `docs/` (and the drivers table in [../README.md](../README.md) if you added an engine).
- Run `npm run lint` / `npm run format` for frontend changes.

## Security

Security issues should be reported through the private disclosure channel in [SECURITY.md](SECURITY.md). See that guide for the project security checklist and deployment controls.
