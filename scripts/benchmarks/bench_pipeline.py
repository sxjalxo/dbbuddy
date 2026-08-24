"""Benchmark: the full query pipeline, end to end.

The integration number. The subsystem benchmarks say whether a component
regressed; this one says whether a *user* would notice, and its per-stage
breakdown says which component to look at. Keep both — a pipeline number alone
cannot tell you where the time went, and subsystem numbers alone cannot tell you
whether the composition is still fast.

Also the only benchmark that can fail outright: if queries stop producing
answers, latency is not the problem.
"""

import argparse
import cProfile
import io
import json
import pstats
import statistics
import threading
import time

import os
import sys

# Allow direct execution as well as import from the suite runner: the repo root
# must be importable before the shared harness is.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.benchmarks import _harness as h  # noqa: E402

h.bootstrap()

QUERIES = [
    "Show all customers",
    "Total revenue by customer",
    "Count orders per status",
    "Top 10 products by price",
    "Show orders from India",
]


def run(tables: int = 120, iterations: int = 200, workers: int = 1,
        profile: bool = False) -> h.BenchResult:
    from dbbuddy_core import context_store
    from dbbuddy_core.orchestrator import process_query

    result = h.BenchResult("pipeline")
    db_path = h.make_db(tables, f"pipeline{workers}")
    config = h.bench_config()

    latencies: list[float] = []
    failures: list[str] = []
    stages: dict[str, list[float]] = {}
    payloads: list[float] = []
    lock = threading.Lock()

    def one(i: int) -> None:
        question = QUERIES[i % len(QUERIES)]
        resp = process_query(config, question, user_id=f"bench-{i % max(1, workers)}")
        with lock:
            if resp.get("rate_limited"):
                return
            if resp.get("error"):
                failures.append(f"{question}: {resp['error']}")
                return
            meta = resp.get("meta", {})
            latencies.append(meta.get("latency_ms", 0.0))
            try:
                payloads.append(float(len(json.dumps(resp, default=str))))
            except (TypeError, ValueError):
                pass
            # The pipeline times itself per stage; aggregating those is how you
            # see *where* the time went, which a total never shows.
            for name, ms in (meta.get("stage_timings") or {}).items():
                stages.setdefault(name, []).append(ms)

    with h.engine_on_sqlite(db_path):
        context_store.reset()

        cold_ms = h.time_ms(lambda: one(0))
        latencies.clear()
        stages.clear()

        prof = cProfile.Profile() if profile else None
        if prof:
            prof.enable()

        t0 = time.time()
        if workers <= 1:
            for i in range(iterations):
                one(i)
        else:
            per = max(1, iterations // workers)
            threads = [threading.Thread(target=lambda w=w: [one(w * per + k) for k in range(per)])
                       for w in range(workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        wall = time.time() - t0

        if prof:
            prof.disable()

    # Builds the whole context (semantic layer, vector index, relationship
    # graph). Tracked across releases — first-query cost is what a user waits
    # through after connecting — but not gated: it swings by an order of
    # magnitude on whether the embedding collection already existed.
    result.add("cold_query", cold_ms, kind=h.MetricKind.TREND)

    p = h.percentiles(latencies)
    result.add("warm_p50", p["p50"])
    # The tail is a real user experience serially, but under contention it is
    # mostly thread scheduling — it moved ~1.3x run to run here with no code
    # change, which is close enough to the tolerance to make it a bad gate.
    result.add("warm_p95", p["p95"],
               kind=h.MetricKind.GATE if workers <= 1 else h.MetricKind.TREND)
    if latencies:
        result.add("throughput", len(latencies) / wall, unit="q/s", lower_is_better=False)

    for name, samples in stages.items():
        # Stage medians are the diagnostic layer: gated, because a stage
        # doubling is exactly the signal worth catching early.
        result.add(f"stage_{name.removesuffix('_ms')}_p50", statistics.median(samples))

    # Response size is what the semantic-layer slicing actually changed, and the
    # cost lands in three places at once (JSON encode, Redis payload, wire). But
    # it also moves with row counts and query mix, so it has not earned a gate:
    # keep it out of the baseline until a few releases show it holding steady.
    if payloads:
        result.add("response_bytes_p50", h.percentiles(payloads)["p50"], unit="B",
                   kind=h.MetricKind.EXPERIMENTAL)

    result.notes.append(f"{tables + h.CORE_TABLES} tables, {len(latencies)} queries, "
                        f"{workers} worker(s)")
    if failures:
        result.notes.append(f"FAILURES: {len(failures)} — first: {failures[0]}")

    if profile:
        buf = io.StringIO()
        pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(20)
        print(buf.getvalue())

    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", type=int, default=120, help="filler tables beyond the 3 core ones")
    ap.add_argument("--iterations", type=int, default=200)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--profile", action="store_true")
    a = ap.parse_args()
    raise SystemExit(h.standalone(run, tables=a.tables, iterations=a.iterations,
                                  workers=a.workers, profile=a.profile))
