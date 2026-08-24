"""Live re-execution of a saved chart's SQL — shared by reports and dashboards.

A published chart or dashboard stores **no data**. Opening one re-runs the stored
SQL against the owner's connection so the client always sees current data, and on
any failure the chart is flagged "needs attention" rather than showing something
stale.

Two things make that affordable for a dashboard, where one open means *N* charts:

* **Parallel execution.** N charts run concurrently on a bounded thread pool.
  This is the dominant win — a 12-chart dashboard is otherwise 12 sequential
  round-trips to the ERP database, and the queries are independent.
* **A short-TTL result cache** (Redis, optional). Keyed on the execution target
  plus the SQL, so several clients opening the same dashboard in the same minute
  cost the ERP database one query per chart rather than one per client.

The cache is *deliberately* short-lived and its age is reported: a hit is by
definition not "now", and a dashboard that silently shows minute-old data while
claiming to be live would be the same kind of dishonesty the truncation disclosure
in the relation graph exists to avoid. Every payload carries ``fetched_at`` and
``cached``, and a caller can force a bypass.

**Thread safety.** Nothing in here touches the ORM or a `Session`. Callers resolve
charts and decrypt credentials on the calling thread and hand over plain
:class:`ChartJob` values, because a SQLAlchemy session is not safe to share across
threads and a lazy-load inside a worker would be a latent, load-dependent bug.
"""

from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dbbuddy_core import erp_concurrency

logger = logging.getLogger(__name__)

# Short by design: long enough to absorb a burst of clients opening the same
# dashboard, short enough that "as of a moment ago" stays true.
CACHE_TTL_SECONDS = int(os.getenv("DASHBOARD_CACHE_TTL_SECONDS", "45"))

# How many of a dashboard's charts may run at once. Bounded so one wide dashboard
# cannot open dozens of simultaneous connections to a customer database — the
# point is to overlap latency, not to flood the source.
#
# This must not exceed the per-target concurrency ceiling: a dashboard that fans
# out wider than the ceiling would spend the difference queueing against itself,
# which looks like an unexplained stall rather than a limit. Derived from that
# ceiling rather than asserted against it, so the two cannot drift apart when
# someone tunes one and forgets the other.
_REQUESTED_PARALLEL = int(os.getenv("DASHBOARD_MAX_PARALLEL_QUERIES", "6"))
MAX_PARALLEL_QUERIES = max(1, min(_REQUESTED_PARALLEL, erp_concurrency.MAX_CONCURRENT_PER_TARGET))
if MAX_PARALLEL_QUERIES < _REQUESTED_PARALLEL:
    logger.warning(
        "DASHBOARD_MAX_PARALLEL_QUERIES=%d exceeds ERP_MAX_CONCURRENT_QUERIES=%d; "
        "using %d. A dashboard cannot usefully fan out wider than its target's "
        "concurrency ceiling.",
        _REQUESTED_PARALLEL, erp_concurrency.MAX_CONCURRENT_PER_TARGET, MAX_PARALLEL_QUERIES,
    )

# Hard ceiling on rows returned for a single chart.
#
# A chart is a *visual*: nothing legible comes from plotting 200 000 points, and
# nothing stops a saved `SELECT * FROM huge` from returning them. Measured before
# this cap: one 200 000-row chart produced a **47 MB** JSON response, which a
# dashboard would then multiply by its chart count — enough to exhaust the
# server's memory, the client's, and Redis's, from a single click.
#
# Truncation is *disclosed*, never silent (the same rule the relation graph's
# edge cap follows): `row_count` carries the true total and `truncated` says so,
# so a chart plotted from a slice can never be mistaken for the whole dataset.
MAX_CHART_ROWS = int(os.getenv("CHART_MAX_ROWS", "5000"))

# Results larger than this are computed and returned but **not** cached. Redis is
# a latency optimization, not a blob store; a handful of huge entries would evict
# everything useful and turn a shared cache into a liability.
MAX_CACHEABLE_ROWS = int(os.getenv("CHART_MAX_CACHEABLE_ROWS", "2000"))

_CACHE_PREFIX = "chartrun"


@dataclass(frozen=True)
class ChartJob:
    """Everything needed to run one chart, resolved off the ORM.

    ``password`` is already decrypted — the caller does that on its own thread,
    after the ownership check.
    """

    chart_id: str
    sql: str
    chart_type: str
    config: dict | None
    title: str = ""
    # Tenancy. Part of the cache key so a cached result can never cross an
    # organization boundary — see _cache_key.
    organization_id: str | None = None
    # Connection target; None when the chart's data source is gone.
    engine: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None

    @property
    def has_source(self) -> bool:
        return self.engine is not None and self.host is not None


@dataclass
class ChartRunResult:
    ok: bool
    chart_id: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    needs_attention: bool = False
    message: str | None = None
    chart_type: str = "bar"
    config: dict | None = None
    fetched_at: str | None = None
    cached: bool = False
    # Rows the query actually produced, which may exceed len(rows) — see
    # MAX_CHART_ROWS. Reported so a truncated chart is never presented as whole.
    row_count: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "chart_id": self.chart_id,
            "columns": self.columns,
            "rows": self.rows,
            "needs_attention": self.needs_attention,
            "message": self.message,
            "chart_type": self.chart_type,
            "config": self.config,
            "fetched_at": self.fetched_at,
            "cached": self.cached,
            "row_count": self.row_count,
            "truncated": self.truncated,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_key(job: ChartJob) -> str:
    """Identity of a chart *result*: tenant + execution target + the exact SQL.

    Deliberately not keyed on chart id — two charts with identical SQL against the
    same database share a result, and editing a chart's SQL misses immediately.
    The username is included because row-level permissions can make the same query
    return different rows for different database users.

    ``organization_id`` is included even though it is, strictly, redundant: two
    orgs with identical connection details would read identical rows anyway. It is
    there because "can org A ever see org B's cached data?" should answer **no**,
    not "no, because of an argument about connection tuples". For an ERP product
    the isolation boundary is worth more than the cache hits it costs.
    """
    blob = "\x1f".join([
        str(job.organization_id), str(job.engine), str(job.host), str(job.port),
        str(job.database), str(job.username), job.sql,
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _get_cache():
    """The shared Redis cache, or None when unavailable.

    Redis is optional everywhere else in the engine and stays optional here: with
    no server the dashboard simply runs every query live, which is slower but
    strictly *more* correct. A cache outage must never take the feature down.
    """
    try:
        from dbbuddy_core.context_store import _get_cache as shared_cache

        cache = shared_cache()
        return cache if getattr(cache, "connected", False) else None
    except Exception:  # noqa: BLE001 — caching is best-effort by design
        logger.debug("Chart result cache unavailable", exc_info=True)
        return None


def execute_chart(job: ChartJob, *, use_cache: bool = True,
                  ttl: int = CACHE_TTL_SECONDS) -> ChartRunResult:
    """Run one chart's SQL live (or serve a fresh-enough cached result).

    Never raises: an unreachable database, a revoked credential, or a query that
    no longer matches the schema all come back as ``needs_attention`` so one bad
    chart cannot blank the dashboard around it.
    """
    from dbbuddy_core.safety import classify_query_safety

    base = dict(chart_id=job.chart_id, chart_type=job.chart_type, config=job.config)

    # Defense in depth, identical to the published-report path: a client-triggered
    # refresh must only ever issue a read.
    category, _ = classify_query_safety(job.sql)
    if category != "read":
        return ChartRunResult(ok=False, needs_attention=True, fetched_at=_now_iso(),
                              message="This chart's query is not read-only and was blocked.",
                              **base)

    if not job.has_source:
        return ChartRunResult(ok=False, needs_attention=True, fetched_at=_now_iso(),
                              message="This chart needs attention — its data source is no longer available.",
                              **base)

    key = _cache_key(job)
    cache = _get_cache() if use_cache else None
    if cache is not None:
        try:
            hit = cache.get(_CACHE_PREFIX, key)
        except Exception:  # noqa: BLE001
            hit = None
        if isinstance(hit, dict) and "rows" in hit:
            rows = hit.get("rows", [])
            return ChartRunResult(
                ok=True, columns=hit.get("columns", []), rows=rows,
                fetched_at=hit.get("fetched_at"), cached=True,
                row_count=hit.get("row_count", len(rows)),
                truncated=hit.get("truncated", False), **base,
            )

    from dbbuddy_core.db import connect_db
    from dbbuddy_core.erp_concurrency import ERPBusy, query_slot
    from dbbuddy_core.query import execute_query

    try:
        # Backpressure: never let DB Buddy's own popularity take a customer's ERP
        # down. Beyond the per-target ceiling this waits, then gives up cleanly.
        with query_slot(job.engine, job.host, job.port, job.database):
            conn = connect_db(job.host, job.username, job.password, job.database,
                              engine=job.engine, port=job.port)
            if conn is None:
                raise RuntimeError("connection failed")
            try:
                conn.autocommit = True  # fresh read, no lingering snapshot
            except Exception:
                pass
            try:
                results = execute_query(conn, job.sql)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    except ERPBusy as exc:
        # A healthy database that is simply saturated — say so, rather than
        # implying the chart or the connection is broken.
        logger.info("Chart %s deferred: %s", job.chart_id, exc)
        return ChartRunResult(ok=False, needs_attention=True, fetched_at=_now_iso(),
                              message=str(exc), **base)
    except Exception:  # noqa: BLE001 — upstream DB problem, reported not raised
        logger.info("Chart %s could not be refreshed", job.chart_id, exc_info=True)
        return ChartRunResult(ok=False, needs_attention=True, fetched_at=_now_iso(),
                              message="This chart needs attention — the query could not be run.",
                              **base)

    columns = list(results[0].keys()) if results else []
    fetched_at = _now_iso()

    # Cap before anything else touches the rows — serialization, the cache write,
    # and the response all work on the capped list, so a runaway query costs a
    # bounded amount of memory rather than however much it happened to return.
    total_rows = len(results)
    truncated = total_rows > MAX_CHART_ROWS
    if truncated:
        logger.info("Chart %s returned %d rows; truncated to %d for rendering.",
                    job.chart_id, total_rows, MAX_CHART_ROWS)
        results = results[:MAX_CHART_ROWS]

    if cache is not None and total_rows <= MAX_CACHEABLE_ROWS:
        try:
            cache.set(_CACHE_PREFIX, key,
                      {"columns": columns, "rows": results, "fetched_at": fetched_at,
                       "row_count": total_rows, "truncated": truncated},
                      ttl=ttl)
        except Exception:  # noqa: BLE001 — a failed write must not fail the read
            logger.debug("Could not cache chart result", exc_info=True)

    return ChartRunResult(ok=True, columns=columns, rows=results,
                          fetched_at=fetched_at, cached=False,
                          row_count=total_rows, truncated=truncated, **base)


def execute_charts(jobs: list[ChartJob], *, use_cache: bool = True,
                   ttl: int = CACHE_TTL_SECONDS) -> list[ChartRunResult]:
    """Run several charts concurrently, preserving input order.

    Order is preserved because a dashboard's charts are ordered by the analyst;
    returning them in completion order would shuffle the narrative.
    """
    if not jobs:
        return []
    if len(jobs) == 1:
        return [execute_chart(jobs[0], use_cache=use_cache, ttl=ttl)]

    workers = min(len(jobs), MAX_PARALLEL_QUERIES)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chartrun") as pool:
        # ``map`` keeps input order and re-raises inside the caller's thread —
        # but execute_chart is total, so there is nothing to re-raise.
        return list(pool.map(lambda j: execute_chart(j, use_cache=use_cache, ttl=ttl), jobs))
