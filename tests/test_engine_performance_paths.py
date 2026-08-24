"""Regression tests for the engine's caching and hot-path optimizations.

These cover behavior that is easy to regress silently because the system keeps
working when it breaks — it just gets slower, or loses a signal:

* the Redis client parking itself when the server disappears mid-process
* concurrent queries sharing one schema fetch instead of issuing N
* query responses carrying a *slice* of the semantic layer, with the
  AI-vs-rule-based signal preserved separately
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import dbbuddy_core.cache as cache_module
from dbbuddy_core import context_store
from dbbuddy_core.cache import Cache
from dbbuddy_core.models import DBConfig
from dbbuddy_core.orchestrator import _any_ai_labels


class _FakeRedisError(Exception):
    pass


def _cache_with_client(client) -> Cache:
    """A Cache wired to a stub client, bypassing the real constructor probe."""
    c = Cache.__new__(Cache)
    c.client = client
    c.connected = True
    c._consecutive_errors = 0
    c._blocked_until = 0.0
    return c


@pytest.fixture
def redis_errors(monkeypatch):
    """Make cache.py treat _FakeRedisError as a Redis failure."""
    fake = MagicMock()
    fake.RedisError = _FakeRedisError
    monkeypatch.setattr(cache_module, "redis", fake)
    return fake


class TestCacheAvailabilityGate:
    def test_stops_calling_redis_after_repeated_failures(self, redis_errors):
        client = MagicMock()
        client.get.side_effect = _FakeRedisError("connection reset")
        c = _cache_with_client(client)

        for _ in range(cache_module.ERROR_THRESHOLD):
            assert c.get("query", "show users") is None
        calls_at_threshold = client.get.call_count

        # Further lookups must not touch the socket at all — that is the whole
        # point: a dead Redis should cost nothing, not a timeout per request.
        for _ in range(5):
            assert c.get("query", "show users") is None
        assert client.get.call_count == calls_at_threshold

    def test_writes_are_gated_too(self, redis_errors):
        client = MagicMock()
        client.set.side_effect = _FakeRedisError("connection reset")
        c = _cache_with_client(client)

        for _ in range(cache_module.ERROR_THRESHOLD):
            assert c.set("query", "q", {"a": 1}) is False
        gated = client.set.call_count
        assert c.set("query", "q", {"a": 1}) is False
        assert client.set.call_count == gated

    def test_retries_after_cooldown_and_recovers(self, redis_errors, monkeypatch):
        client = MagicMock()
        client.get.side_effect = _FakeRedisError("down")
        c = _cache_with_client(client)
        for _ in range(cache_module.ERROR_THRESHOLD):
            c.get("query", "q")
        assert not c._available()

        # Pretend the cooldown elapsed; the server is back.
        monkeypatch.setattr(time, "monotonic", lambda: c._blocked_until + 1)
        client.get.side_effect = None
        client.get.return_value = '{"sql": "SELECT 1"}'

        assert c.get("query", "q") == {"sql": "SELECT 1"}
        # A success clears the gate rather than leaving it half-open forever.
        assert c._consecutive_errors == 0
        assert c._blocked_until == 0.0

    def test_a_success_resets_the_error_run(self, redis_errors):
        client = MagicMock()
        c = _cache_with_client(client)

        client.get.side_effect = _FakeRedisError("blip")
        c.get("query", "q")
        assert c._consecutive_errors == 1

        client.get.side_effect = None
        client.get.return_value = None
        c.get("query", "q")
        assert c._consecutive_errors == 0

    def test_corrupt_payload_is_not_treated_as_a_server_failure(self, redis_errors):
        client = MagicMock()
        client.get.return_value = "{not json"
        c = _cache_with_client(client)

        assert c.get("query", "q") is None
        # A bad value says nothing about the server's health.
        assert c._consecutive_errors == 0

    def test_unserializable_value_is_not_treated_as_a_server_failure(self, redis_errors):
        client = MagicMock()
        c = _cache_with_client(client)

        assert c.set("query", "q", {"fn": lambda: None}) is False
        assert c._consecutive_errors == 0
        client.set.assert_not_called()

    def test_clear_prefix_deletes_in_bounded_batches(self, redis_errors):
        client = MagicMock()
        total = cache_module.CLEAR_BATCH_SIZE * 2 + 7
        client.scan_iter.return_value = iter(f"plan:{i}" for i in range(total))
        c = _cache_with_client(client)

        assert c.clear_prefix("plan") is True
        assert client.delete.call_count == 3
        for call in client.delete.call_args_list:
            assert len(call.args) <= cache_module.CLEAR_BATCH_SIZE
        assert sum(len(call.args) for call in client.delete.call_args_list) == total


class TestSchemaFetchSingleFlight:
    def test_concurrent_resolvers_share_one_fetch(self):
        context_store.reset()
        config = DBConfig(host="h", user="u", password="p", database="d")

        started = threading.Event()
        release = threading.Event()
        fetches = []

        def slow_read(_config, _pool):
            fetches.append(1)
            started.set()
            release.wait(5)
            return {"users": ["id", "name"]}, None

        pool = MagicMock()
        pool.acquire.return_value = MagicMock()

        results = []
        with patch("dbbuddy_core.context_store._read_schema", side_effect=slow_read):
            leader = threading.Thread(
                target=lambda: results.append(context_store._resolve_schema(config, pool)))
            leader.start()
            assert started.wait(5), "leader never began fetching"

            followers = [
                threading.Thread(
                    target=lambda: results.append(context_store._resolve_schema(config, pool)))
                for _ in range(4)
            ]
            for t in followers:
                t.start()
            # Give the followers a moment to queue behind the in-flight fetch.
            time.sleep(0.1)
            release.set()
            leader.join(5)
            for t in followers:
                t.join(5)

        assert len(results) == 5
        assert all(r == {"users": ["id", "name"]} for r in results)
        # One round-trip served all five callers.
        assert len(fetches) == 1
        assert context_store.get_metrics()["schema_fetches_shared"] == 4

    def test_sequential_calls_within_ttl_share_cache(self, monkeypatch):
        """A burst within the TTL collapses to one read (dashboard opening N charts)."""
        context_store.reset()
        monkeypatch.setattr(context_store, "SCHEMA_CACHE_TTL_SECONDS", 5.0)
        config = DBConfig(host="h", user="u", password="p", database="d")
        pool = MagicMock()
        pool.acquire.return_value = MagicMock()

        reads = [({"users": ["id"]}, None), ({"users": ["id", "email"]}, None)]
        with patch("dbbuddy_core.context_store._read_schema", side_effect=reads) as read:
            first = context_store._resolve_schema(config, pool)
            second = context_store._resolve_schema(config, pool)

        # Second call served from the short-TTL cache: one physical read, same result.
        assert first == {"users": ["id"]}
        assert second == {"users": ["id"]}
        assert read.call_count == 1

    def test_cache_invalidation_forces_reread(self, monkeypatch):
        """Invalidation (or Analyze/rebuild) must re-read the live schema now, not after the TTL."""
        context_store.reset()
        monkeypatch.setattr(context_store, "SCHEMA_CACHE_TTL_SECONDS", 5.0)
        config = DBConfig(host="h", user="u", password="p", database="d")
        pool = MagicMock()
        pool.acquire.return_value = MagicMock()

        reads = [({"users": ["id"]}, None), ({"users": ["id", "email"]}, None)]
        with patch("dbbuddy_core.context_store._read_schema", side_effect=reads):
            first = context_store._resolve_schema(config, pool)
            context_store.invalidate(config)
            second = context_store._resolve_schema(config, pool)

        assert first == {"users": ["id"]}
        assert second == {"users": ["id", "email"]}

    def test_ttl_zero_disables_cache_each_call_fetches(self, monkeypatch):
        """TTL<=0 restores the prior behavior: every call re-reads the schema."""
        context_store.reset()
        monkeypatch.setattr(context_store, "SCHEMA_CACHE_TTL_SECONDS", 0.0)
        config = DBConfig(host="h", user="u", password="p", database="d")
        pool = MagicMock()
        pool.acquire.return_value = MagicMock()

        reads = [({"users": ["id"]}, None), ({"users": ["id", "email"]}, None)]
        with patch("dbbuddy_core.context_store._read_schema", side_effect=reads):
            first = context_store._resolve_schema(config, pool)
            second = context_store._resolve_schema(config, pool)

        assert first == {"users": ["id"]}
        assert second == {"users": ["id", "email"]}

    def test_failure_propagates_and_does_not_wedge_later_calls(self):
        context_store.reset()
        config = DBConfig(host="h", user="u", password="p", database="d")
        pool = MagicMock()
        pool.acquire.return_value = MagicMock()

        with patch("dbbuddy_core.context_store._read_schema", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                context_store._resolve_schema(config, pool)

        # The failed flight must have been cleared, not left for others to wait on.
        with patch("dbbuddy_core.context_store._read_schema", return_value=({"t": ["c"]}, None)):
            assert context_store._resolve_schema(config, pool) == {"t": ["c"]}

    def test_none_schema_raises_unavailable(self):
        context_store.reset()
        config = DBConfig(host="h", user="u", password="p", database="d")
        pool = MagicMock()
        pool.acquire.return_value = MagicMock()

        import dbbuddy_core.db as db_module
        # A read that cannot produce a schema raises DatabaseUnavailableError
        # (the plain fallback inside _read_schema returned None).
        with patch("dbbuddy_core.context_store._read_schema",
                   side_effect=db_module.DatabaseUnavailableError("Unable to fetch schema from the database.")):
            with pytest.raises(db_module.DatabaseUnavailableError):
                context_store._resolve_schema(config, pool)


class TestAiLabelSignal:
    def test_detects_ai_source_anywhere_in_the_layer(self):
        layer = {
            "users": {"id": {"term": "id", "source": "rule"}},
            "orders": {"total": {"term": "order value", "source": "ai"}},
        }
        assert _any_ai_labels(layer) is True

    def test_false_for_a_purely_rule_based_layer(self):
        layer = {"users": {"id": {"term": "id", "source": "rule"}}}
        assert _any_ai_labels(layer) is False

    def test_tolerates_malformed_entries(self):
        layer = {"users": "not-a-dict", "orders": {"total": "not-a-dict"}}
        assert _any_ai_labels(layer) is False

    def test_signal_survives_a_slice_that_contains_no_ai_columns(self):
        """The reason the flag exists at all.

        A query touching only rule-based columns of an AI-refined database must
        still report the database as AI-labeled — otherwise the UI badge and the
        CLI's fallback note flip based on which columns the question happened to
        hit.
        """
        full_layer = {
            "users": {"id": {"term": "id", "source": "rule"}},
            "orders": {"total": {"term": "order value", "source": "ai"}},
        }
        returned_slice = {"users": {"id": {"term": "id", "source": "rule"}}}

        assert _any_ai_labels(returned_slice) is False
        assert _any_ai_labels(full_layer) is True


class TestQueryResponsePayload:
    """End-to-end shape of a query response, on a real (SQLite) pipeline."""

    @pytest.fixture
    def db(self):
        import sqlite3
        from tests.test_db_adapter import create_test_schema
        conn = sqlite3.connect(":memory:")
        create_test_schema(conn)
        return conn

    def _run(self, db, question):
        from tests.test_db_adapter import SqliteDialectConnection, fetch_schema_sqlite
        from dbbuddy_core.orchestrator import process_query

        context_store.reset()
        config = DBConfig(host="localhost", user="t", password="t", database=":memory:",
                          ai=False, ai_provider="local")
        with patch("dbbuddy_core.db.connect_db",
                   side_effect=lambda h, u, p, d, **kw: SqliteDialectConnection(db)), \
                patch("dbbuddy_core.schema.fetch_schema", side_effect=fetch_schema_sqlite):
            return process_query(config, question)

    def test_semantic_layer_is_scoped_to_the_tables_the_query_touched(self, db):
        result = self._run(db, "List all users")

        layer = result.get("semantic_layer", {})
        assert layer, "response should still carry semantic labels for its own columns"
        # The database has more tables than this; shipping all of them on every
        # response is what this slice exists to avoid.
        assert set(layer) <= {"users"}, f"unexpected tables in payload: {sorted(layer)}"
        for columns in layer.values():
            for meta in columns.values():
                assert "term" in meta, "sliced entries must keep their label shape"

    def test_response_reports_label_provenance(self, db):
        result = self._run(db, "List all users")
        assert result.get("ai_labeled") is False  # ai=False in this config

    def test_executed_reads_are_recorded_in_the_query_log(self, db):
        """Auto-executed SELECTs used to return before ever being logged."""
        logged = []
        logger_stub = MagicMock()
        logger_stub.log_query.side_effect = lambda entry: logged.append(entry)

        with patch("dbbuddy_core.orchestrator.get_query_logger", return_value=logger_stub):
            result = self._run(db, "List all users")

        assert result.get("auto_executed") is True
        assert logged, "an executed read must produce a query-log entry"
        entry = logged[-1]
        assert entry["success"] is True
        assert entry["sql"] and entry["sql"].lower().startswith("select")
        assert entry["latency_ms"] > 0
