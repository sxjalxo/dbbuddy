# Application-database migrations (Alembic)

Alembic is the single source of schema truth for the app DB. The URL is read
from `app_db.config.settings.APP_DATABASE_URL` (env `APP_DATABASE_URL`) by
`env.py` — it is **not** hardcoded in `alembic.ini`, so the CLI and the running
app always target the same database.

## How it runs

`init_db()` (called on app startup) runs `alembic upgrade head` automatically,
then seeds roles/permissions and the default org. You normally never run Alembic
by hand. The app no longer uses `create_all`.

## Manual commands (from the `backend/` directory)

```bash
# set the target DB (defaults to the dev sqlite file)
export APP_DATABASE_URL="sqlite:///./dbbuddy_app.db"

alembic upgrade head          # apply all migrations
alembic current               # show the current revision
alembic history               # list migrations
alembic downgrade -1          # roll back one
alembic revision -m "msg"     # new (empty) migration; --autogenerate to diff models
```

## Revisions

- **0001 baseline** — the schema as of Milestone 1 (`organization_id` nullable,
  no `is_default`/`slug`). Matches what the old `create_all` produced.
- **0002 org tenancy** — adds `organizations.is_default` + `slug`, generates
  slugs, ensures a default org, back-fills org-less users, then makes
  `users.organization_id` NOT NULL.
- **0003 publication record** — `published_reports`: rename `saved_chart_id` →
  `chart_id`, add `status`/`visibility`, NOT NULL `organization_id`.
- **0004 audit enrichment** — `audit_logs`: add `organization_id` + `ip_address`,
  back-fill org, normalize legacy dotted actions to entity + bare action.
- **0005 request correlation id** — `audit_logs.request_id` (per-request id).
- **0006 add mfa support** — `users.mfa_enabled` / `mfa_secret` /
  `mfa_recovery_codes`.
- **0007 add scheduled jobs** — `scheduled_jobs`, `job_runs`, `notifications`.
- **0008 add personal API keys** — `api_keys` (CLI/automation; only the hash is
  stored, never the key).
- **0009 add token version** — `users.token_version`, backing stateless
  refresh-token revocation (logout / MFA-disable / deactivation bump it).
- **0010 add execution tokens** — `execution_tokens`: single-use, short-lived
  grants that let `/execute` run server-stored SQL so a confirmed write cannot be
  altered client-side.
- **0011 add AI provider configs** — `ai_provider_configs`: per-org provider
  records (adapter, base URL, model, Fernet-encrypted key, priority, fallback
  chain), replacing env-var provider selection.
- **0012 add chart visual config** — `saved_charts.config` + `schema_fingerprint`.
- **0013 add schema snapshots** — `schema_snapshots`: cached per-connection schema
  JSON that the relation graph is built from (one row per connection).
- **0014 add insight cache** — `insight_cache`: generated Insights Engine bundles
  keyed by `sql · result_hash · connection_id · prompt_version · provider · user_id`.
  The key includes `user_id` because the lookup is user-scoped; see QA #19.
- **0015 add dashboards** — `dashboards`, `dashboard_items` (one pinned chart per
  row, unique on `(dashboard_id, chart_id)`), and `published_dashboards` (a
  publication record shaped like `published_reports`).

## Adopting an existing (pre-Alembic) database

A database created before Alembic has the app tables but no `alembic_version`.
`init_db()` detects this and **auto-stamps it at `0001`** before upgrading, so an
M1 database migrates forward cleanly with no manual step.

> Caveat: a *dev* database created during the brief window when M2 ran under
> `create_all` (has `is_default` but no `alembic_version`/`slug`) cannot be
> auto-adopted. These are disposable dev SQLite files — delete
> `backend/dbbuddy_app.db` and let migrations recreate it.

## Notes

- SQLite uses Alembic **batch mode** (`render_as_batch`, set in `env.py`) so
  ALTER COLUMN / constraint changes work via table-rebuild.
- The `users → organizations` FK keeps `ON DELETE SET NULL` on SQLite; with the
  column now NOT NULL that is equivalent to RESTRICT. On PostgreSQL, 0002
  retargets it to an explicit `ON DELETE RESTRICT`.
