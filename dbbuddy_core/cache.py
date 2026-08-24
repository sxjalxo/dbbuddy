"""Redis cache module for DBBuddy performance optimization.

This module provides a clean and extensible Redis caching interface
for query responses, retrieval results, and execution plans.

Phase 10.1 of the Redis integration upgrade.
"""

import json
import os
import hashlib
import time
from typing import Any, Optional

from dbbuddy_core.logger import get_logger

logger = get_logger()

try:
    import redis
except ImportError:  # pragma: no cover - optional dependency
    redis = None

# How long to wait for a Redis connection before treating the cache as absent.
# Deliberately short: caching is an optimization, and every consumer degrades to
# computing the value live, so a slow probe is pure added latency on the request
# path. Overridable for a Redis that genuinely lives a few hops away.
CONNECT_TIMEOUT_SECONDS = float(os.getenv("REDIS_CONNECT_TIMEOUT", "0.5"))

# Per-command ceiling once connected, so a stalled server cannot hold a request
# open indefinitely.
SOCKET_TIMEOUT_SECONDS = float(os.getenv("REDIS_SOCKET_TIMEOUT", "2"))

# A Redis that dies *after* the constructor's probe succeeded is worse than one
# that was never there: `connected` stays True, so every lookup and store still
# tries the socket and pays SOCKET_TIMEOUT_SECONDS before failing. On the query
# path that is two timeouts per request — a cache outage turning into a latency
# outage. After this many consecutive errors the client parks itself as
# unavailable and returns immediately, then retries one command after the
# cooldown to discover recovery on its own.
ERROR_THRESHOLD = int(os.getenv("REDIS_ERROR_THRESHOLD", "3"))
RECOVERY_SECONDS = float(os.getenv("REDIS_RECOVERY_SECONDS", "30"))

# Keys deleted per round-trip when clearing a prefix. Bounded so invalidating a
# large keyspace neither builds one enormous key list in memory nor issues a
# single DEL big enough to stall the server for other clients.
CLEAR_BATCH_SIZE = 500


class Cache:
    """Redis cache client for DBBuddy performance optimization."""

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0,
                 connect_timeout: float = CONNECT_TIMEOUT_SECONDS):
        """Initialize Redis cache client.

        Caching is **optional** everywhere it is used: with no server reachable,
        every caller falls back to computing the value live. The probe below must
        therefore fail *fast*, because the cost of an absent Redis is paid on the
        request path.

        Without an explicit ``socket_connect_timeout`` that probe blocked for
        ~49 s against a closed port (redis-py retries the connect, and each attempt
        rides the OS default), which turned "Redis is not installed" — the default
        deployment — into a request that appears hung. Bounding the connect brings
        it to well under a second.

        Args:
            host: Redis server host
            port: Redis server port
            db: Redis database number
            connect_timeout: seconds to wait for the connect + probe
        """
        # Health state for the availability gate (see ERROR_THRESHOLD).
        self._consecutive_errors = 0
        self._blocked_until = 0.0

        if redis is None:
            logger.warning("Redis package not installed - caching disabled")
            self.client = None
            self.connected = False
            return

        options = {
            "host": host,
            "port": port,
            "db": db,
            "decode_responses": True,
            # Bound both the handshake and subsequent commands: a Redis that hangs
            # mid-request is as damaging as one that is simply absent, and no cache
            # lookup is worth stalling a query behind.
            "socket_connect_timeout": connect_timeout,
            "socket_timeout": SOCKET_TIMEOUT_SECONDS,
            # Disable the client's own connect retries. The timeout alone is not
            # enough — redis-py retries internally, so each attempt multiplies the
            # wait (0.5 s became ~15 s). There is nothing to retry *for* here: an
            # absent cache is a supported state, and the caller computes the value
            # live either way. `retry` landed in redis-py 6; on 5.x the kwarg is
            # dropped below and `retry_on_timeout=False` is the closest equivalent.
            "retry": None,
        }
        try:
            self.client = redis.Redis(**options)
        except TypeError:  # redis-py < 6 does not accept `retry`
            options.pop("retry")
            self.client = redis.Redis(**options, retry_on_timeout=False)
        except Exception as e:  # noqa: BLE001 — construction should not raise, but never fail the caller
            logger.warning(f"Redis client could not be created: {e}")
            self.client = None
            self.connected = False
            return

        try:
            # THIS LINE IS CRITICAL - test actual connection
            self.client.ping()
            self.connected = True
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}")
            self.client = None
            self.connected = False

    def _available(self) -> bool:
        """Whether a command should be attempted at all right now.

        Distinct from ``connected`` (was a server ever reachable): this also says
        no while the client is parked after a run of failures, so a Redis that
        went away mid-process costs one timeout per cooldown instead of one per
        command.
        """
        if not self.connected:
            return False
        if self._blocked_until and time.monotonic() < self._blocked_until:
            return False
        return True

    def _note_success(self) -> None:
        if self._consecutive_errors or self._blocked_until:
            self._consecutive_errors = 0
            self._blocked_until = 0.0

    def _note_error(self) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= ERROR_THRESHOLD and not self._blocked_until:
            self._blocked_until = time.monotonic() + RECOVERY_SECONDS
            logger.warning(
                "Redis unreachable after %d consecutive errors — bypassing cache for %.0fs",
                self._consecutive_errors, RECOVERY_SECONDS,
            )

    def _normalize_query(self, query: str) -> str:
        """Normalize query for consistent cache keys.

        Normalizes by:
        - Converting to lowercase
        - Stripping whitespace
        - Normalizing multiple spaces to single space

        Args:
            query: Query string to normalize

        Returns:
            Normalized query string
        """
        return " ".join(query.lower().strip().split())

    def _make_key(self, prefix: str, value: str, normalize: bool = False, schema_hash: str = None) -> str:
        """Generate cache key from prefix and value.

        Args:
            prefix: Cache key prefix (e.g., "query", "retrieval", "plan")
            value: Value to hash for the key
            normalize: Whether to normalize the value (for queries)
            schema_hash: Optional schema hash for schema-aware caching

        Returns:
            Cache key string
        """
        if normalize:
            value = self._normalize_query(value)
        hashed = hashlib.md5(value.encode()).hexdigest()

        # Include schema hash if provided for schema-aware caching
        if schema_hash:
            return f"{prefix}:{schema_hash}:{hashed}"
        return f"{prefix}:{hashed}"

    def get(self, prefix: str, value: str, normalize: bool = False, schema_hash: str = None) -> Optional[Any]:
        """Get cached data.

        Args:
            prefix: Cache key prefix
            value: Value to hash for the key
            normalize: Whether to normalize the value (for queries)
            schema_hash: Optional schema hash for schema-aware caching

        Returns:
            Cached data or None if not found
        """
        if not self._available():
            return None

        try:
            key = self._make_key(prefix, value, normalize, schema_hash)
            data = self.client.get(key)
        except redis.RedisError:
            self._note_error()
            return None

        self._note_success()
        try:
            return json.loads(data) if data else None
        except json.JSONDecodeError:
            # Corrupt payload is a data problem, not a server problem — it must
            # not count toward the availability gate.
            return None

    def set(self, prefix: str, value: str, data: Any, ttl: int = 300, normalize: bool = False, schema_hash: str = None) -> bool:
        """Set cached data with TTL.

        Args:
            prefix: Cache key prefix
            value: Value to hash for the key
            data: Data to cache
            ttl: Time to live in seconds
            normalize: Whether to normalize the value (for queries)
            schema_hash: Optional schema hash for schema-aware caching

        Returns:
            True if successful, False otherwise
        """
        if not self._available():
            return False

        try:
            payload = json.dumps(data)
        except TypeError:
            # Unserializable value — the caller's problem, not the server's.
            return False

        try:
            key = self._make_key(prefix, value, normalize, schema_hash)
            self.client.set(key, payload, ex=ttl)
        except redis.RedisError:
            self._note_error()
            return False

        self._note_success()
        return True

    def delete(self, prefix: str, value: str, normalize: bool = False, schema_hash: str = None) -> bool:
        """Delete cached data.

        Args:
            prefix: Cache key prefix
            value: Value to hash for the key
            normalize: Whether to normalize the value (for queries)
            schema_hash: Optional schema hash for schema-aware caching

        Returns:
            True if successful, False otherwise
        """
        if not self._available():
            return False

        try:
            key = self._make_key(prefix, value, normalize, schema_hash)
            self.client.delete(key)
        except redis.RedisError:
            self._note_error()
            return False

        self._note_success()
        return True

    def flush_db(self) -> bool:
        """Flush all cache entries in the current database.

        Returns:
            True if successful, False otherwise
        """
        if not self._available():
            return False

        try:
            self.client.flushdb()
        except redis.RedisError:
            self._note_error()
            return False

        self._note_success()
        return True

    def clear_prefix(self, prefix: str) -> bool:
        """Clear all cache entries with a given prefix.

        Args:
            prefix: Cache key prefix to clear

        Returns:
            True if successful, False otherwise
        """
        if not self._available():
            return False

        try:
            # Delete in bounded batches as the scan streams, rather than
            # collecting the whole matching keyspace and issuing one DEL for it.
            batch: list = []
            for key in self.client.scan_iter(f"{prefix}:*", count=CLEAR_BATCH_SIZE):
                batch.append(key)
                if len(batch) >= CLEAR_BATCH_SIZE:
                    self.client.delete(*batch)
                    batch.clear()
            if batch:
                self.client.delete(*batch)

            self._note_success()
            return True
        except redis.RedisError:
            self._note_error()
            return False

    def invalidate_schema_cache(self) -> bool:
        """Invalidate all schema-related caches.

        This should be called when:
        - Schema changes (tables/columns added/removed)
        - Semantic layer updates
        - Learning module updates

        Clears: retrieval cache, plan cache
        Keeps: query cache (user queries don't depend on schema structure)

        Returns:
            True if successful, False otherwise
        """
        if not self._available():
            return False

        try:
            # Clear retrieval cache
            self.clear_prefix("retrieval")
            # Clear plan cache
            self.clear_prefix("plan")
            return True
        except redis.RedisError:
            self._note_error()
            return False
