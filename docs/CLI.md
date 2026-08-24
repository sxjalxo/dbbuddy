# DB Buddy CLI — the Analyst's guide

The DB Buddy CLI is a **power tool for Analysts**. It does everything you'd do in
the web app — connect to an ERP database, analyze its schema, ask questions in
natural language, and manage saved connections — but from your terminal, where
it's scriptable and pipeable.

The CLI is a **first-class client of the DB Buddy backend**, not a separate
program with its own data. You authenticate once, and every command calls the
same REST API the web app uses. That means:

- **Everything syncs.** A connection you save from the CLI shows up in the web
  app; query history recorded from the CLI is the same history you see online.
  There is one platform, with two front-ends.
- **The same rules apply.** Authentication, RBAC permissions, organization
  scoping, and audit logging are enforced by the backend — the CLI can't bypass
  them.

> The CLI never talks to an ERP database directly (except in explicit
> [`--local` mode](#local-offline-mode)). Reaching the ERP, decrypting stored
> credentials, and recording history are the backend's job.

---

## Who the CLI is for

| Role | Uses the CLI? | Where they work instead |
| ---- | ------------- | ----------------------- |
| **Analyst** | Yes — this is the target user | CLI **and** web app |
| **Admin** | No | Web console (users, orgs, audit, security, system settings) |
| **Client / User** | No | Web app (published reports & dashboards, read-only) |

This is **enforced, not just documented.** Every data command checks that your
account holds the `query:run` permission (the core Analyst grant). An admin or
client who runs `dbbuddy query …` is turned away:

```text
DB Buddy CLI is only available to accounts with the 'query:run' permission
(the Analyst role).
Admin and client accounts manage the platform / view reports from the web
console instead. Contact your administrator if you need CLI access.
```

The check is keyed off the **permission**, not a hard-coded role name — so if a
new role is ever granted `query:run`, it gets CLI access with no code change.

---

## Configuring the backend address

The CLI needs to know where the backend is. It resolves the address in this
order:

1. `--api-url https://dbbuddy.example.org` on any command
2. the `DBBUDDY_API_URL` environment variable
3. the value saved at login
4. the default `http://localhost:8000`

---

## Authenticating

You authenticate once; the CLI caches short-lived JWT tokens (and, for API-key
logins, the key) under `~/.dbbuddy/credentials.json` with owner-only
permissions, and refreshes them transparently. Override the location with
`DBBUDDY_CONFIG_DIR`.

### Interactive login (email / password / MFA)

```bash
dbbuddy login                       # prompts for email + password
dbbuddy login --email you@org.com   # prompts for password only
```

If your account has MFA enabled, you'll be prompted for a TOTP or recovery code
to finish signing in — exactly like the web app.

### Personal API keys (scripts & automation)

For unattended use (cron jobs, CI, dashboards) use a **personal API key**. It's a
long-lived credential that carries *your* permissions, and it's exchanged behind
the scenes for the same JWT tokens a normal login produces — so nothing about
the authorization model changes.

```bash
# 1. Mint a key (interactively, once). The secret is shown ONE time — copy it.
dbbuddy keys create --name ci-pipeline

# 2. Authenticate with it
dbbuddy login --api-key            # prompts for the key (kept off your shell history)
dbbuddy login --api-key dbk_ab12…   # or pass it directly

# 3. …or skip login entirely in automation with an env var:
export DBBUDDY_API_KEY=dbk_ab12…
dbbuddy query "Monthly revenue" --connection prod --json > out.json
```

Manage keys from either the CLI or the web app — they're the same list:

```bash
dbbuddy keys list                  # id, name, prefix, last-used, active/revoked
dbbuddy keys revoke <key-id>       # immediately stops working everywhere
```

A revoked key is refused at exchange time, and every key always reflects your
**current** roles — revoke a role and each of your keys narrows with it.

### Who am I?

```bash
dbbuddy whoami        # email, roles, permissions, backend URL, CLI-access yes/no
dbbuddy logout        # clears cached credentials
```

---

## Managing connections

Saved connections live in the platform (their ERP passwords encrypted at rest),
shared with the web app. Reference one by **name or id** in query/analyze so
your work is attributed and recorded.

```bash
dbbuddy connections list
dbbuddy connections add --name prod --engine mysql \
    --host db.example.org --user erp_ro --database sales      # prompts for password
dbbuddy connections remove <connection-id>
```

---

## Querying, chatting, analyzing

```bash
# Ask a question against a saved connection (recommended — this syncs)
dbbuddy query "Top 10 customers by revenue" --connection prod

# Machine-readable output for scripting
dbbuddy query "Monthly sales" --connection prod --json > report.json

# Interactive REPL
dbbuddy chat --connection prod

# Analyze / (re)index the semantic layer for a database
dbbuddy analyze --connection prod

# Explain a result with AI Insights (runs the query, then analyzes its rows)
dbbuddy insights "Revenue by region this quarter" --connection prod

# Ask a follow-up bound to that same result
dbbuddy insights "Revenue by region this quarter" --connection prod \
    --ask "which region grew fastest?"

# Static info (no auth needed)
dbbuddy engines
```

`dbbuddy insights` is the command-line form of the web app's **AI Insights**
panel. It runs the query first (insights analyze a *result*), then sends the rows
to `POST /insights/generate`, or to `POST /insights/ask` with `--ask`. Output is
the same markdown the panel's Copy button yields, followed by suggested
follow-ups; `--json` emits the full bundle. In `--local` mode the rule-based
pipeline runs in-process and the analysis goes to a **local Ollama** provider
(model from `$LOCAL_MODEL`, endpoint from `$OLLAMA_URL`) — the per-org provider
chain the web app resolves needs the platform DB, which local mode does not
touch. Insights are gated by `INSIGHTS_ENABLED`; a disabled or unreachable
provider returns a plain "unavailable" note rather than failing the command.

**AI-enhanced mode is on by default** (falling back to the deterministic
rule-based planner when the AI provider is unavailable — the CLI prints a
one-time note on stderr when that happens, keeping `--json` stdout clean). Use
`--no-ai` for rule-based only. The model that runs is configured per org under
**Settings → AI Providers** (adapter + endpoint + key, with a fallback chain);
the legacy `--ai-provider {local,nemotron,openai,hybrid}` flag is still accepted
for the pre-registry path.

You can also pass **inline credentials** (`--host/--user/--database/…`) instead
of `--connection` for a one-off test-before-save; the CLI warns that such a
connection is *not* persisted.

---

## Customizing charts (Infographics)

Charts saved from the web app (Infographics) can be **customized and published
from the CLI** — the type and colors are plain metadata shared with the web app
and every published client report, so a change here shows up everywhere.

```bash
dbbuddy charts list                                  # id · title · type · status
dbbuddy charts show <chart-id>                       # current type, palette & colors

# Change the chart type and colors (all flags optional; edits merge onto the
# existing config, so you can tweak one thing at a time)
dbbuddy charts customize <chart-id> \
    --type pie \                                      # bar|column|line|area|pie|doughnut|scatter|combo|table
    --palette sunset \                               # default|ocean|sunset|forest|grape|slate
    --category-color "North=#ff0000" \               # per pie-slice / per-bar (repeatable)
    --series-color   "revenue=#22c55e"               # per numeric column (repeatable)

dbbuddy charts customize <chart-id> --reset-colors    # clear color overrides (keep palette)

dbbuddy charts publish   <chart-id>                   # publish to your organization
dbbuddy charts unpublish <chart-id>
```

`charts show` prints the current configuration, which makes editing easier:

```
Chart:    Monthly Revenue
Type:     Doughnut
Palette:  Sunset

Series colors
-------------
revenue: #22c55e

Category colors
---------------
North: #ff0000
South: #0066ff
```

Series colors apply to multi-column charts (one per numeric column); category
colors apply to a pie/doughnut's slices or a single-measure bar/column. The CLI
doesn't draw the chart — for a **live visual preview** while picking colors, use
the web app's Infographics panel.

---

## CLI ↔ Web app parity

Everything an Analyst does in the web app has a CLI equivalent, backed by the
same endpoint:

| Task | Web app | CLI | Endpoint |
| ---- | ------- | --- | -------- |
| Sign in | Login form (+ MFA) | `dbbuddy login` | `POST /auth/login`, `/auth/mfa/login` |
| API keys | Settings → API keys | `dbbuddy keys create/list/revoke` | `/auth/keys*` |
| See your account | Profile menu | `dbbuddy whoami` | `GET /auth/me` |
| Save a connection | Connections screen | `dbbuddy connections add` | `POST /connections` |
| List / remove connections | Connections screen | `dbbuddy connections list/remove` | `GET`/`DELETE /connections` |
| Analyze a schema | "Analyze Schema" | `dbbuddy analyze` | `POST /analyze` |
| Ask a question | Query box | `dbbuddy query` / `chat` | `POST /query` |
| Explain a result (AI Insights) | Insights panel | `dbbuddy insights` / `--ask` | `POST /insights/generate`, `/insights/ask` |
| Query history | History panel | *(recorded automatically)* | `GET /history` |
| List / inspect / customize charts | Infographics panel | `dbbuddy charts list/show/customize` | `GET`/`PATCH /charts` |
| Publish / unpublish a chart | Infographics "Publish" | `dbbuddy charts publish/unpublish` | `POST /charts/{id}/(un)publish` |

What stays **web-only** (by design): platform administration (users, orgs,
audit, security & system settings) for Admins, and report/dashboard viewing for
Clients.

---

## `--local` offline mode

`--local` runs the deterministic pipeline **directly against an ERP database**,
without the backend. It's for offline or developer use only, and it prints a
clear warning because **nothing syncs to the platform** — no shared history, no
saved charts, no audit trail, no permission check.

```bash
dbbuddy query "show all users" --local \
    --host localhost --user root --database mydb --engine mysql
dbbuddy analyze --local --config conn.json
```

Prefer normal (authenticated) mode for anything that should be part of your
team's shared record.

---

## Scripting tips

- `--json` emits machine-readable output on **stdout**; status and notices go to
  **stderr**, so `dbbuddy query … --json > out.json` stays clean.
- Set `DBBUDDY_API_KEY` and `DBBUDDY_API_URL` in CI to avoid any interactive
  prompt.
- **Non-interactive safety:** when stdin is not a TTY, any command that would
  otherwise prompt (password, API key, `--local` host/user/db) fails fast with a
  clear message and exit `1` instead of hanging — so a forgotten flag in CI can't
  block a pipeline. Pass every required value explicitly (`--password`,
  `--api-key`, `--host`, …) or use `DBBUDDY_API_KEY`.
- Exit codes: `0` on success, `1` on error or when access is denied — safe to
  gate a pipeline on.

If the console script isn't on your `PATH`, use `python -m dbbuddy …`.
