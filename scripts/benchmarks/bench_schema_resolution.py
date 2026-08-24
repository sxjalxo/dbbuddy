"""Benchmark: per-query schema resolution.

Every query re-reads the schema to resolve its hash — that is what keeps a
cached context from being served for a schema that has since changed. The cost
is therefore paid forever, and the only lever is not paying it N times over for
N concurrent queries.

`concurrent_resolve_p50` is the metric with teeth: if single-flight breaks, it
regresses toward N x the serial cost while `serial_resolve_p50` looks unchanged.
`fetches_per_resolve` says the same thing structurally — 8 callers should cost
close to 1 read, not 8.
"""

import argparse
import statistics
import threading

import os
import sys

# Allow direct execution as well as import from the suite runner: the repo root
# must be importable before the shared harness is.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.benchmarks import _harness as h  # noqa: E402

h.bootstrap()


def run(tables: int = 120, iterations: int = 60, workers: int = 8) -> h.BenchResult:
    from dbbuddy_core import context_store

    result = h.BenchResult("schema_resolution")
    db_path = h.make_db(tables, "schemares", rows=False)
    config = h.bench_config()

    with h.engine_on_sqlite(db_path):
        context_store.reset()
        pool = context_store._get_pool(config)

        # Warm the connection pool so the first sample is not a connect.
        context_store._resolve_schema(config, pool)

        serial = h.repeat_ms(lambda: context_store._resolve_schema(config, pool), iterations)
        result.add("serial_resolve_p50", h.percentiles(serial)["p50"])

        # Hashing is separate from fetching, and scales with schema *width* — a
        # regression here would be someone making the hash input more expensive.
        schema = context_store._resolve_schema(config, pool)
        hashing = h.repeat_ms(lambda: context_store.compute_schema_hash(schema), iterations)
        result.add("schema_hash_p50", h.percentiles(hashing)["p50"])

        before = context_store.get_metrics()
        samples: list[float] = []
        lock = threading.Lock()
        barrier = threading.Barrier(workers)

        def worker() -> None:
            # Release every thread at once — staggered starts would let each
            # fetch finish before the next began, hiding the very contention
            # this measures.
            barrier.wait()
            local = []
            for _ in range(max(1, iterations // workers)):
                local.append(h.time_ms(lambda: context_store._resolve_schema(config, pool)))
            with lock:
                samples.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        after = context_store.get_metrics()

    p = h.percentiles(samples)
    result.add("concurrent_resolve_p50", p["p50"])
    result.add("concurrent_resolve_p95", p["p95"])

    resolves = len(samples)
    fetches = after["schema_fetches"] - before["schema_fetches"]
    shared = after["schema_fetches_shared"] - before["schema_fetches_shared"]
    if resolves:
        result.add("fetches_per_resolve", fetches / resolves, unit="x")
    result.notes.append(
        f"{resolves} resolves across {workers} threads -> {fetches} DB reads ({shared} shared)")
    result.notes.append(f"serial mean {statistics.mean(serial):.3f} ms")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", type=int, default=120)
    ap.add_argument("--iterations", type=int, default=60)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    raise SystemExit(h.standalone(run, tables=a.tables, iterations=a.iterations, workers=a.workers))
