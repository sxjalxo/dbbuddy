# Changelog

All notable changes to DB Buddy are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Planner note.** A change to planning output also bumps `PLAN_VERSION` in
> `dbbuddy_core/query_planner.py`, which invalidates cached plans by construction. Entries
> that move it say so.

## [Unreleased]

### Added
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, root `SECURITY.md`, issue and pull-request
  templates.
- `docs/ROADMAP.md` — direction, what is explicitly not planned, and items open for
  contribution.
- **Continuous integration.** `.github/workflows/ci.yml` runs ruff, the test suite under
  `DBBUDDY_STRICT=1` on Python 3.10/3.11/3.12, and the frontend lint + build on every push
  and pull request. `.github/workflows/integration.yml` runs the live MySQL / PostgreSQL /
  SQL Server dialect suite nightly and on demand.
- `requires-python = ">=3.10"` and an Apache-2.0 classifier in `pyproject.toml`, which
  previously declared neither.
- **One-command demo.** `docker compose up` brings up PostgreSQL (application database and
  a seeded sample ERP database, kept separate), the backend, and the web UI, with demo
  accounts and the sample connection already registered — sign in and ask a question.
  `Dockerfile`, `frontend/Dockerfile`, `docker-compose.yml`, and `docker/`.
- **Confidence now penalises dropped clauses.** Scoring was bonus-only: it rewarded
  structure the planner found and had no term for structure it was asked for and failed to
  produce, so a plan that lost its `GROUP BY` still reported high confidence.
  `detect_dropped_clauses` compares intent against the compiled plan and costs 0.4 per
  missing clause. The plan carries `dropped_clauses` so the explanation can name what went
  missing. `PLAN_VERSION` v13 → v14. (Covers clauses lost *after* intent building; a clause
  the intent never captured is still invisible to it — see `docs/ROADMAP.md`.)
- **`DBBUDDY_DISABLE_LEARNING=1`** — freezes semantic-memory writes; reads still apply what
  has already been learned. Without it, asking the same question twice could produce two
  different plans, so a demo behaved differently on a warm instance than on a cold one and
  bisecting a planner bug gave answers that drifted. Set in the demo compose file.
- `requests_grouping()` records on the intent whether the question asked for a grain at all,
  so the confidence check can tell a dropped `GROUP BY` from a question that never wanted
  one.
- `.gitignore`: a Docker section, and a negation for `docker/**/*.sql` — the blanket
  `*.sql` rule (which exists to keep dogfood dumps out) would otherwise have silently
  swallowed the demo's seed file.

### Changed
- README trimmed from 681 to ~250 lines; depth now lives in `docs/`, which already
  covered it.
- Typed the three `readJson<any>` call sites in `frontend/src/routes/app.tsx` as
  `QueryApiResponse` / `ExecuteApiResponse`.
- Frontend reformatted with Prettier (8 files were not conformant), so the lint gate can
  be enforced rather than aspirational.

### Fixed
- **`ORDER BY` emitted an aggregate as a quoted identifier.** `compile_sql` wrapped a dict
  `order_by` into a single-element list before the assembly block tested
  `isinstance(order_by, dict)`, so every aggregate-aware branch was unreachable and each
  plan fell through to a path that quoted whatever string it was given —
  `ORDER BY "SUM(payments.amount)"`. PostgreSQL rejects that outright, so "top N X by Y"
  failed on the documented production engine; SQLite accepts it as a string constant, which
  is why the suite stayed green. `COMPILER_VERSION` v1 → v2.
- **The response cache had no version component.** Its key was
  `prefix:schema_hash:md5(query)`, so a planner or compiler fix left previously cached
  questions answering with the old SQL until the TTL expired. Now keyed by
  `PLAN_VERSION` and `COMPILER_VERSION`, matching what the plan cache already did.
- **A grouping phrase cost the query its measure.** `total revenue` resolved
  `SUM(payments.amount)`; `total revenue by region` resolved no aggregation at all — the
  dimension's columns outranked the measure in retrieval, table detection kept only the
  dimension table, and a measure search confined to it found nothing.
  `aggregation_with_widening` retries over the whole schema when the question clearly asked
  for an aggregate, mirroring the widening `find_numeric_column` and `extract_grouping_table`
  already do for the dimension. Also fixes `average order value by segment`, which was
  dropping its aggregation entirely.
- README stated the license as MIT. The project is, and always was, Apache License 2.0
  (see `LICENSE`).
- `docs/SECURITY.md` carried a placeholder security contact; it now points at GitHub
  private vulnerability reporting.
- **Four failing tests.** `test_qa_fixes` and `test_compile_sql_snapshot` still asserted
  the pre-fix, unquoted `orders.status` rendering. `status` is a reserved identifier, so
  the compiler correctly emits ``orders.`status` `` — the WHERE-clause quoting fix
  recorded in `docs/STRESS_ASSESSMENT.md`. The expectations were stale, not the compiler.
- `@typescript-eslint/no-unused-expressions` in `DashboardsPanel.tsx` — a ternary used as
  a statement, rewritten as `if`/`else`.
- Removed an unused import in `scripts/dogfood/benchmark.py` (the only `ruff` error in the
  tree).
- `docs/DEPLOYMENT.md` warned of "pre-existing NL-engine test failures"; the suite is green
  (1008 passed), and the section now describes the environmental skips accurately.
- Stale counts and lists: `scripts/README.md` and `docs/DEVELOPER_GUIDE.md` predated the
  eighth dogfood dataset; `docs/README.md` still claimed the stress assessment found no
  defects, which its own later section contradicts.

## [1.0.0] — 2026-08-24

First public release. Planner at `PLAN_VERSION` v13.

### Added

**Engine**
- Deterministic NL→SQL pipeline: structured intent → execution plan → compiled SQL, with a
  full reasoning trace attached to every result.
- Dialect layer for MySQL/MariaDB, PostgreSQL, and SQL Server behind a shared contract
  (`tests/test_dialect_contract.py`).
- Schema-driven, type-aware filter extraction (`type_handlers.py`) — numeric, date, and
  string handlers, extensible, with no domain values hardcoded anywhere in the SQL path.
- WHERE operators as first-class objects (`sql_conditions.py`) with an operator registry.
- Relationship graph built from declared foreign keys, consumed by the planner as a single
  source of join truth.
- Self-learning semantic layer with frequency thresholds, partitioned per
  `(host|database|engine)`.
- Confidence scoring, including shared-column ambiguity detection.
- Per-database context cache with smart invalidation (`context_store.py`).
- AI Insights: evidence-bound analysis where every cited number must be a real result
  value, enforced by validators rather than prompt instructions.

**Platform**
- FastAPI backend with a dedicated application database (PostgreSQL in production,
  SQLite for local development), 15 Alembic migrations.
- Authentication: Argon2 password hashing, JWT access/refresh tokens, TOTP MFA with
  recovery codes, personal API keys for CLI and automation.
- Stateless session revocation via `User.token_version`, checked on the claims-only hot
  path through a bounded, single-flight in-process cache.
- Role-based access control with an org-scoped permission catalogue.
- At-rest Fernet encryption for connected-database credentials.
- Outbound-request (SSRF) guard on operator-supplied AI provider URLs.
- Redis-shared login throttle that degrades closed; query rate limiter that fails open.
- Per-target-database backpressure (`erp_concurrency.query_slot`) and statement timeouts.
- Execution tokens: planner-reviewed writes run server-stored SQL through single-use
  tokens; raw write execution requires the `query:write:manual` permission.
- Audit log with request correlation ids.
- AI providers as per-org records with an adapter registry (OpenAI-compatible, Ollama) and
  a failover chain.
- Scheduled jobs, notifications, saved charts, dashboards, published reports, and the
  relation graph.

**Clients**
- React/TanStack Start web UI.
- `dbbuddy` CLI at parity with the web app in two modes: platform (REST) and `--local`
  (in-process, no backend or auth).

**Quality**
- ~1000 tests, a schema-portability corpus across realistic ERP/domain schemas, and eight
  dogfood datasets — `erp`, `hospital`, `legacy`, `employees` (~4M rows), `adventureworks`
  (68 tables), `tpch`, `tpcds`, and `airportdb` (up to ~59M rows).
- Per-subsystem performance benchmarks gated against a committed baseline.

[Unreleased]: https://github.com/sxjalxo/dbbuddy/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/sxjalxo/dbbuddy/releases/tag/v1.0.0
