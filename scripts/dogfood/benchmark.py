"""Benchmark harness — records *how* the engine answered, not just whether.

Correctness is the dogfood loop's job (``run.py``). This is its companion: for a
representative set of questions per dataset it records the per-stage latency
breakdown, the confidence band, and the shape of the SQL produced (joins,
predicates, grouping). It asserts nothing by default — it is an instrument, so a
regression in latency or plan complexity is visible as a number that moved rather
than a red test. Pass ``--max-total-ms N`` to turn the total-latency ceiling into
a gate for CI.

    python scripts/dogfood/benchmark.py --dataset employees --db data/employees/employees.db
    python scripts/dogfood/benchmark.py --dataset erp --json out.json --max-total-ms 250

Stages recorded (from the pipeline's own ``meta.stage_timings``): context,
relevance (semantic retrieval), cache_check, intent_building, planning,
compilation, execution. Plan shape is parsed from the emitted SQL so it needs no
engine hooks: tables joined, JOIN count, predicate count, grouping.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scripts.dogfood.run import DATASETS, clear_engine_caches, make_config  # noqa: E402
from scripts.dogfood.sqlite_target import engine_pointed_at  # noqa: E402

# Representative questions per dataset, ordered simple → complex, so the timing
# table shows how cost scales with plan shape. Kept to questions the engine
# answers today (no capability gaps), since a failed parse would skew timings.
QUERIES = {
    "employees": [
        "how many employees",
        "how many employees with gender F",
        "average salary",
        "total salary by gender",
        "top 5 titles by count",
        "departments with more than 20000 employees",
    ],
    "erp": [
        "how many customers",
        "total revenue",
        "orders by status",
        "revenue by customer segment",
        "top 5 products by unit price",
    ],
    "hospital": [
        "how many patients",
        "encounters by patient sex",
        "observations by region",
    ],
    "legacy": [
        "count line-item",
        "total qty by group",
    ],
}

STAGES = ["context_ms", "relevance_ms", "cache_check_ms", "intent_building_ms",
          "planning_ms", "compilation_ms", "execution_ms"]

_CONF_RANK = {"low": 0, "medium": 1, "high": 2, "unknown": -1}


def _sql_shape(sql: str | None) -> dict:
    """Plan complexity read off the emitted SQL — no engine internals needed."""
    if not sql:
        return {"tables": 0, "joins": 0, "predicates": 0, "grouped": False}
    up = sql.upper()
    joins = len(re.findall(r"\bJOIN\b", up))
    # base table + one per join
    tables = joins + 1 if re.search(r"\bFROM\b", up) else 0
    where = re.search(r"\bWHERE\b(.*?)(?:\bGROUP BY\b|\bORDER BY\b|\bLIMIT\b|$)", up, re.DOTALL)
    predicates = 0
    if where:
        clause = where.group(1)
        predicates = 1 + len(re.findall(r"\b(?:AND|OR)\b", clause)) if clause.strip() else 0
    having = re.search(r"\bHAVING\b(.*?)(?:\bORDER BY\b|\bLIMIT\b|$)", up, re.DOTALL)
    if having and having.group(1).strip():
        predicates += 1 + len(re.findall(r"\b(?:AND|OR)\b", having.group(1)))
    return {"tables": tables, "joins": joins, "predicates": predicates,
            "grouped": "GROUP BY" in up}


def _measure(config, question: str, repeats: int) -> dict:
    """Run one question ``repeats`` times; report the median of each stage (warm)."""
    from dbbuddy_core.pipeline import process_query

    per_stage: dict[str, list[float]] = {s: [] for s in STAGES}
    totals: list[float] = []
    last = None
    for _ in range(repeats):
        t0 = time.time()
        result = process_query(config, question, auto_execute_reads=True)
        wall = round((time.time() - t0) * 1000, 2)
        last = result
        meta = result.get("meta", {}) if isinstance(result, dict) else {}
        timings = meta.get("stage_timings", {}) or {}
        for s in STAGES:
            per_stage[s].append(float(timings.get(s, 0.0)))
        totals.append(float(meta.get("latency_ms", wall)))

    med = {s: round(statistics.median(v), 2) for s, v in per_stage.items()}
    shape = _sql_shape(last.get("sql") if isinstance(last, dict) else None)
    return {
        "question": question,
        "confidence": (last or {}).get("confidence"),
        "row_count": len((last or {}).get("results") or []),
        "stages_ms": med,
        "total_ms": round(statistics.median(totals), 2),
        "sql_shape": shape,
        "sql": (last or {}).get("sql"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="employees", choices=sorted(DATASETS))
    ap.add_argument("--db", default=None)
    ap.add_argument("--repeats", type=int, default=5, help="measured runs per query (median reported)")
    ap.add_argument("--json", default=None, help="write full results as JSON here")
    ap.add_argument("--max-total-ms", type=float, default=None,
                    help="fail (exit 1) if any query's median total latency exceeds this")
    args = ap.parse_args()

    clear_engine_caches()
    dataset_mod, _, default_name = DATASETS[args.dataset]
    db_path = args.db or os.path.join(tempfile.gettempdir(), default_name)
    if not os.path.exists(db_path):
        print(f"building '{args.dataset}' at {db_path} …")
        dataset_mod.build(db_path)

    config = make_config(db_path)
    questions = QUERIES.get(args.dataset, [])
    rows = []

    with engine_pointed_at(db_path):
        from dbbuddy_core.pipeline import process_schema, process_query
        process_schema(config)              # analyze (build the value index)
        process_query(config, questions[0] if questions else "count", auto_execute_reads=True)  # warm context

        print(f"\nBenchmark · {args.dataset} · {sum(dataset_mod.stats(db_path).values()):,} rows "
              f"· median of {args.repeats} warm runs\n")
        header = (f"{'question':38} {'conf':7} {'tbl':>3} {'jn':>3} {'pred':>4} "
                  f"{'grp':>3} {'intent':>7} {'plan':>6} {'compile':>7} {'exec':>7} {'TOTAL':>7}")
        print(header)
        print("-" * len(header))
        for q in questions:
            r = _measure(config, q, args.repeats)
            rows.append(r)
            s, sh = r["stages_ms"], r["sql_shape"]
            print(f"{q[:38]:38} {str(r['confidence'] or '-'):7} "
                  f"{sh['tables']:>3} {sh['joins']:>3} {sh['predicates']:>4} "
                  f"{('Y' if sh['grouped'] else '-'):>3} "
                  f"{s['intent_building_ms']:>7} {s['planning_ms']:>6} "
                  f"{s['compilation_ms']:>7} {s['execution_ms']:>7} {r['total_ms']:>7}")

    totals = [r["total_ms"] for r in rows]
    if totals:
        print("\n" + f"total latency ms — p50 {statistics.median(totals):.1f}  "
              f"max {max(totals):.1f}  min {min(totals):.1f}")

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.json}")

    if args.max_total_ms is not None:
        over = [r for r in rows if r["total_ms"] > args.max_total_ms]
        if over:
            print(f"\nFAIL — {len(over)} query(ies) over {args.max_total_ms}ms:")
            for r in over:
                print(f"  {r['total_ms']:>8}ms  {r['question']}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
