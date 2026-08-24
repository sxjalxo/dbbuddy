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
  missing. `PLAN_VERSION` v13 → v15.
- **`DBBUDDY_DISABLE_LEARNING=1`** — freezes semantic-memory writes; reads still apply what
  has already been learned. Without it, asking the same question twice could produce two
  different plans, so a demo behaved differently on a warm instance than on a cold one and
  bisecting a planner bug gave answers that drifted. Set in the demo compose file.
- `requests_grouping()` records on the intent whether the question asked for a grain at all,
  so the confidence check can tell a dropped `GROUP BY` from a question that never wanted
  one.
- **PostgreSQL CI job.** The suite also runs with `APP_DATABASE_URL` pointed at a real
  PostgreSQL service, because SQLite is the development default and production is not:
  length caps, NUL bytes in text and cascade behaviour all differ.
- **`tests/integration/test_generated_sql_executes.py`** — compiles representative plans and
  requires the database to *execute* them. The axis SQLite cannot cover: it accepts a quoted
  unknown identifier as a string constant, which is exactly how an invalid `ORDER BY` shipped
  under a green suite. One test pins that the broken form really is rejected, so the guard
  cannot quietly become vacuous.
- **Dependency scanning.** `.github/dependabot.yml` (pip, npm, github-actions) and a nightly
  `audit.yml` running `pip-audit` and `npm audit`.
- **Dogfood against a PostgreSQL target** — `scripts/dogfood/run.py --target postgres`
  copies the dataset into a real server and runs the same suites with the engine declared as
  PostgreSQL and nothing rewritten. The SQLite shim declares MySQL and translates on the way
  through, so it can never grade the SQL itself. `erp`, `hospital`, `legacy`, `tpch` and
  `tpcds` pass; the matrix runs in the nightly integration workflow.
- `SuiteContext.column_names()` — suites asked for a table's columns with `PRAGMA`, which
  is SQLite's spelling. Each target now supplies the lookup.
- **`requirements.lock`** — the compiled, fully pinned resolution of the version floors
  (121 packages, `uv pip compile --universal` so it is correct on any platform). CI keeps
  installing from the floors, so an upstream release that breaks us still surfaces.
- **Password reset.** `POST /auth/password-reset/request` and `/confirm`, with a single-use
  token stored only as a SHA-256 hash, a 30-minute expiry, and throttling applied *before*
  the account lookup so the 429 cannot become an enumeration signal. The request endpoint
  answers identically for a known address, an unknown one and a deactivated account; every
  confirm failure is the same 400. A completed reset bumps `token_version` and invalidates
  the revocation cache, ending every outstanding session. Migration `0016`.
  Until now a user who forgot their password needed an admin to edit a hash by hand.
- **Audit rows are signed.** Each `audit_logs` row carries an HMAC over its content, keyed
  by the server secret, so database write access alone is no longer enough to change who did
  what undetectably. `scripts/verify_audit_log.py` checks them and separates *unsigned*
  (written before signing existed) from *failed*. Migration `0018`. Deletion is still not
  detected — a per-row signature says nothing about how many rows there should be; see
  `docs/SECURITY.md` for why a hash chain was deliberately not used.
- **Session revocation reaches every worker.** `invalidate_revocation()` only cleared the
  process that handled the logout, so with N workers the other N−1 kept serving a revoked
  session for up to `AUTH_REVOCATION_CACHE_TTL` — on the endpoints that authorize from JWT
  claims alone, which are the ones that reach customer data. The revoking process now
  publishes the user id and every worker drops its own entry on receipt (~40 ms, measured
  across two processes). Without Redis it degrades to exactly the previous behaviour.
- **SSRF guard now validates the socket, not just the URL.** It checked the destination and
  then let `requests` resolve the hostname again at connect time, so a name could pass on one
  address and connect on another — DNS rebinding, which the guard documented as out of scope.
  `net_guard.resolve_and_pin` resolves once, requires every returned address to pass, and
  `safe_http.post` dials that address, keeping the original hostname for `Host`, SNI and
  certificate verification. The rules moved from `backend/app_db/url_guard.py` to
  `dbbuddy_core/net_guard.py` because the outbound call is made by the engine, which cannot
  import the backend — the reason validation and connection were separated in the first place.
- **HTTP-layer rate limiting** on the endpoints that cost something to serve: `/analyze`
  (a full schema walk), `/query`, `/ai-providers/{id}/test` (an outbound request made by
  this server) and `/auth/refresh`. Budgets are per (caller, endpoint), keyed by user when
  the token verifies and by IP otherwise. The failure mode is per budget rather than
  global — throughput fails open so a Redis outage cannot become a service outage, while
  `/auth/refresh` degrades closed because it mints sessions.
- **The at-rest encryption key can be rotated.** Changing `APP_SECRET_KEY` used to make
  every stored ERP password, AI provider key and MFA secret permanently unreadable.
  `MultiFernet` now encrypts with the current key and decrypts with any key still listed in
  `APP_SECRET_KEYS_PREVIOUS`, and `scripts/rotate_secrets.py` re-encrypts at rest so the old
  key can then be dropped. `--dry-run` reports what would change and doubles as the check
  that every row is still readable.
- **Browser sessions moved out of `localStorage`.** The refresh token is now an httpOnly,
  `SameSite=Strict` cookie and the access token lives in memory, so an XSS can no longer
  exfiltrate a session that keeps working after the page closes. Opt-in per request via
  `X-Auth-Mode: cookie`, so the CLI still receives `refresh_token` in the body and is
  entirely unaffected. `/auth/refresh` accepts either, and requires a double-submit CSRF
  token when the credential came from the cookie.
- **Email verification**, opt-in via `REQUIRE_EMAIL_VERIFICATION` (default off, so existing
  installs and the demo are unaffected). Registration mails a confirmation link either way;
  when enforcement is on it returns 202 instead of a session and `/auth/login` refuses with
  403 until the address is confirmed. Tokens follow the password-reset rules — hashed,
  single use, 48-hour expiry, a new link spends the old one. Migration `0017` adds the
  columns and **grandfathers every existing account as verified**: turning enforcement on
  must not lock out a user base that registered under the old rules. `/verify-email` route
  redeems the link on arrival.
- **Reset UI.** A "Forgot your password?" mode on the sign-in screen, and a
  `/reset-password` route for the emailed link. The screen says the same thing for a known
  and an unknown address, because saying otherwise would hand back the enumeration oracle
  the API declines to give.
- **`backend/app_db/email.py`** — pluggable sender. SMTP when `SMTP_HOST` is set, otherwise
  the message is logged rather than sent, so a fresh install does not appear broken. Send
  failures are logged, never raised: an exception would answer "does this address exist?"
  with a 500.
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
- **A learned mapping cost the query its grain.** The enhancer applies a mapping by
  appending the column reference to the query text, which is indistinguishable from the user
  naming that column — so the measure landed in the select list, the dimension-binding branch
  was skipped, and the plan came out with an aggregate and no `GROUP BY`. An instance that
  had learned something answered worse than a cold one. The measure is never the grain
  (`strip_measure_only_select`).
- **A bare temporal phrase was dropped when the question named a dimension.**
  `total revenue by region last quarter` covered every quarter: scope selection was
  `named_tables or focus or schema`, so naming `regions` (which has no date column) shut out
  the measure's table. Each scope is now a candidate tier rather than a veto, and ambiguity
  is still a refusal to guess at every tier.
- **`GROUP BY` picked the key over the label.** "by region" grouped on `region_id` — right
  grain, unreadable output — while the near-identical "total amount by region" grouped on
  `region_name` (`prefer_label_over_key`).
- **A multi-word dimension became several dimensions.** `total amount by product category`
  grouped by product name, category and product id: 60 rows for a question about five
  categories. The head noun of the phrase decides (`narrow_to_phrase_head`), and a qualified
  reference now terminates the phrase so the enhancer's appended text cannot break it.
- **Seven known-vulnerable frontend dependencies** (1 critical, 6 high) — `vite`,
  `postcss`, `undici`, `seroval`, `js-yaml`, `nanoid`, `brace-expansion`. All build-time
  transitives, but the `vite` advisories (`server.fs.deny` bypass, NTLM hash disclosure via
  UNC paths on Windows) hit anyone running the dev server. Resolved by a lock-file update;
  `package.json` unchanged, and typecheck, lint and build all still pass.
- **Mixed-case identifiers were emitted unquoted on PostgreSQL.** A table introspected as
  `CUSTOMER_LOG` was compiled verbatim; PostgreSQL folds unquoted identifiers to lower case
  and reported `relation "customer_log" does not exist`. Dialects now declare whether they
  fold case (`unquoted_identifier_case`), and the compiler quotes accordingly — MySQL,
  SQLite and SQL Server preserve case and are unaffected, so ordinary output is unchanged.
  Found by the first dogfood run against a real PostgreSQL target: 11 findings became 2.
- **`SUM`/`AVG` could target a text column.** `SELECT SUM("Customer"."Customer")` —
  PostgreSQL rejects it, SQLite coerces and returns 0.0. The aggregation is now dropped when
  the column is not numeric, so the question's unsatisfied aggregation signal lowers
  confidence instead of producing a zero that reads as an answer. `COUNT`, `MIN` and `MAX`
  are exempt.
- **PostgreSQL databases outside the `public` schema were invisible.** Every introspection
  query was scoped to `table_schema = 'public'`, so a database keeping its tables in a named
  schema — ordinary for an ERP, and what Hibernate and Entity Framework produce — reported
  *no tables at all*: no error, just an empty schema and every question failing to ground.
  Introspection now follows `current_schemas(false)`, and a schema can be named on the
  connection (`DBConfig.db_schema`, `dbbuddy … --schema`), which sets the session's
  `search_path` so discovery and execution stay on the same path. MySQL has no level below
  the database and ignores the setting; SQL Server's `dbo` assumption is unchanged for now.
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
