# Contributing

## Workflow

- One branch per module: `feat/m<phase>.<n>-<slug>`. Docs-only updates use `docs/…`.
- Conventional commits: `feat:`, `fix:`, `test:`, `perf:`, `docs:`, `ci:`, `refactor:`, `chore:`.
- Open a PR from the template, wait for green CI, squash-merge. Never push to `main`.
- Never commit secrets or `.env`. Pre-commit runs gitleaks; CI runs it again.
- Never delete, skip or loosen a test to get green. `xfail`/`skip` needs a linked issue.

Local checks, the same targets CI calls:

```bash
make lint typecheck       # ruff, mypy
make coverage             # unit tests + per-package coverage gates
make security-scan        # bandit + pip-audit
make test-integration     # needs Postgres and Redis (REDIS_URL, DATABASE_URL)
make test-system test-functional test-chaos test-security   # need Docker
```

## CI workflows

| Workflow | Runs on | Jobs |
|----------|---------|------|
| `ci.yml` | every PR, push to `main` | `python` (lint, mypy, unit tests, coverage gates), `integration` (Postgres/Timescale and Redis service containers) |
| `security.yml` | every PR, push to `main`, weekly (Mon 06:00 UTC) | `bandit`, `pip-audit`, `gitleaks`, `trivy` (gateway, api, worker), `security-tests` |
| `system.yml` | push to `main`, and PRs labelled `system` | `system`: compose stack, system, functional and chaos tests |

Caching: uv's cache via `setup-uv`; Docker layers via the GitHub Actions cache (one scope per service image).

**Coverage gates** (`scripts/coverage_gate.py`): `packages/core` ≥ 95% line / 90% branch,
`services/gateway` ≥ 90% / 85%, `services/api` and `services/worker` ≥ 85% / 75%. A PR also
fails if line or branch coverage drops by more than 1 point against `main`. Results go to the
job summary and the `coverage` artifact.

**Security gates:** bandit fails on medium severity or higher. pip-audit fails on any known
vulnerability in the locked runtime and dev dependencies. trivy fails on HIGH/CRITICAL findings
that have a fix available. When one fires, upgrade the pinned version; don't suppress it.

## Branch protection for `main` (you apply these in the GitHub UI)

Settings → Branches → Add branch ruleset (or classic rule) for `main`:

1. **Require a pull request before merging.** Required approvals: 0 for a solo project, 1 once there are other contributors. Require review from Code Owners if you want `CODEOWNERS` enforced.
2. **Require status checks to pass before merging**, and **require branches to be up to date**. Add these checks:
   - `python`
   - `integration`
   - `bandit`
   - `pip-audit`
   - `gitleaks`
   - `trivy (gateway)`, `trivy (api)`, `trivy (worker)`
   - `security-tests`
3. **Block force pushes** and **restrict deletions**.
4. **Do not require the admin bypass** (leave "Do not allow bypassing the above settings" on) so the gate also applies to you.
5. Allow **squash merging** only (Settings → General → Pull Requests), and delete head branches on merge.

`system` is deliberately not required: it only runs on PRs labelled `system`, so a required check
would wait forever on unlabelled PRs. It runs on every push to `main`. Label a PR `system` when it
touches the compose stack, Dockerfiles or anything the stack tests cover.

Check names appear exactly as listed once each workflow has run at least once.
