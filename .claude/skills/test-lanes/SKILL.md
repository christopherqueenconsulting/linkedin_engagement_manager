---
name: test-lanes
description: Use when writing or modifying tests — choose the unit or integration lane, the right markers and mock fixtures, respect the hermetic autouse guards, and hit 80% patch coverage.
---

# Test lanes, fixtures, and coverage

1. Pick the lane — there are **TWO**: **unit** (`tests/unit/`) mocks ALL external I/O; **integration** (`tests/integration/`) uses real MySQL + Redis service containers. Mark accordingly (`@pytest.mark.unit/integration`, plus `slow` / `requires_openai` / `requires_database` / `requires_selenium`) — `--strict-markers` is on, and `e2e` is no longer a registered marker (#1215).
2. Mock fixtures: `mock_openai_client` (patches `cqc_lem.utilities.ai.client.OpenAI`), `mock_database_connection` (patches `mysql.connector.connect`), `mock_selenium_driver`.
3. Hermetic autouse guards already pin the environment — don't fight them, opt back in explicitly in the tests that need the real behaviour: `_no_real_llm_calls` / `_no_real_redis` / `_feature_flags_env_only` (unit conftest), `_humanize_disabled_by_default` (`HUMANIZE_ENABLED=off`), `_human_pacing_disabled_by_default`, `_db_pool_disabled_by_default` (root conftest — the pool bypasses the mocked `connect`).
4. The integration lane's guard is harder: `_no_live_llm_calls` (integration conftest, #1188) FAILS the test on any unmocked LLM request, with a `BaseException` no `except Exception` fallback can swallow. Mock the `client` the module under test imported, or the helper wrapping it — a real call there was 311s of a 354s lane while the tests still passed.
5. Run: `poetry run pytest tests/unit -v --tb=short` (fast lane); coverage: `poetry run pytest --cov=src/cqc_lem --cov-report=xml`. CI enforces **≥80% patch coverage** via Codecov on every PR.
6. **Before the first run in a fresh worktree: `poetry install --with test`.** The `test` group is `optional = true`, so a plain `poetry install` skips every pytest plugin and still says "No dependencies to install or update". The symptom is `pytest: error: unrecognized arguments: --snapshot-warn-unused` or `ERROR: Unknown config option: asyncio_mode` — that is syrupy / pytest-asyncio missing, **not** a broken `pyproject.toml`. Never silence it by editing `addopts` or passing `-o addopts=""`: those are the settings CI runs under. `--with dev` is the wrong group (jupyter tooling, no pytest plugins).
6. Test the pure half of Selenium code (parsing, scoring, message shaping) in unit tests. DOM interaction has no CI lane — the read-only live probe grades it against the real page (see the linkedin-live-validation skill). #1215 deleted the e2e lane rather than keep claiming one: it was 17 tests, 6 of them mocks already duplicated in `tests/unit/`, 11 permanently skipped, under `continue-on-error`.

Authoritative: `tests/README.md` (markers, commands, xdist notes), `CONTRIBUTING.md` (TDD checklists).
