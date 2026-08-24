"""Run the System Validation Suite for the Semantic Query Engine.

Exercises determinism, cache consistency, adversarial-query rejection, and
retrieval latency against an indexed schema, then reports pass/fail. Suitable
as a pre-deploy / CI smoke check.

Usage:
    python scripts/run_validation.py                      # built-in demo schema (no DB)
    python scripts/run_validation.py --schema schema.json # a {table: [columns]} JSON file
    python scripts/run_validation.py --host localhost --user root \\
        --database mydb --engine mysql --port 3306        # introspect a live DB
    python scripts/run_validation.py --json               # machine-readable output

Exit code: 0 if the core checks pass (determinism + cache consistency), 1 otherwise.
Adversarial handling and latency are reported for visibility.
"""

import argparse
import json
import os
import sys

# Allow running as `python scripts/run_validation.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbbuddy_core.cache import Cache  # noqa: E402
from dbbuddy_core.system_validation import SystemValidator  # noqa: E402
from dbbuddy_core.vector_store import VectorStore  # noqa: E402

# Self-contained schema so the suite runs without a database.
DEMO_SCHEMA = {
    "users": ["id", "name", "email", "country", "created_at"],
    "payments": ["id", "user_id", "amount", "created_at"],
    "subscriptions": ["id", "user_id", "plan", "status", "created_at"],
    "orders": ["id", "user_id", "total", "created_at"],
}

STANDARD_QUERIES = [
    "show total revenue",
    "users from india",
    "top users by revenue",
    "count active subscriptions",
    "average payment amount",
    "users with high revenue",
    "recent orders",
    "subscription trends",
]

ADVERSARIAL_QUERIES = [
    {"query": "highest lowest revenue", "expected_behavior": "reject"},
    {"query": "users without payments but with subscriptions", "expected_behavior": "reject"},
    {"query": "top users with minimum max payments", "expected_behavior": "reject"},
    {"query": "average total count per user", "expected_behavior": "reject"},
    {"query": "show me the thing with the stuff", "expected_behavior": "reject"},
    {"query": "revenue for users who are also customers", "expected_behavior": "reject"},
]


def load_schema(args) -> dict:
    """Resolve the schema to validate against: explicit file, live DB, or demo."""
    if args.schema:
        with open(args.schema, "r", encoding="utf-8") as f:
            return json.load(f)

    if args.host or args.database:
        from dbbuddy_core.db import connect_db

        conn = connect_db(
            args.host or "localhost", args.user, args.password,
            args.database or "", engine=args.engine, port=args.port,
        )
        if conn is None:
            print("[-] Could not connect to the database.", file=sys.stderr)
            sys.exit(2)
        try:
            return conn.fetch_schema()
        finally:
            conn.close()

    return DEMO_SCHEMA


def evaluate(results: dict):
    """Turn raw suite results into (passed, [(name, ok, detail, gating)])."""
    d = results.get("determinism", {}) or {}
    total = d.get("total_queries", 0)
    det_ok = total > 0 and d.get("deterministic_queries", 0) == total

    c = results.get("cache_consistency", {}) or {}
    cache_ok = bool(c.get("fresh_vs_cached_match", False))

    a = results.get("adversarial", {}) or {}
    adv_total = a.get("total_queries", 0)
    adv_handled = a.get("properly_rejected", 0)

    p = results.get("performance_metrics", {}) or {}
    avg_ms = p.get("avg_time_ms", 0) or 0

    checks = [
        ("Determinism", det_ok, f"{d.get('deterministic_queries', 0)}/{total} deterministic", True),
        ("Cache consistency", cache_ok, "match" if cache_ok else "mismatch", True),
        ("Adversarial handling", None, f"{adv_handled}/{adv_total} handled", False),
        ("Avg retrieval latency", None, f"{avg_ms:.1f} ms", False),
    ]
    # Only gating checks (4th tuple element True) decide the exit code.
    passed = all(ok for _, ok, _, gating in checks if gating)
    return passed, checks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the DB Buddy system validation suite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--schema", help="Path to a JSON {table: [columns]} schema file.")
    parser.add_argument("--host", help="DB host (introspect a live schema).")
    parser.add_argument("--port", type=int, help="DB port (engine default if omitted).")
    parser.add_argument("--user", default="", help="DB user.")
    parser.add_argument("--password", default="", help="DB password.")
    parser.add_argument("--database", help="DB name.")
    parser.add_argument("--engine", default="mysql",
                        help="mysql | postgresql | sqlserver (default: mysql).")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()

    schema = load_schema(args)

    cache = Cache()
    vector_store = VectorStore(cache=cache)
    vector_store.index_schema(schema, {})  # empty semantic layer is fine for validation

    validator = SystemValidator(vector_store, cache)
    results = validator.run_full_validation_suite(STANDARD_QUERIES, ADVERSARIAL_QUERIES)

    passed, checks = evaluate(results)

    if args.json:
        print(json.dumps({"passed": passed, "results": results}, indent=2, default=str))
    else:
        print("\n" + "=" * 60)
        print("SYSTEM VALIDATION SUITE")
        print("=" * 60)
        for name, ok, detail, gating in checks:
            mark = "----" if ok is None else ("PASS" if ok else "FAIL")
            tag = "" if gating else "  (informational)"
            print(f"  [{mark}] {name}: {detail}{tag}")
        print("=" * 60)
        print(f"RESULT: {'PASS' if passed else 'FAIL'}")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
