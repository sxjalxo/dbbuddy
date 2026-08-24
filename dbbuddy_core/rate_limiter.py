"""Rate limiter for production API stability.

Phase 20.2: Add rate limiting (10 requests/second per user).

This module provides rate limiting functionality to prevent:
- Redis cache overload under load
- Database hammering
- System instability from spam queries
"""

import time
from typing import Optional, Tuple

from dbbuddy_core.logger import get_logger
from dbbuddy_core.cache import Cache

logger = get_logger()


class RateLimiter:
    """Rate limiter for API requests.

    Phase 20.2: Add rate limiting (10 requests/second per user).

    Uses Redis to track request counts per user with sliding window
    rate limiting algorithm.
    """

    def __init__(self, cache: Optional[Cache] = None, max_requests: int = 10, window_seconds: int = 1):
        """Initialize rate limiter.

        Args:
            cache: Cache instance for storing rate limit data (optional)
            max_requests: Maximum number of requests allowed per window (default: 10)
            window_seconds: Time window in seconds (default: 1)
        """
        self.cache = cache or Cache()
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.prefix = "rate_limit:"

    def check_rate_limit(self, user_id: str) -> Tuple[bool, Optional[str]]:
        """Check if user has exceeded rate limit.

        Args:
            user_id: User identifier (can be IP, user ID, API key, etc.)

        Returns:
            Tuple of (allowed: bool, error_message: Optional[str])
        """
        try:
            if not self.cache.connected:
                # If cache is not available, allow all requests (fail open)
                logger.warning("Cache not connected, rate limiting disabled")
                return True, None

            current_time = time.time()
            window_start = current_time - self.window_seconds
            key = f"{self.prefix}{user_id}"

            # Get current request timestamps
            timestamps = self.cache.client.lrange(key, 0, -1)
            timestamps = [float(ts) for ts in timestamps if float(ts) > window_start]

            # Check if limit exceeded
            if len(timestamps) >= self.max_requests:
                # Calculate retry time
                oldest_timestamp = min(timestamps) if timestamps else current_time
                retry_after = int(oldest_timestamp + self.window_seconds - current_time) + 1
                error_message = f"Rate limit exceeded. Try again in {retry_after} seconds."
                logger.warning(f"Rate limit exceeded for user {user_id}: {len(timestamps)} requests")
                return False, error_message

            # Record the request: push, bound the list, and refresh the TTL in a
            # single round-trip. These were three sequential commands, so every
            # allowed request paid four network round-trips to Redis on the query
            # hot path (one to read, three to write) — a pipeline makes it two.
            try:
                pipe = self.cache.client.pipeline()
                pipe.lpush(key, str(current_time))
                pipe.ltrim(key, 0, self.max_requests - 1)
                pipe.expire(key, self.window_seconds)
                pipe.execute()
            except AttributeError:
                # A client without pipeline support (a stub/mock) — fall back to
                # the individual commands rather than failing the request.
                self.cache.client.lpush(key, str(current_time))
                self.cache.client.ltrim(key, 0, self.max_requests - 1)
                self.cache.client.expire(key, self.window_seconds)

            return True, None

        except Exception as e:
            logger.error(f"Rate limiter error: {e}")
            # Fail open - allow request if rate limiter fails
            return True, None

# Global rate limiter instance
_rate_limiter = None


def get_rate_limiter(max_requests: int = 10, window_seconds: int = 1) -> RateLimiter:
    """Get the global rate limiter instance.

    Args:
        max_requests: Maximum requests per window (default: 10)
        window_seconds: Time window in seconds (default: 1)

    Returns:
        RateLimiter instance
    """
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(max_requests=max_requests, window_seconds=window_seconds)
    return _rate_limiter
