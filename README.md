# DB Buddy

**An explainable, deterministic, self-learning NL→SQL engine for real databases.**

[![CI](https://github.com/sxjalxo/dbbuddy/actions/workflows/ci.yml/badge.svg)](https://github.com/sxjalxo/dbbuddy/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Engines: MySQL · PostgreSQL · SQL Server](https://img.shields.io/badge/engines-MySQL%20%C2%B7%20PostgreSQL%20%C2%B7%20SQL%20Server-informational)](docs/ADDING_DATABASE_DIALECT.md)

Most NL→SQL tools prompt an LLM and hope: they hallucinate columns, break on joins, and
cannot explain their output. DB Buddy **compiles** SQL instead of generating it.

- **Deterministic** — the planner produces an execution plan; a Predicate AST renders it
  to SQL. The same question against the same schema yields the same query, always.
- **Explainable** — every result carries the reasoning: grouping, aggregation, join
  inference, ranking.
- **AI never writes SQL.** It labels the schema semantically, once, at analyze time. If no
  model is reachable the engine falls back to deterministic rules and honestly reports
  `AI used: No`.
- **Self-learning, guarded** — learns that "revenue" means `payments.amount`, behind
  frequency thresholds, noise filtering, temporal decay, and memory caps. Learning is
  assistive, never authoritative.

---

## Try it in one command

```bash
docker compose up
```

Open **http://localhost:3000** and sign in as `analyst@dbbuddy.io` / `Analyst#12345`.
A sample ERP database — customers, orders, order items, payments, regions — is already
connected. Click **Analyze Schema** once, then ask:

> total amount by segment last quarter

You get the answer, the SQL, and the reasoning behind it — which tables it joined, which
column it treated as the measure, and why:

```sql
SELECT customers.segment, SUM(payments.amount) AS sum_amount
FROM customers
JOIN orders   ON customers.customer_id = orders.customer_id
JOIN payments ON orders.order_id = payments.order_id
WHERE payments.paid_at >= '2026-04-01' AND payments.paid_at < '2026-07-01'
GROUP BY customers.segment;
```

Three tables joined from declared foreign keys, the measure picked by column meaning, and
the quarter resolved against the payment date — none of it prompted from an LLM.

> The demo's secrets and passwords are committed and identical everywhere, so it is a
> demo, not a deployment. `docker compose down -v` removes it entirely. For anything real,
> start at [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Install it properly

**Prerequisites:** Python 3.10+ and a MySQL, PostgreSQL, or SQL Server database.

```bash
git clone https://github.com/sxjalxo/dbbuddy.git
cd dbbuddy

python -m venv .venv
source .venv/bin/activate     # Linux/macOS
.venv\Scripts\activate        # Windows

pip install -r requirements.txt
pip install -e .
```

Verify the install with no database required:

```bash
python scripts/run_validation.py
```

Then query your database offline — no backend, no login, password prompted:

```bash
dbbuddy analyze --local --engine mysql --host localhost --user root --database sales
dbbuddy query  --local --engine mysql --host localhost --user root --database sales "Show all users"
dbbuddy chat   --local --engine mysql --host localhost --user root --database sales
```

`--local` runs the engine in-process. For the full multi-user platform — web app, shared
history, saved charts, audit — start the backend and sign in:
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**. For everything the CLI can do:
**[docs/CLI.md](docs/CLI.md)**.

> If the `dbbuddy` console script isn't on your PATH, use `python -m dbbuddy …`.

---

## Example

**Question:** `top 3 users by total payments`

```sql
SELECT users.name, SUM(payments.amount)
FROM users
JOIN payments ON users.id = payments.user_id
GROUP BY users.id, users.name
ORDER BY SUM(payments.amount) DESC
LIMIT 3;
```

**Explanation returned with it:** grouping on `users`; aggregation `SUM(payments.amount)`;
join routed from the declared foreign key; ranking `DESC` + `LIMIT 3`.

---

## How it works

```
User Query
  ↓
Semantic Enhancer (memory; AI-assisted enrichment)
  ↓
Intent Builder
  ↓
Query Planner (deterministic plan)
  ↓
SQL Compiler (Predicate AST) ──→ Dialect layer (MySQL / PostgreSQL / SQL Server)
  ↓
Execution Engine (parameterized, safe)
  ↓
Explainability Engine
  ↓
Learning Engine
```

The planner emits an engine-agnostic plan. The compiler renders it through a **Predicate
AST** — first-class operators (`=`, comparisons, `LIKE`/`ILIKE`, `BETWEEN`, `IN`/`NOT IN`,
`IS [NOT] NULL`, `EXISTS`/`ANY`/`ALL`) that each know how to render themselves. The dialect
layer is the only code that knows vendor SQL; there are no `if engine == "postgres"`
branches in the planner.

Filters are **schema-driven and type-aware** — numeric, date, boolean, and text
comparisons, plus calendar and relative dates (`last month`, `past 30 days`) — derived
from column types, with no hardcoded domain values anywhere in the SQL path. Joins route
from **declared foreign keys**, which is what makes it work on cryptic ERP schemas whose
key names follow no convention.

Full detail: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Performance

The per-database context — connection pool, schema, AI-refined semantic layer, vector
index, relationship graph — is built **once per `(host, database, schema_hash)`** and
reused, not recomputed per query. A schema change yields a new key and an automatic
rebuild, so stale joins are never served.

| Step | Latency |
| ---- | ------- |
| Analyze Schema (one-time) | ~5–6 s |
| Cold query (first; loads from disk) | ~150–250 ms |
| Warm query | ~5–15 ms |

You can query the moment you connect: while a background analyze runs, queries use fast
rule-based labels (*Fast mode*) and upgrade to AI-enhanced labels when it finishes.

---

## Supported engines

| Engine | Identifier | Driver |
| ------ | ---------- | ------ |
| MySQL / MariaDB | `mysql` | `mysql-connector-python` |
| PostgreSQL | `postgresql` | `psycopg2` |
| SQL Server | `sqlserver` | `pymssql` |

Each engine implements one `Dialect` contract covering connection lifecycle, schema
introspection, SQL fragments, and capability flags — so adding an engine means
implementing the contract, not editing the planner. Drivers load lazily; a missing
optional driver never breaks the others.

**Adding one:** [docs/ADDING_DATABASE_DIALECT.md](docs/ADDING_DATABASE_DIALECT.md).

---

## Platform

On top of the engine, DB Buddy is a multi-user platform with its own application database
(PostgreSQL in production; your business databases are *query targets only*):
organizations and RBAC, JWT auth with MFA and stateless session revocation, personal API
keys, an audit dashboard, saved and published charts, dashboards, a 3D relation graph, an
evidence-bound Insights Engine, and scheduled jobs.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#platform-layer-auth-rbac-multi-tenancy-publishing-audit-mfa-jobs)
and [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Safety

- **Parameterized queries** — values are bound, never interpolated into a statement.
- `SELECT` executes directly (switchable to review-first); writes require confirmation.
- Writes confirmed from natural language run **server-stored SQL** through a single-use
  execution token — the SQL a client posts alongside a token is ignored. Raw write
  execution needs the explicit `query:write:manual` permission.
- Dry-run previews and foreign-key dependency warnings before a destructive statement.
- Encryption at rest for connection credentials and MFA secrets; CORS allow-list, never a
  credentialed wildcard; SSRF guard on operator-supplied outbound URLs; opaque 500s so
  driver errors never echo a DSN back to a caller.
- `DBBUDDY_ENV=production` refuses to start without `JWT_SECRET` (≥ 32 bytes),
  `APP_SECRET_KEY`, a non-SQLite app database, and `ALLOWED_ORIGINS`.

Full model: **[docs/SECURITY.md](docs/SECURITY.md)**. To report a vulnerability:
[SECURITY.md](SECURITY.md).

---

## Testing

```bash
pytest                    # unit suite
DBBUDDY_STRICT=1 pytest   # contract violations raise instead of being recovered
pytest -m integration     # live MySQL/PostgreSQL/SQL Server (needs Docker)
```

Correctness is checked three ways: a **schema-adaptive behavioral suite** that validates
intent against any connected schema without hardcoded expectations; a **schema-portability
corpus** of realistic ERP schemas (SAP, Odoo, ERPNext, healthcare, e-commerce, banking);
and **dogfood suites** that run the full pipeline against eight populated datasets — from
generated `erp`/`hospital`/`legacy`/`tpch`/`tpcds` to the real MySQL `employees` sample
(~4M rows), Microsoft's `adventureworks` OLTP (68 tables), and `airportdb` (up to ~59M
rows) — asserting invariants such as grouped totals summing back to the ungrouped total.

Details in [CONTRIBUTING.md](CONTRIBUTING.md#testing) and
[docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md).

---

## Project structure

```
dbbuddy_core/    → the deterministic engine (planner, compiler, execution, dialects/)
backend/         → FastAPI API + application database (app_db/, Alembic migrations)
frontend/        → React / TanStack Start UI
dbbuddy/         → the CLI
tests/           → pytest suite
scripts/         → dogfood correctness suites, benchmarks, developer utilities
docs/            → documentation
```

---

## Documentation

| Doc | Covers |
| --- | ------ |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | The pipeline, execution engine, dialect layer, relation graph, Insights Engine |
| [CLI.md](docs/CLI.md) | The analyst's CLI and its parity with the web app |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production setup, configuration, migrations, security checklist |
| [DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) | Codebase map, setup, tests, engineering conventions |
| [SECURITY.md](docs/SECURITY.md) | Auth, RBAC, encryption, safe execution, AI output validation |
| [SEMANTIC_ROLES.md](docs/SEMANTIC_ROLES.md) | How literals are grounded by column meaning, not word order |
| [ADDING_DATABASE_DIALECT.md](docs/ADDING_DATABASE_DIALECT.md) | Adding a SQL engine |

Full index: [docs/](docs/).

---

## Contributing

Contributions are welcome — start with [CONTRIBUTING.md](CONTRIBUTING.md), especially the
**non-negotiables** (no hardcoded schema knowledge; planner changes bump `PLAN_VERSION`;
validators enforce what prompts merely request).

Found a wrong answer? The [wrong-answer issue template](.github/ISSUE_TEMPLATE/wrong_answer.yml)
asks for a minimal schema — it turns your bug into a permanent regression test.

## License

[Apache License 2.0](LICENSE)
