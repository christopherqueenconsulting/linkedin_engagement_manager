---
applyTo: ".github/**"
---

# CI Failure Handling Instructions

## Required Status Checks

A PR cannot be merged until all four checks pass:

1. `CI / Unit Tests`
2. `CI / Integration Test w/ Coverage`
3. `CodeQL Security Analysis`
4. `GitGuardian Security Scan`

## Dependabot PR Failures

When a Dependabot PR's CI fails, the `dependabot-ci-autofix.yml` workflow posts `@copilot fix` automatically. This means:

1. Copilot will attempt to generate a fix commit on the Dependabot branch.
2. If Copilot's fix does not resolve the failure within 2 hours, manually investigate.
3. The `@copilot fix` comment is posted **once per PR head SHA** — the workflow prevents spam by checking existing comments.

## Common Failure Causes and Resolutions

### Unit test failure after dependency update
- Run `poetry run pytest tests/unit -v` locally against the updated lock file.
- Check if the updated package changed an API that test mocks depend on.
- Update mocks in `tests/conftest.py` or test files to match the new interface.

### Integration test failure
- Integration tests require MySQL 8 + Redis 7. In CI these are service containers.
- Locally: `docker compose up mysql redis -d` before running.
- If a migration is missing, add a Flyway migration under `compose/local/db/migrations/`.

### CodeQL failure
- Open the Security tab → Code scanning alerts to see the specific alert.
- Do NOT suppress alerts with `# nosec` or query suppression unless reviewed by a human.
- Fix the actual code pattern (e.g., SQL injection → parameterized query, XSS → escaping).

### GitGuardian failure
- A secret was detected in the diff. **Never push past this with `--no-verify`.**
- Remove the secret from the commit, rotate the credential immediately, then push.
- If the detection is a false positive, file a GitGuardian suppression request via their dashboard.

### Coverage gate failure
- Codecov reports patch coverage < 80%.
- Add unit tests for the newly added or modified lines.
- Coverage report artifacts are uploaded in the integration workflow; download to see the HTML report.

## Workflow Dependency Map

```
unit-tests.yml          → runs on every PR/push to main
integration-coverage.yml → runs on every PR/push to main
codeql-analysis.yml     → runs on PR/push + weekly schedule
gitguardian-scan.yml    → runs on every PR/push (skip Dependabot)
dependabot-auto-merge.yml    → fires on Dependabot PRs, merges on minor/patch after checks
dependabot-ci-autofix.yml   → fires on CI failure for Dependabot PRs, posts @copilot fix
```
