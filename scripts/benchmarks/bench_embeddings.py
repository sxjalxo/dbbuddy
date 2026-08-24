"""Benchmark: vector-store schema search.

Isolates the embedding path from the rest of the pipeline. The regression this
exists to catch is specific and has happened once already: if the collection
stops being bound to the process-wide embedding function, Chroma constructs one
per call, each building a fresh onnxruntime InferenceSession, and every uncached
search pays ~150 ms instead of ~17 ms.

`uncached_search` is the number that matters — a cached search never touches the
model and so can look perfectly healthy while the model path is broken.
"""

import argparse
import uuid

import os
import sys

# Allow direct execution as well as import from the suite runner: the repo root
# must be importable before the shared harness is.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.benchmarks import _harness as h  # noqa: E402

h.bootstrap()


def run(tables: int = 15, searches: int = 12) -> h.BenchResult:
    from dbbuddy_core.cache import Cache
    from dbbuddy_core.vector_store import VectorStore, get_embedding_function

    result = h.BenchResult("embeddings")

    # A schema-shaped corpus, indexed once. Every run gets its own collection so
    # a previous run's embeddings cannot make this one look fast.
    schema = {"customers": ["id", "name", "email", "country", "city", "status"],
              "orders": ["id", "customer_id", "total_amount", "status"],
              "products": ["id", "name", "price", "category"]}
    for i in range(tables):
        schema[f"dim_entity_{i}"] = ["id", "label"] + [f"attr_{j}" for j in range(12)]

    store = VectorStore(collection_name=f"bench-{uuid.uuid4().hex[:16]}",
                        cache=Cache.__new__(Cache))
    # A Cache built without its constructor has no client; force the "no Redis"
    # shape so this benchmark measures embedding, not a cache round-trip.
    store.redis_cache = None

    ef = get_embedding_function()
    result.notes.append(
        "shared embedding function active" if ef is not None
        else "NO shared embedding function — Chroma will build one per call")

    if store.collection is None:
        result.notes.append("ChromaDB unavailable; in-memory fallback measured instead")

    index_ms = h.time_ms(lambda: store.index_schema(schema, None, force=True))
    # Worth watching across releases — a schema of this width should not suddenly
    # cost twice as much to embed. Not gated: it is a one-time cost dominated by
    # model load and disk, so it swings with the machine rather than the code.
    result.add("index_schema", index_ms, kind=h.MetricKind.TREND)

    # Distinct query strings so nothing hits the store's local memo — this is the
    # cold path a user's new question takes.
    uncached = [h.time_ms(lambda i=i: store.search(f"unique probe {uuid.uuid4().hex} {i}", top_k=5))
                for i in range(searches)]
    p = h.percentiles(uncached)
    result.add("uncached_search_p50", p["p50"])
    result.add("uncached_search_p95", p["p95"])

    # Repeat one query: served from the store's local memo, no embedding at all.
    store.search("repeated probe", top_k=5)
    cached = h.repeat_ms(lambda: store.search("repeated probe", top_k=5), searches)
    result.add("cached_search_p50", h.percentiles(cached)["p50"])

    try:
        store.delete_collection()
    except Exception:  # noqa: BLE001 - cleanup must not fail the benchmark
        pass

    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", type=int, default=15)
    ap.add_argument("--searches", type=int, default=12)
    a = ap.parse_args()
    raise SystemExit(h.standalone(run, tables=a.tables, searches=a.searches))
