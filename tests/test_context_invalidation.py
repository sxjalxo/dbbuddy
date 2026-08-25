"""Dropping a prepared context — locally, and across workers.

A prepared context holds a live connection pool, a Chroma index, a relationship
graph and a semantic layer. None of that is serializable, so it cannot be shared
between worker processes; what can cross workers is the *message* that one of them
is stale.

Two things are tested here:

* `invalidate()` actually drops the context. It did not — the scan looked for keys
  beginning `host|database|engine|` while the keys were `host|database|hash`, so
  the pool and the schema cache were dropped and the prepared context, the
  expensive part, survived. With an unchanged schema the next request recomputed
  the same hash and got the stale object straight back.
* A rebuild on one worker tells the others, so they drop theirs instead of each
  converging whenever something happens to rebuild them.
"""

from dbbuddy_core import context_store
from dbbuddy_core.models import DBConfig


def _config(**over):
    base = dict(host="db.internal", user="reader", password="secret",
                database="erp", engine="postgresql")
    base.update(over)
    return DBConfig(**base)


def _seed_context(config, schema_hash="hash-1", value="ctx"):
    """Put a stand-in context into the cache under the real key."""
    key = context_store._ctx_key(config, schema_hash)
    context_store._contexts[key] = value
    return key


def setup_function():
    context_store._contexts.clear()
    context_store._schema_cache.clear()


# ── Key shape ─────────────────────────────────────────────────────────────────

def test_a_context_key_extends_the_database_key():
    # The property invalidate() relies on. Without it the prefix scan silently
    # matches nothing, which is exactly the bug this file exists for.
    config = _config()
    key = context_store._ctx_key(config, "abc123")
    assert key.startswith(context_store._db_key(config) + "|")


def test_context_keys_separate_engines():
    # host|database alone is not an identity: the same names can front a MySQL
    # and a PostgreSQL server, and their prepared contexts are not interchangeable.
    assert (context_store._ctx_key(_config(engine="mysql"), "h")
            != context_store._ctx_key(_config(engine="postgresql"), "h"))


def test_context_keys_separate_schemas():
    assert (context_store._ctx_key(_config(db_schema="sales"), "h")
            != context_store._ctx_key(_config(db_schema="warehouse"), "h"))


def test_context_keys_separate_schema_versions():
    assert context_store._ctx_key(_config(), "h1") != context_store._ctx_key(_config(), "h2")


# ── Local invalidation ────────────────────────────────────────────────────────

def test_invalidate_drops_the_prepared_context():
    config = _config()
    key = _seed_context(config)
    context_store.invalidate(config)
    assert key not in context_store._contexts


def test_invalidate_drops_every_schema_version_for_that_database():
    config = _config()
    first = _seed_context(config, "hash-1")
    second = _seed_context(config, "hash-2")
    context_store.invalidate(config)
    assert first not in context_store._contexts
    assert second not in context_store._contexts


def test_invalidate_leaves_other_databases_alone():
    mine = _seed_context(_config())
    theirs = _seed_context(_config(database="other"))
    context_store.invalidate(_config())
    assert theirs in context_store._contexts
    assert mine not in context_store._contexts


def test_invalidate_leaves_another_schema_alone():
    sales = _seed_context(_config(db_schema="sales"))
    warehouse = _seed_context(_config(db_schema="warehouse"))
    context_store.invalidate(_config(db_schema="sales"))
    assert warehouse in context_store._contexts
    assert sales not in context_store._contexts


# ── Cross-worker ──────────────────────────────────────────────────────────────

def test_a_remote_message_drops_the_matching_context():
    config = _config()
    key = _seed_context(config)
    context_store._apply_remote_invalidation(context_store._db_key(config))
    assert key not in context_store._contexts


def test_a_remote_message_for_another_database_changes_nothing():
    key = _seed_context(_config())
    context_store._apply_remote_invalidation("someone|else|postgresql")
    assert key in context_store._contexts


def test_a_malformed_message_is_ignored_rather_than_killing_the_listener():
    # A pub/sub channel is not a trusted schema. One bad message must not end the
    # thread that every other worker's invalidations depend on.
    key = _seed_context(_config())
    for junk in (None, "", 42, b"\xff", {"not": "a key"}):
        context_store._apply_remote_invalidation(junk)
    assert key in context_store._contexts


def test_publishing_without_redis_is_a_no_op(monkeypatch):
    # No shared store is a supported state, not a failure: each worker then
    # rebuilds on its own next analyze.
    monkeypatch.setattr(context_store, "_invalidation_channel", lambda: None)
    context_store._publish_invalidation(_config())      # must not raise


def test_publish_sends_the_database_key(monkeypatch):
    published = []

    class FakeClient:
        def publish(self, channel, payload):
            published.append((channel, payload))

    monkeypatch.setattr(context_store, "_invalidation_channel", lambda: FakeClient())
    config = _config(db_schema="sales")
    context_store._publish_invalidation(config)
    assert published == [(context_store.INVALIDATION_CHANNEL, context_store._db_key(config))]


def test_a_publish_failure_does_not_break_the_rebuild(monkeypatch):
    # Telling other workers is best-effort. A rebuild that succeeded locally must
    # not be reported as failed because a broadcast did not go out.
    class Exploding:
        def publish(self, *_):
            raise RuntimeError("redis gone")

    monkeypatch.setattr(context_store, "_invalidation_channel", lambda: Exploding())
    context_store._publish_invalidation(_config())      # must not raise
