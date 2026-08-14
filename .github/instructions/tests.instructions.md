---
applyTo: "tests/**"
---

# Test Writing Instructions

These rules apply to all files under `tests/`.

## Test Layout

```
tests/unit/          Fast, no real services — mock everything
tests/integration/   Real MySQL + Redis — use service containers in CI
                     TWO lanes only. #1215 deleted tests/e2e/; nothing in CI drives a
                     browser. Selenium work is graded by the read-only live probe,
                     scripts/linkedin_live_validation.py.
```

## Fixture Usage

Import fixtures from `tests/conftest.py` by name:

| Fixture | What it provides |
|---|---|
| `mock_openai_client` | MagicMock; `.chat.completions.create` returns `"Mock AI response"` |
| `mock_database_connection` | dict `{"connection": MagicMock, "cursor": MagicMock}` |
| `mock_selenium_driver` | MagicMock WebDriver |
| `sample_linkedin_profile` | dict with realistic profile data |
| `sample_post_data` | dict with id, content, status, scheduled_time, post_type |
| `sample_message_data` | dict with recipient info and message content |

## Marking Tests

```python
@pytest.mark.unit          # default for all unit tests
@pytest.mark.integration   # needs MySQL + Redis
@pytest.mark.slow          # long-running, excluded from quick runs
```

## Mocking Pattern

Patch at the point of use, not the source:

```python
# CORRECT — patch where ai_helper imports client
with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
    ...

# CORRECT — patch where db.py calls connect
with patch("cqc_lem.utilities.db.get_db_connection") as mock:
    mock.return_value = mock_database_connection["connection"]
    ...
```

## Assertions

- Always assert on the actual result: `assert result == expected`
- Verify mock interactions: `mock.assert_called_once_with(arg1, arg2)`
- Never use `assert True`, `assert result is not None` alone, or `pass` in test bodies
- Test both happy path and at least one error path per function

## Coverage

Minimum 80% patch coverage enforced by Codecov. Run locally:

```bash
poetry run pytest tests/unit --cov=src/cqc_lem --cov-report=term-missing -v
```

## Import Rule

Never import `cqc_lem` modules at the test file's top level. Import inside the test function after patching is set up:

```python
def test_something(mock_openai_client):
    with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
        from cqc_lem.utilities.ai.ai_helper import get_ai_message_refinement  # inside patch
        result = get_ai_message_refinement("Hello")
        assert isinstance(result, str)
```
