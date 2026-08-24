# Security Policy

## Reporting a vulnerability

**Do not open a public issue, PR, or discussion describing a vulnerability.**

Report privately through GitHub's private vulnerability reporting:

**[Report a vulnerability →](https://github.com/sxjalxo/dbbuddy/security/advisories/new)**

(On the repository: **Security** → **Report a vulnerability**.)

If you cannot use GitHub, email **dbbuddy99@gmail.com** instead.

Please include:

- Affected version or commit
- Reproduction steps, and the impact you were able to demonstrate
- Any configuration relevant to the finding (deployment mode, `DBBUDDY_ENV`, whether
  Redis was reachable, worker count)

**Response targets:** acknowledgement within 5 days, an initial assessment within 10 days,
and a coordinated disclosure timeline agreed with you before anything is published. You
will be credited in the advisory unless you prefer otherwise.

Please act in good faith while researching: no privacy violations, data destruction, or
service disruption against deployments you do not own.

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | ✅ |
| < 1.0 | ❌ |

DB Buddy is pre-1.1 and moves quickly. Fixes land on `main`; run a recent commit.

## Scope

**In scope** — the code in this repository: authentication and session handling, RBAC
enforcement, at-rest encryption of connection secrets, the SQL compilation and execution
path, the outbound-request (SSRF) guard, and AI output validation.

**Out of scope**

- Vulnerabilities in a deployment's own configuration (a weak `JWT_SECRET`, `ALLOWED_ORIGINS`
  set to `*`, an exposed app database). The production checklist in
  [docs/SECURITY.md](docs/SECURITY.md#production-checklist-security-relevant) exists to
  prevent these, and `DBBUDDY_ENV=production` turns most of them into startup errors.
- Limits the project already documents as limits rather than defenses. Two current ones:
  the SSRF guard validates the URL, not the socket, so DNS rebinding is not covered
  ([`backend/app_db/url_guard.py`](backend/app_db/url_guard.py)); and session revocation
  converges per worker process within `AUTH_REVOCATION_CACHE_TTL`
  ([`backend/app_db/deps.py`](backend/app_db/deps.py)). Reports that these behave as
  documented are not findings — reports that they can be *exceeded* are.
- Findings against demo accounts seeded by `backend/seed_test_accounts.py`, which the
  production checklist requires you to remove.

## Security model

The full model — Argon2 password hashing, JWT sessions and revocation, MFA, personal API
keys, at-rest encryption of connection secrets, RBAC, safe query execution, and the
production checklist — is documented in **[docs/SECURITY.md](docs/SECURITY.md)**.

Two properties worth stating up front, because they shape everything else:

1. **Two databases, kept apart.** The *application database* holds platform state (users,
   roles, charts, audit). *Connected business databases* are only ever query targets, and
   their credentials are encrypted at rest.
2. **SQL is compiled, not generated.** Values reach the database as bound parameters, never
   interpolated into a statement. Write statements require a reviewed plan or an explicit
   `query:write:manual` permission.
