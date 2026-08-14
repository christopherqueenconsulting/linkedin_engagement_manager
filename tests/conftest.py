"""Pytest configuration and shared fixtures for LinkedIn Engagement Manager tests.

This module provides:
- Mock fixtures for external dependencies (OpenAI, LinkedIn API, Database)
- Common test data fixtures
- Test configuration and markers
"""

import importlib
import os
from typing import Any, Iterator
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

# Also MODULE level, and for a sharper reason: `cqc_lem.utilities.db` is a FACADE that does
# `from cqc_lem.platform.db.connection import get_db_connection` at import time. Whatever that name
# is bound to at THAT moment is what the facade re-exports forever. Hundreds of tests patch
# `platform.db.connection.get_db_connection` — so if the very first import of the facade happens
# inside one of those `with patch(...)` blocks, the facade permanently re-exports a MagicMock and
# every later test reading `db.get_db_connection` silently gets a dead mock.
#
# Which test imports it first depends on collection order, so this was a latent landmine that
# passed on a full run and failed on a subset. Importing it here binds the real functions before
# any test can patch anything.
# Done as an import_module CALL rather than a plain import with a per-line suppression comment,
# because the two linters disagree about that line: the suppression silences ruff, and CodeQL --
# which cannot see ruff directives -- files a py/unused-import anyway. A call is a use to both.
importlib.import_module("cqc_lem.utilities.db")


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

    Patched on `platform.db.connection`, which is where `get_db_connection` READS the flag.
    Setting
    it on `utilities.db` — where it used to live — now only rebinds the facade's copy and leaves the
    real one untouched, which is the whole patch-seam hazard this split had to get right. It fails
    loudly rather than silently: the pool has no mocked socket to hand out, so it exhausts.
    """
    from cqc_lem.platform.db import connection
    monkeypatch.setattr(connection, "MYSQL_POOL_ENABLED", False)


@pytest.fixture
def api_client() -> Iterator[Any]:
    """The ONE TestClient over the real `cqc_lem.api.main.app` — issue #1214.

    Every API test in both lanes goes through here, so the app is imported exactly once, by a
    caller that is patching nothing. That import order is the point. `api/main.py` binds its five
    Celery tasks into its own namespace with `from ... import <task>` at module scope, so the 73
    per-file client factories this replaced — most of which started
    `patch("cqc_lem.app.engagement.posting.automate_reply_commenting")` and four siblings BEFORE
    importing the app — left `api.main` holding MagicMocks for the rest of the session once the
    first of them ran, and were vacuous for every file after it. Measured, not inferred:
    `tests/unit/api/test_api_client_fixture.py` asserts those symbols are still the real tasks.

    A test that needs a dispatch stubbed patches it where the handler READS it
    (`cqc_lem.api.main.<task>`, `cqc_lem.api.routers.<module>.<task>`) for the length of its own
    request, which is what every such test here already did. That is the rule #1194 landed for
    every other symbol on `main`, and it is why this fixture patches NOTHING itself: a blanket
    per-file patch is exactly how 13 tests came to bind a dead copy.

    Function-scoped, and `app.dependency_overrides` is restored on the way out, so a test may
    install an override without leaking it into the next one. The lifespan costs ~1.7ms per test.

    `raise_server_exceptions=False` is the house default: a handler that raises is reported as the
    500 the caller would really have received, which is what the middleware-level tests assert on.
    """
    from fastapi.testclient import TestClient

    from cqc_lem.api.main import app

    saved_overrides = dict(app.dependency_overrides)
    with TestClient(app, raise_server_exceptions=False) as client:
        try:
            yield client
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(saved_overrides)


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
        "markers", "slow: mark test as slow running"
    )
