"""Latency benchmark for the dbbuddy query pipeline.

Runs the real pipeline (no HTTP) and prints meta.latency_ms — the same number
the UI shows — for: a one-time Analyze Schema, a cold query, and warm queries.

Password is read from the env var DBBUDDY_TEST_PASSWORD, or prompted via
getpass (never echoed, never logged). Nothing is sent anywhere.

Usage (PowerShell):
    $env:DBBUDDY_TEST_PASSWORD = "your-mysql-password"
    .venv\\Scripts\\python.exe Examples\\bench_pipeline.py
    # or override connection:
    .venv\\Scripts\\python.exe Examples\\bench_pipeline.py --host 127.0.0.1 --user root --database analytics_demo --query "Show all users"
"""

import argparse
import getpass
import os
import time

from dbbuddy_core import context_store
from dbbuddy_core.models import DBConfig
from dbbuddy_core.pipeline import process_query, process_schema


def _password_from_env_file():
    """Read DBBUDDY_TEST_PASSWORD from a local .env, if present.

    Lets the value stay in the developer's gitignored .env instead of being
    passed on a command line. The value is only used to connect — never printed.
    """
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DBBUDDY_TEST_PASSWORD="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _time(label, fn):
    t0 = time.time()
    result = fn()
    wall = round((time.time() - t0) * 1000, 1)
    meta_ms = None
    if isinstance(result, dict):
        meta_ms = result.get("meta", {}).get("latency_ms")
    meta_str = f"{meta_ms}ms (meta)" if meta_ms is not None else "n/a"
    print(f"  {label:<32} wall={wall:>8.1f}ms   {meta_str}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--user", default="root")
    ap.add_argument("--database", default="analytics_demo")
    ap.add_argument("--query", default="Show all users")
    ap.add_argument("--provider", default="hybrid", choices=["local", "nemotron", "hybrid"])
    ap.add_argument("--warm", type=int, default=3, help="number of warm queries")
    args = ap.parse_args()

    password = os.getenv("DBBUDDY_TEST_PASSWORD") or _password_from_env_file()
    if not password:
        password = getpass.getpass(f"MySQL password for {args.user}@{args.host}: ")

    config = DBConfig(
        host=args.host,
        user=args.user,
        password=password,
        database=args.database,
        ai=True,
        ai_provider=args.provider,
    )

    print(f"\nDB: {args.user}@{args.host}/{args.database}  provider={args.provider}")
    print(f"Query: {args.query!r}\n")

    # Start from a clean slate so "Analyze" and "cold" are honest.
    context_store.reset()

    print("[1] Analyze Schema (one-time: fetch schema + AI label + index + persist)")
    _time("analyze", lambda: process_schema(config))

    print("\n[2] Cold query (fresh process simulation: context reset, loads from disk)")
    context_store.reset()
    _time("cold query", lambda: process_query(config, args.query))

    print(f"\n[3] Warm queries (cached context, x{args.warm})")
    warm_ms = []
    for i in range(args.warm):
        r = _time(f"warm query #{i + 1}", lambda: process_query(config, args.query))
        if isinstance(r, dict):
            m = r.get("meta", {}).get("latency_ms")
            if m is not None:
                warm_ms.append(m)

    if warm_ms:
        print(f"\nWarm avg (meta.latency_ms): {round(sum(warm_ms) / len(warm_ms), 1)}ms")
    print()


if __name__ == "__main__":
    main()
