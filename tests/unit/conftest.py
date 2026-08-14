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
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import APIConnectionError

_BLOCKED_URL = "http://litellm.invalid/v1/chat/completions"

# Sentinel for "caller said nothing", so `fetch_all=None` can still mean a literal None row set —
# a handful of readers are tested against a driver that returns None instead of an empty list.
_UNSET = object()


def pytest_collection_modifyitems(config, items):
    """Keep only the shard named by UNIT_SHARD / UNIT_SHARDS, for splitting the lane across jobs.

    The lane is CPU-bound on a 4-vCPU runner, and `-n 4` has no more cores to claim, so the only way
    left to shorten it is more machines. Two jobs at `-n 4` is eight-way parallelism.

    Sharding is by FILE, not by test, so `--dist loadfile` still means what it means and a file's
    module-scoped fixtures are still built once. There is no committed durations file to go stale;
    the bucket is a hash instead.

    That hash is taken over the ROOTDIR-RELATIVE path, from `item.nodeid`, and that detail is the
    whole reason this is reproducible. Hashing `item.fspath` — the absolute path — makes the split
    depend on the checkout directory, so a balance measured locally says nothing about what CI will
    do, and CI itself would re-shuffle if the runner's working directory ever changed. Still a valid
    partition either way, just an unpredictable one, which defeats the point of being able to
    measure the balance before shipping it.

    Deliberately not used above UNIT_SHARDS=2: past that, the handful of heavy files
    (test_connection_seam.py alone is 16s of 98s) dominate a bucket and imbalance runs to +67%,
    and there is nothing to gain once this lane drops under the CodeQL floor that sets wall clock.

    Unset (the default, and every local run) selects everything.
    """
    shards = int(os.getenv("UNIT_SHARDS", "1"))
    if shards <= 1:
        return
    shard = int(os.getenv("UNIT_SHARD", "1"))
    kept, dropped = [], []
    for item in items:
        rel = item.nodeid.split("::", 1)[0]
        bucket = int(hashlib.sha256(rel.encode()).hexdigest(), 16) % shards + 1
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
def _no_real_celery_broker():
    """Queue a task without a broker, instead of retrying Redis twenty times (issue #1214).

    There is no broker and no result store in this lane, so an API handler that dispatches spends
    ~19s in `celery.backends.redis` reconnect retries and then raises — three minutes of the
    tests/unit/api lane on its own. Until #1214 that was masked per file: 43 client fixtures started
    `patch("cqc_lem.app.run_scheduler.generate_newsletter_drafts_for_user")` and four siblings
    BEFORE importing `api/main.py`, which bound the mocks into `api.main`'s namespace permanently
    and left the same names MagicMocks for every later file in the session. A guard here is a
    MECHANISM rather than a hand-kept list of names, so it cannot go stale the way that did, and it
    holds no matter which module a task is dispatched from.

    `send_task` is the seam every path converges on — `Task.apply_async`, `Task.delay`,
    `QueueOnce.apply_async` through its `super()` call, and a `chain(...).apply_async()`. What comes
    back is the shape production hands the caller on a SUCCESSFUL dispatch: `EagerResult` answers
    `.id` and `.state` out of its own attributes and never opens the result backend, so
    `_queued()`'s REJECTED check reads exactly what a real queued task reads.

    A test that asserts on a dispatch still patches the task where the handler reads it; that patch
    replaces the whole task object, so it never reaches this.
    """
    from celery import states
    from celery.app.base import Celery
    from celery.result import EagerResult

    def _queued_without_a_broker(self, name, *args, **kwargs):
        return EagerResult(kwargs.get("task_id") or f"test-task-{name}", None, states.PENDING)

    with patch.object(Celery, "send_task", _queued_without_a_broker):
        yield


@pytest.fixture(autouse=True)
def _no_real_mysql():
    """Fail un-mocked MySQL connections with the error CI already produced (issue #1496).

    There is no MySQL in the unit lane, so an un-mocked `get_db_connection()` was always going to
    fail in CI — but it failed as `TypeError: int() argument ... not 'NoneType'`, because an unset
    `MYSQL_PORT` reached the connector as `None`. That is not a `mysql.connector.Error`, so it
    escaped every caller's `except`, surfaced as a 500 through the API middleware, and was captured
    into production error tracking as a real defect (`update_linkedin_password` →
    `count_auth_factors`). #1486 fixed the port resolution; this stops the unit lane opening the
    socket at all.

    On a dev box the same call reaches the compose stack's LIVE database — measured, with the
    request answering `1045 Access denied` after a real round trip — so the guard also pins local
    behaviour to CI's, the same contract as `_no_real_redis`.

    The error raised is a `mysql.connector.Error`, which is what every reader in
    `platform/db/repositories/` already answers with its own fallback, so the code under test takes
    the identical branch. Tests that want a working handle keep patching `mysql.connector.connect`
    (`mock_database_connection`) or `get_db_connection` (the `fake_cursor` factory) themselves;
    those patches nest inside this one and win.
    """
    import mysql.connector

    def _blocked(*_args, **_kwargs):
        raise mysql.connector.errors.InterfaceError(
            "MySQL blocked in unit tests by tests/unit/conftest.py. Patch get_db_connection with "
            "the fake_cursor factory, or state the fixture (e.g. `signed_in`) whose collaborators "
            "this test forgot to stub."
        )

    with patch("mysql.connector.connect", side_effect=_blocked):
        yield


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


def _make_fake_cursor(
    *,
    fetch_one: Any = None,
    fetch_all: Any = _UNSET,
    fetch_one_side_effect: Any = None,
    fetch_all_side_effect: Any = None,
    rowcount: int = 1,
    lastrowid: int | None = 1,
    execute_error: Any = None,
) -> tuple[MagicMock, MagicMock]:
    """Build the (connection, cursor) pair every `db_cursor()` caller sees under test."""
    cursor = MagicMock()
    cursor.fetchone.return_value = fetch_one
    cursor.fetchall.return_value = [] if fetch_all is _UNSET else fetch_all
    if fetch_one_side_effect is not None:
        cursor.fetchone.side_effect = fetch_one_side_effect
    if fetch_all_side_effect is not None:
        cursor.fetchall.side_effect = fetch_all_side_effect
    cursor.rowcount = rowcount
    cursor.lastrowid = lastrowid
    if execute_error is not None:
        cursor.execute.side_effect = execute_error
    connection = MagicMock()
    # `db_cursor()` calls `connection.cursor(dictionary=...)`; return_value ignores the kwarg, so
    # one stub serves both the tuple-row and dict-row readers. Whichever rows the test supplies are
    # the rows the reader gets — the cursor never decides the shape.
    connection.cursor.return_value = cursor
    return connection, cursor


@pytest.fixture
def fake_cursor():
    """Issue #1213: the ONE fake MySQL cursor. Returns a factory; call it per test.

    77 test modules used to hand-roll this pair, each a slightly different shape. That is how a stub
    silently stops matching what `db_cursor()` actually returns — the drift is invisible until a
    reader is tested against a cursor production never hands it. One factory means one thing to fix
    when the real contract moves.

        conn, cur = fake_cursor(fetch_one={"id": 1})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            ...

    Keyword-only, because a positional row argument is exactly the ambiguity being retired:

    - `fetch_one` / `fetch_all` — what `fetchone()` / `fetchall()` return. `fetch_all` defaults to
      `[]`; pass `fetch_all=None` when the reader must survive a literal `None` result set.
    - `fetch_one_side_effect` / `fetch_all_side_effect` — a sequence, for a helper that runs several
      queries on ONE cursor and needs a different result set per call.
    - `rowcount` / `lastrowid` — read after `execute` by the write helpers.
    - `execute_error` — the `execute()` side effect, for the `mysql.connector.Error` fallback
      contracts. A single exception raises on every call; a LIST is consumed per call, which is how
      the stale-claim readers get an `IntegrityError` on the INSERT and a clean second statement.

    Two neighbours it deliberately does NOT replace. `mock_database_connection` (tests/conftest.py)
    patches a seam this factory does not — `mysql.connector.connect` — so it is the one to reach for
    when the code under test opens its own connection. (Eleven modules use it today; several of them
    only want its mock pair and patch `get_db_connection` themselves, which this factory would serve
    just as well — migrating those is housekeeping, not a correctness fix.) And the handful of stateful
    fakes that dispatch on the SQL text to model real table behaviour (`_Cursor` in
    test_db_early_adopter_trial / test_db_affiliate, `_FakeCursor` in test_brand_showcase_endpoint)
    are simulators, not stubs: their point is the state machine, and flattening them into canned
    rows would delete the assertion. Reach for those, not for a second copy of this factory.

    Returns:
        A callable returning the `(connection, cursor)` pair. Patch `get_db_connection` with the
        connection; assert against the cursor.
    """
    return _make_fake_cursor
