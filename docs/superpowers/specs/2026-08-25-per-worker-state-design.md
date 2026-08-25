# Per-worker state: a shared ERP ceiling and an honest worker profile

**Date:** 2026-08-25
**Status:** approved, implementing
**Roadmap item:** *Later — running it at scale → Shared state*, the remaining half.

## The problem

Three coordination structures were per worker process. Session revocation was fixed
earlier by broadcasting over Redis. Two remain:

* **The per-target concurrency semaphore** (`dbbuddy_core/erp_concurrency.py`) is a
  plain process dict of `BoundedSemaphore`. With N workers the effective ceiling
  against a customer's ERP is N × the configured limit. `DEPLOYMENT.md` currently
  tells operators to set `ERP_MAX_CONCURRENT_QUERIES = desired_total / worker_count`
  to compensate, which is a workaround asking the operator to do arithmetic the
  system should do.
* **The prepared database contexts** (`dbbuddy_core/context_store.py`) hold a live
  connection pool, a Chroma index, a relationship graph and a semantic layer per
  `(host, database, engine, schema)`. Two workers converge on their own copies, and
  an "Analyze Schema" on one leaves the others serving the previous schema until
  something rebuilds them independently.

The harm is not symmetric, and the design follows that. An N× ceiling is damage done
to *someone else's* production database — the exact failure `erp_concurrency` exists
to prevent. A stale context is damage done to our own answer quality, recoverable by
re-analyzing.

## What is and is not shareable

A prepared context cannot be moved to Redis. It holds a live socket and a Chroma
client handle; neither is serializable, and any design claiming to "share prepared
contexts" would in fact be sharing a subset and quietly leaving the rest per-worker.
What *can* cross workers is **invalidation**: a message saying "the context for this
key is stale, drop yours".

The semaphore is the opposite. It is pure counting, and counting is what Redis is
good at.

## Decisions

| Question | Decision |
|----------|----------|
| Scope | Shared semaphore + broadcast context invalidation + a declared worker profile. |
| Redis unreachable | Semaphore falls back to the **local** semaphore, never to unlimited. |
| Crashed slot holder | Lease with expiry in a sorted set; expired leases pruned on the next acquire. |
| Atomicity primitive | `MULTI/EXEC` pipeline with rank-based admission. **No Lua.** |
| Worker profile | Declared via `DBBUDDY_WORKERS`; warn loudly at startup, never refuse to boot. |
| Where the profile is visible | Authenticated admin endpoint. `/` stays unchanged. |

### Why the semaphore does not fail open

The engine's query limiter fails open and `/auth/refresh` degrades closed; this is a
third case. Failing open here means an unbounded number of concurrent queries against
a customer's production ERP at the moment our own cache is already unhealthy — an
outage we cause at someone else's site. Degraded-but-bounded (N × limit, the status
quo) beats unbounded. The local semaphore is the fallback.

### Why rank-based admission, and why no Lua

Acquire runs as one transaction:

```
ZREMRANGEBYSCORE key 0 <now>      -- reclaim leases from crashed holders
ZADD            key <expiry> <token>   -- admit optimistically
ZRANK           key <token>            -- where did this holder land
PEXPIRE         key <lease_ms>         -- an idle target's key evaporates
```

Rank `< limit` means admitted; otherwise `ZREM` the token and retry with jittered
backoff until `ERP_QUEUE_TIMEOUT`, then raise `ERPBusy`.

Rank rather than a count because every contender computes the same answer from the
same ordering. Two workers racing agree on which of them won, instead of both seeing
"count exceeds limit" and both backing out. The scheme can **under**-admit for one
retry cycle under contention; it cannot over-admit. For a ceiling that exists to
protect someone else's database, under-admission is the correct direction to be wrong
in.

Lua would also be atomic, and was rejected on testability. There is no Redis service
in `docker-compose.test.yml` today and no `fakeredis` in the dev extras, so a Lua
script could only be exercised against a live server. The four commands above can be
faithfully reimplemented by a ~40-line in-test stub, which makes the admission
algorithm — the part that is actually easy to get wrong — testable with no server at
all.

### Lease duration, and the hole in it

Lease = `ERP_STATEMENT_TIMEOUT + 30s`, floor 60s. The statement timeout is what
guarantees a query cannot outlive its lease, which is what makes reclaiming an expired
lease safe.

With the statement timeout disabled (`ERP_STATEMENT_TIMEOUT=0`) there is no such
guarantee. The lease falls back to `ERP_SLOT_LEASE_SECONDS` (default 300), and a query
running longer than that has its slot reclaimed while still executing — real
over-admission. This is documented at the setting that opens it rather than hidden;
`ERP_STATEMENT_TIMEOUT` is already marked **Recommended** in `DEPLOYMENT.md` for
related reasons.

No heartbeat renewal. A background thread per in-flight query to extend a lease is
more machinery than the failure justifies.

### Mixed mode

During a Redis outage each worker independently degrades to its local semaphore and
recovers when Redis returns. The ceiling is N × limit for the duration — the status
quo, reached only while degraded rather than permanently.

## Components

### 1. `dbbuddy_core/erp_concurrency.py`

`query_slot()` keeps its exact signature, its context-manager shape and its `ERPBusy`
contract. Callers do not change. Internals gain a shared path selected per acquire via
`Cache._available()`'s existing circuit breaker.

New surface:

* `_SharedSlots` — the sorted-set admission algorithm, taking a Redis-like client so a
  stub can be substituted in tests.
* `lease_seconds()` — the derivation above, exposed for testing.
* `snapshot()` — extended to report which backend is authoritative and the shared
  occupancy per target.
* `reset()` — clears both local and shared state.

### 2. `dbbuddy_core/context_store.py`

* Channel `dbbuddy:context-invalidate`, payload `_db_key(config)`.
* `rebuild()` publishes after a successful build.
* `start_context_listener()` mirrors `app_db.deps.start_revocation_listener` — the
  proven pattern in this codebase, including its short-`socket_timeout` gotcha.
* On receipt, drop that key's cached context and its pool.
* Wired from `main.py`'s lifespan beside the revocation listener.
* No Redis: no-op, and each worker rebuilds on its own next analyze. Documented
  degradation, not a failure.

### 3. Worker profile

* `DBBUDDY_WORKERS`, default 1. `LOGIN_GUARD_WORKERS` stays as an alias so existing
  deployments keep working.
* At startup, log the resolved profile: worker count, Redis reachability, and per
  subsystem whether it is shared or per-worker.
* An authenticated admin endpoint reports the same structure. `/` is deliberately
  unchanged: an anonymous caller learning that Redis is down also learns that the
  login throttle is running on its weaker per-process fallback.

## Testing

**Unit, no server**

* Admission against the command stub: the ceiling holds; an expired lease is
  reclaimed; contenders agree on the tie-break; release frees exactly one slot; a
  token is never admitted twice.
* `lease_seconds()` derivation, including the `ERP_STATEMENT_TIMEOUT=0` fallback.
* Fallback to the local semaphore when the cache reports unavailable.
* The existing `tests/test_erp_backpressure.py` continues to cover the local path
  unchanged — the fallback *is* that path.

**Integration (marked `integration`)**

* Adds a `redis` service to `docker-compose.test.yml`; there is none today.
* Two limiter instances in one process, standing in for two workers, sharing one
  Redis: the global ceiling holds across both.
* A lease written with an expiry in the past is reclaimed by the next acquire.

## Out of scope

* Moving any serializable part of a prepared context (schema snapshot, semantic layer,
  relationship graph) into shared storage so a cold worker starts warm. Separate
  change, touches the pipeline hot path.
* Per-organization concurrency shares — a different ceiling with a different purpose,
  already its own roadmap item.
