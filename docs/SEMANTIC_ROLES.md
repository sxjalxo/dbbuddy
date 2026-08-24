# Semantic Column Roles — design note

Status: **Phases A, B, C implemented** (2026-07-08). Author: QA/design pass 2026-07-08.

Implementation map. `dbbuddy_core/semantic_roles.py` owns the semantic identity of
columns — the role vocabulary, the deterministic prior, the AI overlay, and the
ranked identifier lookup:
- Phase A — `semantic_roles.classify_roles`; cached on
  `DBContext.column_roles`; consumed by `_find_identifier_column` /
  `extract_value_filters` / `ensure_entity_identifier`. Tests: `test_semantic_roles.py`.
- Phase B — `semantic_roles.overlay_ai_roles` overlays roles derived from the AI
  classification already in `semantic` (`ai_role_from_term`); no extra model call.
- Phase C — `dbbuddy_core/column_values.py` `build_value_index` samples distinct
  dimension values at Analyze time (persisted per schema_hash under
  `.dbbuddy_cache/valueidx_*.json`), cached on `DBContext.value_index`;
  `resolve_value` binds a literal to its dimension column before name grounding.
  Tests: `test_column_values.py`. Precision note: value-index binding applies only
  to literals already gated as name candidates (possessive / entity-noun /
  capitalized), so it never resurrects the "guess enum values" behavior for a
  bare lowercase token like "active".

## Problem

Record-level lookups ground a bare literal onto a column by **sentence position**:
`extract_value_filters` treats a lone capitalized token (`Alice age`, `Alice details`)
as a name and binds it to a table's identifier column. This is a last-resort
heuristic and is brittle in two ways it cannot fix on its own:

1. **Which identifier column?** A table with `first_name`, `username`, and
   `display_name` has no positional signal for which one `Alice` belongs to.
   `_find_identifier_column` currently picks by a hardcoded priority list
   (`_IDENTIFIER_COLUMNS`), not by what the columns actually hold.
2. **Is the literal even a name?** `customers in Pune` binds `Pune` to
   `customers.name` because grammar says "capitalized noun → name". `Pune` is a
   *city value*. Grammar cannot know that `Pune` lives in `city`, not `name`.

Neither is a parser bug anymore. Both need the engine to know **what a column
means (role)** and, for case 2, **what values a column holds (value space)**.

The synonym case (`how old is Alice` needs `old → age`) is a *third*, orthogonal
axis — the `term` label / synonym map — and is out of scope for this note.

## Design: roles as an enrichment on the semantic layer

The semantic layer is already `{table: {col: {"term", "source", "provider"}}}`,
persisted per `schema_hash` in `context_store` and carried on `DBContext.semantic`.
Add one nullable field per column:

```python
semantic[table][col] = {
    "term": "age",            # existing: normalized label
    "role": "measure",        # NEW: semantic role (nullable)
    "source": "ai",           # existing provenance
    "provider": "local",
}
```

No new `DBContext` field and no new persistence path — the role rides inside the
column dict that is already built, cached, and reloaded. (Contrast with
`column_types`, which is separate because it comes from the dialect, not the AI.)

### Role taxonomy (closed set)

| role          | meaning                                  | example columns              |
|---------------|------------------------------------------|------------------------------|
| `identifier`  | surrogate/technical key                  | `id`, `uuid`, `customer_id`  |
| `person_name` | holds a human name                       | `name`, `first_name`, `username` |
| `label`       | non-person human-readable name           | `product_title`, `city_name` |
| `measure`     | numeric quantity you aggregate           | `amount`, `age`, `quantity`  |
| `temporal`    | date/time                                | `created_at`, `order_date`   |
| `dimension`   | low-cardinality categorical              | `status`, `city`, `country`  |
| `free_text`   | high-cardinality prose                   | `notes`, `description`       |

Closed set on purpose: the planner branches on it, so an open vocabulary would
reintroduce the guessing this is meant to remove.

## Population — assistive, cached, offline

Roles are labeled at **Analyze-Schema time only** (`process_schema` →
`context_store` rebuild), never in the hot `/query` path. Two-source, in priority
order, so a missing/failed AI label always degrades to a deterministic answer:

1. **Deterministic prior** (always runs): type + name heuristics.
   - `column_types` numeric + not a key → `measure`.
   - type is date/datetime → `temporal`.
   - name in `_IDENTIFIER_COLUMNS` or ends `_name` → `person_name`/`label`.
   - name ends `_id` or is `id` → `identifier`.
   - low distinct-count (see value-space below) → `dimension`.
2. **AI refinement** (when a provider answers): `ai_refine` already prompts
   *"You classify database columns. Return only one word."* and returns a `term`.
   Extend the prompt/parse to also return a role from the closed set. Store the
   AI role only when it is a valid taxonomy member; otherwise keep the prior.

`source: "ai" | "rule"` already records provenance — reuse it for the role.
This matches the project rule: **AI is assistive, the deterministic layer decides,
AI never enters the SQL path.**

## Consumption — planner reads roles, keeps deterministic fallback

- `_find_identifier_column(table, schema, semantic=None)`: prefer the column
  whose `role == person_name` (then `label`); fall back to the current
  `_IDENTIFIER_COLUMNS` + `_name`-suffix heuristic when no role is present.
  This alone fixes case 1 (the right identifier column, no positional guess).
- `extract_value_filters`: keep the grammar signals (possessive, `named/called`,
  entity-noun) as strong evidence, but replace the raw positional
  capitalized-token guess with a **role-aware** bind: a candidate literal grounds
  onto a `person_name` column. If several tables have one, prefer the table the
  rest of the pipeline already picked.
- Grouping hidden-key decision: a GROUP BY on a `person_name` column gets the
  table's key added (hidden) so two entities sharing a display name are not
  merged; a `label` (`title`) or `dimension` (`gender`) column does **not** —
  collapsing equal values there is the intent. Gating on the role, not a literal
  name list, is what keeps this schema-agnostic.

- Identifier detection is convention-agnostic: `is_identifier_name(name)` (this
  module) recognises a technical key as `id` / `pk` / `uuid` / `guid`, a
  snake_case `<x>_id`, **or** a camelCase `XxxID` (an uppercase `ID` preceded by a
  lowercase letter — so `GRID` / `PAID` are not keys). Every measure/identifier
  guard calls it, which is what stops a SQL-Server/.NET schema's `XxxID` keys from
  being summed as measures. It pairs with `intent_builder.split_identifier`, which
  tokenizes names across the same conventions for word-level matching.

Fallback is load-bearing: role is a *hint with a deterministic floor*, so a wrong
or absent role degrades to today's behavior — never a 500, never a silently wrong
bind. (Same lesson as global semantic-memory: assistive, not authoritative.)

## Value space — the tier that fixes `customers in Pune`

Roles say "`city` is a dimension" but not "`Pune` lives in `city`". To decide
`city = Pune` over `name = Pune` you need **data**, not grammar:

- At Analyze-Schema time, for each `dimension` column below a cardinality cap
  (e.g. `COUNT(DISTINCT) <= 500`), sample `SELECT DISTINCT <col> LIMIT N` and
  index the value → column map (persist beside the semantic layer, keyed by
  `schema_hash`).
- At query time, an ungrounded literal is matched against that value index first:
  `Pune ∈ city.values` → `WHERE city = 'Pune'`. Only if it matches no dimension
  value does the `person_name` fallback fire.

Cost/guardrails: only low-cardinality dimensions (bounded rows, one cheap query
per column at analyze time, cached). Skip high-cardinality/`free_text`. This is
strictly additive to roles and can ship later.

## Phasing

- **A** — deterministic role prior + store `role` in the semantic dict; rewire
  `_find_identifier_column` to consult it. Fixes case 1. Zero AI dependency.
- **B** — AI role in `ai_refine` (extend existing call), prior as fallback.
- **C** — dimension value-space index; role-then-value resolution in
  `extract_value_filters`. Fixes `customers in Pune`.

Each phase is independently shippable and degrades to the phase below.

## Tests

Extend `tests/schema_portability/*.json` with a `roles` expectation per schema and
add cases to `tests/test_record_lookup.py`:
- multi-identifier table (`first_name`/`username`) → literal binds to
  `person_name`, not positional.
- dimension value (`city` holds `Pune`) → `WHERE city = 'Pune'` (Phase C).
- determinism: with no AI provider, the role prior alone still grounds the
  identifier case (no network, deterministic SQL).
