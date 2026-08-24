"""Dogfood harness: run the engine against a real, populated database and score it.

Distinct from `scripts/benchmarks/` (speed) and `tests/schema_portability/`
(schema shape, no rows). This one needs **data**, because the failure that matters
is SQL that runs fine and returns the wrong number.
"""
