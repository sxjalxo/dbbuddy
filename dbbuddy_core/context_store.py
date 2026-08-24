"""Process-level cache of prepared per-database context.

The heavy setup work — opening a DB connection, fetching the schema,
AI-refining the semantic layer, building the vector index and the relationship
graph — used to run on *every* /query request. That setup was the dominant cost
(~6s of an ~8s request), dwarfing the deterministic planning/execution stages
(~3ms combined).

This module builds that context **once** per ``(host, database, schema_hash)``
and reuses it across requests:

* DB connection is kept alive and ``ping(reconnect=True)``-ed per use.
* Semantic layer is persisted to disk so the expensive AI labeling survives
  process restarts and is never recomputed unless the schema changes.
* The ChromaDB client is a process singleton and the index is built once.
* The relationship graph is computed once and reused.

By default the context is built on "Analyze Schema" (``rebuild=True``) and lazily
on the first query for a database. Queries reuse the cached context without
re-fetching the schema; click Analyze Schema to refresh after a schema change.
"""

import hashlib
import json
import os
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import dbbuddy_core.db as db_module
import dbbuddy_core.mapping as mapping_module
import dbbuddy_core.schema as schema_module
from dbbuddy_core.ai import ai_refine
from dbbuddy_core.cache import Cache
from dbbuddy_core.logger import get_logger
from dbbuddy_core.models import DBConfig
from dbbuddy_core.relationship_graph import build_relationship_graph
from dbbuddy_core.vector_store import VectorStore

logger = get_logger()

# Persisted semantic layers live here, keyed by database + schema_hash.
CACHE_DIR = Path(__file__).parent.parent / ".dbbuddy_cache"

# Idle connections kept per database. Small because this is typically a
# single-user tool; the pool exists for thread-safety (FastAPI runs sync
# endpoints in a threadpool) and to avoid a fresh TCP+auth handshake per request.
POOL_SIZE = 5


class _ConnectionPool:
    """Tiny thread-safe pool layered over ``db.connect_db``.

    Built on ``connect_db`` rather than mysql.connector pooling so the existing
    mock seam (tests patch ``dbbuddy_core.db.connect_db``) keeps working, while
    still reusing connections instead of reconnecting on every request.
    """

    def __init__(self, config: DBConfig, maxsize: int = POOL_SIZE):
        self._config = config
        self._idle: "queue.LifoQueue" = queue.LifoQueue(maxsize=maxsize)

    def _new(self):
        c = self._config
        conn = db_module.connect_db(c.host, c.user, c.password, c.database, engine=c.engine,
                                    port=c.port, db_schema=getattr(c, "db_schema", None))
        if conn is None:
            raise db_module.DatabaseUnavailableError("Unable to connect to the database.")
        try:
            conn.autocommit = True
        except Exception:
            pass
        return conn

    def acquire(self):
        try:
            conn = self._idle.get_nowait()
        except queue.Empty:
            return self._new()

        # Revalidate a reused connection; replace it if the server dropped it.
        try:
            conn.ping()
            conn.autocommit = True
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return self._new()

    def release(self, conn) -> None:
        try:
            self._idle.put_nowait(conn)
        except queue.Full:
            try:
                conn.close()
            except Exception:
                pass

    def close(self) -> None:
        """Close all idle connections held by the pool."""
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except Exception:
                pass


@dataclass
class DBContext:
    """Everything the query pipeline needs that is expensive to build."""

    key: str
    schema: dict
    schema_hash: str
    semantic: dict
    vector_store: VectorStore
    relationship_graph: dict
    cache: Cache
    pool: _ConnectionPool
    build_ms: float = 0.0
    analyzed: bool = False  # whether an AI-refined layer is persisted for this schema
    dialect: object = None  # Dialect instance for this database's engine
    # {table: {column: raw_sql_type}} — powers type-aware filter comparisons.
    column_types: dict = field(default_factory=dict)
    # {table: {column: semantic_role}} — deterministic role prior (Phase A);
    # lets the planner ground literals by column meaning, not sentence position.
    column_roles: dict = field(default_factory=dict)
    # {value_lower: [[table, column], ...]} — sampled distinct values of low-
    # cardinality dimension columns (Phase C); lets a literal like "Pune" bind to
    # the column that actually holds it (city) instead of a name column.
    value_index: dict = field(default_factory=dict)
    # {table: [(fk_col, ref_table, ref_col), ...]} — declared FKs, for the join graph.
    foreign_keys: dict = field(default_factory=dict)
    # {table: [pk_col, ...]} — declared primary keys. Lets the planner anchor a
    # COUNT/GROUP BY on a table's real key instead of assuming a surrogate ``id``.
    primary_keys: dict = field(default_factory=dict)

    @contextmanager
    def connection(self):
        """Borrow a pooled connection under the per-target concurrency cap.

        Connections run with autocommit on so every SELECT sees fresh data
        rather than a stale snapshot held open by a long-lived connection.

        The slot is acquired *before* the pool because ``_ConnectionPool.maxsize``
        bounds only how many connections are kept **idle** — ``acquire()`` opens a
        new one whenever the idle queue is empty, so N concurrent requests opened N
        connections against the customer's ERP with no ceiling at all. The
        ``query_slot`` semaphore is that ceiling: concurrent borrows per physical
        target are capped, which in turn caps live connections, and work beyond the
        cap waits (bounded) instead of piling on. Without this the whole query path
        — the one users actually wait on — bypassed the backpressure that
        ``erp_concurrency`` exists to provide, which only the dashboard path used.
        """
        from dbbuddy_core.erp_concurrency import query_slot

        c = self._config_for_slot()
        with query_slot(c[0], c[1], c[2], c[3]):
            conn = self.pool.acquire()
            try:
                yield conn
            finally:
                self.pool.release(conn)

    def _config_for_slot(self):
        """(engine, host, port, database) identifying the physical target."""
        cfg = self.pool._config
        return cfg.engine, cfg.host, cfg.port, cfg.database


# Module state. A single lock guards the maps, cache, and metrics.
#   _pools:    one connection pool per database, keyed by host|database.
#   _contexts: one prepared context per *schema version*, keyed by
#              host|database|schema_hash — so a schema change yields a new key
#              and never serves a stale graph/index.
_pools: dict[str, _ConnectionPool] = {}
_contexts: dict[str, DBContext] = {}
_lock = threading.RLock()
_shared_cache: Optional[Cache] = None

# In-flight schema fetches, keyed by database. Every query re-fetches the schema
# to resolve its hash — that is deliberate, and it is what keeps a cached context
# from ever being served for a schema that has since changed. But N concurrent
# queries against the same database were each issuing their own round-trip, so
# the cost grew with load precisely when it could least be afforded (measured at
# 76 ms of a 95 ms query with 8 threads, ~80% of the total).
#
# Callers that arrive while a fetch is already running now wait for it and share
# the answer. Nothing is cached past the fetch and no staleness window is
# introduced: every sharer receives a schema read at essentially the same instant
# its own read would have happened. Only the redundancy is removed.
_schema_flights: dict[str, "_SchemaFlight"] = {}

# Upper bound on how long a sharer waits before giving up and fetching for
# itself. Bounds the blast radius of a leader that hangs; it is not a timeout on
# the fetch, which the connection's own socket timeout still governs.
FLIGHT_WAIT_SECONDS = 30.0

# Short-TTL cache of the resolved schema per DB. Resolving the context re-reads
# the schema on every query to detect a DDL change (the schema-aware cache key),
# which on a wide ERP schema is the largest per-query cost (~65 ms for 465
# columns) and is paid once per chart — so opening a 40-chart dashboard re-reads
# the same schema 40 times in a burst. Caching it for a few seconds collapses
# that burst to one read; a schema change is still picked up within the TTL, and
# an explicit Analyze/rebuild invalidates it immediately. TTL<=0 disables the
# cache (re-read every time, the prior behavior).
_schema_cache: dict[str, tuple[dict, float]] = {}
SCHEMA_CACHE_TTL_SECONDS = float(os.getenv("SCHEMA_CACHE_TTL_SECONDS", "5"))


class _SchemaFlight:
    """One in-progress schema fetch that later arrivals can wait on."""

    __slots__ = ("done", "schema", "rich", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.schema: Optional[dict] = None
        self.rich: object = None
        self.error: Optional[BaseException] = None


_metrics = {
    "context_hits": 0,
    "schema_fetches": 0,
    "schema_fetches_shared": 0,
    "schema_fetches_cached": 0,
    "context_builds": 0,
    "last_build_ms": None,
    "total_build_ms": 0.0,
    "last_cold_query_ms": None,
    "last_warm_query_ms": None,
}


def compute_schema_hash(schema: dict) -> str:
    """Stable hash of a schema, independent of Redis availability."""
    return hashlib.md5(json.dumps(schema, sort_keys=True).encode()).hexdigest()


def _db_key(config: DBConfig) -> str:
    return f"{config.host}|{config.database}|{config.engine}"


def _ctx_key(config: DBConfig, schema_hash: str) -> str:
    return f"{config.host}|{config.database}|{schema_hash}"


def _get_cache() -> Cache:
    """Return the process-wide Redis cache client (created once)."""
    global _shared_cache
    if _shared_cache is None:
        _shared_cache = Cache()
    return _shared_cache


def _get_pool(config: DBConfig, *, fresh: bool = False) -> _ConnectionPool:
    """Return the connection pool for a database, creating it if needed.

    ``fresh=True`` (used on rebuild/Analyze) discards any existing pool so
    changed credentials take effect and a new connection is actually opened.
    """
    k = _db_key(config)
    if fresh:
        old = _pools.pop(k, None)
        if old is not None:
            old.close()
    pool = _pools.get(k)
    if pool is None:
        pool = _ConnectionPool(config)
        _pools[k] = pool
    return pool


def builds_count() -> int:
    """Number of context builds so far (used to detect cold vs warm queries)."""
    return _metrics["context_builds"]


def _semantic_path(config: DBConfig, schema_hash: str) -> Path:
    db_id = hashlib.md5(f"{config.host}|{config.database}".encode()).hexdigest()[:12]
    return CACHE_DIR / f"semantic_{db_id}_{schema_hash}.json"


def _value_index_path(config: DBConfig, schema_hash: str) -> Path:
    db_id = hashlib.md5(f"{config.host}|{config.database}".encode()).hexdigest()[:12]
    return CACHE_DIR / f"valueidx_{db_id}_{schema_hash}.json"


def _load_json(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Failed to read {path.name}: {exc}")
    return None


def _persist_json(path: Path, data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Failed to persist {path.name}: {exc}")


def _load_persisted_semantic(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Failed to read persisted semantic layer: {exc}")
    return None


def _persist_semantic(path: Path, semantic: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(semantic), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Failed to persist semantic layer: {exc}")


def _build_semantic(config: DBConfig, schema: dict, schema_hash: str, force_refine: bool) -> dict:
    """Build the semantic layer.

    The LLM refinement (``ai_refine``) is the multi-second cost, so it runs ONLY
    on an explicit Analyze Schema (``force_refine=True``) and the result is
    persisted. The query path (``force_refine=False``) never calls the LLM: it
    loads the persisted AI layer if Analyze produced one, otherwise it uses the
    cheap rule-based mapping. This keeps every query fast (cold or warm); only
    Analyze pays the AI cost, once.
    """
    path = _semantic_path(config, schema_hash)

    if not force_refine:
        cached = _load_persisted_semantic(path)
        if cached:
            logger.info("Loaded persisted semantic layer — skipping AI refinement")
            return cached
        # No persisted AI layer yet — use rule-based and do NOT block on the LLM.
        logger.info("No persisted semantic layer — using rule-based mapping (run Analyze Schema for AI labels)")
        return mapping_module.map_schema(schema)

    # Analyze Schema: pay the AI cost once and persist the result.
    semantic = mapping_module.map_schema(schema)
    if config.ai:
        chain = getattr(config, "ai_provider_chain", None)
        label = chain[0].name if chain else config.ai_provider
        logger.info(f"Refining semantic layer with AI provider '{label}'")
        semantic = ai_refine(semantic, provider=config.ai_provider, schema=schema, provider_chain=chain)
    _persist_semantic(path, semantic)
    return semantic


def _read_schema(config: DBConfig, pool: "_ConnectionPool") -> tuple[dict, object]:
    """One physical schema read that serves both purposes.

    Reads the RICH schema (types, PKs, FKs) and derives the plain
    ``{table: [cols]}`` form from it, so a build does not read the schema twice —
    once plain for the cache key, once rich for column meta. The plain projection
    is byte-identical to ``fetch_schema`` (same tables, same column order), so the
    schema hash — and every layer/index keyed by it — is unchanged.

    Rich introspection is best-effort: if it fails or the engine can't provide it,
    fall back to the plain ``fetch_schema`` and return ``rich=None`` so the build
    degrades to text-only comparisons and the naming-heuristic join graph, exactly
    as before. Raises ``DatabaseUnavailableError`` if even the plain read fails.
    """
    from dbbuddy_core.dialects import get_dialect

    dialect = get_dialect(config.engine)
    with _pool_connection(pool) as conn:
        rich = None
        try:
            rich = dialect.fetch_schema_rich(conn)
        except Exception:  # noqa: BLE001 — degrade to plain, never block a query
            rich = None
        if rich is not None and getattr(rich, "tables", None):
            schema = {t: [c.name for c in m.columns] for t, m in rich.tables.items()}
            if schema:
                return schema, rich
        schema = schema_module.fetch_schema(conn)
    if schema is None:
        raise db_module.DatabaseUnavailableError("Unable to fetch schema from the database.")
    return schema, None


def _resolve_schema(config: DBConfig, pool: "_ConnectionPool") -> dict:
    """Plain schema for the cache key (drops the rich half). See :func:`_resolve_schema_ex`."""
    return _resolve_schema_ex(config, pool)[0]


def _resolve_schema_ex(config: DBConfig, pool: "_ConnectionPool") -> tuple[dict, object]:
    """Resolve the schema, served from a short-TTL cache when fresh.

    Returns ``(plain_schema, rich_or_None)``. ``rich`` is present only when this
    call performed a fresh read (a cache miss); a cache hit returns
    ``(plain, None)``. The build reuses ``rich`` so a cold build reads the schema
    once, not twice. The cache collapses a burst (a dashboard opening N charts)
    into a single read, staleness bounded by ``SCHEMA_CACHE_TTL_SECONDS``.
    """
    key = _db_key(config)
    if SCHEMA_CACHE_TTL_SECONDS > 0:
        with _lock:
            hit = _schema_cache.get(key)
            if hit is not None and (time.monotonic() - hit[1]) < SCHEMA_CACHE_TTL_SECONDS:
                _metrics["schema_fetches_cached"] += 1
                return hit[0], None
    schema, rich = _resolve_schema_flight(config, pool)
    if SCHEMA_CACHE_TTL_SECONDS > 0:
        with _lock:
            _schema_cache[key] = (schema, time.monotonic())
    return schema, rich


def _resolve_schema_flight(config: DBConfig, pool: "_ConnectionPool") -> tuple[dict, object]:
    """Single-flight the schema read: concurrent callers for one DB share a read.

    Raises ``DatabaseUnavailableError`` if the schema cannot be read — the same
    contract as fetching directly.
    """
    key = _db_key(config)

    with _lock:
        flight = _schema_flights.get(key)
        leader = flight is None
        if leader:
            flight = _SchemaFlight()
            _schema_flights[key] = flight
        else:
            _metrics["schema_fetches_shared"] += 1

    if not leader:
        if flight.done.wait(FLIGHT_WAIT_SECONDS) and flight.error is None and flight.schema is not None:
            return flight.schema, flight.rich
        # Leader hung, failed, or came back empty — fetch our own.
        with _lock:
            _metrics["schema_fetches"] += 1
        return _read_schema(config, pool)

    try:
        with _lock:
            _metrics["schema_fetches"] += 1
        flight.schema, flight.rich = _read_schema(config, pool)
    except BaseException as exc:
        flight.error = exc
        raise
    finally:
        with _lock:
            _schema_flights.pop(key, None)
        flight.done.set()

    if flight.schema is None:
        raise db_module.DatabaseUnavailableError("Unable to fetch schema from the database.")
    return flight.schema, flight.rich


def get_context(config: DBConfig, *, rebuild: bool = False) -> DBContext:
    """Return the prepared context for ``config``, building it if needed."""
    ctx, _ = get_context_ex(config, rebuild=rebuild)
    return ctx


def get_context_ex(config: DBConfig, *, rebuild: bool = False) -> tuple[DBContext, bool]:
    """Like :func:`get_context` but also returns whether a build happened.

    The context is keyed by ``(host, database, schema_hash)``. Every call does a
    lightweight schema fetch to resolve the current hash, so a schema change
    produces a new key and a cached context is never served for a stale schema
    (which would otherwise yield wrong joins/SQL). The expensive work — semantic
    layer, vector index, relationship graph — is still reused whenever the schema
    is unchanged.

    Args:
        config: Database configuration.
        rebuild: Force a rebuild even if a context for the current schema exists
            (used by "Analyze Schema"): re-run AI refinement, re-index, refresh.

    Returns:
        ``(context, built)`` where ``built`` is True when this call (re)built.

    Concurrency: the heavy build (AI refinement, indexing) runs WITHOUT holding
    the global lock, so a fast rule-based query is never blocked by a slow
    background Analyze. The lock only guards the short dict/metrics sections.
    This is what makes the "query instantly while AI analyzes in background"
    UX actually instant.
    """
    # Resolve the pool and current schema (a quick DB round-trip), then the key.
    with _lock:
        pool = _get_pool(config, fresh=rebuild)

    # One read resolves the key (plain) and, on a miss, also hands back the rich
    # schema the build needs — so a cold build reads the schema once, not twice.
    schema, rich = _resolve_schema_ex(config, pool)
    schema_hash = compute_schema_hash(schema)
    key = _ctx_key(config, schema_hash)

    with _lock:
        existing = _contexts.get(key)
        # Cache hit: same schema, not forcing a rebuild.
        if existing is not None and not rebuild:
            existing.pool = pool  # adopt the (possibly refreshed) pool
            _metrics["context_hits"] += 1
            return existing, False

    # Build (new schema / first time) or rebuild (Analyze Schema). Done OUTSIDE
    # the lock. Concurrent callers for the same new key may both build (e.g. a
    # query builds the fast rule-based layer while Analyze builds the AI layer);
    # last write wins, which is the desired eventual-consistency behavior.
    t0 = time.time()
    semantic = _build_semantic(config, schema, schema_hash, force_refine=rebuild)

    cache = _get_cache()
    vector_store = VectorStore(cache=cache, schema_hash=schema_hash)
    # On Analyze (rebuild) re-embed so the index reflects refreshed terms.
    vector_store.index_schema(schema, semantic, force=rebuild)

    from dbbuddy_core.dialects import get_dialect as _get_dialect
    dialect = _get_dialect(config.engine)

    # Rich introspection: column types (type-aware comparisons) + declared foreign
    # keys (authoritative join graph). Best-effort — if unavailable we degrade to
    # text-only equality and the naming-heuristic graph, so a query is never blocked.
    # ``rich`` is normally already in hand from schema resolution (one read); it is
    # only re-fetched here when the plain schema was served from the short-TTL
    # cache (so no rich came with it) yet a build is still needed.
    column_types: dict = {}
    foreign_keys: dict = {}
    primary_keys: dict = {}
    try:
        if rich is None:
            with _pool_connection(pool) as conn:
                rich = dialect.fetch_schema_rich(conn)
        column_types = {
            tname: {c.name: c.data_type for c in tmeta.columns}
            for tname, tmeta in rich.tables.items()
        }
        foreign_keys = {
            tname: [(fk.column, fk.referenced_table, fk.referenced_column)
                    for fk in tmeta.foreign_keys]
            for tname, tmeta in rich.tables.items() if tmeta.foreign_keys
        }
        # Declared primary keys — the join graph uses them to relate tables whose
        # key columns carry no ``_id`` suffix (warehouse/legacy schemas where the
        # child column is literally the parent's key name).
        primary_keys = {
            tname: tmeta.primary_keys()
            for tname, tmeta in rich.tables.items() if tmeta.primary_keys()
        }
    except Exception:
        column_types, foreign_keys, primary_keys = {}, {}, {}

    # Semantic-role map used to ground literals by column meaning, not position.
    # Phase A: deterministic prior from name + type heuristics (no AI). Phase B:
    # overlay roles derived from the AI column classification already present in
    # `semantic` — but only where the prior was a weak default, so a confident
    # structural signal is never overridden and a missing AI label degrades to
    # the prior. Both layers are cached on the context; see docs/SEMANTIC_ROLES.md.
    from dbbuddy_core.semantic_roles import classify_roles, overlay_ai_roles
    column_roles = classify_roles(schema, column_types or None)
    overlay_ai_roles(column_roles, semantic)

    # Dimension value index (Phase C) — data-driven literal grounding. Sampling
    # hits the DB (one DISTINCT per dimension column), so it runs only on an
    # explicit Analyze (rebuild) and is persisted per schema_hash; a lazy query
    # build loads the persisted index (or an empty one, degrading to Phase B).
    vpath = _value_index_path(config, schema_hash)
    if rebuild:
        from dbbuddy_core.column_values import build_value_index
        try:
            value_index = build_value_index(
                lambda: _pool_connection(pool), schema, column_roles, dialect
            )
        except Exception:  # noqa: BLE001 — sampling must never block Analyze
            value_index = {}
        _persist_json(vpath, value_index)
    else:
        value_index = _load_json(vpath) or {}

    # Join graph from declared FKs when available; heuristic fallback otherwise.
    relationship_graph = build_relationship_graph(schema, foreign_keys or None,
                                                  primary_keys or None)
    build_ms = round((time.time() - t0) * 1000, 1)

    ctx = DBContext(
        key=key,
        schema=schema,
        schema_hash=schema_hash,
        semantic=semantic,
        vector_store=vector_store,
        relationship_graph=relationship_graph,
        cache=cache,
        pool=pool,
        build_ms=build_ms,
        analyzed=_semantic_path(config, schema_hash).exists(),
        dialect=dialect,
        column_types=column_types,
        column_roles=column_roles,
        value_index=value_index,
        foreign_keys=foreign_keys,
        primary_keys=primary_keys,
    )

    with _lock:
        _metrics["context_builds"] += 1
        _metrics["last_build_ms"] = build_ms
        _metrics["total_build_ms"] += build_ms
        _contexts[key] = ctx
    logger.info(f"Built DB context for {key} (schema_hash={schema_hash[:8]}, {build_ms}ms)")
    return ctx, True


@contextmanager
def _pool_connection(pool: _ConnectionPool):
    """Borrow a connection from a pool (used during context build)."""
    conn = pool.acquire()
    try:
        yield conn
    finally:
        pool.release(conn)


def rebuild(config: DBConfig) -> DBContext:
    """Force a full rebuild of a database's context (Analyze Schema / manual)."""
    # Drop the short-TTL schema cache so a rebuild always reads the live schema
    # (a just-applied DDL change must be seen now, not up to the TTL later).
    with _lock:
        _schema_cache.pop(_db_key(config), None)
    return get_context(config, rebuild=True)


def record_query_latency(latency_ms: float, *, cold: bool) -> None:
    """Record a query's end-to-end latency, tagged cold (built) or warm."""
    with _lock:
        if cold:
            _metrics["last_cold_query_ms"] = latency_ms
        else:
            _metrics["last_warm_query_ms"] = latency_ms


def get_metrics() -> dict:
    """Snapshot of context-cache and latency metrics."""
    with _lock:
        hits = _metrics["context_hits"]
        builds = _metrics["context_builds"]
        total = hits + builds
        return {
            "context_hits": hits,
            "context_builds": builds,
            "context_hit_rate": round(hits / total, 3) if total else None,
            "last_build_ms": _metrics["last_build_ms"],
            "avg_build_ms": round(_metrics["total_build_ms"] / builds, 1) if builds else None,
            "last_cold_query_ms": _metrics["last_cold_query_ms"],
            "last_warm_query_ms": _metrics["last_warm_query_ms"],
            "schema_fetches": _metrics["schema_fetches"],
            "schema_fetches_shared": _metrics["schema_fetches_shared"],
            "schema_fetches_cached": _metrics["schema_fetches_cached"],
            "schema_cache_ttl_s": SCHEMA_CACHE_TTL_SECONDS,
            "cached_schema_versions": len(_contexts),
            "cached_databases": sorted({k.rsplit("|", 1)[0] for k in _contexts}),
        }


def get_status(config: DBConfig) -> dict:
    """Report readiness for a database without building the heavy context.

    Does a lightweight schema fetch to resolve the current schema version, then
    reports whether the semantic layer has been analyzed (AI-refined + persisted)
    and whether the vector index is ready for it.
    """
    with _lock:
        pool = _get_pool(config)
        try:
            with _pool_connection(pool) as conn:
                schema = schema_module.fetch_schema(conn)
        except Exception as exc:
            return {"connected": False, "error": str(exc)}

        if schema is None:
            return {"connected": False, "error": "Unable to fetch schema."}

        schema_hash = compute_schema_hash(schema)
        key = _ctx_key(config, schema_hash)
        ctx = _contexts.get(key)
        semantic_persisted = _semantic_path(config, schema_hash).exists()
        vector_ready = ctx is not None and ctx.vector_store.indexed

        return {
            "connected": True,
            "database": config.database,
            "tables": len(schema),
            "schema_hash": schema_hash[:12],
            "schema_analyzed": semantic_persisted,
            "semantic_layer_ready": semantic_persisted or ctx is not None,
            "vector_index_ready": vector_ready,
            "context_cached": ctx is not None,
        }


def invalidate(config: DBConfig) -> None:
    """Drop all cached contexts and the pool for a database."""
    with _lock:
        prefix = _db_key(config) + "|"
        for k in [k for k in _contexts if k.startswith(prefix)]:
            _contexts.pop(k, None)
        _schema_cache.pop(_db_key(config), None)
        pool = _pools.pop(_db_key(config), None)
        if pool is not None:
            pool.close()


def reset() -> None:
    """Drop all cached contexts/pools and metrics. For test isolation/restarts."""
    with _lock:
        for pool in _pools.values():
            try:
                pool.close()
            except Exception:
                pass
        _pools.clear()
        _contexts.clear()
        _schema_flights.clear()
        _schema_cache.clear()
        _metrics.update({
            "context_hits": 0,
            "schema_fetches": 0,
            "schema_fetches_shared": 0,
            "schema_fetches_cached": 0,
            "context_builds": 0,
            "last_build_ms": None,
            "total_build_ms": 0.0,
            "last_cold_query_ms": None,
            "last_warm_query_ms": None,
        })
