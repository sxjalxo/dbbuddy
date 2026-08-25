# Rulesets

Repository rulesets, kept in version control so the protections are reviewable and
restorable. GitHub does not read these files — they must be imported.

## Import

Settings → Rules → Rulesets → New ruleset → **Import a ruleset** → pick one JSON file.
Repeat per file. Import does not merge: re-importing creates a second ruleset, so to
change one, edit the existing ruleset in the UI (or delete and re-import) after
updating the file here.

| File | Protects |
|------|----------|
| `default-branch.json`   | the default branch (`~DEFAULT_BRANCH`) |
| `release-branches.json` | `release/**`, `hotfix/**` |
| `release-tags.json`     | `v*` tags |

## What they enforce

- **No direct pushes or merges** — every change lands through a pull request with
  1 approval, code-owner approval (`.github/CODEOWNERS`), all review threads
  resolved, stale approvals dismissed on push, and the last push approved by
  someone other than its author.
- **No deletion** of protected branches or `v*` tags.
- **No force push** (`non_fast_forward`), and on the default branch no merge commits
  from long-lived side branches (`required_linear_history`).
- **CI must pass** before merge, against the merge result (`strict` — the branch has
  to be up to date with the base).
- `v*` tags cannot be moved (`update`) or deleted, so a published release cannot be
  re-pointed at different code.

## Two things to check after importing

1. **Bypass actor.** Each file grants bypass to `RepositoryRole` id `5` (admin) with
   `bypass_mode: always`, so the sole maintainer is not locked out of their own
   repository. Confirm the imported ruleset's Bypass list actually reads
   "Repository admin" in the UI. To hold yourself to the same rules, delete the
   `bypass_actors` entry; to keep a bypass only for emergencies, set
   `"bypass_mode": "pull_request"` so it applies to PR requirements but not pushes.

2. **Status check names.** The required contexts are the `name:` of each job in
   `.github/workflows/ci.yml`, including the matrix values. A required check that
   never reports blocks every PR forever, so open one throwaway PR after importing
   and confirm each required check turns green rather than staying "Expected".
   Rename a job in `ci.yml` and you must rename it here too.

## Not covered by rulesets

Set these in Settings → General:

- **Allow merge commits / squash / rebase** — the ruleset restricts merge methods to
  squash and merge, but the repository-level toggles are what remove the buttons.
- **Automatically delete head branches** — safe to leave on; the deletion rules above
  only match protected refs, not feature branches.
- **Allow forking / who can create branches** — unchanged by these files.

Not enabled, deliberately: `required_signatures`. It blocks every contributor without
a signing key, which is a hard gate for an open-source repo. Turn it on if the project
wants signed commits.
