# ── Observability Dashboard ─────────────────────────────────────────────────
"""
Metrics tracking and observability for the DBBuddy system.

Tracks:
- Auto-executed vs confirmed queries
- Fallback usage patterns
- Confidence distribution
- Query success rates
- Performance metrics
"""

import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class QueryMetrics:
    """Track query execution metrics for observability."""
    
    def __init__(self, storage_path: str = "dbbuddy_metrics.json"):
        self.storage_path = Path(storage_path)
        self.metrics = self._load_metrics()
    
    def _load_metrics(self) -> dict:
        """Load metrics from storage or initialize defaults."""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        
        return {
            "total_queries": 0,
            "auto_executed": 0,
            "confirmed": 0,
            "cancelled": 0,
            "fallback_usage": {
                "local": 0,
                "nemotron": 0,
                "unknown": 0
            },
            "confidence_distribution": {
                "high": 0,
                "medium": 0,
                "low": 0
            },
            "query_types": defaultdict(int),
            "success_rate": {
                "successful": 0,
                "failed": 0
            },
            "performance": {
                "total_latency_ms": 0,
                "query_count": 0
            },
            "history": []
        }
    
    def _save_metrics(self):
        """Save metrics to storage."""
        try:
            with open(self.storage_path, 'w') as f:
                json.dump(self.metrics, f, indent=2, default=str)
        except Exception as e:
            print(f"Failed to save metrics: {e}")
    
    def track_query(self, query_result: dict, latency_ms: float = 0):
        """Track a single query execution."""
        self.metrics["total_queries"] += 1
        
        # Track auto-executed vs confirmed
        if query_result.get("auto_executed"):
            self.metrics["auto_executed"] += 1
        elif query_result.get("requires_confirmation"):
            # Track as confirmed if it was executed (would need external tracking)
            self.metrics["confirmed"] += 1
        
        # Track fallback usage
        model = query_result.get("model_used", "unknown")
        if model in self.metrics["fallback_usage"]:
            self.metrics["fallback_usage"][model] += 1
        
        # Track confidence distribution
        confidence = query_result.get("confidence", "unknown")
        if confidence in self.metrics["confidence_distribution"]:
            self.metrics["confidence_distribution"][confidence] += 1
        
        # Track query types
        query_type = query_result.get("query_type", "unknown")
        self.metrics["query_types"][query_type] += 1
        
        # Track success rate
        if query_result.get("error"):
            self.metrics["success_rate"]["failed"] += 1
        else:
            self.metrics["success_rate"]["successful"] += 1
        
        # Track performance
        if latency_ms > 0:
            self.metrics["performance"]["total_latency_ms"] += latency_ms
            self.metrics["performance"]["query_count"] += 1
        
        # Add to history (keep last 100)
        history_entry = {
            "timestamp": datetime.now().isoformat(),
            "query": query_result.get("query", "")[:100],  # Truncate long queries
            "query_type": query_type,
            "confidence": confidence,
            "model_used": model,
            "auto_executed": query_result.get("auto_executed", False),
            "requires_confirmation": query_result.get("requires_confirmation", False),
            "latency_ms": latency_ms
        }
        self.metrics["history"].append(history_entry)
        if len(self.metrics["history"]) > 100:
            self.metrics["history"] = self.metrics["history"][-100:]
        
        self._save_metrics()
    
    def get_summary(self) -> dict:
        """Get a summary of current metrics."""
        total = self.metrics["total_queries"]
        if total == 0:
            return {"message": "No queries tracked yet"}
        
        auto_executed_pct = (self.metrics["auto_executed"] / total) * 100
        confirmed_pct = (self.metrics["confirmed"] / total) * 100
        
        success_total = self.metrics["success_rate"]["successful"] + self.metrics["success_rate"]["failed"]
        success_rate = (self.metrics["success_rate"]["successful"] / success_total * 100) if success_total > 0 else 0
        
        avg_latency = 0
        if self.metrics["performance"]["query_count"] > 0:
            avg_latency = self.metrics["performance"]["total_latency_ms"] / self.metrics["performance"]["query_count"]
        
        return {
            "total_queries": total,
            "execution_pattern": {
                "auto_executed": self.metrics["auto_executed"],
                "auto_executed_percentage": round(auto_executed_pct, 1),
                "confirmed": self.metrics["confirmed"],
                "confirmed_percentage": round(confirmed_pct, 1)
            },
            "fallback_usage": dict(self.metrics["fallback_usage"]),
            "confidence_distribution": dict(self.metrics["confidence_distribution"]),
            "query_types": dict(self.metrics["query_types"]),
            "success_rate": round(success_rate, 1),
            "average_latency_ms": round(avg_latency, 2),
            "recent_history": self.metrics["history"][-10:]  # Last 10 queries
        }
    
    def get_confidence_distribution(self) -> dict:
        """Get confidence distribution as percentages."""
        total = sum(self.metrics["confidence_distribution"].values())
        if total == 0:
            return {"high": 0, "medium": 0, "low": 0}
        
        return {
            k: round((v / total) * 100, 1) 
            for k, v in self.metrics["confidence_distribution"].items()
        }
    
    def get_fallback_usage_percentage(self) -> dict:
        """Get fallback usage as percentages."""
        total = sum(self.metrics["fallback_usage"].values())
        if total == 0:
            return {"local": 0, "nemotron": 0, "unknown": 0}
        
        return {
            k: round((v / total) * 100, 1) 
            for k, v in self.metrics["fallback_usage"].items()
        }


# Global metrics instance
_metrics_instance: Optional[QueryMetrics] = None


def get_metrics() -> QueryMetrics:
    """Get or create the global metrics instance."""
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = QueryMetrics()
    return _metrics_instance


def track_query(query_result: dict, latency_ms: float = 0):
    """Track a query execution using the global metrics instance."""
    metrics = get_metrics()
    metrics.track_query(query_result, latency_ms)


def get_observability_summary() -> dict:
    """Get the observability summary."""
    metrics = get_metrics()
    return metrics.get_summary()
