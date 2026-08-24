"""Query logger for query history and replay functionality.

Phase 20.3: Add query history/replay for production observability.

This module provides query logging capabilities to track:
- What queries were run
- What failed
- What succeeded
- Performance metrics
- Confidence scores

This data becomes:
- Debugging tool
- Analytics source
- Improvement dataset
"""

import json
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from dbbuddy_core.logger import get_logger
from dbbuddy_core.cache import Cache

logger = get_logger()


class QueryLogger:
    """Query logger for tracking query history and enabling replay.

    Phase 20.3: Add query history/replay for production observability.

    Logs query execution data to Redis (short-term) or can be extended
    to store in database (long-term) for analytics and improvement.
    """

    def __init__(self, cache: Optional[Cache] = None):
        """Initialize query logger.

        Args:
            cache: Cache instance for storing query logs (optional)
        """
        self.cache = cache or Cache()
        self.log_ttl = 86400  # 24 hours for short-term logs
        self.log_prefix = "query_log:"

    def log_query(self, query_data: Dict[str, Any]) -> bool:
        """Log a query execution for history and replay.

        Args:
            query_data: Dict containing query execution data:
                {
                    "request_id": str,
                    "query": str,
                    "sql": str,
                    "success": bool,
                    "latency_ms": float,
                    "confidence": str,
                    "error": str (optional),
                    "query_type": str,
                    "schema_hash": str (optional),
                    "timestamp": str (optional)
                }

        Returns:
            bool: True if log was saved successfully
        """
        try:
            request_id = query_data.get("request_id")
            if not request_id:
                logger.warning("Cannot log query without request_id")
                return False

            # Add timestamp if not provided
            if "timestamp" not in query_data:
                # tz-aware UTC (utcnow() is deprecated on 3.12+); drop tzinfo to
                # keep the historical naive-ISO string format used for sorting.
                query_data["timestamp"] = (
                    datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                )

            # Store in cache with TTL
            log_key = f"{self.log_prefix}{request_id}"
            log_value = json.dumps(query_data)

            if self.cache.connected:
                self.cache.client.set(log_key, log_value, ex=self.log_ttl)
                logger.debug(f"Logged query {request_id} to cache")
                return True
            else:
                # Fallback: log to file or skip
                logger.warning(f"Cache not connected, skipping query log for {request_id}")
                return False

        except Exception as e:
            logger.error(f"Failed to log query: {e}")
            return False

# Global query logger instance
_query_logger = None


def get_query_logger() -> QueryLogger:
    """Get the global query logger instance.

    Returns:
        QueryLogger instance
    """
    global _query_logger
    if _query_logger is None:
        _query_logger = QueryLogger()
    return _query_logger
