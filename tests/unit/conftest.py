"""Unit-lane guards that keep tests/unit/ hermetic and fast.

Issue #480: a handful of unit tests reached the real network because the shared
`cqc_lem.utilities.ai.client.client` singleton was never mocked. Every such call burned
~1.3s in httpx connect + the OpenAI SDK's retry back-off sleeps before the production
except-branch finally ran. Nothing under tests/unit/ is allowed to talk to a real LLM
endpoint, so the call is failed immediately instead: the code under test takes the exact
same failure branch it already took in CI, it just gets there without the sleeps.

Tests that need a working LLM/Redis/Selenium handle still patch it themselves — their patch nests
inside these fixtures and wins.
"""

import hashlib
import os
from unittest.mock import patch

import httpx
import pytest
from openai import APIConnectionError

_BLOCKED_URL = "http://litellm.invalid/v1/chat/completions"


def pytest_collection_modifyitems(config, items):
    """Keep only the shard named by UNIT_SHARD / UNIT_SHARDS, for splitting the lane across jobs.

    The lane is CPU-bound on a 4-vCPU runner, and `-n 4` has no more cores to claim, so the only way
    left to shorten it is more machines. Two jobs at `-n 4` is eight-way parallelism.

    Sharding is by FILE, not by test, so `--dist loadfile` still means what it means and a file's
    module-scoped fixtures are still built once. The bucket is a stable hash of the path, which is
    what makes this need no committed durations file to go stale: measured over the 456 unit files
    it lands within 2% of an even split at UNIT_SHARDS=2. It is deliberately NOT used above 2 —
    at 3 or 4 the handful of heavy files (test_connection_seam.py alone is 16s of 98s) dominate a
    bucket and imbalance reaches +67%, and there is nothing to gain anyway once the lane drops
    under the CodeQL floor that sets the PR's wall clock.

    Unset (the default, and every local run) selects everything.
    """
    shards = int(os.getenv("UNIT_SHARDS", "1"))
    if shards <= 1:
        return
    shard = int(os.getenv("UNIT_SHARD", "1"))
    kept, dropped = [], []
    for item in items:
        path = str(item.fspath if hasattr(item, "fspath") else item.path)
        bucket = int(hashlib.sha256(path.encode()).hexdigest(), 16) % shards + 1
        (kept if bucket == shard else dropped).append(item)
    items[:] = kept
    config.hook.pytest_deselected(items=dropped)


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
    from cqc_lem.utilities.linkedin.rate_limit import reset_redis_client

    # The handle is cached per (pid, url) so hot paths stop paying a TCP handshake per command.
    # Clear it around every test, or a test that patches `from_url` to SUCCEED leaves its mock
    # cached for the next one — the same reason `flags.reset_flag_state()` is called below.
    reset_redis_client()
    blocked = ConnectionError("redis blocked in unit tests")
    with patch("redis.Redis.from_url", side_effect=blocked):
        yield
    reset_redis_client()


@pytest.fixture(autouse=True)
def _no_real_selenium():
    """Fail un-mocked Grid readiness checks instantly instead of polling for the full 60s.

    `selenium_util._wait_for_selenium_ready` polls `selenium-chrome:4444` in a `sleep(2)` loop
    against a hard-coded 60s deadline before raising. There is no Grid in the unit lane, so every
    un-mocked `get_docker_driver()` already ended at that `TimeoutError` — it just spent 30 sleeps
    getting there. One test (`test_dwell_score_persist.py`, whose patch targeted a name the code
    never reads) was 60s of a 179s suite on its own.

    Same contract as `_no_real_llm_calls`: raise the exception production raises, so the code under
    test takes the identical branch without the sleeps. The four unit modules that drive
    `get_docker_driver` for real already patch this symbol themselves; their patch nests inside
    this one and wins.
    """
    from cqc_lem.utilities import selenium_util

    def _blocked(host, port, timeout=60):
        raise TimeoutError(
            f"Selenium not ready at http://{host}:{port}/wd/hub/status — blocked by "
            "tests/unit/conftest.py. Patch _wait_for_selenium_ready or get_docker_driver if "
            "this test needs a driver."
        )

    with patch.object(selenium_util, "_wait_for_selenium_ready", side_effect=_blocked):
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
