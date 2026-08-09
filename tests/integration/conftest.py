"""Integration-lane isolation: one MySQL database, one Redis logical DB, and no live LLM.

The DB-touching tests in this lane key their state on a per-file email constant and clear it either
side of the test (`DELETE FROM users WHERE email=%s`, add, then delete again). That is enough while
ONE process owns the server. It stops being enough the moment `-n` is greater than 1: two workers
running two files share `users`, `logs`, `sessions` and `auth_audit_log`, so anything that counts,
sweeps, or asserts the absence of rows it did not itself write starts answering differently
depending on what the other worker happens to be doing at that instant.

**Why a whole database per worker and not a transaction per test.** A rollback fixture has to own
every statement the test causes, and nothing here routes through a single connection: `db_cursor`
checks out its own connection per call and commits explicitly, and the API tests go through a
FastAPI `TestClient` that does the same several times per request. Wrapping that in one transaction
would mean pinning the whole process to one connection and neutering `commit()` — and committing is
precisely what several of these tests exist to prove: the ON DELETE CASCADE in
`test_comment_outcomes_db`, the UNIQUE claim that makes a challenge single-use in `test_strong_auth`,
the pool's `pool_reset_session` round trip in `test_db_pooling`. A per-worker database leaves every
one of those exactly as real as it is today and puts the isolation boundary where the workers
actually collide, which is the server's row space rather than any one test's control flow.

The clone is structural, not a re-migration: `SHOW CREATE TABLE` off the migrated template preserves
the foreign keys that `CREATE TABLE ... LIKE` would silently drop, and the template's rows come
across too, so seed data a migration wrote (`early_adopter_slots`' P0/P1 cohorts) is present.

Serial runs are untouched. Without `PYTEST_XDIST_WORKER` this yields immediately and the suite talks
to the configured database exactly as it did before.

The other thing this file owns is `_no_live_llm_calls` (issue #1188) — the guard that turns an
unmocked LLM call from a slow test into a LOUD one. The reasoning is in that fixture's docstring.
"""

import os
import re
from collections.abc import Iterator
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import mysql.connector
import pytest
from openai import OpenAI

from cqc_lem.platform.db import connection as db_connection

# Redis' default `databases` setting. A wider `-n` than this wraps back onto db 0 and shares a
# window with gw0 — the same sharing every worker has today, not a new failure mode.
_REDIS_LOGICAL_DATABASES = 16

# Database and table names are interpolated into DDL that takes no placeholders. They come from the
# environment and from information_schema, so they are ours either way — but validating them is what
# makes that true rather than assumed, and keeps the intent legible to a reader and to CodeQL.
_IDENTIFIER = re.compile(r"\A[A-Za-z0-9_]+\Z")


def _worker_id() -> Optional[str]:
    """This xdist worker's id (`gw0`, `gw1`, …), or None when pytest is running single-process."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    return worker if worker and worker != "master" else None


def _checked(identifier: str) -> str:
    """Returns the identifier, or raises if it is not a bare name safe to quote into DDL."""
    if not _IDENTIFIER.match(identifier):
        raise RuntimeError(f"refusing to build SQL around the identifier {identifier!r}")
    return identifier


def _server_reachable(config: dict[str, Any]) -> bool:
    """True when the configured MySQL answers. False is a box without one, not a failure."""
    try:
        connection = mysql.connector.connect(connect_timeout=5, **config)
    except Exception:  # noqa: BLE001 - unset or unreachable both mean "no server here"
        return False
    connection.close()
    return True


def _clone_database(config: dict[str, Any], template: str, target: str) -> None:
    """Rebuilds `template`'s tables and rows in a fresh `target` database.

    Raises:
        RuntimeError: the server is up but would not give us the database — almost always a test
            user without rights outside the one schema. Failing here is deliberate: quietly falling
            back to the shared database under `-n 4` is the flaky suite this fixture exists to
            prevent.
    """
    template = _checked(template)
    target = _checked(target)
    connection = mysql.connector.connect(**config)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
            (template,),
        )
        tables = [_checked(row[0]) for row in cursor.fetchall()]
        # Read every CREATE while the connection is still pointed at the template, then switch once.
        ddl = []
        for table in tables:
            cursor.execute(f"SHOW CREATE TABLE `{table}`")
            ddl.append(cursor.fetchone()[1])

        cursor.execute(f"DROP DATABASE IF EXISTS `{target}`")
        cursor.execute(f"CREATE DATABASE `{target}`")
        cursor.execute(f"USE `{target}`")
        # The CREATEs are replayed in information_schema order, which is not dependency order, and
        # the row copy would trip every foreign key it re-creates. Both are safe to defer: the
        # source rows already satisfy the constraints.
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
        for statement in ddl:
            cursor.execute(statement)
        for table in tables:
            cursor.execute(f"INSERT INTO `{table}` SELECT * FROM `{template}`.`{table}`")
        cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        connection.commit()
        cursor.close()
    except mysql.connector.Error as e:
        raise RuntimeError(
            f"could not build the per-worker database `{target}`: {e}\n"
            f"Running this lane under pytest-xdist needs the test user to own its own databases:\n"
            f"    GRANT ALL PRIVILEGES ON `{template}\\_%`.* TO '<user>'@'%';"
        ) from e
    finally:
        connection.close()


def _drop_database(config: dict[str, Any], target: str) -> None:
    """Best-effort teardown. A drop that fails must never mask the test result that preceded it.

    `config` is the one captured before the fixture repointed the process, so this connects to the
    template — the one database still guaranteed to be there once the clone goes away.
    """
    try:
        connection = mysql.connector.connect(**config)
        try:
            cursor = connection.cursor()
            cursor.execute(f"DROP DATABASE IF EXISTS `{_checked(target)}`")
            cursor.close()
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - teardown of a throwaway schema; CI drops the whole server
        pass


def _redis_url_for_worker(url: str, worker: str) -> str:
    """Points a `redis://` URL at this worker's own logical database; anything else is returned as is.

    Every Redis handle this lane reaches for is patched with a fake today, so nothing currently
    depends on it. It is here because the moment one of them is not — an auth window, a pacing draw,
    a 429 breaker key — two workers on db 0 would be reading each other's counters, and that failure
    presents as a heisenbug rather than as an error.
    """
    if not url.startswith("redis"):
        return url
    digits = "".join(character for character in worker if character.isdigit())
    index = (int(digits) if digits else 0) % _REDIS_LOGICAL_DATABASES
    return urlunsplit(urlsplit(url)._replace(path=f"/{index}"))


@pytest.fixture(autouse=True)
def _no_live_llm_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Refuses, loudly, any LLM request a test in this lane did not mock (issue #1188).

    Three tests here were 281s of a 354s lane while doing no work at all: each fell through to a
    real request, and with no LiteLLM proxy on the runner every one of them paid the PRODUCTION
    connect-retry ride-out (#986, ~24s) for a response the test never asserted on. That is a
    *slow* failure, which is the kind nobody reads — the lane got faster and the tests stayed
    wrong. The fix is to make it a failure at all.

    `OpenAI.post` is the seam, not the three endpoint methods the unit lane's #480 guard patches:
    every endpoint the SDK exposes (chat, embeddings, images, speech) funnels through it, so a new
    call shape inherits the guard, and it catches the raw `openai.OpenAI()` fallback in
    `ai_helper.choose_post_reaction` as well as the shared `AttributedOpenAI` singleton. Setting it
    on the base class covers the subclass too, because `AttributedOpenAI.post` reaches it via
    `super()` — and the retry loop there passes a refusal straight through, since only a real
    `httpx.ConnectError` is retryable.

    Refusing with pytest's own `Failed` (a BaseException) rather than the `APIConnectionError` the
    unit-lane guard raises is the deliberate part, and it is what makes this LOUD rather than
    merely fast: LEM's LLM helpers are full of `except Exception` fallbacks — that is exactly how
    an unmocked call sits here for months looking like a passing test — and `Failed` is not
    catchable by any of them. Every `except BaseException` in `src/` re-raises, so there is nowhere
    for a refusal to be quietly absorbed.

    A test that CONSTRUCTS a client and never calls it is untouched — the guard is on the request,
    not the constructor. A test that wants a failing call still gets one by mocking the failure it
    means to assert on, which is a clearer test than borrowing this one.
    """
    def _refuse(self: OpenAI, *args: Any, **kwargs: Any) -> Any:
        path = args[0] if args else kwargs.get("path", "<unknown>")
        pytest.fail(
            f"An integration test made a live LLM request to {path!r}. Mock it: patch the "
            f"`client` the module under test imported (the `mock_openai_client` fixture is the "
            f"house shape), or patch the helper that wraps it. See issue #1188."
        )  # The traceback is kept: which helper reached the network is the whole question here.

    monkeypatch.setattr(OpenAI, "post", _refuse)
    yield


@pytest.fixture(scope="session", autouse=True)
def _worker_isolated_state() -> Iterator[None]:
    """Gives this xdist worker its own database and Redis logical DB for the length of the session."""
    worker = _worker_id()
    template = db_connection.MYSQL_DATABASE
    if worker is None or not template:
        yield
        return

    config = db_connection._get_mysql_config()
    if not _server_reachable(config):
        # No migrated server on this box. Every DB-touching test already skips itself for that, and
        # a hard failure here would turn a skip into a red lane.
        yield
        return

    target = f"{template}_{worker}"
    _clone_database(config, template, target)

    patcher = pytest.MonkeyPatch()
    # `_get_mysql_config()` reads this module global, so this is the one binding that decides which
    # database the whole process talks to. The env var follows it for anything that re-reads os.environ.
    patcher.setattr(db_connection, "MYSQL_DATABASE", target)
    patcher.setenv("MYSQL_DATABASE", target)
    for variable in ("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
        current = os.environ.get(variable, "")
        if current:
            patcher.setenv(variable, _redis_url_for_worker(current, worker))
    db_connection.reset_connection_pool()
    try:
        yield
    finally:
        db_connection.reset_connection_pool()
        patcher.undo()
        _drop_database(config, target)
