"""Run the benchmark suite and compare it against the stored baseline.

    python scripts/benchmarks/run_all.py               # run and print
    python scripts/benchmarks/run_all.py --check       # fail on regression
    python scripts/benchmarks/run_all.py --update      # re-record the baseline
    python scripts/benchmarks/run_all.py --json out.json

Individual benchmarks run standalone too (``python scripts/benchmarks/bench_cache.py``)
when you are iterating on one subsystem and don't want to pay for the rest.
"""

import argparse
import json
import os
import sys

# The repo root must be importable before the shared harness is.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.benchmarks import _harness as h  # noqa: E402

h.bootstrap()


def _benchmarks():
    """Imported lazily so a broken benchmark cannot stop the others loading."""
    from scripts.benchmarks import (bench_cache, bench_embeddings, bench_pipeline,
                                    bench_schema_resolution)
    return [
        ("cache", lambda: bench_cache.run()),
        ("embeddings", lambda: bench_embeddings.run()),
        ("schema_resolution", lambda: bench_schema_resolution.run()),
        # Serial first (clean per-query cost), then concurrent (contention).
        ("pipeline", lambda: bench_pipeline.run(workers=1)),
        ("pipeline_concurrent", lambda: _concurrent()),
    ]


def _concurrent() -> h.BenchResult:
    from scripts.benchmarks import bench_pipeline
    r = bench_pipeline.run(iterations=400, workers=8)
    r.benchmark = "pipeline_concurrent"
    return r


def list_metrics(kind_filter: str | None) -> int:
    """Answer "which metrics fail CI?" from the baseline alone.

    Reads the recorded classification rather than running the suite, so it works
    in a CI config step or a code review without paying for a benchmark run.
    """
    baseline = h.load_baseline()
    if not baseline:
        print("no baseline recorded — run with --update first", file=sys.stderr)
        return 1

    unclassified = 0
    for bench in sorted(baseline):
        rows = []
        for name in sorted(baseline[bench]):
            entry = baseline[bench][name]
            kind = entry.get("kind")
            if kind is None:
                unclassified += 1
                kind = "unknown"
            if kind_filter and kind != kind_filter:
                continue
            rows.append((name, kind, entry.get("value", 0.0), entry.get("unit", "")))
        if not rows:
            continue
        print(f"\n[{bench}]")
        width = max(len(r[0]) for r in rows)
        for name, kind, value, unit in rows:
            print(f"  {name.ljust(width)}  {kind:<11} {value:>10.3f} {unit}")

    if unclassified:
        print(f"\n{unclassified} metric(s) predate classification — "
              f"re-record with --update", file=sys.stderr)
    if kind_filter == h.MetricKind.EXPERIMENTAL.value:
        # Not an empty result — an impossible one. Say so, rather than letting it
        # read as "there are none".
        print("experimental metrics are never written to the baseline; "
              "run the suite to see them", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if a gated metric regressed past --tolerance")
    ap.add_argument("--update", action="store_true", help="overwrite baseline.json with this run")
    ap.add_argument("--tolerance", type=float, default=h.DEFAULT_TOLERANCE,
                    help=f"regression factor (default {h.DEFAULT_TOLERANCE})")
    ap.add_argument("--json", metavar="PATH", help="also write results as JSON")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="run only these benchmarks")
    ap.add_argument("--list-metrics", action="store_true",
                    help="print every recorded metric and its kind, without running anything")
    ap.add_argument("--kind", choices=[k.value for k in h.MetricKind],
                    help="with --list-metrics, show only this kind")
    args = ap.parse_args()

    if args.list_metrics:
        return list_metrics(args.kind)

    results: list[h.BenchResult] = []
    for name, fn in _benchmarks():
        if args.only and name not in args.only:
            continue
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 — one broken benchmark should not hide the rest
            print(f"\n[{name}] FAILED TO RUN: {type(exc).__name__}: {exc}", file=sys.stderr)

    if not results:
        print("no benchmarks ran", file=sys.stderr)
        return 1

    for r in results:
        r.print()

    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump([r.to_dict() for r in results], f, indent=2)
            f.write("\n")
        print(f"\nwrote {args.json}")

    # Experimental metrics are easy to add and easy to forget. Naming them on
    # every run is the pressure that keeps the staging area from becoming a
    # permanent home for numbers nobody trusts.
    pending = [(r.benchmark, m) for r in results for m in r.experimental]
    if pending:
        names = ", ".join(f"{b}.{m.name}" for b, m in pending)
        print(f"\n{len(pending)} experimental metric(s), excluded from baseline: {names}")
        print("  promote to gate/trend once stable, or drop them")

    if args.update:
        h.save_baseline(results)
        print(f"\nbaseline updated: {h.BASELINE_PATH}"
              + (f" ({len(pending)} experimental metric(s) not recorded)" if pending else ""))
        return 0

    baseline = h.load_baseline()
    if not baseline:
        print("\nno baseline recorded — run with --update to create one")
        return 0

    drifts = h.compare_to_baseline(results, baseline, args.tolerance)
    blocking = [d for d in drifts if d.blocking]
    watching = [d for d in drifts if not d.blocking]
    print()

    # Trend drift is printed either way — a metric sliding 3x is worth seeing
    # even when, by classification, it must not fail the build.
    if watching:
        print(f"TREND DRIFT ({len(watching)}, not blocking):")
        for d in watching:
            print("  " + d.describe())
        print()

    if blocking:
        print(f"REGRESSIONS ({len(blocking)} gated metrics, tolerance {args.tolerance}x):")
        for d in blocking:
            print("  " + d.describe())
        # A cross-machine comparison is the single most common cause of a wall of
        # confident-looking regressions. Say so here rather than letting someone
        # spend an afternoon bisecting a laptop.
        mismatch = h.baseline_environment_mismatch()
        if mismatch:
            print(f"\n  NOTE: {mismatch}\n"
                  "  Wall-clock baselines are only comparable on comparable hardware.\n"
                  "  Before treating these as real, re-run on the baseline machine, or\n"
                  "  `git stash` and re-run here to see whether they predate your changes.")
        elif not h.baseline_environment():
            print("\n  NOTE: this baseline predates environment recording, so it cannot be\n"
                  "  checked against this machine. Re-record it with --update on the\n"
                  "  reference machine to enable that check.")
        return 1 if args.check else 0

    gated = sum(1 for r in results for m in r.metrics if m.kind is h.MetricKind.GATE)
    print(f"no regressions across {gated} gated metrics (tolerance {args.tolerance}x)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
