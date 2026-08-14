# Deprecated Test Scripts

These files are legacy test scripts from the early development of LEM. They predate the
structured test suite under `tests/unit/` and `tests/integration/`.

**They are not run by pytest** (the `_deprecated` directory is listed in `norecursedirs`
in `pyproject.toml`). They exist here only for historical reference and should be deleted
once the functionality they cover has been absorbed into the proper test suite.

## Files

| File | What it tested | Replacement |
|------|---------------|-------------|
| `chrome_test.py` | Raw Selenium browser launch, LinkedIn login, and basic scraping via hardcoded `LI_USER`/`LI_PASSWORD` env vars | `scripts/linkedin_live_validation.py` (the live probe) |
| `run_automation_test.py` | End-to-end automation flows (commenting, invites, DMs) driven directly by `LI_USER`/`LI_PASSWORD` | `scripts/linkedin_live_validation.py` (the live probe) |
| `simple_automation_tests.py` | Smoke tests for automation helpers using Selenium + real LinkedIn login | `scripts/linkedin_live_validation.py` (the live probe) |
| `ai_tests.py` | Direct calls to LiteLLM/OpenAI without mocking — used to verify model responses manually | `tests/unit/utilities/ai/` |
| `db_tests.py` | Ad-hoc database queries run against a live MySQL connection | `tests/unit/utilities/test_db.py`, `tests/integration/` |
| `date_tests.py` | Manual checks on date/time utility functions | `tests/unit/utilities/` |
| `aws_test.py` | Ad-hoc AWS SDK calls (S3, CloudWatch, SSM) against real AWS credentials | `tests/integration/` (to be written) |
| `run_content_plan_test.py` | Manual run of `plan_content_for_user` and `auto_create_weekly_content` against a live stack | `tests/integration/` (to be written) |

## Why they were deprecated

- All scripts rely on live external services (LinkedIn, AWS, MySQL, LiteLLM) with no mocking.
- Credentials were passed via `LI_USER` / `LI_PASSWORD` environment variables — an approach
  replaced by per-user DB storage (`users.password` column read via `get_user_password_pair_by_id`).
- None use `pytest` fixtures, markers, or assertions in the standard format, so they cannot
  be reliably integrated into CI.

## Deleting these files

Safe to delete once the corresponding `tests/integration/` coverage (or live-probe coverage) exists.
Note there is no browser lane to write them into — #1215 deleted `tests/e2e/`.
Check the table above for what needs to be covered before removing each file.
