"""Unit-lane guards that keep tests/unit/ hermetic and fast.

Issue #480: a handful of unit tests reached the real network because the shared
`cqc_lem.utilities.ai.client.client` singleton was never mocked. Every such call burned
~1.3s in httpx connect + the OpenAI SDK's retry back-off sleeps before the production
except-branch finally ran. Nothing under tests/unit/ is allowed to talk to a real LLM
endpoint, so the call is failed immediately instead: the code under test takes the exact
same failure branch it already took in CI, it just gets there without the sleeps.

Tests that need a working LLM/Redis handle still patch it themselves — their patch nests
inside these fixtures and wins.
"""

from unittest.mock import patch

import httpx
import pytest
from openai import APIConnectionError

_BLOCKED_URL = "http://litellm.invalid/v1/chat/completions"


def _blocked_llm_call(*args, **kwargs):
    raise APIConnectionError(request=httpx.Request("POST", _BLOCKED_URL))


@pytest.fixture(autouse=True)
def _no_real_llm_calls():
    """Fail un-mocked LLM traffic instantly instead of dialing out and retrying."""
    # Imported lazily: constructing the singleton needs the API key that the
    # session-scoped setup_test_environment fixture puts in os.environ, which is not
    # yet set at the time this conftest module is imported.
    from cqc_lem.utilities.ai.client import client

    with patch.object(client.chat.completions, "create",
                      side_effect=_blocked_llm_call), \
         patch.object(client.embeddings, "create", side_effect=_blocked_llm_call), \
         patch.object(client.images, "generate", side_effect=_blocked_llm_call):
        yield


@pytest.fixture(autouse=True)
def _no_real_redis():
    """Keep un-mocked `_redis_client()` callers on the fails-open (None) path.

    In CI there is no Redis in the unit lane, so every one of those call sites already
    took the None branch. On a dev box running the compose stack the same tests would
    instead connect to the real broker and read/write live breaker keys — slower and,
    worse, non-deterministic. Failing construction pins both environments to the CI
    behaviour. Tests that want a working handle keep patching `_redis_client` directly;
    those patches bypass this entirely.
    """
    blocked = ConnectionError("redis blocked in unit tests")
    with patch("redis.Redis.from_url", side_effect=blocked):
        yield


@pytest.fixture(autouse=True)
def _feature_flags_env_only(monkeypatch):
    """Issue #651: the flag wrapper polls PostHog for flag DEFINITIONS the first time any flag is
    checked. `tests/conftest.py` calls load_dotenv(), so a dev box with a real
    POSTHOG_PERSONAL_API_KEY in .env would make the unit lane reach the network — and worse, make
    a test's outcome depend on whichever rollout is live in PostHog right now. Pin the whole lane
    to the fail-open (env var) path, which is exactly what CI already gets; tests/unit/utilities/
    test_flags.py turns the backend back on with an explicitly faked SDK.
    """
    monkeypatch.setenv("POSTHOG_FLAGS_ENABLED", "false")
    from cqc_lem.utilities import flags
    flags.reset_flag_state()
