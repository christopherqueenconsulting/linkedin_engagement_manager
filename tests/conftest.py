"""Pytest configuration and shared fixtures for LinkedIn Engagement Manager tests.

This module provides:
- Mock fixtures for external dependencies (OpenAI, LinkedIn API, Database)
- Common test data fixtures
- Test configuration and markers
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv

# Load .env at session start so integration tests can see real API keys.
# os.environ.setdefault() calls below won't override values already present here.
load_dotenv()

# MODULE level, not a fixture: a session fixture runs after COLLECTION, so a test module that
# imports something with import-time credentials (utilities/ai/client.py builds the OpenAI client
# at module scope) exploded during collection on any box without a .env — CI. Locally the .env
# masked it, which is exactly the kind of pass-here/fail-there this block exists to prevent.
os.environ.setdefault("OPENAI_API_KEY", "test-api-key-12345")
os.environ.setdefault("LI_USER", "test_user@example.com")
os.environ.setdefault("LI_PASSWORD", "test_password")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_USER", "test_user")
os.environ.setdefault("DB_PASSWORD", "test_password")
os.environ.setdefault("DB_NAME", "test_db")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/0")
os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
os.environ.setdefault("PEXELS_API_KEY", "test-pexels-api-key-12345")


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Kept for anything that depends on this fixture by name — the values now land at import."""
    yield


@pytest.fixture(autouse=True)
def _humanize_disabled_by_default(monkeypatch):
    """Issue #416: the humanization pass (humanize_text) is default-ON in production, but it makes a
    second LLM call after every generator, which perturbs the many unit tests that introspect a single
    _call_llm. Default it OFF for the suite so those tests keep asserting on the generation call
    directly (this is exactly the "HUMANIZE_ENABLED=off restores prior behavior" guarantee). The
    humanization pass's own tests opt back in with HUMANIZE_ENABLED=on.
    """
    monkeypatch.setenv("HUMANIZE_ENABLED", "off")


@pytest.fixture(autouse=True)
def _human_pacing_disabled_by_default(monkeypatch):
    """Issue #626: the human-pacing engine is default-ON in production, where a comment costs a
    45s–4min read delay and the day's volume is a random draw seeded on (user, action, TODAY'S
    DATE). Both are poison for a test suite — real sleeps, and a budget that silently becomes 0 on
    whichever calendar day the rest-day draw comes up. Default it OFF so unrelated tests keep the
    pre-#626 behaviour (full cap, no delay, no jitter); the pacing tests turn it back on with
    HUMAN_PACING_ENABLED=true and pin the day/RNG explicitly.
    """
    monkeypatch.setenv("HUMAN_PACING_ENABLED", "false")


@pytest.fixture(autouse=True)
def _db_pool_disabled_by_default(monkeypatch):
    """Issue #555: get_db_connection() checks connections out of a per-process pool in production.
    The pool opens its connections through mysql-connector's own internal connect(), which the
    mock_database_connection fixture (it patches mysql.connector.connect) does NOT intercept — so a
    pooled unit test would try to open a REAL socket. Default the pool OFF for the suite so tests
    exercise the mocked direct-connect path; the pooling tests turn it back on explicitly.
    """
    from cqc_lem.utilities import db
    monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", False)


@pytest.fixture
def mock_openai_client():
    """Mock OpenAI client for testing AI-related functions."""
    with patch("cqc_lem.utilities.ai.client.OpenAI") as mock_client:
        mock_instance = MagicMock()
        mock_client.return_value = mock_instance
        
        # Mock chat completions
        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock(message=MagicMock(content="Mock AI response"))]
        mock_instance.chat.completions.create.return_value = mock_completion
        
        yield mock_instance


@pytest.fixture
def mock_database_connection():
    """Mock database connection for testing database operations."""
    with patch("mysql.connector.connect") as mock_connect:
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        
        mock_connection.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_connection
        
        yield {
            "connection": mock_connection,
            "cursor": mock_cursor,
        }


@pytest.fixture
def mock_selenium_driver():
    """Mock Selenium WebDriver for testing browser automation."""
    with patch("selenium.webdriver.Chrome") as mock_chrome:
        mock_driver = MagicMock()
        mock_chrome.return_value = mock_driver
        
        # Mock common WebDriver methods
        mock_driver.get.return_value = None
        mock_driver.find_element.return_value = MagicMock()
        mock_driver.find_elements.return_value = []
        mock_driver.quit.return_value = None
        
        yield mock_driver


@pytest.fixture
def sample_linkedin_profile():
    """Sample LinkedIn profile data for testing."""
    return {
        "full_name": "John Doe",
        "job_title": "Software Engineer",
        "company_name": "Tech Company",
        "industry": "Technology",
        "profile_url": "https://www.linkedin.com/in/johndoe/",
        "mutual_connections": ["Alice Smith", "Bob Johnson"],
        "education": [
            "University of California - B.S. Computer Science (2012-2016)"
        ],
        "experiences": [],
        "skills": ["Python", "AI", "Machine Learning"],
    }


@pytest.fixture
def sample_post_data():
    """Sample post data for testing."""
    return {
        "id": 1,
        "user_id": 60,
        "content": "This is a test post about AI and automation.",
        "status": "PENDING",
        "scheduled_time": "2024-01-01 12:00:00",
        "post_type": "TEXT",
        "media_url": None,
        "video_url": None,
    }


@pytest.fixture
def sample_message_data():
    """Sample message data for testing."""
    return {
        "recipient_profile_url": "https://www.linkedin.com/in/johndoe/",
        "recipient_name": "John Doe",
        "message_content": "Hi John, I appreciate you connecting with me on LinkedIn.",
        "user_id": 60,
    }


@pytest.fixture
def mock_replicate_training():
    """Mock Replicate training API for avatar tests."""
    with patch("replicate.trainings.create") as mock_create, \
         patch("replicate.trainings.get") as mock_get:
        training = MagicMock()
        training.id = "train-mock-abc123"
        training.status = "starting"
        training.output = None
        mock_create.return_value = training
        mock_get.return_value = training
        yield {"create": mock_create, "get": mock_get, "training": training}


@pytest.fixture
def mock_runwayml():
    """Mock the RunwayML SDK used by video_models (task completes immediately)."""
    with patch("cqc_lem.utilities.ai.video_models.RunwayML") as mock_cls, \
         patch("cqc_lem.utilities.ai.video_models.time.sleep"):
        client = MagicMock()
        mock_cls.return_value = client
        task = MagicMock()
        task.id = "task-mock-1"
        task.status = "SUCCEEDED"
        task.output = ["https://runway.example/video.mp4"]
        client.image_to_video.create.return_value = task
        client.text_to_video.create.return_value = task
        client.tasks.retrieve.return_value = task
        yield {"client": client, "class": mock_cls, "task": task}


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests whose external service keys are absent or placeholder."""
    # --- REPLICATE_API_TOKEN ---
    replicate_token = os.environ.get("REPLICATE_API_TOKEN", "")
    replicate_missing = not replicate_token or replicate_token.startswith("your_") or replicate_token in ("test-key", "")
    if replicate_missing:
        skip_replicate = pytest.mark.skip(
            reason=(
                "REPLICATE_API_TOKEN is not set or is a placeholder — "
                "set a real token to run avatar E2E tests"
            )
        )
        for item in items:
            if "requires_replicate" in item.keywords:
                item.add_marker(skip_replicate)

    # --- CAPSOLVER_API_KEY ---
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")
    capsolver_missing = not capsolver_key or capsolver_key.startswith("your_") or capsolver_key in ("", "test-key")
    if capsolver_missing:
        skip_capsolver = pytest.mark.skip(
            reason=(
                "CAPSOLVER_API_KEY is not set or is a placeholder — "
                "sign up at capsolver.com and add the key to .env to run CAPTCHA E2E tests"
            )
        )
        for item in items:
            if "requires_capsolver" in item.keywords:
                item.add_marker(skip_capsolver)


# Marker for skipping tests that require real external services
def pytest_configure(config):
    """Configure custom pytest markers."""
    config.addinivalue_line(
        "markers", "requires_openai: mark test as requiring real OpenAI API access"
    )
    config.addinivalue_line(
        "markers", "requires_database: mark test as requiring real database connection"
    )
    config.addinivalue_line(
        "markers", "requires_selenium: mark test as requiring real browser automation"
    )
    config.addinivalue_line(
        "markers", "requires_replicate: mark test as requiring a real REPLICATE_API_TOKEN"
    )
    config.addinivalue_line(
        "markers", "requires_capsolver: mark test as requiring a real CAPSOLVER_API_KEY"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
    config.addinivalue_line(
        "markers", "e2e: mark test as end-to-end test"
    )
