# ── Query History Intelligence ─────────────────────────────────────────────
"""
Query history tracking and intelligence for improved user experience.

Features:
- Track past queries with success rates
- Detect similar queries for reuse
- Suggest frequently used queries
- Analyze query patterns
"""

import json
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class QueryHistory:
    """Track and analyze query history for intelligence."""
    
    def __init__(self, storage_path: str = "dbbuddy_query_history.json"):
        self.storage_path = Path(storage_path)
        self.history = self._load_history()
    
    def _load_history(self) -> dict:
        """Load query history from storage or initialize defaults."""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        
        return {
            "queries": [],
            "patterns": defaultdict(int),
            "success_rates": defaultdict(dict),
            "frequent_queries": Counter()
        }
    
    def _save_history(self):
        """Save history to storage."""
        try:
            with open(self.storage_path, 'w') as f:
                # Convert defaultdict to regular dict for JSON serialization
                history_copy = {
                    "queries": self.history["queries"],
                    "patterns": dict(self.history["patterns"]),
                    "success_rates": {k: dict(v) for k, v in self.history["success_rates"].items()},
                    "frequent_queries": dict(self.history["frequent_queries"])
                }
                json.dump(history_copy, f, indent=2, default=str)
        except Exception as e:
            print(f"Failed to save query history: {e}")
    
    def add_query(self, query: str, sql: str, success: bool, confidence: str, query_type: str):
        """Add a query to history."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "query": query,
            "sql": sql,
            "success": success,
            "confidence": confidence,
            "query_type": query_type
        }
        
        self.history["queries"].append(entry)
        
        # Track patterns (simplified - first word and query type)
        first_word = query.split()[0].lower() if query.split() else ""
        pattern = f"{first_word}_{query_type}"
        self.history["patterns"][pattern] += 1
        
        # Track frequent queries (normalized)
        normalized_query = self._normalize_query(query)
        self.history["frequent_queries"][normalized_query] += 1
        
        # Track success rates per query pattern
        if pattern not in self.history["success_rates"]:
            self.history["success_rates"][pattern] = {"total": 0, "successful": 0}
        self.history["success_rates"][pattern]["total"] += 1
        if success:
            self.history["success_rates"][pattern]["successful"] += 1
        
        # Keep only last 1000 queries
        if len(self.history["queries"]) > 1000:
            self.history["queries"] = self.history["queries"][-1000:]
        
        self._save_history()
    
    def _normalize_query(self, query: str) -> str:
        """Normalize query for pattern matching."""
        return " ".join(query.lower().split())
    
    def _similarity(self, str1: str, str2: str) -> float:
        """Calculate similarity between two strings."""
        return SequenceMatcher(None, str1.lower(), str2.lower()).ratio()
    
    def find_similar_queries(self, query: str, threshold: float = 0.6) -> List[dict]:
        """Find similar queries in history."""
        normalized_query = self._normalize_query(query)
        similar = []
        
        for entry in self.history["queries"]:
            similarity = self._similarity(normalized_query, self._normalize_query(entry["query"]))
            if similarity >= threshold:
                similar.append({
                    "query": entry["query"],
                    "sql": entry["sql"],
                    "similarity": round(similarity, 2),
                    "success": entry["success"],
                    "confidence": entry["confidence"],
                    "timestamp": entry["timestamp"]
                })
        
        # Sort by similarity (highest first)
        similar.sort(key=lambda x: x["similarity"], reverse=True)
        return similar[:5]  # Return top 5
    
    def get_frequent_queries(self, limit: int = 10) -> List[dict]:
        """Get most frequently used queries."""
        frequent = self.history["frequent_queries"].most_common(limit)
        return [
            {
                "query": query,
                "count": count,
                "success_rate": self._get_success_rate_for_query(query)
            }
            for query, count in frequent
        ]
    
    def _get_success_rate_for_query(self, normalized_query: str) -> float:
        """Calculate success rate for a specific query."""
        pattern = normalized_query.split()[0] + "_" if normalized_query.split() else ""
        if pattern in self.history["success_rates"]:
            stats = self.history["success_rates"][pattern]
            if stats["total"] > 0:
                return round((stats["successful"] / stats["total"]) * 100, 1)
        return 0.0
    
    def get_success_rate_by_pattern(self) -> dict:
        """Get success rates grouped by query pattern."""
        success_rates = {}
        for pattern, stats in self.history["success_rates"].items():
            if stats["total"] > 0:
                success_rates[pattern] = round((stats["successful"] / stats["total"]) * 100, 1)
        return success_rates
    
    def get_recent_queries(self, limit: int = 10) -> List[dict]:
        """Get most recent queries."""
        recent = self.history["queries"][-limit:]
        return [
            {
                "query": entry["query"],
                "sql": entry["sql"],
                "success": entry["success"],
                "confidence": entry["confidence"],
                "query_type": entry["query_type"],
                "timestamp": entry["timestamp"]
            }
            for entry in reversed(recent)
        ]
    
    def get_statistics(self) -> dict:
        """Get overall statistics."""
        total_queries = len(self.history["queries"])
        if total_queries == 0:
            return {"message": "No query history yet"}
        
        successful = sum(1 for q in self.history["queries"] if q["success"])
        success_rate = round((successful / total_queries) * 100, 1)
        
        confidence_distribution = Counter(q["confidence"] for q in self.history["queries"])
        
        return {
            "total_queries": total_queries,
            "successful_queries": successful,
            "success_rate": success_rate,
            "confidence_distribution": dict(confidence_distribution),
            "unique_patterns": len(self.history["patterns"]),
            "most_common_patterns": dict(self.history["patterns"].most_common(5))
        }


# Global query history instance
_query_history_instance: Optional[QueryHistory] = None


def get_query_history() -> QueryHistory:
    """Get or create the global query history instance."""
    global _query_history_instance
    if _query_history_instance is None:
        _query_history_instance = QueryHistory()
    return _query_history_instance


def track_query(query: str, sql: str, success: bool, confidence: str, query_type: str):
    """Track a query using the global history instance."""
    history = get_query_history()
    history.add_query(query, sql, success, confidence, query_type)


def find_similar_queries(query: str, threshold: float = 0.6) -> List[dict]:
    """Find similar queries using the global history instance."""
    history = get_query_history()
    return history.find_similar_queries(query, threshold)


def get_query_statistics() -> dict:
    """Get query statistics using the global history instance."""
    history = get_query_history()
    return history.get_statistics()
