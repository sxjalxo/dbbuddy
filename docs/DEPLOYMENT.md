# DB Buddy — Deployment Guide

**Audience:** maintainers, contributors, and operators deploying DB Buddy in a real
environment.

This document is everything required to get the platform set up, running, and
secured. It assumes no prior familiarity with the codebase.

---

## 1. What DB Buddy is

DB Buddy is an open-source data-intelligence platform with two parts:

1. **The NL→SQL engine** (`dbbuddy_core/`) — turns plain-English questions into
   deterministic, validated SQL against your ERP databases.
2. **The platform layer** (`backend/app_db/` + `frontend/`) — authentication,
   role-based access control (RBAC), organizations/tenancy, chart publishing,
   an audit dashboard, MFA, and scheduled background jobs.

**Critical security boundary:** platform state (users, roles, charts, audit, etc.)
lives in a **dedicated application database**. Your **connected business databases
are only ever query targets** — DB Buddy never writes platform data into them, and
their credentials are stored **encrypted at rest** in the app DB.

---

## 2. Architecture at a glance

```
┌──────────────┐     HTTPS/JSON      ┌──────────────────────────┐
│  Frontend    │  ───────────────▶  │  Backend (FastAPI)        │
│  React/Vite  │   Bearer JWT        │  backend/main.py          │
└──────────────┘                     │   ├─ app_db/  (platform)  │
                                     │   └─ dbbuddy_core/ (NL→SQL)│
                                     └─────────┬─────────┬────────┘
                                               │         │
                          App database ◀───────┘         └──────▶ Connected DBs
                       (PostgreSQL — source of truth)            (MySQL / PostgreSQL /
                       users, roles, charts, audit,                SQL Server —
                       jobs, notifications, MFA secrets            QUERY TARGETS ONLY)
```

- **App database:** PostgreSQL in production (SQLite only for local dev).
- **Schema is managed by Alembic** (`backend/migrations/`). The app runs
  migrations automatically on startup.
- **Auth:** JWT access + refresh tokens; passwords hashed with Argon2; ERP
  secrets and TOTP secrets encrypted with Fernet.

---

## 3. Prerequisites

| Tool | Version | Notes |
| ---- | ------- | ----- |
| Python | 3.11+ | backend |
| Node.js | 18+ (or Bun) | frontend build |
| PostgreSQL | 13+ | the application database (production) |
| A reverse proxy | nginx/Caddy | TLS termination in production |

Target databases (MySQL / PostgreSQL / SQL Server) are connected at
runtime through the UI — they are not part of the install.

---

## 4. Environment variables

Set these in the backend's environment (e.g. a systemd unit, container env, or a
`.env` that your process manager loads). **Never commit them** — copy
[`.env.example`](../.env.example) (a secret-free template of every variable) to
`.env` and fill it in.

| Variable | Required | Purpose |
| -------- | -------- | ------- |
| `DBBUDDY_ENV` | Recommended | `development` (default), `production`, or `test`. In **`production`** the backend **fails to start** unless `JWT_SECRET`, `APP_SECRET_KEY`, a non-SQLite `APP_DATABASE_URL`, and `ALLOWED_ORIGINS` are all explicitly set. An unrecognized value (e.g. a `prod` typo) is itself a startup error. |
| `APP_DATABASE_URL` | **Yes (prod)** | App DB URL. Prod: `postgresql+psycopg2://user:pass@host:5432/dbbuddy_app`. Dev default: a local SQLite file. |
| `JWT_SECRET` | **Yes** | Signs JWTs. Must be **≥ 32 bytes** — a shorter value makes the process **fail to start**. If unset, a throwaway per-process secret is generated (tokens die on restart) — dev only. |
| `APP_SECRET_KEY` | **Yes** | Derives the Fernet key that encrypts ERP passwords + MFA secrets at rest, and (through a separate label) the audit-log signing key. Keep stable and secret. Rotating it is now a supported procedure — see `APP_SECRET_KEYS_PREVIOUS`. |
| `APP_SECRET_KEYS_PREVIOUS` | No | Comma-separated retired keys, newest first. Everything is encrypted with `APP_SECRET_KEY` and decrypted with whichever of these fits, which is what makes rotation safe. Full procedure in [SECURITY.md](SECURITY.md#rotating-the-at-rest-encryption-key). |
| `REQUIRE_EMAIL_VERIFICATION` | No | Off by default. When on, an account can be created but cannot sign in until its address is confirmed. Accounts predating the feature are grandfathered by migration `0017`. |
| `EMAIL_VERIFICATION_TTL_HOURS` | No | Default 48. |
| `PASSWORD_RESET_TTL_MINUTES` | No | Default 30. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_STARTTLS` / `EMAIL_FROM` | No | Where password-reset and verification mail is sent. With `SMTP_HOST` unset the message is **logged instead of sent**, so a fresh install does not appear broken. |
| `APP_BASE_URL` | No | Where the frontend lives; reset and verification links are built from it. Defaults to the dev frontend. |
| `ALLOWED_ORIGINS` | **Yes (prod)** | Comma-separated allow-list of browser origins permitted to call the API with credentials, e.g. `https://dbbuddy.example.org,https://analytics.example.org`. **Never `*`.** Defaults to the local dev frontend (`http://localhost:5173,http://127.0.0.1:5173`). |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Recommended | If both set and no such user exists, a **platform admin** is provisioned on startup. The only non-interactive way to get the first admin. |
| `REGISTRATION_ALLOWED_DOMAINS` | No | Comma-separated email-domain allow-list for self-registration (`/auth/register`), e.g. `example.org,corp.example.org`. Empty (default) leaves registration open; set it to confine new self-service accounts to trusted domains. Registration is also IP-rate-limited regardless. |
| `ACCESS_TOKEN_TTL_MINUTES` | No | Access-token lifetime (default 15). |
| `REFRESH_TOKEN_TTL_DAYS` | No | Refresh-token lifetime (default 7). |
| `DBBUDDY_DISABLE_SCHEDULER` | No | Set to `1` to disable the in-process background-job scheduler (see §9). |
| `NEMOTRON_MODEL` / `OPENAI_MODEL` | No | Legacy default model ids used only when migrating an existing key into a provider record. New providers set the model on the record. |
| `AI_RETRY_MAX_ATTEMPTS` / `AI_RETRY_BASE_DELAY` / `AI_RETRY_MAX_DELAY` / `AI_RETRY_AFTER_CAP` | No | Per-provider retry/backoff tuning for AI labeling (defaults `3` / `0.5` / `8` / `8` s). A server `Retry-After` above the cap fails over instead of blocking. |
| `AI_BREAKER_THRESHOLD` / `AI_BREAKER_COOLDOWN` / `AI_BREAKER_MAX_COOLDOWN` | No | Circuit-breaker tuning: consecutive transient failures before a provider trips open, and the cooldown bounds in seconds (defaults `5` / `60` / `300`). |
| `INSIGHTS_ENABLED` | No | Master switch for the analyst-only Insights Engine (default on). Off → `/insights/*` returns `503` with a clear reason. |
| `INSIGHTS_TEMPERATURE` | No | Sampling temperature for insight generation (default `0.2` — deliberately low; this is analysis, not prose). Provider *selection* is not an insights setting: insights use the org's existing AI provider chain. |
| `INSIGHTS_MAX_SAMPLE_ROWS` / `INSIGHTS_MAX_COLUMNS` / `INSIGHTS_MAX_CELL_CHARS` | No | Bounds on the evidence packet sent to the model (defaults `20` / `40` / `120`). Statistics still cover every row supplied; these cap only what is quoted verbatim. Lower them if a provider rejects the prompt for length. |
| `INSIGHTS_MAX_HISTORY_TURNS` / `INSIGHTS_MAX_TURN_CHARS` | No | Bounds on the client-supplied follow-up transcript (defaults `8` / `1000`). This input is attacker-controlled; see [SECURITY.md](SECURITY.md#ai-output-validation-prompt-injection). |
| `INSIGHTS_CACHE_TTL_HOURS` | No | Lifetime of a cached insight bundle (default `24`). A stale entry regenerates in place. `0` or negative disables expiry — entries then live until the data or the prompt version changes. |
| `DASHBOARD_CACHE_TTL_SECONDS` | No | Lifetime of a cached chart result when a dashboard is opened (default `45`). Short by design: long enough to absorb a burst of clients opening the same dashboard, short enough that "as of a moment ago" stays true. Each chart reports its own `fetched_at`, and Refresh always bypasses the cache. |
| `DASHBOARD_MAX_PARALLEL_QUERIES` | No | How many of a dashboard's charts may query concurrently (default `6`). Bounded so one wide dashboard cannot open dozens of simultaneous connections to a target database. |
| `ERP_STATEMENT_TIMEOUT` | **Recommended** | Wall-clock ceiling (seconds, default `60`) on any single statement against a target database, applied per connection. Without it a lock wait or a huge scan holds a worker and a pool slot indefinitely. PostgreSQL/MySQL enforce it server-side; **SQL Server has no session equivalent and is not covered**. `0` disables. |
| `ERP_MAX_CONCURRENT_QUERIES` | **Recommended** | Backpressure: concurrent queries allowed against any one target database (default `10`). Protects connected business databases from DB Buddy's own popularity. **Global when Redis is reachable** — do not divide it by your worker count. Without Redis it degrades to a per-process ceiling, so N workers allow N× (bounded, never unlimited). See §8b. |
| `ERP_SLOT_LEASE_SECONDS` | No | How long a concurrency slot stays claimed when `ERP_STATEMENT_TIMEOUT` is disabled (default `300`). Only consulted in that case; otherwise the lease is the statement timeout plus 30 s. A query outliving its lease has its slot reclaimed while still running. |
| `DBBUDDY_WORKERS` | **Required if multi-worker** | How many worker processes are running (default `1`). A worker cannot detect its siblings, so this is declared. Supersedes `LOGIN_GUARD_WORKERS`, which still works as an alias. Reported at startup and from `GET /runtime-profile`. |
| `ERP_QUEUE_TIMEOUT` | No | How long a query waits for a concurrency slot before failing with "busy" (default `20` s), surfaced as `503` + `Retry-After`. Bounded so a saturated target surfaces as an error, not a hung worker. |
| `AI_PROVIDER_BLOCK_PRIVATE_NETWORKS` | **Required for hosted/multi-tenant** | Set to `1` to refuse AI-provider `base_url`s resolving to loopback or RFC1918 addresses. Off by default because the normal deployment runs Ollama on `localhost` and self-hosted models on the LAN — turn it on wherever a tenant admin is untrusted relative to the server's network. Cloud instance metadata (link-local, `169.254.169.254`) is refused **regardless** of this setting. |
| `LOGIN_GUARD_WORKERS` | **Required if multi-worker** | Your uvicorn worker count (default `1`). The login/registration throttle is a **shared Redis sliding window**, so the configured limit holds at any scale. This value is consulted only while Redis is unreachable: the fallback in-process cap is divided by it, so N workers each allowing `cap/N` stay near the intended global limit rather than N times it. Set it wrong (too low) and logins throttle early during a Redis outage; leave it at `1` on a multi-worker box and the degraded path allows N× the attempts. |
| `AUTH_REVOCATION_CACHE_TTL` | No | Seconds an account's `(token_version, is_active)` is cached in-process (default `10`) so the claims-only query path does no per-request app-DB read. Bumps made through the API invalidate the entry immediately, so this only bounds how long an **externally** applied change (a direct DB edit, or a bump on another worker) takes to take effect. |
| `JOB_FIRE_DEDUPE_SECONDS` | No | Window in which two fires of the same scheduled job count as the same fire (default `30`). Stops a job running once per worker process. |
| `CHART_MAX_ROWS` | No | Hard ceiling on rows returned for one chart (default `5000`). Applied before serialization and caching. Truncation is disclosed (`row_count` + `truncated`), never silent. |
| `CHART_MAX_CACHEABLE_ROWS` | No | Results above this are returned but not cached (default `2000`), so a few huge entries cannot evict the whole cache. |
| `DASHBOARD_MAX_CHARTS` | No | Maximum charts in one dashboard (default `40`). Opening a dashboard runs every chart, so this bounds the per-open cost. |
| `REDIS_CONNECT_TIMEOUT` / `REDIS_SOCKET_TIMEOUT` | No | Bounds on the Redis handshake and each command (defaults `0.5` / `2` s). Caching is optional everywhere — every consumer falls back to computing live — so these keep an **absent or stalled** Redis from being paid for on the request path. |
| `REDIS_ERROR_THRESHOLD` / `REDIS_RECOVERY_SECONDS` | No | Availability gate for a Redis that dies *after* startup (defaults `3` / `30`). The connect-time probe only proves the server was up once; without this, a server that goes away mid-process still costs `REDIS_SOCKET_TIMEOUT` on **every** lookup and store. After this many consecutive errors the client stops calling out entirely, then retries one command after the cooldown to notice recovery on its own. Decode errors and unserializable values do not count — those are data problems, not server health. |
| `SCHEMA_CACHE_TTL_SECONDS` | No | Seconds a resolved schema is cached before the next query re-reads it to detect a DDL change (default `5`, `0` = re-read every query). On a wide ERP schema the read is the largest per-query cost; caching it collapses a dashboard's burst of charts into one read, staleness bounded by this value. Analyze / rebuild / connection edits invalidate immediately. Reported at `GET /context-metrics` (`schema_fetches_cached`). |
| `OLLAMA_NUM_CTX` / `OLLAMA_NUM_PREDICT` | No | Context-window and reply-token budget for a local Ollama labeling provider (defaults `16384` / `4096`). Ollama defaults `num_ctx` to `2048` regardless of the model, which truncates the JSON reply on a wide schema and silently drops labeling to rule-based; these give it the model's real capacity. Lower for a small local model. |
| `LOCAL_CLASSIFY_CHUNK` | No | Columns classified per Ollama request during Analyze (default `30`). Bounds each reply so a wide schema cannot overflow the context; a chunk that still fails degrades only its own columns. |
| `CHROMADB_PERSIST_DIRECTORY` | No | Where the schema embeddings live (default `./chroma_db`). Point it at a persistent volume in production: it survives restarts, and losing it forces a full re-embed on the next Analyze Schema. |
| `VITE_API_BASE` | Frontend build | Base URL the frontend calls, e.g. `https://dbbuddy.example.org`. Defaults to `http://127.0.0.1:8000`. |

> **AI providers are records, not environment variables.** Configure them in-app
> under **Settings → AI Providers** (`settings:ai` permission): each record has an
> adapter (OpenAI-Compatible / Ollama), base URL, model, and an optional key stored
> **encrypted in the app DB** (never displayed). Any existing keyring/env keys
> (`NEMOTRON_API_KEY` / `OPENAI_API_KEY`) are migrated into records automatically on
> first start and remain a deprecated compatibility path.

---

## 5. First-time setup

```bash
# 1. Clone and enter the project
git clone <repo> dbbuddy && cd dbbuddy

# 2. Backend: virtualenv + dependencies
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # Linux/macOS
pip install -r requirements.txt

# 3. Provision the application database (PostgreSQL)
#    Create an empty database + user, then point APP_DATABASE_URL at it.
createdb dbbuddy_app           # or via your DB tooling
export DBBUDDY_ENV="production"   # fail-fast on any missing/insecure setting below
export APP_DATABASE_URL="postgresql+psycopg2://dbbuddy:secret@localhost:5432/dbbuddy_app"
export JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
export APP_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
export ALLOWED_ORIGINS="https://dbbuddy.example.org"
export ADMIN_EMAIL="admin@example.org"
export ADMIN_PASSWORD="<a strong password>"

# 4. Frontend: install + build
cd frontend
npm install        # or: bun install
VITE_API_BASE="https://your-api-host" npm run build   # outputs static assets to dist/
cd ..
```

**Database migrations run automatically** on backend startup (`init_db()` calls
`alembic upgrade head`). To run them by hand:

```bash
cd backend
APP_DATABASE_URL="postgresql+psycopg2://..." python -m alembic upgrade head
```

Existing pre-Alembic databases are auto-adopted (stamped at the baseline, then
upgraded). See `backend/migrations/README.md`.

### Upgrade note — schema embeddings

The vector store stamps `EMBEDDINGS_VERSION` into each collection name. When that
constant moves, existing collections are **ignored rather than migrated**: the
first Analyze Schema after the upgrade re-embeds each connected database once,
which takes seconds to tens of seconds depending on schema width. Queries keep
working throughout on rule-based labels — nothing is blocked, the first Analyze
is just slower than usual.

It last moved to **`v2`**, when collections began binding an explicitly named
embedding function. Nothing to run; just expect one slow Analyze per database.

---

## 6. Running the platform

**Backend (development):**
```bash
cd backend
PYTHONPATH=.. python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

**Backend (production):** run behind a reverse proxy with TLS. **Use a single
worker** (`--workers 1`) unless you disable the scheduler on extra workers — see
§9. Example:
```bash
cd backend
PYTHONPATH=.. uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

**Frontend (production):** serve the built `frontend/dist/` as static files from
your reverse proxy (nginx/Caddy), and proxy `/auth`, `/jobs`, `/admin`, etc. to
the backend. For a quick check: `npm run preview`.

> **Troubleshooting:** a `NetworkError when attempting to fetch resource` on the
> login screen means the **frontend can't reach the backend** — the backend isn't
> running or `VITE_API_BASE` is wrong. On Windows, free a stuck port with
> PowerShell: `Get-NetTCPConnection -LocalPort 8000 | %{ Stop-Process -Id $_.OwningProcess -Force }`.

---

## 7. Accounts & roles

### Provisioning the first admin
Set `ADMIN_EMAIL` + `ADMIN_PASSWORD` before first start. There is no self-service
path to the admin role.

### Demo / evaluation accounts
A reproducible script seeds one account per role (default organization):
```bash
cd backend
APP_DATABASE_URL="..." PYTHONPATH=.. python seed_test_accounts.py
```
| Email | Password | Role | Lands on |
| ----- | -------- | ---- | -------- |
| `admin@dbbuddy.io` | `Admin#12345` | admin | Platform admin console (orgs, users, audit) |
| `orgadmin@dbbuddy.io` | `OrgAdmin#12345` | org_admin | Org admin console (own-org users, connections, audit) |
| `analyst@dbbuddy.io` | `Analyst#12345` | analyst | Analyst workspace (query, charts, schedules) |
| `client@dbbuddy.io` | `Client#12345` | user | Read-only Reports viewer |

**These are demo credentials — delete or change them before going live.**

### Role model
| Role | Can do |
| ---- | ------ |
| **admin** (platform) | Manage all organizations & users, read all audit, system/security settings |
| **org_admin** | Manage their own org's users + connections + AI settings; read their org's audit; schedule jobs |
| **analyst** | Connect DBs, run queries, save / customize / publish charts, schedule jobs, configure AI keys |
| **user** (client) | View published reports only (read-only) |

Self-registered users become **analysts** in the default organization. Restrict
who can self-register with `REGISTRATION_ALLOWED_DOMAINS` (§4); registration is
IP-rate-limited regardless.

The Analyst role includes `query:write:manual`, which permits running hand-written
write SQL directly against a connected database. For a locked-down deployment,
create an Analyst-style role **without** that permission — such users can still
run and confirm planner-generated writes (via the execution-token flow) but cannot
execute arbitrary raw writes.

The frontend routes each role to the right workspace automatically
(`query:run` → analyst workspace, `user:manage` → Admin console, `report:view` →
Reports viewer).

---

## 8. Feature overview (what's included)

| Area | Summary |
| ---- | ------- |
| **Authentication** | JWT access/refresh, Argon2 passwords, register/login/refresh/logout |
| **RBAC** | Granular permissions; every endpoint gated; no anonymous access to data/queries |
| **Organizations** | Every user belongs to one org (tenancy); platform-admin vs org-admin scoping; URL-safe org slugs |
| **Publishing** | Analyst saves a chart → customizes it (type / palette / per-element colors) → publishes → clients see a read-only report that re-queries **live** (no stale data, never exposes SQL/credentials) and renders the analyst's exact styling |
| **Audit** | Every action recorded as (actor, org, entity, action) with correlation ids; filterable dashboard + CSV export |
| **MFA / 2FA** | TOTP with QR enrollment, one-time recovery codes, login challenge flow, `amr` claim |
| **Background jobs** | Scheduled report refreshes + context rebuilds, Run-now, history, in-app notifications |
| **Relation graph** | Analyst-only view of how databases and tables interconnect, built from cached schema snapshots (never live introspection) |
| **Dashboards** | Analyst-authored collections of charts with narrative (Infographics → Dashboards). Published to clients like reports; opening one re-runs every chart live and concurrently, with per-chart freshness and per-chart failure isolation |
| **Insights Engine** | Analyst-only, evidence-bound explanations of an *executed* result — summary, findings with inline evidence, recommendations, limitations, follow-up Q&A. Never generates SQL; output is validated so an unsupported cause cannot reach the user |
| **AI labeling** | Semantic labeling via per-org provider records — a local **Ollama** server or any **OpenAI-compatible** endpoint (OpenAI, NVIDIA, OpenRouter, …), with an active provider + fallback chain. Labels the schema only, never generates SQL |

---

## 8b. Running more than one worker

A worker process cannot see its siblings — nothing in the runtime knows how many
others exist — so **declare the count**:

```
DBBUDDY_WORKERS=4
```

`LOGIN_GUARD_WORKERS` is the old name for the same thing and still works.

At startup the backend logs the resolved profile: how many workers, whether the
shared store is reachable, and for each subsystem whether it is shared or
per-worker. The same structure is served from `GET /runtime-profile`
(authenticated; it is deliberately not on the public health endpoint, because
telling an anonymous caller that Redis is down also tells them the login throttle
is on its weaker fallback). **A misconfiguration is never a startup failure** — a
container that refuses to boot because a cache is down turns a Redis blip during a
rolling restart into a total outage.

### What is shared, and what is not

| Subsystem | With Redis | Without Redis |
|---|---|---|
| ERP concurrency ceiling | **Shared.** `ERP_MAX_CONCURRENT_QUERIES` is the real global limit. | Per worker: a target database sees up to N × the limit. |
| Session revocation | Broadcast; other workers drop the entry in ~40 ms. | Each worker converges within `AUTH_REVOCATION_CACHE_TTL`. |
| Login throttle | Shared sliding window. | Per worker, cap divided by `DBBUDDY_WORKERS`. |
| Prepared schema contexts | **Per worker**, with invalidation broadcast: a rebuild on one worker drops the others' copies. | Per worker, and each rebuilds only on its own next analyze. |
| Scheduler | Not a Redis question. One worker must own it: set `DBBUDDY_DISABLE_SCHEDULER=1` on the others. | Same. |

Prepared contexts never become "shared", and that is not a gap waiting to be
closed. One holds a live connection pool and a Chroma client handle; neither
survives serialization. What crosses workers is the message that a context is
stale.

### The ERP ceiling

With Redis, the count lives in a sorted set per target and `ERP_MAX_CONCURRENT_QUERIES`
means what it says at any worker count — **do not divide it by your worker count
any more**. Without Redis it falls back to a per-process semaphore, which is N ×
the limit rather than unlimited; that fallback is deliberate, because failing open
here would mean unbounded concurrent queries against a customer's production
database at the moment our own cache is already unhealthy.

Each holder's slot carries a lease so a worker that dies mid-query does not remove
a slot permanently. The lease is `ERP_STATEMENT_TIMEOUT + 30s`, because that
timeout is what guarantees the query cannot outlive it. **With
`ERP_STATEMENT_TIMEOUT=0` there is no such guarantee**: the lease falls back to
`ERP_SLOT_LEASE_SECONDS` (default 300), and a query running longer than that has
its slot reclaimed while still executing — real over-admission, and one more reason
the statement timeout is marked Recommended.

---

## 9. Background jobs — operational notes

- The scheduler is **in-process (APScheduler)** — no external broker (Celery/
  Redis/RabbitMQ) is required. This is intentional for the current scale.
- **Run with a single backend worker.** With multiple workers, each would start
  its own scheduler and run jobs N times. If you must scale to multiple workers,
  set `DBBUDDY_DISABLE_SCHEDULER=1` on all but one worker (or run one dedicated
  scheduler process). The executor is abstracted behind a clean seam, so a
  distributed queue can be slotted in later without changing callers.
- Job types: **report_refresh** (re-runs a published report's SQL to validate/
  warm it) and **context_rebuild** (rebuilds a connection's semantic layer).
- Schedules: manual (run-now only), hourly, daily, weekly. Times are **UTC**.

---

## 10. Production security checklist

- [ ] Set `DBBUDDY_ENV=production` — this turns the checks below into hard startup errors (fail-fast) instead of dev-only warnings.
- [ ] `JWT_SECRET` (≥ 32 bytes) and `APP_SECRET_KEY` set to strong secrets (store in a secrets manager). A short `JWT_SECRET` now aborts startup. `APP_SECRET_KEY` no longer has to be permanent — see the rotation procedure — but changing it without following that procedure still destroys every stored secret.
- [ ] Redis reachable if running more than one worker, and `DBBUDDY_WORKERS` set to the real count. Without Redis the ERP concurrency ceiling applies per worker (N× the load on a customer's database), session revocation converges on `AUTH_REVOCATION_CACHE_TTL`, and each worker keeps its own prepared contexts. Check `GET /runtime-profile` after deploying — it reports exactly this. See §8b.
- [ ] `python scripts/verify_audit_log.py` scheduled, so audit tampering is noticed rather than discovered.
- [ ] Decide on `REQUIRE_EMAIL_VERIFICATION` and configure `SMTP_HOST`, or password reset mail is only written to the log.
- [ ] `APP_DATABASE_URL` points to **PostgreSQL**, not SQLite.
- [ ] Serve everything over **HTTPS**; terminate TLS at the proxy.
- [ ] **Set `ALLOWED_ORIGINS`** to your frontend domain(s) — CORS is an explicit
      allow-list; it defaults to localhost and must never be `*` in production.
- [ ] Change/remove the demo accounts; provision a real admin via `ADMIN_EMAIL`.
- [ ] Encourage/enforce **MFA** for admin and analyst accounts.
- [ ] Run the backend with **one worker** (or disable the scheduler on extras).
- [ ] Back up the application database regularly (it is the source of truth).
- [ ] Keep ERP database accounts **read-scoped** where possible — published
      reports already refuse to execute non-read-only SQL.

---

## 11. Running the test suite

```bash
# from the project root, using the project venv
.venv/Scripts/python -m pytest -q
```
The suite covers the platform layer (auth/RBAC, orgs, publishing, audit, MFA,
jobs) and the NL engine. **Every skip in the default run is environmental** —
there is no list of tests skipped for asserting stale contracts; the last such
list was removed after all seven of its entries turned out to be masking live
defects (see [QA_CHECKLIST.md](QA_CHECKLIST.md)).

Live-database tests are opt-in and excluded from the default run: the dialect
integration suite runs with `pytest -m integration` after
`docker compose -f docker-compose.test.yml up -d` (MySQL, PostgreSQL, SQL Server),
and the real-execution tests run only when `DBBUDDY_LIVE_DB_TESTS=1` is set. See
`tests/conftest.py` and `docs/ADDING_DATABASE_DIALECT.md`.

> **If Redis is running locally**, some suites exercise the shared code paths
> (chart-result cache, login throttle) rather than their in-process fallbacks —
> which is the point, but it means the run is not identical with and without it.
> `conftest.py` clears every shared namespace around each test so the *results*
> are the same either way.

---

## 12. Where things live

| Path | What |
| ---- | ---- |
| `backend/main.py` | FastAPI app, query endpoints, middleware, router wiring |
| `backend/app_db/` | Platform: models, auth, RBAC, routers, jobs, security |
| `backend/migrations/` | Alembic migrations (`0001`–`0018`) + README |
| `backend/seed_test_accounts.py` | Reproducible demo-account seeding |
| `dbbuddy_core/` | The NL→SQL engine (deterministic pipeline) + AI labeling |
| `dbbuddy_core/insights/` | Insights Engine: context builder, prompts, validators, formatter, service |
| `frontend/src/` | React app (role-branched workspaces, admin console, reports) |
| `docs/ARCHITECTURE.md` | Deeper architecture of the NL→SQL engine |

---

## 13. Pre-production review items

These are **deployment and operational concerns**, not defects in the
implementation. They are listed here so they are decided deliberately before a
deployment rather than discovered in production.

### Must address before production (High)

1. **CORS allow-list (`ALLOWED_ORIGINS`).**
   - CORS is now an explicit env-driven allow-list (no `*` wildcard); it defaults
     to the localhost dev frontend.
   - Set `ALLOWED_ORIGINS` to your real frontend domain(s) before deployment — a
     credentialed wildcard is no longer possible, but the default localhost list
     will reject your production origin until you set it.

2. **Demo accounts.**
   - Remove the seeded demo accounts (`admin@dbbuddy.io`, `orgadmin@dbbuddy.io`,
     `analyst@dbbuddy.io`, `client@dbbuddy.io`) -- or change their credentials -- before production
     deployment.
   - Ensure production admin accounts are provisioned securely (via
     `ADMIN_EMAIL` / `ADMIN_PASSWORD`; see §7).

### Operational considerations (Medium)

3. **Single-worker scheduler.**
   - Background jobs are executed in-process using APScheduler.
   - In multi-worker deployments, only one worker should run the scheduler. Set
     `DBBUDDY_DISABLE_SCHEDULER=1` on all additional workers.
   - If horizontal scaling becomes a requirement, migrate the scheduler backend
     to a centralized/distributed job executor (the executor sits behind a clean
     seam — see §9).

### Known non-blocking issues (Low)

4. **Skipped tests in the default run.**
   - Every skip in a default `pytest` run is environmental: live-database
     integration tests (opt in with `-m integration`) and engine-specific tests
     whose driver (`psycopg2`, `pymssql`) is not installed.
   - There is no skip list for tests asserting stale contracts — see
     [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md#on-skipping-tests) for why that list
     was removed rather than maintained.
   - Run `DBBUDDY_STRICT=1 pytest` before a deployment: it turns silently
     recovered contract violations into failures.

---

*Questions during deployment: start the backend with `--log-level info` to see
migration and scheduler startup logs, and check the audit dashboard (admin) for a
live view of platform activity.*
