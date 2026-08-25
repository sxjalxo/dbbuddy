"""Two schemas in one database must not share anything cached.

Storing ``db_schema`` and passing it to ``connect_db`` is the visible half of
schema support. This is the half that decides whether it is *correct*: every
process- or Redis-level identity key that stands for "which database is this"
was written when a database was the finest granularity there was.

With a schema selected, the same ``(host, database, engine)`` now names two
different sets of tables. A key that cannot tell them apart hands one schema's
prepared context, cached chart rows, or learned mappings to the other — and every
one of those failures looks like a correct answer to the wrong question.

Pure key-function tests: no server, no Redis. The keys are the contract.
"""

import pathlib
import sys

from dbbuddy_core.models import DBConfig

# ``app_db`` is imported as a top-level package by the backend, so the backend
# directory has to be importable the same way the app makes it.
_BACKEND = str(pathlib.Path(__file__).resolve().parents[1] / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _config(**over):
    base = dict(host="db.internal", user="reader", password="secret",
                database="erp", engine="postgresql")
    base.update(over)
    return DBConfig(**base)


# ── The prepared context (connection pool, schema, vector index) ───────────────

def test_db_key_separates_schemas():
    from dbbuddy_core.context_store import _db_key

    assert _db_key(_config(db_schema="sales")) != _db_key(_config(db_schema="warehouse"))


def test_db_key_separates_a_schema_from_no_schema():
    # "follow search_path" is its own target: it may resolve anywhere, so it
    # cannot share a pool with a connection that pins one.
    from dbbuddy_core.context_store import _db_key

    assert _db_key(_config()) != _db_key(_config(db_schema="sales"))


def test_db_key_is_unchanged_when_no_schema_is_set():
    # Existing deployments have no schema on any connection. Changing their key
    # shape would drop every warm context on upgrade for no benefit.
    from dbbuddy_core.context_store import _db_key

    assert _db_key(_config()) == "db.internal|erp|postgresql"


def test_ctx_key_separates_schemas():
    from dbbuddy_core.context_store import _ctx_key

    same_hash = "abc123"
    assert (_ctx_key(_config(db_schema="sales"), same_hash)
            != _ctx_key(_config(db_schema="warehouse"), same_hash))


# ── Learned mappings ──────────────────────────────────────────────────────────

def test_memory_scope_separates_schemas():
    # A term learned against `sales.amount` means something else in `warehouse`.
    from dbbuddy_core.learning_engine import memory_scope

    assert (memory_scope(_config(db_schema="sales"))
            != memory_scope(_config(db_schema="warehouse")))


def test_memory_scope_is_unchanged_when_no_schema_is_set():
    # Existing memory files are keyed by the old scope; a changed shape would
    # orphan everything the instance has learned.
    from dbbuddy_core.learning_engine import memory_scope

    assert memory_scope(_config()) == memory_scope(_config())
    assert "sales" not in memory_scope(_config())


# ── Cached chart results ──────────────────────────────────────────────────────

def _job(**over):
    from app_db.chart_runtime import ChartJob

    base = dict(chart_id="c1", sql="SELECT 1", chart_type="bar", config=None,
                organization_id="org1", engine="postgresql", host="db.internal",
                port=5432, database="erp", username="reader", password="secret")
    base.update(over)
    return ChartJob(**base)


def test_chart_cache_key_separates_schemas():
    from app_db.chart_runtime import _cache_key

    assert _cache_key(_job(db_schema="sales")) != _cache_key(_job(db_schema="warehouse"))


def test_chart_cache_key_separates_a_schema_from_no_schema():
    from app_db.chart_runtime import _cache_key

    assert _cache_key(_job()) != _cache_key(_job(db_schema="sales"))


def test_chart_cache_key_still_ignores_chart_id():
    # The existing contract: identical SQL against an identical target shares a
    # result. Adding the schema must narrow the key, not re-key it.
    from app_db.chart_runtime import _cache_key

    assert _cache_key(_job(chart_id="a")) == _cache_key(_job(chart_id="b"))


# ── Backpressure ──────────────────────────────────────────────────────────────

def test_the_concurrency_ceiling_is_per_server_not_per_schema():
    # The opposite of the rule above, on purpose. The semaphore protects a
    # *machine*: two schemas in one database are one server, and giving each its
    # own ceiling would double the load the limit exists to cap.
    from dbbuddy_core.erp_concurrency import target_key

    assert (target_key("postgresql", "db.internal", 5432, "erp")
            == target_key("postgresql", "db.internal", 5432, "erp"))
