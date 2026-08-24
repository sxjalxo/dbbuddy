# DB Buddy Documentation

Project documentation lives here. The top-level [README](../README.md) is the
overview and quick start.

Contributing? Start with [CONTRIBUTING.md](../CONTRIBUTING.md) (setup, tests, and the
project's non-negotiables), then [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) for the codebase
map. Reporting a vulnerability: [SECURITY.md](../SECURITY.md).

| Doc | What it covers |
| --- | -------------- |
| [CLI.md](CLI.md) | The Analyst's CLI: authentication, personal API keys, connections, querying, chart customization & publishing, and the CLI ↔ web-app parity table. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architecture: the deterministic pipeline, execution engine, dialect layer, relation graph, and the evidence-bound Insights Engine. |
| [SEMANTIC_ROLES.md](SEMANTIC_ROLES.md) | Semantic column roles (identifier / person_name / measure / dimension / …): the deterministic prior, AI overlay, and dimension value index that ground literals by column meaning instead of sentence position. |
| [ADDING_DATABASE_DIALECT.md](ADDING_DATABASE_DIALECT.md) | How to add a new SQL engine — the dialect contract, registry, pagination, and required contract + integration tests. |
| [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) | Contributor setup, tests, codebase map, and engineering conventions. |
| [SECURITY.md](SECURITY.md) | Auth/JWT, MFA, RBAC, encryption, safe query execution, AI output validation (prompt injection), and responsible disclosure. |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Production setup, configuration, environment variables, migrations, and the security checklist. |
| [QA_CHECKLIST.md](QA_CHECKLIST.md) | Manual + automated QA checklist and findings. |
| [PRE_DEPLOYMENT_REVIEW.md](PRE_DEPLOYMENT_REVIEW.md) | System-design review ahead of deployment: correctness and scalability findings, ordered by what breaks first, with a suggested sequence. **Start here before scaling past one worker** — it lists exactly which shared state is process-local and what that costs. |
| [STRESS_ASSESSMENT.md](STRESS_ASSESSMENT.md) | End-to-end stress test of every documented feature (8 datasets, ~1,000 automated tests, adversarial probes) plus a system-design and cybersecurity assessment. The harness-driven pass found nothing; a later pass that fuzzed the compiler directly with a hostile schema found four real defects the green suite had missed — all fixed, and the lesson is recorded there. |
| [ROADMAP.md](ROADMAP.md) | Where the project is going, what is explicitly not planned, and which items are open for contribution. |

## Before you deploy

Four settings do not have safe defaults for a real deployment. Full list and
rationale in [DEPLOYMENT.md](DEPLOYMENT.md#4-environment-variables); the security
reasoning in [SECURITY.md](SECURITY.md).

| Setting | Why it matters |
| --- | --- |
| `DBBUDDY_ENV=production` | Turns the dev-friendly defaults into fail-fast startup errors, so a missing `JWT_SECRET` / `APP_SECRET_KEY` / `ALLOWED_ORIGINS` / non-SQLite DB URL refuses to boot instead of running insecure. |
| `AI_PROVIDER_BLOCK_PRIVATE_NETWORKS=1` | Required for hosted / multi-tenant installs — an AI provider's `base_url` is a URL *this server* fetches. Cloud instance metadata is refused regardless; this also refuses LAN/loopback. |
| `LOGIN_GUARD_WORKERS` | Your uvicorn worker count. The login throttle is a shared Redis window; this sizes the fallback if Redis is unreachable. |
| `ERP_MAX_CONCURRENT_QUERIES` | Per-target ceiling on queries against a connected database — **per process**, so set it to `desired_total / worker_count`. |

## Supported database engines

MySQL, PostgreSQL, and SQL Server — all through one dialect layer. To add another,
follow [ADDING_DATABASE_DIALECT.md](ADDING_DATABASE_DIALECT.md).

## Running the live database tests

The dialect integration suite is opt-in (excluded from the default `pytest` run):

```bash
docker compose -f docker-compose.test.yml up -d
pip install pymssql
pytest -m integration
docker compose -f docker-compose.test.yml down -v
```

Every skip in the default run is environmental (live DB / `-m integration`).
There is no list of tests skipped for asserting stale contracts — see
[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md#on-skipping-tests) for why that list was
removed rather than maintained.
