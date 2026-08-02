---
name: test-lanes
description: Use when writing or modifying tests — choose the unit/integration/e2e lane, the right markers and mock fixtures, respect the hermetic autouse guards, and hit 80% patch coverage.
---

# Test lanes, fixtures, and coverage

1. Pick the lane: **unit** (`tests/unit/`) mocks ALL external I/O; **integration** (`tests/integration/`) uses real MySQL + Redis service containers; **e2e** (`tests/e2e/`) needs `selenium/standalone-chrome`. Mark accordingly (`@pytest.mark.unit/integration/e2e`, plus `slow` / `requires_openai` / `requires_database` / `requires_selenium`) — `--strict-markers` is on.
2. Mock fixtures: `mock_openai_client` (patches `cqc_lem.utilities.ai.client.OpenAI`), `mock_database_connection` (patches `mysql.connector.connect`), `mock_selenium_driver`.
3. Hermetic autouse guards already pin the environment — don't fight them, opt back in explicitly in the tests that need the real behaviour: `_no_real_llm_calls` / `_no_real_redis` / `_feature_flags_env_only` (unit conftest), `_humanize_disabled_by_default` (`HUMANIZE_ENABLED=off`), `_human_pacing_disabled_by_default`, `_db_pool_disabled_by_default` (root conftest — the pool bypasses the mocked `connect`).
4. Run: `poetry run pytest tests/unit -v --tb=short` (fast lane); coverage: `poetry run pytest --cov=src/cqc_lem --cov-report=xml`. CI enforces **≥80% patch coverage** via Codecov on every PR.
5. Test the pure half of Selenium code (parsing, scoring, message shaping) in unit tests; DOM interaction belongs in e2e or the live probe (see the linkedin-live-validation skill).

Authoritative: `tests/README.md` (markers, commands, xdist notes), `CONTRIBUTING.md` (TDD checklists).
