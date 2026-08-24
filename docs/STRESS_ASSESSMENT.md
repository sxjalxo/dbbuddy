# DB Buddy — Stress-Test, System-Design & Cybersecurity Assessment

**Date:** 2026-07-23 · **Superseded in part by the 2026-07-28 update below.**
**Scope:** Every feature described in `docs/`, exercised end to end.
**Constraint honored:** No hardcoded fixes introduced — every defect below is fixed by a
general mechanism, per the project's [non-negotiables](../CONTRIBUTING.md#non-negotiables).
**Bottom line (2026-07-23):** No defects found by the harness-driven pass. **This did not hold up** — a later probing pass (2026-07-28) that fuzzed the compiler directly with a hostile schema found **four** real core-logic defects the green test suite and the 8-dataset dogfood did not surface. The lesson is recorded below: *a green suite proves the cases the suite encodes, not the cases it omits.*

---

## Update — 2026-07-28 (direct adversarial pass)

The 2026-07-23 pass drove the shipped harnesses to their limits and found nothing.
A second pass instead **fuzzed the pipeline directly** — a synthetic schema built
from reserved words (`order`, `group`), a space-named column (`total amount`), a
non-ASCII table (`café`), a numeric column named `time`, and dual-date tables,
crossed with adversarial NL (injection strings, NUL bytes, 2 000-char input) — and
found four core-logic defects. All four are fixed, each with a regression test and no
hardcoded schema knowledge.

| # | Defect | Where | Impact |
| - | ------ | ----- | ------ |
| 1 | `WHERE`/`HAVING` condition columns emitted **bare** — a filter on a reserved-word table compiled to `... FROM "order" WHERE order.status = %s` (syntax error) | `sql/compiler.py` (`_quote_condition_columns`) | Any schema with a reserved-word table + a filter was unqueryable. Common ERP names (`order`, `user`, `group`). |
| 2 | `ORDER BY` simple-column paths emitted the raw column (`ORDER BY group DESC`) and dropped the table qualifier | `sql/compiler.py` (4 emission sites) | Reserved-word ordering → syntax error; qualifier loss → ambiguous on a join. |
| 3 | Name-convention temporal detection promoted a **numeric** column named `time`/`date` to a date, applying an ISO-date range to a float | `type_handlers.py` (`_looks_temporal`) | "average time last month" on a `REAL time` column silently returned the wrong rows. |
| 4 | Insights causal-grounding validator split the clause on any `.`, truncating `"15.3% decline in revenue"` at `15` and deleting a **correct**, column-grounded finding | `insights/validators.py` | Deleting a right finding — the costliest error the engine's own trust model names. |

Bugs 1 and 2 are the same class: the "compiler quotes identifiers" property was
true for `SELECT`/`FROM`/`GROUP BY`/`HAVING` but had never been checked at the
`WHERE` and `ORDER BY` rendering sites. Quoting now covers **every** clause; see
[ARCHITECTURE.md → Identifier quoting](ARCHITECTURE.md#identifier-quoting).

Why the suite missed them: the dogfood datasets use ordinary snake_case / camelCase
identifiers, so no probe ever put a reserved word in a `WHERE`/`ORDER BY`, and the
numeric-`time` and decimal-clause shapes were simply absent from the corpus. Tests
encode the cases their authors thought of; adversarial fuzzing supplies the ones
they didn't.

### Platform routers — audited, clean

The routers not exercised by the SQL fuzz were read for authz/IDOR/ownership:
`admin`, `orgs`, `reports`, `dashboards`, `jobs`, `charts`, `keys`, `history`,
`notifications`, `audit`. Every by-id access enforces `user_id == caller` or
org-scoping (cross-tenant/cross-user → `404`/`403`); the privilege-escalation
boundary (`ORG_ADMIN_GRANTABLE`), self-delete/self-deactivate guards, and the
scheduler's idempotent multi-worker fire-claim all hold. **No new defects in the
platform layer** — it is more uniformly authorized than the compiler was.

### Interfaces — audited, clean

CLI `session.py` (token refresh, env-key never persisted, server-side ownership),
frontend `client.ts` (single-flight refresh, empty-body guard), and
`ChartRenderer.tsx` (untrusted published `config` colors reach React SVG `fill`
attributes, which React escapes — no XSS) are sound. Two cosmetic nits noted, no
code change: CLI token file is `chmod`-ed after write (tiny POSIX window);
`apiJson` uses raw `res.json()` rather than the empty-body-tolerant `readJson`.

### Deployment readiness (unchanged by this pass)

The pre-existing gates still stand: the P0 process-local state (token revocation,
ERP ceiling, breaker) must move to shared Redis before running **>1 worker**;
`AI_PROVIDER_BLOCK_PRIVATE_NETWORKS=1` and DNS-rebinding pinning for multi-tenant;
the production config checklist in [SECURITY.md](SECURITY.md). Verdict:
**single-tenant + single-worker + config checklist + these four fixes committed →
deployable, low risk.** Multi-worker or multi-tenant → not yet.

---

## 1. What was tested and how

The system ships its own stress harnesses, so testing drove those to their limits rather than inventing ad-hoc checks. Everything ran serially in small subsets (per the machine-resource constraint — parallel heavy runs have crashed this laptop before).

### 1.1 Correctness — dogfood pipeline (invariant-scored, no hardcoded expected values)

The full NL→SQL pipeline (semantic enhance → intent → plan → compile → execute) was run against **all 8 datasets**, rule-based engine only (AI off, so failures are reproducible and attributable to a layer):

| Dataset | Rows / shape | Probes | Result |
| --- | --- | --- | --- |
| erp (generated) | 36,562 rows / 12 tables | 54 | **PASS** — 40 checks |
| hospital (generated) | — | 77 | **PASS** — 58 checks |
| legacy (generated) | — | 34 | **PASS** — 10 checks |
| tpch (generated) | 432k rows | 85 | **PASS** — 58 checks |
| employees (real MySQL sample) | ~4M rows | 41 | **PASS** — 27 checks |
| tpcds (official DDL, 104 FKs) | — | 66 | **PASS** — 52 checks |
| adventureworks (SQL-Server/.NET convention) | 68 tables / 16 schemas | 37 | **PASS** — 27 checks |
| airportdb (real MySQL dump, tier `s`) | 859k+ rows | 62 | **PASS** — 36 checks |

- **Shuffled-order run** (erp, randomized suite order — the shared-state probe that catches one query's context/cache/memory leaking into the next): **PASS**, identical result. State does not depend on question order.
- **Naming-convention agnosticism confirmed live:** snake_case, camelCase/PascalCase (`SalesOrderID`), cryptic keys, and `*key` warehouse columns all planned correctly across datasets without a name list.

### 1.2 Determinism, cache consistency, adversarial handling

`SystemValidator`: **PASS** — 3/3 deterministic, cache-consistency match, 6/6 adversarial queries handled, 7 ms avg retrieval latency.

### 1.3 Schema-adaptive capability matrix

`exercise_schemas.py` across SAP/Odoo/ERPNext/healthcare/banking/ecommerce schemas: every predicate kind emitted correctly — equality, numeric comparison, currency/thousands/percentage parsing, unit- and money-suffixed columns, boolean adjective phrasing, `IN`, negation, `IS NULL`, `LIKE`, multi-filter. Exotic type classification (uuid/jsonb/array/timestamptz/money/numeric) all mapped to the right handler family.

### 1.4 Automated test suite (`pytest`)

Run in four serial subsets covering the whole tree — **~1,033 passed, 0 failed** (a handful of environment skips for live-DB integration; 29 subtests passed). Coverage included: planner determinism, SQL predicate AST & conditions, dialect contract (MySQL/Postgres/SQL Server), type handlers, semantic roles, relationship graph, confidence, camelCase/prefixed/reserved-word identifiers, star-schema, record lookup, schema portability, behavioral suite, semantic-memory isolation, QA-fix regression sets, ERP backpressure, fetch cap, RBAC enforcement, MFA, API keys, execution tokens, hardening, audit, org management, login guard, publishing, dashboards, jobs, relations API, AI providers & resilience, insights engine/API/dogfood, CLI enforcement & insights, e2e real execution.

### 1.5 Targeted adversarial probing of the highest-value claims

Beyond the suites, four security/correctness boundaries were driven directly:

- **Identifier quoting** — `order`, `group`, `to`, `total amount`, `café`, `1weird` (digit-leading), and dotted `competitor_prices.price` all quote correctly through the real compile path (`_needs_quoting` → `dialect.quote_identifier`); `normal_col` stays bare (selective quoting preserves copy-pasteable SQL). *(A first-pass mis-read of padded console output suggested `group`/`1weird` weren't quoting; a clean re-run with delimiters proved they do — false alarm, no code change.)*
- **SQL injection via NL values** — `"order"; DROP TABLE "Customer"; --` and equivalents on tpcds/adventureworks were **classified dangerous and held for confirmation**, never executed. Values are parameterized (`%s` + bound params), never interpolated.
- **Insights prompt-injection validators** (the actual security boundary — post-hoc, run regardless of what the input said):
  - "…because of supply chain issues" (external cause, no column/figure) → **flagged ungrounded**.
  - "…because of a 47% pandemic effect" (fabricated figure not in result values) → **flagged ungrounded**.
  - "…because of a 38% decline in revenue" (38 is a real result value, names a real column) → **allowed**.
  - Speculation, `BANNED_TOPICS` (weather), and SQL-in-output all detected.
- **Relation-graph taper (QA #12)** — `_token_weight` is **monotonic non-increasing and continuous** across fan-out 1→200; the 8→9 transition drops only 0.111 (no cliff), and the compute bound engages exactly at fan-out ≈160 where weight ≲0.05, as documented.

### 1.6 Performance benchmarks

`benchmarks/run_all.py --check` reported 10 "gated regressions" — but the harness itself discloses the cause: **the committed baseline predates environment recording and was captured on a different (faster) reference machine**, so its wall-clock numbers are not comparable to this laptop. These are not code regressions. The self-disclosure ("this baseline… cannot be checked against this machine") is exactly the honesty the design prizes. *Action for the team: re-record `baseline.json` with `--update` on the reference machine — not on this laptop, which would silently lower the bar for everyone.*

---

## 2. System-design assessment

### Strengths (verified, not just claimed)

1. **The load-bearing invariant holds: AI assists, the system decides.** SQL is only ever produced by the deterministic planner → Predicate AST → dialect compiler. AI is confined to schema labeling and evidence-bound insight prose. No probing found a path where model output becomes SQL. This is the single property that makes the rest of the safety story credible, and it is architecturally enforced (the insights package cannot even import `backend/app_db`), not merely conventional.

2. **Determinism is real and defended.** Resolution order is order-preserving (no `list(set(...))`), verified live via the shuffle run and the determinism validator. This is the difference between a demo and a system an analyst can trust to give the same answer twice.

3. **Schema-adaptivity generalizes.** The same engine planned correctly across 8 structurally different real and synthetic schemas — snake_case ERP, .NET PascalCase, warehouse `*key`, cryptic SAP-style keys — with no per-schema code. Filter extraction, key resolution, and FK/relationship inference are driven by declared metadata (`column_types`, PKs, FKs, value index), not name lists. This is the property the "no hardcoding" hard rule protects, and it is intact.

4. **"Down-weight, don't delete" is applied consistently** across ranking surfaces (relation graph taper, insights hedged-finding taper). Verified continuous/monotonic, which is what prevents the "connect one more database and the graph blanks" class of bug.

5. **Every bound is disclosed rather than hidden** — chart row caps (`row_count`/`truncated`), relation-graph edge cap (`links` vs `total_links` + `truncated`), dashboard cache age (`fetched_at`/`cached`), AI provenance (`source: ai|rule`). A truncated or cached result never masquerades as the whole/live truth.

6. **The target database is treated as a protected external system, not the product's own.** Statement timeouts, per-target backpressure (semaphore is the real ceiling, not the idle pool), derived dashboard fan-out, and distinct `502/503/500` operator signals. The worst failure mode for an analytics tool — taking down a production ERP — is specifically engineered against.

### Design risks / limits (all documented, none are defects)

1. **Process-local shared state caps the app at effectively one worker for a few concerns** (`PRE_DEPLOYMENT_REVIEW.md` P0). Per-DB context, AI metrics, AI circuit breaker, ERP semaphores, and the access-token revocation cache are per-process. With N workers: cold-cache inconsistency until each warms, N× the intended ERP load unless `ERP_MAX_CONCURRENT_QUERIES` is divided down, breaker retried N× longer, and a revoked access token honored for up to `AUTH_REVOCATION_CACHE_TTL` (10 s) on workers that didn't handle the logout. **This is the first thing to fix before horizontal scaling** and the docs say so plainly. A true global ERP ceiling and global breaker need Redis.

2. **Query rate limiter fails open.** By design (it protects throughput, not auth), but it means it is a fairness mechanism, not an abuse control, on single-node deployments with no Redis. Correct trade — just size expectations accordingly.

3. **Relation-graph layout is O(n²) synchronous on the main thread** (QA R4) — ~~bounds practical graph size on the client~~. **Superseded (2026-07-27):** `RelationGraph.tsx` now renders in WebGL 3D (`react-force-graph-3d` → three.js); layout is `d3-force-3d` stepped per animation frame, so the synchronous main-thread ceiling is gone. What remains is server-side: the detail graph still has no server-side size cap (server half of QA #17), so very large schemas ship a large payload even though the 3D view renders them.

4. **Insights prompt cost is unbounded upward** — 5000×40 builds a ~18k-token prompt; a small-context model returns 4xx → failover → honest bundle, so correctness is safe but cost/latency on large results is future work (prompt-budget pass).

### Recommended sequence (from the review, confirmed still current)

1. Re-record benchmark baseline on the reference machine (removes a false-alarm signal).
2. Move the P0 process-local security/limit state (revocation cache, ERP ceiling, breaker) to shared Redis before running >1 worker.
3. Prompt-budget pass for Insights.
4. Replace the relation-graph quadratic pair generation (QA #13) so the compute bound can be retired.

---

## 3. Cybersecurity assessment

### Threat model the system correctly recognizes

DB Buddy queries databases it does not own, on behalf of multiple tenants, using AI over attacker-influenceable data. The three attacker-controlled surfaces are: **(a)** natural-language input, **(b)** result-set cell contents from a target database, **(c)** client-held follow-up history. The design treats all three as untrusted, and testing confirmed the controls are enforcement, not decoration.

### Controls verified

| Control | Mechanism | Verified |
| --- | --- | --- |
| SQL injection via values | Parameterized execution (`%s` + bound params); inlined string is display-only | Injection strings held for confirmation / never interpolated |
| Injection via NL structure | Deterministic compiler; NL never becomes raw SQL | 8-dataset dogfood, adversarial suite |
| Write safety | Read/write classification; confirmed-write **execution tokens** (single-use, 5-min TTL, SHA-256-stored, bound to user + `context_hash`, redeems server-stored SQL); raw writes need `query:write:manual` | `test_execution_tokens`, `test_security` pass |
| Prompt injection (AI output) | Grounding-over-denylisting: post-hoc validators remove invented causality, taper hedged findings; run regardless of input | Direct probe — fabricated causes/figures flagged, grounded allowed |
| AuthN | Argon2 password hashing; HS256 JWT access+refresh; stateless refresh revocation via `token_version` | `test_mfa`, `test_api_keys`, `test_login_guard` pass |
| AuthZ (RBAC) | Every endpoint gated; analyst-only surfaces (relations, insights) enforced server-side, not just hidden | `test_rbac_enforcement`, `test_relations_api`, `test_insights_api` pass |
| Login throttling | Shared Redis sliding window; **degrades closed** (stricter local window, cap ÷ `LOGIN_GUARD_WORKERS`) when Redis is down | `test_login_guard`, `test_qa_fixes` pass |
| Secrets at rest | Fernet (ERP passwords + AI keys), key from `APP_SECRET_KEY`; never echoed; `credentials_ok`/`credentials_ok` heal flow | `test_security`, `test_ai_providers` pass |
| SSRF | AI-provider `base_url` validated on create **and** patch; cloud metadata refused always; LAN/loopback refused when `AI_PROVIDER_BLOCK_PRIVATE_NETWORKS=1` | Documented control; required for multi-tenant |
| CORS | Explicit allow-list, never wildcard; `ALLOWED_ORIGINS` required in prod | Startup validation |
| Tenant isolation | Per-`(host\|database\|engine)` semantic memory partitioning; org-scoped provider records; user-scoped insight cache keys | `test_semantic_memory_isolation` pass; QA #19 (cache key/read-scope match) resolved |
| Fail-fast config | `DBBUDDY_ENV=production` refuses to boot without `JWT_SECRET`/`APP_SECRET_KEY`/`ALLOWED_ORIGINS`/non-SQLite DB | Documented |
| Auditability | Every `/execute` classified + logged with `source`; audit rows survive user deletion (actor nulled) | `test_audit`, `test_execution_tokens` pass |

### Security observations & residual risk

1. **Access-token revocation converges, it is not instant.** On multi-worker deployments a revoked access token remains valid up to `AUTH_REVOCATION_CACHE_TTL` (default 10 s) on workers that didn't process the logout/deactivation. Bounded and documented; acceptable for most deployments, but for high-assurance environments make revocation global (shared Redis) — same fix as the P0 state above. Refresh tokens are already revoked immediately via `token_version`.

2. **JWT is HS256 (shared secret).** Fine for a single-org symmetric deployment; if signing ever needs to be delegated or verified by a party that shouldn't be able to mint tokens, move to asymmetric (RS/ES256). Not a current gap.

3. **Insights validators are the boundary, and they are permissive on missing evidence by design** — when result values are unavailable to a caller, the figure-grounding check leans on the column-name net rather than deleting a possibly-correct finding. This is the right trust trade (a false positive silently deletes a correct finding), but it means the strongest form of the check depends on the caller threading through `grounded_numbers`. Worth a periodic audit that all insight call sites do.

4. **Prompt-injection mitigation is defense-in-depth, honestly labeled.** Prompt hardening is described as reducing how often the boundary is tested, *not* as a control — the validators are the control. This is the correct framing; teams should not be tempted to weaken the validators because the prompt "already handles it."

5. **No secrets or generated SQL ever reach published-report clients** — verified in the publishing/dashboard model (clients render from live re-query through `ChartRenderer`, never receiving SQL or credentials).

### Cybersec verdict

The security posture is **strong and, unusually, honest about its own edges**. Every control that weakens under scale (rate limiter fails open, revocation cache converges, per-process ERP ceiling) is documented at the exact place it matters, with the asymmetry reasoned through (throughput limiter yields; auth throttle degrades closed). The prompt-injection model correctly places enforcement in post-hoc validation rather than prompt wording. The one item to prioritize before scaling past a single worker is moving the security-sensitive process-local state (token revocation, and to a lesser extent the ERP ceiling and breaker) into shared storage.

---

## 4. Conclusion *(2026-07-23 — read with the 2026-07-28 update at the top)*

Every feature described in the documentation was exercised — 8 datasets through the full pipeline, ~1,033 automated tests, the determinism/adversarial validator, the capability matrix, and direct adversarial probes of the four highest-value security/correctness boundaries. **No defects were found**, so no code changes were made and nothing was committed. The single reported benchmark "regression" is a cross-machine baseline artifact the harness itself flags, not a code issue.

The system does what its docs claim, its safety and security boundaries are enforced rather than aspirational, and its known limits are documented at the point they bite. The highest-leverage next step is not a bug fix but a scaling prerequisite: relocate the process-local security-sensitive state into shared Redis before running more than one worker.
