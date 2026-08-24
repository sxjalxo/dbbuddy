## What & why

<!-- What changes, and what problem it solves. Link the issue: Fixes #123 -->

## How I verified it

<!--
Be specific. "Tests pass" is not verification of a correctness fix.
Name the dogfood case, the schema-portability case, or the reproduction that
now behaves correctly, and paste the decisive line of output.
-->

- [ ] `DBBUDDY_STRICT=1 pytest`
- [ ] `ruff check .`
- [ ] Dogfood suite (if the change touches the engine): `python scripts/dogfood/run.py --dataset <name>`
- [ ] Benchmarks (if the change touches a hot path): `python scripts/benchmarks/run_all.py --check`

## Checklist

- [ ] No hardcoded table names, column names, or domain values in the engine
- [ ] `PLAN_VERSION` bumped if planning output changed
- [ ] Any new path that queries a target database holds a `query_slot()`
- [ ] Docs updated (`docs/DEVELOPER_GUIDE.md` codebase map, if things moved)
- [ ] `CHANGELOG.md` entry under `[Unreleased]` for anything user-visible
- [ ] New migration, if the app-DB schema changed

## Anything reviewers should look at closely

<!-- Trade-offs you made, alternatives you rejected, parts you're unsure about. -->
