# Pre-Deployment System Design Review

**Date:** 2026-07-19 · **Scope:** whole project · **Focus:** correctness and scalability

This is a design review, not a bug list — the bug list is
[QA_CHECKLIST.md](QA_CHECKLIST.md). Everything here is something the *architecture*
does, which is fine at current scale and becomes a problem at deployment scale.

Findings are ordered by **what breaks first in production**, not by effort.

---

## Summary

The core is in good shape. The deterministic SQL path, the RBAC boundary, the
dialect layer, and the AI-constrained-to-explanation split are all sound and have
held up under adversarial QA. **What is not yet production-shaped is everything
around a single process**: the design currently assumes one backend process, one
machine, and a small number of concurrent users. Most findings below are the same
root cause wearing different hats.

**The one-line version:** DB Buddy is architecturally ready for a single-instance
deployment behind a reverse proxy. It is *not* ready to be scaled horizontally,
and several components will fail confusingly (not loudly) the moment a second
worker process exists.

---

> **Update (2026-07-19).** Items 2, 6, 10, 11 and the backpressure addition (13)
> have since been implemented; each is marked **DONE** below with what shipped.
> The remaining P0 — shared process-global state (1) — is unchanged and is still
> the one true blocker for horizontal scaling.

## P0 — Blocks a correct multi-process deployment

### 1. Process-global state makes the app single-instance only

Seven separate pieces of state live in module-level dicts guarded by a
`threading.Lock`:

| State | Location | What breaks with 2+ workers |
| --- | --- | --- |
| Prepared per-DB context (schema, semantic layer, pool) | `context_store._contexts` / `_pools` | Each worker builds its own; "Analyze Schema" warms one worker, others stay cold. Users see inconsistent latency and — until each rebuilds — potentially different semantic labels. |
| ~~Login throttle~~ | ~~`login_guard._failures`~~ | **RESOLVED.** Now backed by a shared Redis sliding window (a sorted set per key), so the configured limit is the real limit at any worker count. The in-process deque is still maintained and takes over if Redis is unreachable — with the cap divided by `LOGIN_GUARD_WORKERS`, so the limiter gets *stricter* when the shared view is lost, never laxer, and never disappears. See *Authentication throttling* below. |
| AI provider metrics | `ai_metrics._stats` | `/ai-metrics` reports one worker's view; the dashboard understates real traffic. |
| AI circuit breaker | `ai_providers._breaker` | Each worker trips independently, so an unhealthy provider is retried N× longer than intended. |
| ERP concurrency semaphores | `erp_concurrency._semaphores` | The per-target ceiling is per worker, so N workers put N× the configured load on a target database. Set `ERP_MAX_CONCURRENT_QUERIES = desired_total / worker_count`. |
| Access-token revocation cache | `deps._rev_cache` | **Security control that weakens as you scale**, like the login throttle. Each worker caches `(token_version, is_active)` independently. `invalidate_revocation()` reaches only the worker that handled the logout/deactivation, so on the other N−1 a revoked access token keeps working for up to `AUTH_REVOCATION_CACHE_TTL` (default 10 s). Bounded and short by design — the TTL *is* the convergence bound — but not zero. |
| Redis client singleton | `context_store._shared_cache` | Benign (one client per worker is correct). |

**Recommendation.** The security-relevant ones first. `login_guard` is **done**
(see *Authentication throttling*). `deps._rev_cache` remains process-local by
design: unlike the login throttle it is a *cache in front of* an authoritative
check, its staleness is bounded by a short TTL that is documented as the
convergence window, and making it shared would put a Redis round-trip on the
query hot path — the exact cost it exists to avoid. It is a bounded, disclosed
window, not an unbounded weakening.

What remains is therefore **performance state, not security state**: the AI
breaker and metrics (cosmetic — a provider is retried N× longer than intended,
and `/ai-metrics` shows one worker's view), the ERP semaphores (bounded by
setting `ERP_MAX_CONCURRENT_QUERIES = desired_total / worker_count`), and the
prepared context — the expensive one, and best kept as a deliberate per-worker
cache with a documented warm-up cost rather than made distributed.

**If you deploy multi-worker now:** set `LOGIN_GUARD_WORKERS` and
`ERP_MAX_CONCURRENT_QUERIES` to match the worker count, and accept a ≤
`AUTH_REVOCATION_CACHE_TTL` revocation window. `--workers 4` used to quietly
multiply the brute-force budget by four; that specific trap is closed.

### Authentication throttling (shared, degrading closed)

`login_guard` keeps a Redis sorted set per `(client-ip, identity)` key, scores are
attempt timestamps, pruned to a 15-minute sliding window. One shared window across
every worker, so `MAX_FAILURES` means what it says regardless of scale. The
member carries a UUID so two failures inside one clock tick both count.

**Why this one does not fail open.** The query rate limiter
(`dbbuddy_core.rate_limiter`) fails open deliberately: no Redis, queries still
run, the ERP gets a little warmer. Authentication is a different class of
control. A limiter that vanished with Redis would hand every attacker unlimited
password attempts precisely when the system is already degraded.

So it degrades **closed**, in the useful sense:

| State | Behavior |
| --- | --- |
| Redis reachable | Shared window is authoritative. Configured limit = actual limit. |
| Redis unreachable | Local deque decides, cap divided by `LOGIN_GUARD_WORKERS`. Stricter, never laxer. |
| No Redis at all (default single-node) | Same local path; `LOGIN_GUARD_WORKERS=1` leaves the original behavior byte-for-byte. |

Note what "fail closed" deliberately does *not* mean: refusing all logins when
Redis is down. That trades a bounded weakening for a guaranteed authentication
outage. Degrading to a stricter local limit is the better failure mode, and the
local window is written on *every* attempt — not only when Redis is down — so a
process that loses Redis mid-attack already knows what it has seen instead of
starting from zero.

**Residual:** with Redis down the global ceiling is approximate (each worker
counts only its own traffic), and credential stuffing spread across many distinct
accounts from one IP is only partly mitigated, since each identity is its own
key. Front an internet-facing deployment with a WAF for that.

### 2. The job scheduler will double-run under multiple workers

`jobs.py` starts an in-process APScheduler in FastAPI's `lifespan`. Every worker
process starts its own. With N workers, each scheduled report refresh fires N
times — N queries against the target database, N notifications, N audit rows.

**DONE** — fixed by making *execution* idempotent rather than by electing a
leader. `jobs._claim_fire()` stamps `last_run_at` with a conditional UPDATE that
matches only if the job has not been claimed within `JOB_FIRE_DEDUPE_SECONDS`
(default 30). Exactly one worker's UPDATE affects a row; the losers stand down.
No new table, and the guarantee lives in the database, so it holds however many
schedulers exist. Regression-tested with four threads firing one job → one run.

Still recommended for production: run the scheduler as a separate single-instance
process with `DBBUDDY_DISABLE_SCHEDULER=1` on the web workers. The claim is the
safety net, not a licence to skip that. APScheduler's memory jobstore also means a
restart loses in-flight state — a `SQLAlchemyJobStore` would fix that separately.

### 3. `.venv`-time config is read at import, not per-request

`InsightsSettings` and the various `os.getenv` module constants are evaluated at
**import time** into frozen dataclasses and module globals. Changing an env var
requires a restart — which is correct and normal — but two of them are documented
as if they were live tunables. Not a bug; worth stating in the runbook so nobody
edits `.env` and wonders why nothing changed.

---

## P1 — Correct today, degrades badly with data volume

### 4. `audit_logs` grows without bound and has no retention policy

Every login, query, execute, report run, and dashboard open writes a row.
Dashboards make this materially worse: a client opening a 20-chart dashboard is
one audit row, but they will do it many times a day. There is no partitioning, no
archival, and no retention setting. The audit dashboard queries it with
`limit/offset` (good) but `offset` degrades linearly on large tables.

**Recommendation.** Add a retention setting (`AUDIT_RETENTION_DAYS`) plus a
scheduled purge, and a composite index on `(organization_id, created_at DESC)`.
For a compliance story, archive to cold storage rather than delete. Longer term,
switch the audit dashboard from `offset` to keyset pagination.

### 5. Unbounded list endpoints

`GET /charts`, `/connections`, `/history`, `/reports`, `/dashboards` all end in
`.all()` with no limit. Fine for tens of rows; a power analyst with 5000 saved
charts gets a slow query and a large payload, and the frontend renders every card.
`/history` is the one most likely to get there first.

**Recommendation.** Add `limit`/`offset` with sane defaults to all of them, and
paginate or virtualize the corresponding lists in the UI. `audit.py` already does
this correctly — copy that shape.

### 6. ERP connection pool is per-process and fixed at 5

`context_store.POOL_SIZE = 5` per `(host, database, schema_hash)`. Dashboards now
run up to 6 charts concurrently (`DASHBOARD_MAX_PARALLEL_QUERIES`), so **a single
dashboard open can exhaust the pool for that database** and serialize behind it.
The two numbers were chosen independently and interact.

**DONE** — the relationship is now *derived* rather than asserted, so the two
cannot drift when someone tunes one and forgets the other:
`MAX_PARALLEL_QUERIES = min(requested, ERP_MAX_CONCURRENT_QUERIES)`, with a
startup warning when the request is lowered. The per-target concurrency budget
also shipped — see **13. Backpressure**.

### 7. Insight cache and execution tokens have no sweeper

`insight_cache` now has a TTL, but expired rows are only *regenerated in place* —
never deleted. A row for a query never run again lives forever. `execution_tokens`
similarly accumulates redeemed/expired rows.

**Recommendation.** One scheduled cleanup job that deletes expired rows from both
tables. Cheap, and it keeps two tables that only ever grow from becoming the
largest in the database.

---

## P2 — Architectural sharp edges worth addressing before they calcify

### 8. Chart results flow through the app server in full

Every chart re-run pulls the whole result set into the backend, serializes it to
JSON, and ships it to the browser, which renders it with Recharts. The new
`CHART_MAX_ROWS` cap bounds the damage, but the shape is still "move all the rows
to the client and let it aggregate."

**Recommendation.** Longer term, push aggregation into SQL — a chart almost always
wants a `GROUP BY`, not 5000 raw rows. The planner already builds aggregation
plans; a "charts must aggregate" rule would cut payloads by orders of magnitude
and make the row cap almost never bind. This is the single highest-leverage
scalability change available.

### 9. `chart_runtime` opens a fresh ERP connection per chart

It calls `connect_db` directly rather than going through `context_store`'s pool,
so a 12-chart dashboard opens and closes 12 connections. That was the simplest
correct thing (the pool is keyed on schema hash, which dashboards don't compute)
but connection setup is often the dominant cost against a remote ERP database.

**Recommendation.** Route chart execution through a shared pool keyed on the
connection tuple. Worth measuring first — with a nearby database this is noise;
across a WAN it dominates.

### 10. No request timeout on ERP queries

Nothing bounds how long a target-database query may run. A pathological `SELECT` on an
unindexed 50M-row table holds a worker (and a pool slot) indefinitely. The AI path
has timeouts, retries, and a circuit breaker; the *data* path — which is the one
users actually wait on — has none of that.

**DONE** — `Dialect.apply_statement_timeout()` is applied in `connect_db` on every
connection, bounded by `ERP_STATEMENT_TIMEOUT` (default 60 s): PostgreSQL
`statement_timeout`, MySQL `MAX_EXECUTION_TIME`. Applied as a **session setting
after connect**, not a connect argument, so an older server loses the ceiling
instead of losing the connection — and the method returns a boolean so a caller
can tell a real bound from a best-effort one.

**SQL Server is not covered.** It has no session-level statement timeout; the
dialect returns `False` rather than pretending. A pymssql per-connection `timeout`
would be the equivalent and is still open.

⚠️ **Verified against fakes only.** The SQL is right per each vendor's docs and
the degrade path is tested, but no live MySQL/PostgreSQL exercised it. Confirm on
a real server before relying on it.

### 11. Cache key omits tenancy

`chart_runtime._cache_key` is `engine|host|port|database|username|sql`. Two
organizations with identical connection details share cache entries. That is
*arguably* correct — same database, same user, same query, same rows — but it
means a cached result can outlive a revoked permission by up to the TTL, and it
makes cross-tenant reasoning depend on an argument rather than on a boundary.

**DONE** — `organization_id` is now the first component of the chart cache key,
and the scheduled-refresh path passes it too so a background warm and a live view
derive the same key. Regression-tested: different orgs → different keys, same org
→ shared.

### 12. Frontend `app.tsx` is ~4400 lines

It holds routing, chat, results, charts, infographics, settings, and now dashboards.
It typechecks and works, but it is the file every future change touches, which
makes conflicts and accidental regressions likely. The newer features
(`RelationGraph`, `InsightsPanel`, `DashboardsPanel`, `PinToDashboard`) are already
extracted — the older surfaces are not.

**Recommendation.** Extract `ResultsView`, `SavedChartCard` + customizer, and
`SettingsPanel` into their own files. Mechanical, low-risk, and it makes the next
feature cheaper.

---

### 13. Backpressure: a concurrency ceiling per target database — **DONE**

*Raised in review; this was the gap.* Pool sizes and parallelism limits protect
**DB Buddy** from exhausting its own connections. Nothing protected the
**target database** from DB Buddy. Eighty analysts opening dashboards at 9 a.m. is
a foreseeable Monday, and with everything else working correctly it still lands as
hundreds of simultaneous queries on a production database that has a business to
run. *An analytics tool taking down a target database because the tool got
popular is the worst failure mode this product has.*

Shipped as `dbbuddy_core/erp_concurrency.py`: a bounded semaphore per **target
database** (engine+host+port+database — deliberately not per user, since it is the
machine being protected). Work beyond `ERP_MAX_CONCURRENT_QUERIES` (default 10)
waits up to `ERP_QUEUE_TIMEOUT` (default 20 s), then fails with `ERPBusy`.

Two properties worth keeping:

* **Targets are independent.** A saturated ERP A never starves ERP B.
* **Busy ≠ broken.** A saturated database surfaces as "handling too many requests
  right now", not as a broken chart. The operator response to those is completely
  different, and conflating them sends people to debug a healthy system. On the
  HTTP API that is `503` + `Retry-After`, distinct from `502` (unreachable).

**Correction to an earlier claim in this document.** For a period the semaphore
was taken only on the dashboard/chart path, so `/query`, `/analyze` and `/execute`
— the primary traffic — bypassed it entirely and the protection above was largely
decorative. It is now taken on every path that reaches a target database
(`DBContext.connection()` for the pooled pipeline, `backend/main.py` for
`/execute`, `chart_runtime` for dashboards).

Related and easy to misread: `_ConnectionPool(maxsize=POOL_SIZE)` bounds only
**idle** connections. `acquire()` opens a new one whenever the idle queue is
empty, so the pool never limited concurrency — it was the semaphore or nothing.
Raising `ERP_MAX_CONCURRENT_QUERIES` very high re-opens that hole; it is the real
ceiling on live ERP sessions.

**Limitation, stated plainly:** the semaphores are per *process*, so N workers
means N × the ceiling. Still a bound where there was none, but a true global limit
needs shared state — the same prerequisite as item 1. Until then set the
per-process limit to `desired_total / worker_count`.

---

## What is already right (do not regress these)

* **AI cannot produce SQL or make decisions.** Constrained to labeling and
  explanation, with post-hoc validation as the enforcement boundary.
* **The deterministic path is genuinely deterministic** — planner → Predicate AST
  → dialect compiler, with parameterized values throughout.
* **Confirmed writes cannot be tampered with** — server-stored SQL redeemed via
  single-use execution tokens.
* **Ownership is unwound explicitly** rather than trusting cascades, because
  SQLite and PostgreSQL disagree. This has already prevented several bugs and
  caught two more during QA.
* **Truncation, staleness, and failure are disclosed rather than hidden** — the
  relation graph's edge cap, the dashboard's `fetched_at`, the chart row cap, and
  per-chart "needs attention" all follow the same rule.
* **Bounds live where growth happens**, not where it is noticed.

---

## Suggested sequencing

**Before first deploy** (small, high value): — **all done**
1. ~~Per-query timeouts on the ERP data path (#10)~~ — done; verify on a live server.
2. ~~Scheduler double-run (#2)~~ — done via the fire claim; still run it single-instance.
3. ~~`organization_id` in the chart cache key (#11)~~ — done.
4. ~~Pool size vs dashboard parallelism (#6)~~ — done, derived not asserted.
5. ~~Per-target backpressure (#13)~~ — done.
6. **Remaining:** document the single-worker constraint in the runbook (#1 interim),
   and add a SQL Server statement-timeout equivalent (#10).

**First month** (as data accumulates):
5. Audit retention + index (#4).
6. Pagination on list endpoints (#5).
7. Cleanup job for `insight_cache` + `execution_tokens` (#7).

**Before scaling horizontally** (the real work):
8. Shared login throttle, then breaker and metrics (#1).
9. Chart aggregation pushdown (#8).
10. Connection pooling for chart runs (#9).

**Whenever convenient:** `app.tsx` decomposition (#12).
