# Roadmap

Where DB Buddy is going and what is worth working on. Directional, not a commitment —
dates are deliberately absent; sequence is not.

**Want to help?** Anything marked **[help wanted]** is scoped, has no hidden design
decision waiting inside it, and is a good first contribution. Open an issue before starting
something large so we don't duplicate work. Read
[CONTRIBUTING.md](../CONTRIBUTING.md) first — especially the non-negotiables.

---

## The through-line

Every NL→SQL tool can demo. Almost none can tell you *why* it wrote the query it wrote, or
behave the same way twice. DB Buddy's bet is that **determinism and explainability are the
product**, and everything below is ordered by how much it protects or proves that bet.

Three things follow from it:

1. **Correctness is measured, not asserted.** A capability ships with a dogfood case that
   holds on schemas nobody anticipated.
2. **AI is never in the SQL path.** It labels schemas. If that boundary ever needs to move,
   it is a design discussion, not a patch.
3. **No hardcoded schema knowledge, ever.** A fix that works because it knows your column is
   called `revenue` is a bug with good manners.

---

## Now — making it usable by someone who isn't us

The gap between "good engine" and "project people can adopt" is packaging, and that is the
whole of this milestone.

### Getting started in five minutes
- [x] **Docker Compose demo** — `docker compose up` gives a running backend, frontend, and a
  seeded sample ERP database, with the demo account and connection already registered.
- [ ] **SQLite as a first-class engine** — the test suite already runs on SQLite and the
  dialect layer is already abstracted; promoting it means someone can try DB Buddy against a
  local file with no server at all. **[help wanted]**
- [ ] Screenshot / short demo recording in the README.

### Continuous integration
- [x] GitHub Actions: `ruff` → `pytest` → `vite build` on every push and PR.
- [x] A second workflow running `pytest -m integration` against the docker-compose database
  tier — nightly and on demand rather than per-PR, so a container hiccup never blocks a
  contributor.
- [x] A job running the suite against **PostgreSQL**, not only SQLite — production is
  Postgres, and a green SQLite run does not prove storage constraints hold. Covers the
  *application* database; the engine defects that only appear on PostgreSQL need a
  PostgreSQL query *target*, which is the next item.
- [x] `tests/integration/test_generated_sql_executes.py` — compile a representative plan,
  execute it, require the database to accept it. This is the check that would have caught
  the `ORDER BY "SUM(...)"` defect: SQLite reads the broken form as a string constant and
  returns rows, so no amount of SQLite testing could see it. Runs in the nightly
  integration workflow.
- [x] Dependabot + `pip-audit` + `npm audit`, in a nightly workflow rather than per-PR — an
  advisory published against a transitive dependency has nothing to do with the pull request
  in front of it, and blocking unrelated work on it trains people to ignore a red check.
- [x] Dogfood against a **PostgreSQL target** (`run.py --target postgres`): the dataset is
  copied into a real server and the suites run with the engine declared as PostgreSQL and
  nothing rewritten. `erp`, `hospital`, `legacy`, `tpch` and `tpcds` all pass; the matrix
  runs nightly. Its first run found two more defects — mixed-case identifiers emitted
  unquoted, and `SUM` over a text column.
- [x] The large imported datasets (`employees`, `airportdb`) are no longer SQLite-only. Rows
  stream into PostgreSQL through `COPY … FROM STDIN` straight off the SQLite cursor, and the
  primary keys and foreign keys are added *after* the data rather than maintained per row —
  bounded memory and bulk-load speed, so scale and shape can be graded on the same engine.
- [x] A compiled lock file (`requirements.lock`, `uv pip compile --universal`) alongside the
  version floors. CI still installs from the floors on purpose — resolving fresh is what
  catches an upstream release that breaks us.

### Account lifecycle
These are the reason a self-hoster currently gets stuck.

- [x] **Password reset** — request/confirm with a single-use hashed token, throttled before
  the account lookup so the 429 is not itself an enumeration signal. Bumps `token_version`,
  which ends every outstanding session.
- [x] A pluggable email sender (SMTP, console in development) with no hard dependency on any
  SaaS. `SMTP_HOST` unset means "log the message", so a fresh install does not look broken.
- [x] **Email verification** on registration (`REQUIRE_EMAIL_VERIFICATION`, default off, with
  existing accounts grandfathered by migration `0017`), plus a `/verify-email` route.
- [x] A reset form in the web app: a "Forgot your password?" mode on the sign-in screen, and
  a `/reset-password` route for the emailed link. The screen mirrors the API's refusal to
  confirm whether an address exists.
- [ ] Revisit whether open self-registration is the right default for a hosted profile.

### Security hardening
- [x] **Move the refresh token out of `localStorage`** into an httpOnly, `SameSite=Strict`
  cookie, with the access token held in memory and CSRF protection on the refresh route.
  Opt-in per request (`X-Auth-Mode: cookie`) so the CLI's contract is unchanged.
- [x] **Key rotation for data at rest** — `MultiFernet` with an ordered key list
  (`APP_SECRET_KEYS_PREVIOUS`) and `scripts/rotate_secrets.py`. No `key_id` column proved
  necessary: MultiFernet tries each key on decrypt, so the column would only have recorded
  what the ciphertext already answers.
- [x] **HTTP-layer rate limiting** beyond the login throttle (`app_db/rate_limit.py`), on
  `/analyze`, `/query`, `/ai-providers/{id}/test` and `/auth/refresh`. The failure-mode
  split is preserved per budget: throughput fails open, `/auth/refresh` degrades closed.
- [x] SSRF guard: resolve-then-pin the IP at connect time, closing the DNS-rebinding gap.
  The rules moved to `dbbuddy_core/net_guard.py` so the engine — which makes the outbound
  call — can enforce them at the socket, not just at the form.

---

## Next — reach and proof

### MCP server
Expose the engine as [Model Context Protocol](https://modelcontextprotocol.io) tools, so any
MCP client gets deterministic, explainable database querying instead of a model guessing at
SQL. The engine, the CLI, and the pipeline API already exist; this is an adapter over them.

Read-only by default; writes behind an explicit opt-in.

### Published benchmark
"Deterministic, no hallucination" is a claim, and a claim without numbers is marketing.

- [ ] Run against the Spider and BIRD development sets.
- [ ] Compare against a raw-LLM baseline on the same schemas.
- [ ] Publish methodology, a reproduction command, and **the failures** — the misses are what
  make the numbers believable.

### Confidence that tracks what was actually answered

Scoring now penalises a clause the question asked for that the compiled plan does not
contain. It does not yet cover the harder half: a clause the **intent builder never
captured**. `total amount by region last quarter` still reports high confidence while
returning a single global total, because its intent carries no grouping to lose.

- [x] Record a requested grouping in the intent even when the target cannot be resolved
  (`requests_grouping`), so the confidence check can see it. The phrase detection lives in
  the intent builder — putting it in the scorer would mean two places parsing natural
  language.
- [ ] Treat "the query returned an execution error" as a confidence input, not just an
  error field.
- [x] The planner half: a question with a grain phrase compiling without a `GROUP BY`. Four
  causes, all fixed — the measure masquerading as the grain, a named dimension shadowing the
  measure's date column, the key winning over the label, and a multi-word dimension becoming
  several.

### Learning that cannot make the answer worse

A learned mapping is applied by rewriting the query with the column reference it stands for.
That reference then competes with the dimension for retrieval rank, and can displace it — so
an instance that has learned `amount -> payments.amount` (a *correct* mapping) drops the
`GROUP BY` from "total amount by region", while a cold instance answers it correctly.

`DBBUDDY_DISABLE_LEARNING=1` makes runs repeatable, which is what made this visible, but it
is a workaround.

- [x] An injected memory reference must not be mistaken for the grain
  (`strip_measure_only_select`), and must not break grouping-phrase parsing (a qualified
  reference now terminates the phrase).
- [ ] It still competes for retrieval *rank*, which is the remaining half — the fixes above
  stop it doing damage, they do not stop it distorting what retrieval returns.
- [ ] Feed plan quality back into the learning engine: a mapping that lowers it should be
  down-weighted, not reinforced.

### Desktop app (Tauri)

Three interfaces over one core: web app, CLI, and — missing — a local desktop application.
The engine already has no opinion about who calls it, and the CLI's `--local` mode proves
the in-process path works without a backend, so the shape is already there.

Package the existing React frontend with [Tauri](https://tauri.app) into a signed `.exe`
(and the macOS/Linux equivalents). This is the shape most database tools ship in, and it
removes the two things that stop an analyst evaluating DB Buddy on their own machine:
standing up a backend, and trusting a browser with their database credentials.

Design questions to settle before writing code:

- **Where does the engine run?** Bundling Python inside the app (PyInstaller sidecar) makes
  it genuinely standalone but ships a ~1 GB runtime; connecting to a backend keeps the app
  thin but is then not really a desktop app. A third option is `--local`-style in-process
  operation for a single user, with the platform features (sharing, audit, publishing)
  appearing only when a backend is configured.
- **Credential storage.** The `keyring` dependency is already present. A desktop build
  should use the OS keychain rather than the app database, which also sidesteps the
  `localStorage` token problem the web app has.
- **What the frontend must stop assuming.** `VITE_API_BASE` is compiled in, and the app
  currently assumes a reachable HTTP API; a Tauri build would call Rust commands instead
  for at least the local path.

- [ ] Decide the engine-location question above — everything else follows from it
- [ ] Tauri shell around the existing frontend, talking to a local backend first (cheapest
  proof, no packaging problem)
- [ ] OS keychain for credentials and tokens
- [ ] Bundled engine sidecar, if the standalone story is worth the install size
- [ ] Signed installers + auto-update

### PostgreSQL schemas other than `public` — engine done, platform pending

Every introspection query in the PostgreSQL dialect is scoped to
`table_schema = 'public'`. A database that keeps its tables in a named schema — which is
ordinary for an ERP, and what tools like Hibernate and Entity Framework produce — reports
no tables at all. There is no error, just an empty schema and a query that cannot be
grounded.

Found while building the PostgreSQL dogfood target, which had to be given a whole database
rather than a schema to work around it.

- [x] Introspect the schemas on `search_path` (`current_schemas(false)`) instead of assuming
  `public`, and accept a schema explicitly on the connection (`DBConfig.db_schema`,
  `dbbuddy … --schema`). Setting the session's search path rather than qualifying every
  identifier keeps discovery and execution on the same path, so they cannot drift apart.
- [x] **Platform plumbing.** `database_connections.db_schema` (migration `0019`), the field on
  the connection API, and an optional Schema input in the connect/edit dialog — shown only for
  the engines that have a namespace inside a database, since for MySQL the database *is* the
  schema. NULL stays distinct from `"public"`: it means "follow the search path", so no
  existing connection changes behaviour.
  The half that makes it correct rather than merely stored: the schema is now part of every
  identity key that used to treat a database as the finest granularity — the prepared-context
  key, the learned-mapping scope, and the chart result cache. Without that, two schemas in one
  database share a connection pool, a vector index, and each other's cached rows. The
  concurrency ceiling deliberately stays per *server*: two schemas are still one machine.
- [x] The same question asked of the SQL Server dialect — and the answer was worse than a
  `dbo` assumption. Introspection was scoped to **no** schema, so `sales.orders` and
  `hr.orders` collapsed into one `orders` whose column list was both tables' columns
  concatenated; the planner would then join on a column belonging to the other table.
  Every `INFORMATION_SCHEMA` query is now scoped to `SCHEMA_NAME()` — the connecting
  user's own default schema, not the literal `dbo`, which would repeat PostgreSQL's
  hardcoded-`public` mistake. The foreign-key join is scoped on both sides, because a
  constraint name is unique per schema rather than per database.

  A requested `db_schema` that is not the session's schema is **refused** with an
  actionable message rather than half-honoured. T-SQL has no `search_path`: a user's
  default schema is DDL, so introspecting one schema while unqualified names resolve to
  another is exactly the drift the PostgreSQL fix exists to prevent. Pointing at an
  arbitrary schema needs schema-qualified identifier emission, which touches the AST, the
  compiler and the validator — its own item, not a bullet.

### Semantic-layer correction
The engine already learns mappings. It cannot yet be *told* it is wrong.

- [ ] An inline "this mapping is wrong" affordance on the explanation trace.
- [ ] An admin view to browse, edit, and delete learned mappings per database.
- [ ] Provenance on every mapping: learned, corrected, or seeded.

This is the durable advantage over a general-purpose model — the system gets measurably
better at *your* schema, and you can see exactly how.

### Contributor experience
- [ ] **Split the large files.** `frontend/src/routes/app.tsx` (4.4k lines),
  `dbbuddy_core/intent_builder.py`, and `dbbuddy_core/query_planner.py` are the biggest
  structural barrier to outside contribution. **[help wanted]**
- [ ] **Frontend tests** — Vitest plus a smoke render per panel; there are currently none.
  **[help wanted]**
- [ ] One end-to-end happy path: login → analyze → query → chart.

---

## Later — running it at scale

### Shared state
Several coordination structures are per worker process: the session-revocation cache, the
prepared database contexts, and the per-target concurrency semaphore. Two replicas behind a
load balancer therefore behave differently from one.

- [x] **Session revocation** now broadcasts: the revoking worker publishes and every other
  drops its cached entry on receipt (~40 ms), instead of each converging on its own TTL.
  Falls back to the previous TTL behaviour without Redis.
- [x] **The per-target concurrency semaphore** is now counted in Redis: a sorted set of
  leases per target, so `ERP_MAX_CONCURRENT_QUERIES` is a real global limit instead of a
  per-process one that operators were told to divide by their worker count. Leases expire,
  so a worker that dies mid-query does not shrink the ceiling permanently. Without Redis it
  degrades to the per-process semaphore — never to unlimited, because failing open here
  means an outage at a customer's site rather than ours.
- [x] **Prepared contexts stay per worker, and that is the honest answer**: one holds a live
  connection pool and a Chroma handle, neither serializable. What crosses workers is
  invalidation — a rebuild broadcasts, and the others drop their copy. Closing this
  uncovered that `invalidate()` never dropped a context at all: the prefix scan looked for
  `host|database|engine|` while the keys were `host|database|hash`, so an unchanged schema
  handed the stale context straight back.
- [x] **A declared worker profile.** `DBBUDDY_WORKERS` (superseding `LOGIN_GUARD_WORKERS`,
  kept as an alias), a startup log naming what is shared and what is not, and
  `GET /runtime-profile`. It warns; it never refuses to boot.

### Pluggable vector backend
Embedded Chroma writes to local disk, which makes any node holding it stateful and gives two
replicas divergent semantic memory.

- [ ] A backend interface behind `vector_store.py`, with **pgvector** as the natural default
  — the application database is already PostgreSQL in production.

### Audit-log deletion detection

Audit rows are signed, so an *edit* is detectable. A *deletion* is not — nothing in
a per-row signature says how many rows there should be.

The fix is a database-assigned monotonic sequence on `audit_logs`, so gaps are
visible without any application-side coordination. A hash chain computed at insert
would fork under concurrent workers and report tampering on an honest system,
which is worse than no check at all.

- [x] A monotonic sequence column (migration `0020`). PostgreSQL gets a real `SEQUENCE`
  owned by the column; SQLite gets the column and nothing to fill it, because an
  application-assigned counter would reintroduce the coordination problem the hash chain
  was rejected for. Existing rows are not back-filled — numbering them now would invent
  an order nothing recorded.
- [x] `scripts/verify_audit_log.py` reports gaps as *possible* deletion, never as a
  finding, and does not fail the exit code on one: a rolled-back transaction consumes a
  sequence value, and a check that goes red on healthy systems is one people silence. On
  an engine with no sequence it reports detection as **unavailable** rather than clean.
- [x] A tail check — the sequence's own `last_value` against the highest row. Closing a gap
  by renumbering the survivors is possible (the sequence value is not signed, and cannot
  be: the database assigns it after the signature is computed), but it leaves the counter
  ahead of the data it numbered.

### Observability
For a system whose selling point is its pipeline, the pipeline is currently invisible in
production.

- [ ] Prometheus `/metrics`: per-stage latency histograms, cache hit rate, AI provider
  latency and error rate, target-database pool saturation.
- [ ] OpenTelemetry spans per pipeline stage.
- [ ] Structured JSON logs carrying the existing request correlation id.

### Multi-tenancy at load
- [ ] Per-organization concurrency share and queue depth, so one heavy query cannot starve
  every other tenant.
- [ ] Per-organization AI token budget with a meter and a hard stop, surfaced in the admin
  console.
- [ ] Long-running work (`/analyze` against a very large schema) routed through the existing
  job queue rather than a request handler.

### More engines
- [ ] DuckDB **[help wanted]**
- [ ] Snowflake / BigQuery — each needs the dialect contract plus a sensible cost ceiling,
  since on those engines a careless query costs money rather than time.

---

## Explicitly not planned

Saying no is part of a roadmap.

- **LLM-generated SQL, even as an opt-in "advanced mode."** It would quietly become the path
  everything falls back to, and the guarantee the project exists to make would be gone.
- **A hosted SaaS.** DB Buddy is self-hosted. That may change; it is not the plan.
- **A BI suite.** Charts and dashboards exist to make query results legible, not to compete
  with tools built for that job.
- **Schema-specific fixes.** If a query shape only works on one schema, the mechanism is
  wrong. See [CONTRIBUTING.md](../CONTRIBUTING.md#non-negotiables).

---

## Suggesting a change

Open an issue with the
[feature request template](https://github.com/sxjalxo/dbbuddy/issues/new?template=feature_request.yml).
The template asks how a capability generalizes across schemas, which is the question that
decides whether something lands here.
