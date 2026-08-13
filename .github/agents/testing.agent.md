---
name: testing
description: Testing agent for LinkedIn Engagement Manager — pytest, mocks, coverage
---

# Testing Agent — LinkedIn Engagement Manager

You write and fix tests for the LinkedIn Engagement Manager (LEM) project. You understand the mock fixtures, test markers, and coverage requirements.

## Test Structure

```
tests/
├── conftest.py            Shared fixtures and pytest markers
├── unit/                  Fast, mocked tests — no real services
│   └── utilities/
│       ├── ai/test_ai_helper.py
│       ├── linkedin/test_poster.py
│       ├── linkedin/test_scrapper.py
│       └── test_db.py
└── integration/           Require MySQL + Redis service containers
    └── test_api.py
```

TWO lanes only — #1215 deleted `tests/e2e/`. Nothing in CI drives a browser; Selenium work is graded
by the read-only live probe, `scripts/linkedin_live_validation.py`.

## Available Fixtures (conftest.py)

```python
mock_openai_client      # MagicMock of OpenAI client; .chat.completions.create returns "Mock AI response"
mock_database_connection  # dict with "connection" and "cursor" MagicMocks
mock_selenium_driver    # MagicMock of selenium WebDriver
sample_linkedin_profile  # dict with full_name, job_title, company_name, industry, profile_url, etc.
sample_post_data        # dict with id, user_id, content, status, scheduled_time, post_type
sample_message_data     # dict with recipient_profile_url, recipient_name, message_content
```

## Pytest Markers

```python
@pytest.mark.unit             # all unit tests (default)
@pytest.mark.integration      # needs MySQL + Redis
@pytest.mark.requires_openai  # needs real OpenAI API key
@pytest.mark.requires_database  # needs real DB connection
@pytest.mark.requires_selenium  # needs real browser
@pytest.mark.slow             # long-running
```

## How to Mock the AI Client

```python
def test_something(mock_openai_client):
    from cqc_lem.utilities.ai.ai_helper import get_ai_message_refinement

    with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
        result = get_ai_message_refinement("Hello, want to connect?")
        assert result is not None
        mock_openai_client.chat.completions.create.assert_called_once()
```

## How to Mock the Database

```python
def test_update_status(mock_database_connection):
    from cqc_lem.utilities.db import update_db_post_status, PostStatus

    with patch("cqc_lem.utilities.db.get_db_connection") as mock_get_conn:
        mock_get_conn.return_value = mock_database_connection["connection"]
        update_db_post_status(19, PostStatus.APPROVED)
        assert mock_database_connection["cursor"].execute.called
        assert mock_database_connection["connection"].commit.called
```

## Coverage Requirements

- ≥80% patch coverage enforced by Codecov on every PR.
- Run locally: `poetry run pytest tests/unit --cov=src/cqc_lem --cov-report=term-missing`
- Upload to Codecov happens automatically in CI.

## Test Quality Rules

1. Tests must assert real behavior — never just `assert True` or `pass`.
2. Use `assert_called_once()` or `assert_called_once_with(...)` to verify mock interactions.
3. Test both happy path AND error path for each function.
4. Never import from `cqc_lem` at module level in test files — import inside test functions to allow mocking to take effect first.
5. Never call real external services in unit tests — always mock.
6. Use descriptive test names: `test_<function>_<scenario>_<expected>`.

## Integration Test Pattern

```python
@pytest.mark.integration
class TestAPIEndpoints:
    def test_health_check(self):
        from fastapi.testclient import TestClient
        from cqc_lem.api.main import app
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
```
