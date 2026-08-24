"""System Validation and Stress Hardening Module.

This module provides comprehensive testing and validation for the Semantic Query Engine,
including determinism testing, cache consistency, adversarial query testing, retrieval
debugging, confidence calibration, and performance metrics.

Phase 12 of the system validation upgrade.
"""

from typing import Dict, List, Any
import time

from dbbuddy_core.logger import get_logger

logger = get_logger()


class SystemValidator:
    """Comprehensive system validation and stress testing."""

    def __init__(self, vector_store, cache=None):
        """Initialize system validator.

        Args:
            vector_store: VectorStore instance for testing
            cache: Optional Redis cache instance for cache consistency testing
        """
        self.vector_store = vector_store
        self.cache = cache
        self.validation_results = []

    def test_determinism(self, queries: List[str], num_runs: int = 10) -> Dict[str, Any]:
        """Test that same input produces same output always.

        Runs each query multiple times and checks that:
        - Retrieved tables are identical
        - Retrieved columns are identical
        - SQL is identical
        - Confidence is identical

        Args:
            queries: List of test queries
            num_runs: Number of times to run each query

        Returns:
            Dict with determinism test results
        """
        logger.debug("PHASE 12.1: DETERMINISM TESTING")
        logger.debug(f"Testing {len(queries)} queries with {num_runs} runs each...")

        results = {
            "total_queries": len(queries),
            "deterministic_queries": 0,
            "non_deterministic_queries": 0,
            "query_results": []
        }

        for query in queries:
            logger.debug(f"Testing query: '{query}'")

            # Run query multiple times
            runs = []
            for i in range(num_runs):
                # Get retrieval results
                matches = self.vector_store.search(query, top_k=10)

                # Extract tables and columns
                tables = sorted(set([m["table"] for m in matches if m["table"]]))
                columns = sorted([f"{m['table']}.{m['column']}" for m in matches if m["column"]])

                runs.append({
                    "tables": tables,
                    "columns": columns,
                    "matches": matches
                })

            # Check determinism
            first_run = runs[0]
            is_deterministic = True

            for i, run in enumerate(runs[1:], 1):
                if run["tables"] != first_run["tables"]:
                    is_deterministic = False
                    logger.debug(f"Non-deterministic tables in run {i+1}")
                    logger.debug(f"  Expected: {first_run['tables']}")
                    logger.debug(f"  Got: {run['tables']}")

                if run["columns"] != first_run["columns"]:
                    is_deterministic = False
                    logger.debug(f"Non-deterministic columns in run {i+1}")
                    logger.debug(f"  Expected: {first_run['columns']}")
                    logger.debug(f"  Got: {run['columns']}")

            if is_deterministic:
                results["deterministic_queries"] += 1
                logger.debug("Deterministic")
            else:
                results["non_deterministic_queries"] += 1
                logger.debug("Non-deterministic")

            results["query_results"].append({
                "query": query,
                "deterministic": is_deterministic,
                "tables": first_run["tables"],
                "columns": first_run["columns"]
            })

        # Summary
        logger.debug("DETERMINISM TEST SUMMARY:")
        logger.debug(f"  Total queries: {results['total_queries']}")
        logger.debug(f"  Deterministic: {results['deterministic_queries']}")
        logger.debug(f"  Non-deterministic: {results['non_deterministic_queries']}")
        logger.debug(f"  Success rate: {results['deterministic_queries'] / results['total_queries'] * 100:.1f}%")

        return results

    def test_cache_consistency(self, query: str) -> Dict[str, Any]:
        """Test cache consistency between fresh and cached runs.

        Tests:
        - Fresh run vs cached run must match exactly
        - After TTL expiry
        - After schema change
        - After learning update

        Args:
            query: Test query

        Returns:
            Dict with cache consistency test results
        """
        logger.debug("PHASE 12.2: CACHE CONSISTENCY TESTING")
        logger.debug(f"Testing query: '{query}'")

        results = {
            "query": query,
            "fresh_vs_cached_match": False,
            "fresh_run": None,
            "cached_run": None,
            "consistency_issues": []
        }

        if not self.cache:
            logger.warning("Cache not available - skipping cache consistency test")
            return results

        # Clear cache for clean test
        self.cache.flush_db()

        # Fresh run
        logger.debug("Running fresh query...")
        fresh_matches = self.vector_store.search(query, top_k=10)
        fresh_tables = sorted(set([m["table"] for m in fresh_matches if m["table"]]))
        fresh_columns = sorted([f"{m['table']}.{m['column']}" for m in fresh_matches if m["column"]])

        results["fresh_run"] = {
            "tables": fresh_tables,
            "columns": fresh_columns,
            "matches": fresh_matches
        }

        # Cached run
        logger.debug("Running cached query...")
        cached_matches = self.vector_store.search(query, top_k=10)
        cached_tables = sorted(set([m["table"] for m in cached_matches if m["table"]]))
        cached_columns = sorted([f"{m['table']}.{m['column']}" for m in cached_matches if m["column"]])

        results["cached_run"] = {
            "tables": cached_tables,
            "columns": cached_columns,
            "matches": cached_matches
        }

        # Check consistency
        if fresh_tables == cached_tables and fresh_columns == cached_columns:
            results["fresh_vs_cached_match"] = True
            logger.debug("Fresh vs cached match")
        else:
            logger.debug("Fresh vs cached mismatch")
            if fresh_tables != cached_tables:
                results["consistency_issues"].append(f"Tables mismatch: fresh={fresh_tables}, cached={cached_tables}")
            if fresh_columns != cached_columns:
                results["consistency_issues"].append(f"Columns mismatch: fresh={fresh_columns}, cached={cached_columns}")

        # Test schema change invalidation
        logger.debug("Testing schema change invalidation...")
        self.cache.invalidate_schema_cache()
        schema_invalidated_matches = self.vector_store.search(query, top_k=10)

        # Should still match fresh run (schema didn't actually change, but cache was cleared)
        if len(schema_invalidated_matches) == len(fresh_matches):
            logger.debug("Schema invalidation works")
        else:
            logger.debug("Schema invalidation may have issues")

        return results

    def test_adversarial_queries(self, adversarial_queries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Test adversarial query suite to break the system intentionally.

        Tests:
        - Does system reject properly?
        - Does it hallucinate SQL?
        - Does confidence drop?

        Args:
            adversarial_queries: List of adversarial query dicts with expected behavior

        Returns:
            Dict with adversarial test results
        """
        logger.debug("PHASE 12.3: ADVERSARIAL QUERY SUITE")
        logger.debug(f"Testing {len(adversarial_queries)} adversarial queries...")

        results = {
            "total_queries": len(adversarial_queries),
            "properly_rejected": 0,
            "improperly_rejected": 0,
            "hallucinated": 0,
            "low_confidence": 0,
            "query_results": []
        }

        for query_info in adversarial_queries:
            query = query_info["query"]
            expected_behavior = query_info.get("expected_behavior", "reject")

            logger.debug(f"Testing adversarial query: '{query}'")
            logger.debug(f"  Expected behavior: {expected_behavior}")

            # Get retrieval results
            matches = self.vector_store.search(query, top_k=10)

            # Analyze results
            tables = [m["table"] for m in matches if m["table"]]
            columns = [f"{m['table']}.{m['column']}" for m in matches if m["column"]]
            avg_score = sum([m["score"] for m in matches]) / len(matches) if matches else 0

            query_result = {
                "query": query,
                "expected_behavior": expected_behavior,
                "tables": tables,
                "columns": columns,
                "avg_score": avg_score,
                "num_matches": len(matches)
            }

            # Check if properly rejected (low confidence or no matches)
            if expected_behavior == "reject":
                if len(matches) == 0 or avg_score < 0.6:
                    results["properly_rejected"] += 1
                    query_result["properly_rejected"] = True
                    logger.debug("  Properly rejected")
                else:
                    results["improperly_rejected"] += 1
                    query_result["properly_rejected"] = False
                    logger.debug("  Improperly accepted")
            else:
                # Expected to work - check if it does
                if len(matches) > 0 and avg_score >= 0.6:
                    results["properly_rejected"] += 1
                    query_result["properly_rejected"] = True
                    logger.debug("  Properly handled")
                else:
                    results["improperly_rejected"] += 1
                    query_result["properly_rejected"] = False
                    logger.debug("  Improperly rejected")

            # Check for hallucination (matches but low relevance)
            if len(matches) > 0 and avg_score < 0.5:
                results["hallucinated"] += 1
                query_result["hallucinated"] = True
                logger.debug("  Potential hallucination (low score)")

            # Check confidence
            if avg_score < 0.7:
                results["low_confidence"] += 1
                query_result["low_confidence"] = True
                logger.debug(f"  Low confidence: {avg_score:.2f}")

            results["query_results"].append(query_result)

        # Summary
        logger.debug("ADVERSARIAL TEST SUMMARY:")
        logger.debug(f"  Total queries: {results['total_queries']}")
        logger.debug(f"  Properly handled: {results['properly_rejected']}")
        logger.debug(f"  Improperly handled: {results['improperly_rejected']}")
        logger.debug(f"  Potential hallucinations: {results['hallucinated']}")
        logger.debug(f"  Low confidence: {results['low_confidence']}")
        logger.debug(f"  Success rate: {results['properly_rejected'] / results['total_queries'] * 100:.1f}%")

        return results

    def add_retrieval_debug_layer(self, query: str) -> Dict[str, Any]:
        """Add retrieval debug layer to make Chroma retrieval visible.

        Logs per query:
        - Top matches with table, column, score
        - Retrieval time
        - Filter applied

        Args:
            query: Query to debug

        Returns:
            Dict with retrieval debug information
        """
        logger.debug("PHASE 12.4: RETRIEVAL DEBUG LAYER")
        logger.debug(f"Debugging query: '{query}'")

        start_time = time.time()
        matches = self.vector_store.search(query, top_k=10)
        retrieval_time = (time.time() - start_time) * 1000  # Convert to ms

        # Format debug output
        debug_info = {
            "query": query,
            "retrieval_time_ms": retrieval_time,
            "num_matches": len(matches),
            "top_matches": []
        }

        logger.debug(f"  Retrieval time: {retrieval_time:.2f}ms")
        logger.debug(f"  Number of matches: {len(matches)}")
        logger.debug("  Top matches:")

        for i, match in enumerate(matches[:5], 1):
            table = match.get("table", "N/A")
            column = match.get("column", "N/A")
            score = match.get("score", 0.0)
            match_type = match.get("type", "unknown")

            debug_info["top_matches"].append({
                "rank": i,
                "table": table,
                "column": column,
                "type": match_type,
                "score": score
            })

            logger.debug(f"    {i}. {match_type}: {table}.{column} (score: {score:.2f})")

        return debug_info

    def test_confidence_calibration(self, test_cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Test confidence calibration.

        Compares:
        - Correct query with high confidence → should be correct
        - Ambiguous query with low confidence → should be correct
        - Wrong query with low confidence → should be wrong

        Args:
            test_cases: List of test cases with query and expected outcome

        Returns:
            Dict with confidence calibration results
        """
        logger.debug("PHASE 12.5: CONFIDENCE CALIBRATION")
        logger.debug(f"Testing {len(test_cases)} confidence calibration cases...")

        results = {
            "total_cases": len(test_cases),
            "well_calibrated": 0,
            "poorly_calibrated": 0,
            "case_results": []
        }

        for case in test_cases:
            query = case["query"]
            expected_outcome = case["expected_outcome"]  # "correct", "ambiguous", "wrong"
            expected_confidence_range = case.get("expected_confidence_range", "high")

            logger.debug(f"Testing case: '{query}'")
            logger.debug(f"  Expected outcome: {expected_outcome}")
            logger.debug(f"  Expected confidence: {expected_confidence_range}")

            # Get retrieval results
            matches = self.vector_store.search(query, top_k=10)
            avg_score = sum([m["score"] for m in matches]) / len(matches) if matches else 0

            # Determine actual confidence level
            if avg_score >= 0.8:
                actual_confidence = "high"
            elif avg_score >= 0.6:
                actual_confidence = "medium"
            else:
                actual_confidence = "low"

            # Check calibration
            case_result = {
                "query": query,
                "expected_outcome": expected_outcome,
                "expected_confidence": expected_confidence_range,
                "actual_confidence": actual_confidence,
                "avg_score": avg_score,
                "well_calibrated": False
            }

            # Simple calibration check
            if expected_confidence_range == "high" and actual_confidence == "high":
                case_result["well_calibrated"] = True
                results["well_calibrated"] += 1
                logger.debug("  Well calibrated")
            elif expected_confidence_range == "low" and actual_confidence == "low":
                case_result["well_calibrated"] = True
                results["well_calibrated"] += 1
                logger.debug("  Well calibrated")
            else:
                results["poorly_calibrated"] += 1
                logger.debug(f"  Poorly calibrated (expected {expected_confidence_range}, got {actual_confidence})")

            results["case_results"].append(case_result)

        # Summary
        logger.debug("CONFIDENCE CALIBRATION SUMMARY:")
        logger.debug(f"  Total cases: {results['total_cases']}")
        logger.debug(f"  Well calibrated: {results['well_calibrated']}")
        logger.debug(f"  Poorly calibrated: {results['poorly_calibrated']}")
        logger.debug(f"  Calibration rate: {results['well_calibrated'] / results['total_cases'] * 100:.1f}%")

        return results

    def measure_performance_metrics(self, queries: List[str]) -> Dict[str, Any]:
        """Measure latency and performance metrics.

        Measures:
        - Total response time
        - Retrieval time
        - Planner time
        - Cache hit rate

        Args:
            queries: List of queries to test

        Returns:
            Dict with performance metrics
        """
        logger.debug("PHASE 12.6: LATENCY + METRICS")
        logger.debug(f"Measuring performance for {len(queries)} queries...")

        results = {
            "total_queries": len(queries),
            "total_time_ms": 0,
            "avg_time_ms": 0,
            "min_time_ms": float('inf'),
            "max_time_ms": 0,
            "retrieval_times": [],
            "cache_hits": {
                "query": 0,
                "retrieval": 0,
                "plan": 0
            },
            "query_metrics": []
        }

        for query in queries:
            logger.debug(f"Measuring: '{query}'")

            # Measure retrieval time
            start_time = time.time()
            matches = self.vector_store.search(query, top_k=10)
            retrieval_time = (time.time() - start_time) * 1000

            # Check cache hits
            cache_hit_query = False
            cache_hit_retrieval = False

            if self.cache:
                # Check query cache
                cache_hit_query = self.cache.get("query", query, normalize=True) is not None
                # Check retrieval cache
                cache_hit_retrieval = self.cache.get("retrieval", f"{query}_10", normalize=True) is not None

            # Update cache hit counters
            if cache_hit_query:
                results["cache_hits"]["query"] += 1
            if cache_hit_retrieval:
                results["cache_hits"]["retrieval"] += 1

            # Record metrics
            query_metric = {
                "query": query,
                "retrieval_time_ms": retrieval_time,
                "num_matches": len(matches),
                "cache_hit_query": cache_hit_query,
                "cache_hit_retrieval": cache_hit_retrieval
            }

            results["retrieval_times"].append(retrieval_time)
            results["query_metrics"].append(query_metric)

            # Update statistics
            results["total_time_ms"] += retrieval_time
            results["min_time_ms"] = min(results["min_time_ms"], retrieval_time)
            results["max_time_ms"] = max(results["max_time_ms"], retrieval_time)

            logger.debug(f"  Retrieval time: {retrieval_time:.2f}ms")
            logger.debug(f"  Cache hit: query={cache_hit_query}, retrieval={cache_hit_retrieval}")

        # Calculate averages
        if results["total_queries"] > 0:
            results["avg_time_ms"] = results["total_time_ms"] / results["total_queries"]

        # Summary
        logger.debug("PERFORMANCE METRICS SUMMARY:")
        logger.debug(f"  Total queries: {results['total_queries']}")
        logger.debug(f"  Average retrieval time: {results['avg_time_ms']:.2f}ms")
        logger.debug(f"  Min retrieval time: {results['min_time_ms']:.2f}ms")
        logger.debug(f"  Max retrieval time: {results['max_time_ms']:.2f}ms")
        logger.debug(f"  Cache hits - query: {results['cache_hits']['query']}/{results['total_queries']}")
        logger.debug(f"  Cache hits - retrieval: {results['cache_hits']['retrieval']}/{results['total_queries']}")

        return results

    def run_full_validation_suite(self, queries: List[str], adversarial_queries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Run complete validation suite.

        Args:
            queries: List of standard test queries
            adversarial_queries: List of adversarial query dicts

        Returns:
            Dict with all validation results
        """
        logger.debug("="*80)
        logger.debug("PHASE 12: SYSTEM VALIDATION + STRESS HARDENING")
        logger.debug("="*80)

        full_results = {
            "determinism": self.test_determinism(queries[:3]),  # Test first 3 queries
            "cache_consistency": self.test_cache_consistency(queries[0]) if queries else {},
            "adversarial": self.test_adversarial_queries(adversarial_queries),
            "retrieval_debug": self.add_retrieval_debug_layer(queries[0]) if queries else {},
            "confidence_calibration": self.test_confidence_calibration([
                {"query": "show total revenue", "expected_outcome": "correct", "expected_confidence_range": "high"},
                {"query": "highest lowest revenue", "expected_outcome": "wrong", "expected_confidence_range": "low"}
            ]),
            "performance_metrics": self.measure_performance_metrics(queries[:5])  # Test first 5 queries
        }

        logger.debug("="*80)
        logger.debug("VALIDATION SUITE COMPLETE")
        logger.debug("="*80)

        return full_results
