"""Regression tests for the 2026-07-08 QA pass on the deterministic planner.

Covers three bugs found by exercising the pipeline against a live database:

1. "how many <table>" produced no COUNT — the aggregation keyword scan only
   matched single words, so multi-word phrases ("how many", spelled-out
   "number of" worked only via "number") never triggered COUNT.
2. "orders with amount greater than 100" produced a bogus aggregate — the
   schema-blind ``extract_having`` claimed the "greater than 100" text as a
   HAVING threshold (forcing a COUNT aggregation) even though the schema-driven
   filter extractor grounded the same comparison to a real column
   (``WHERE orders.amount > 100``).
3. Repeating a read query within the cache TTL returned no results — the
   response was cached *before* execution (``auto_executed: False``, no rows)
   and a cache hit returned that pre-execution snapshot verbatim. A hit must
   re-execute the cached SQL (fresh rows), and cached non-read responses must
   fall through to the full confirmation pipeline.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from dbbuddy_core.intent_builder import build_query_intent

SCHEMA = {
    "customers": ["id", "name", "email", "city"],
    "orders": ["id", "customer_id", "amount", "status", "order_date"],
}
COLUMN_TYPES = {
    "customers": {"id": "int", "name": "varchar", "email": "varchar", "city": "varchar"},
    "orders": {
        "id": "int", "customer_id": "int", "amount": "decimal",
        "status": "varchar", "order_date": "date",
    },
}


# ── 1. Multi-word COUNT phrases ───────────────────────────────────────────────

def test_how_many_phrase_maps_to_count():
    intent = build_query_intent(
        "how many orders are there", [], None, SCHEMA, column_types=COLUMN_TYPES
    )
    agg = intent.get("aggregation")
    assert agg is not None, "'how many' must trigger a COUNT aggregation"
    assert agg["function"] == "COUNT"
    assert agg["column"]["table"] == "orders"


def test_number_of_phrase_maps_to_count():
    intent = build_query_intent(
        "number of customers", [], None, SCHEMA, column_types=COLUMN_TYPES
    )
    agg = intent.get("aggregation")
    assert agg is not None and agg["function"] == "COUNT"


# ── 2. Grounded column comparison is a WHERE filter, not a HAVING ─────────────

def test_grounded_filter_is_not_treated_as_having():
    intent = build_query_intent(
        "orders with amount greater than 100", [], None, SCHEMA, column_types=COLUMN_TYPES
    )
    assert intent.get("having") is None, "column-grounded comparison must not become HAVING"
    assert intent.get("aggregation") is None, "no aggregation was asked for"
    assert {"column": "orders.amount", "operator": ">", "value": 100} in intent["filters"]


def test_ungrounded_threshold_still_becomes_having_count():
    # "more than 5 orders" grounds to no column — it is an aggregate threshold.
    intent = build_query_intent(
        "customers with more than 5 orders", [], None, SCHEMA, column_types=COLUMN_TYPES
    )
    assert intent.get("having") == {"operator": ">", "value": 5}
    agg = intent.get("aggregation")
    assert agg is not None and agg["function"] == "COUNT"


# ── 3. Cache hits re-execute reads / writes fall through ─────────────────────

class FakeCache:
    """Minimal stand-in for dbbuddy_core.cache.Cache (dict-backed, no Redis)."""

    def __init__(self):
        self.store = {}
        self.connected = True

    def _key(self, prefix, value, normalize, schema_hash):
        v = " ".join(value.lower().strip().split()) if normalize else value
        return f"{prefix}:{schema_hash}:{v}"

    def get(self, prefix, value, normalize=False, schema_hash=None):
        return self.store.get(self._key(prefix, value, normalize, schema_hash))

    def set(self, prefix, value, data, ttl=300, normalize=False, schema_hash=None):
        self.store[self._key(prefix, value, normalize, schema_hash)] = data
        return True


def _stub_context():
    vector_store = MagicMock()
    vector_store.search.return_value = [
        {"table": "customers", "column": "name", "type": "column", "score": 0.9},
        {"table": "customers", "column": "city", "type": "column", "score": 0.5},
    ]
    semantic = {
        t: {c: {"term": c, "source": "rule"} for c in cols} for t, cols in SCHEMA.items()
    }

    @contextmanager
    def connection():
        yield MagicMock()

    return SimpleNamespace(
        key="test|db|hash",
        schema=SCHEMA,
        schema_hash="testhash",
        semantic=semantic,
        vector_store=vector_store,
        relationship_graph={},
        cache=FakeCache(),
        column_types=COLUMN_TYPES,
        foreign_keys={},
        connection=connection,
        analyzed=True,
        dialect=None,
    )


def _patch_pipeline(monkeypatch, ctx, executor):
    import dbbuddy_core.orchestrator as orch

    monkeypatch.setattr(orch, "get_context", lambda config, **kw: ctx)
    monkeypatch.setattr(orch, "execute_query_safely", executor)
    # Keep the repo-level learned-memory file untouched by tests.
    monkeypatch.setattr(orch, "update_memory", lambda *a, **kw: None)
    # Signature takes the schema now, so an injected mapping learned against a
    # different database cannot name a table/column this one lacks.
    monkeypatch.setattr(
        orch, "enhance_query",
        lambda q, threshold=2, schema=None, scope=None: q)


def test_cache_hit_reexecutes_read_with_fresh_results(monkeypatch):
    from dbbuddy_core.models import DBConfig
    from dbbuddy_core.orchestrator import process_query

    ctx = _stub_context()
    rows = [{"count_id": 3}]
    calls = []

    def executor(conn, sql, params=None):
        calls.append(sql)
        return {"success": True, "results": list(rows), "truncated": False}

    _patch_pipeline(monkeypatch, ctx, executor)
    cfg = DBConfig(host="h", user="u", password="p", database="db", engine="mysql", ai=False)

    first = process_query(cfg, "how many customers are there")
    assert first.get("auto_executed") is True
    assert first.get("results") == [{"count_id": 3}]

    # The data changes between the two identical questions…
    rows[0] = {"count_id": 4}

    second = process_query(cfg, "how many customers are there")
    assert second.get("cached") is True, "second run should hit the query cache"
    assert second.get("auto_executed") is True, "cache hit must still execute the read"
    assert second.get("results") == [{"count_id": 4}], "cache hit must return fresh rows"
    assert len(calls) == 2, "the SQL must run on both requests"


def test_cache_hit_for_dangerous_query_falls_through_to_confirmation(monkeypatch):
    from dbbuddy_core.models import DBConfig
    from dbbuddy_core.orchestrator import process_query

    ctx = _stub_context()
    executor_calls = []

    def executor(conn, sql, params=None):
        executor_calls.append(sql)
        return {"success": True, "results": [], "truncated": False}

    _patch_pipeline(monkeypatch, ctx, executor)
    cfg = DBConfig(host="h", user="u", password="p", database="db", engine="mysql", ai=False)

    q = "delete customers from city Pune"
    first = process_query(cfg, q)
    assert first.get("auto_executed") is False
    assert first.get("requires_confirmation") is True

    second = process_query(cfg, q)
    # A cached dangerous-intent query must not be served as a pre-execution
    # snapshot: it goes through the full pipeline and is held again.
    assert second.get("auto_executed") is False
    assert second.get("requires_confirmation") is True
    assert not executor_calls, "a held query must never auto-execute"


def test_cache_hit_respects_auto_execute_reads_off(monkeypatch):
    from dbbuddy_core.models import DBConfig
    from dbbuddy_core.orchestrator import process_query

    ctx = _stub_context()

    def executor(conn, sql, params=None):
        return {"success": True, "results": [{"count_id": 3}], "truncated": False}

    _patch_pipeline(monkeypatch, ctx, executor)
    cfg = DBConfig(host="h", user="u", password="p", database="db", engine="mysql", ai=False)

    # Prime the cache with an executed read…
    first = process_query(cfg, "how many customers are there")
    assert first.get("auto_executed") is True

    # …then ask again with auto-execute off: the hit must not run the SQL.
    second = process_query(cfg, "how many customers are there", auto_execute_reads=False)
    assert second.get("auto_executed") is False
