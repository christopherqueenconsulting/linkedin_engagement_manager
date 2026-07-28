# Contributing to LinkedIn Engagement Manager

Thank you for your interest in contributing to the LinkedIn Engagement Manager! This document provides guidelines and best practices for contributing to this project.

## Table of Contents

1. [Getting Started](#getting-started)
2. [Development Setup](#development-setup)
3. [Testing Guidelines](#testing-guidelines)
4. [Code Quality Standards](#code-quality-standards)
5. [Pull Request Process](#pull-request-process)
6. [Issue Reporting](#issue-reporting)

## Getting Started

1. Fork the repository
2. Clone your fork locally
3. Create a new branch for your feature or bugfix
4. Make your changes
5. Test your changes thoroughly
6. Submit a pull request

## Development Setup

### Prerequisites

- Python 3.12+
- Poetry for dependency management
- Docker (optional, for full stack development)
- Git

### Installation

```bash
# Clone the repository
git clone https://github.com/your-username/linkedin-engagement-manager.git
cd linkedin-engagement-manager

# Install dependencies
poetry install --with test,dev,lint

# Set up environment variables
cp .env.example .env
# Edit .env with your configuration
```

## Testing Guidelines

### Test-Driven Development (TDD)

We follow a Test-Driven Development approach:

1. **Write failing tests first** that demonstrate the expected behavior
2. **Implement the minimum code** needed to make the tests pass
3. **Refactor** while keeping all tests passing
4. **Document** what you've done

### Test Organization

Tests are organized into three categories:

- **Unit Tests** (`tests/unit/`): Fast, isolated tests with mocked dependencies
- **Integration Tests** (`tests/integration/`): Tests that verify multiple components work together
- **End-to-End Tests** (`tests/e2e/`): Full workflow tests

### Running Tests

```bash
# Run all tests
poetry run pytest

# Run only unit tests
poetry run pytest tests/unit -v

# Run tests with coverage
poetry run pytest --cov=src/cqc_lem --cov-report=html --cov-report=term

# Run specific test file
poetry run pytest tests/unit/utilities/test_db.py -v

# Run tests matching a pattern
poetry run pytest -k "test_database" -v

# Run with markers
poetry run pytest -m "unit" -v
poetry run pytest -m "not slow" -v
```

### Test Markers

Use these markers to categorize your tests:

- `@pytest.mark.unit` - Fast unit tests with mocked dependencies
- `@pytest.mark.integration` - Integration tests
- `@pytest.mark.e2e` - End-to-end tests
- `@pytest.mark.slow` - Tests that take longer to execute
- `@pytest.mark.requires_openai` - Tests requiring OpenAI API access
- `@pytest.mark.requires_database` - Tests requiring database connection
- `@pytest.mark.requires_selenium` - Tests requiring browser automation

### Writing Good Tests

#### Test Structure

Follow the Arrange-Act-Assert pattern:

```python
def test_example_function():
    # Arrange: Set up test data and mocks
    mock_data = {"key": "value"}
    
    # Act: Execute the function being tested
    result = example_function(mock_data)
    
    # Assert: Verify the expected outcome
    assert result == expected_value
```

#### Test Naming

Use descriptive test names that explain what is being tested:

```python
# Good
def test_generate_ai_response_returns_string_for_valid_input():
    pass

# Bad
def test_ai():
    pass
```

#### Use Fixtures

Leverage pytest fixtures for common setup:

```python
def test_with_sample_profile(sample_linkedin_profile):
    """Test using the sample_linkedin_profile fixture from conftest.py."""
    profile = LinkedInProfile(**sample_linkedin_profile)
    assert profile.full_name == "John Doe"
```

### Mocking External Dependencies

Always mock external dependencies in unit tests:

```python
from unittest.mock import patch, MagicMock

def test_function_with_external_api(mock_openai_client):
    """Test function that calls external API."""
    with patch("module.external_api_call") as mock_api:
        mock_api.return_value = {"result": "success"}
        result = function_to_test()
        assert result == expected_value
```

### Test Coverage Requirements

- **Minimum**: 70% code coverage for core modules
- **Target**: 85%+ code coverage
- **New Code**: All new code must include tests
- **Bug Fixes**: Include a test that would have caught the bug

### Pre-Implementation Testing Checklist

Before implementing any feature or fix:

- [ ] Write failing tests that demonstrate the expected behavior
- [ ] Ensure tests are runnable (even if failing)
- [ ] Document expected behavior in test docstrings
- [ ] Verify tests fail for the right reason

### Post-Implementation Testing Checklist

After implementing a feature or fix:

- [ ] All new tests pass
- [ ] All existing tests still pass (no regressions)
- [ ] Code coverage maintained or improved
- [ ] Tests include edge cases
- [ ] Tests are well-documented
- [ ] Integration tests added for cross-module features

## Code Quality Standards

### Linting

We use `ruff` for linting:

```bash
# Run linter
poetry run ruff check src/ tests/

# Auto-fix issues
poetry run ruff check --fix src/ tests/
```

### Code Style

- Follow PEP 8 guidelines
- Use type hints where applicable
- Keep functions focused and small
- Write docstrings for public functions and classes
- Use meaningful variable and function names

### Documentation

- Update README.md for user-facing changes
- Add docstrings to new functions and classes
- Update TODO_PROJECT_TIMELINE.md for task tracking
- Include inline comments for complex logic

## Pull Request Process

### Commit Messages (Conventional Commits — required)

Releases are automated by [release-please](https://github.com/googleapis/release-please),
which derives the next version and the `CHANGELOG.md` from commit messages on
`main`. Use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat: ...` → minor version bump (new feature)
- `fix: ...` → patch version bump (bug fix)
- `feat!: ...` or a `BREAKING CHANGE:` footer → major version bump
- `chore: / docs: / refactor: / test: / ci:` → no release on their own

Scopes are encouraged (e.g. `fix(carousel): ...`). When a PR squash-merges, the
PR title becomes the commit — make it a valid Conventional Commit.

### Before Submitting

1. **Update your branch** with the latest changes from main:
   ```bash
   git fetch origin
   git rebase origin/main
   ```

2. **Run all tests**:
   ```bash
   poetry run pytest
   ```

3. **Check code coverage**:
   ```bash
   poetry run pytest --cov=src/cqc_lem --cov-report=term
   ```

4. **Run linter**:
   ```bash
   poetry run ruff check src/ tests/
   ```

5. **Update documentation** if needed

### PR Description Template

```markdown
## Description
Brief description of what this PR does.

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
- [ ] All new code has tests
- [ ] All tests pass
- [ ] Coverage maintained or improved

## Checklist
- [ ] Code follows project style guidelines
- [ ] Self-review completed
- [ ] Documentation updated
- [ ] No new warnings introduced
```

### Review Process

1. PRs require at least one approval
2. All CI checks must pass
3. Code coverage must not decrease
4. Address all review comments

## Issue Reporting

> **This repo is worked primarily by an autonomous agent pipeline.** Whether an issue gets picked
> up automatically, waits on the owner, or sits invisible is controlled entirely by labels — see
> **[docs/AGENT_WORKFLOW_PLAYBOOK.md](docs/AGENT_WORKFLOW_PLAYBOOK.md)** for the label state
> machine, how to write an agent-executable issue, and how to answer a Decision Comment.

### Bug Reports

Include:
- Clear description of the bug
- Steps to reproduce
- Expected vs. actual behavior
- Environment details (OS, Python version, etc.)
- Error messages and stack traces
- Screenshots if applicable

### Feature Requests

Include:
- Clear description of the feature
- Use case and motivation
- Proposed implementation approach
- Potential impact on existing features

## Additional Resources

- [TODO Project Timeline](docs/TODO_PROJECT_TIMELINE.md)
- [README](README.md)
- [Test Infrastructure Documentation](tests/README.md)

## Questions?

If you have questions or need help, please:
1. Check existing documentation
2. Search closed issues for similar questions
3. Open a new issue with the "question" label

Thank you for contributing to LinkedIn Engagement Manager!
