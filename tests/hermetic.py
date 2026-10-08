"""Shared hermetic guards: block real LLM, MySQL, Redis, Celery-broker and Selenium I/O.

ONE implementation, two callers. `tests/unit/conftest.py` installs each guard as an autouse fixture
(its docstrings carry the history of why each one exists), and `scripts/prompt_capture.py` installs
all of them at once with `hermetic_io()` — capture runs the REAL production prompt builders outside
pytest, so without these a builder whose spec forgot a patch would write to the host's database or
spend on a real model (docs/prompt-evals.md).

Every guard raises the exception production already raises when that dependency is down, so the code
under test takes its existing failure branch instead of a new one. A patch applied inside a guard
nests within it and wins.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import httpx

_BLOCKED_URL = "http://litellm.invalid/v1/chat/completions"

#: Raised by `block_llm`, for the same reason `BLOCKED_MYSQL_MESSAGE` exists below and one more.
#: `APIConnectionError`'s SDK default is "Connection error." — the exact string a genuinely
#: unreachable LiteLLM proxy produces (#986) — so the message is the ONLY thing that tells this guard
#: apart from the outage it imitates. It is also what a leaked `$exception` titles its error group
#: with: this guard is the single largest piece of the 2026-08 test-pollution residue at 6,154
#: occurrences, and read as a production incident until someone traced the frame (#1665).
#: Asserted on by tests/unit/utilities/ai/test_client_connect_retry.py.
BLOCKED_LLM_MESSAGE = (
    "unit-test fixture: LLM calls blocked by tests/unit/conftest.py. Patch the `client` the "
    "module under test imported (e.g. the mock_openai_client fixture) instead of reaching a "
    "real endpoint."
)

#: Raised by `block_mysql`. The wording is asserted on by
#: tests/unit/platform/db/test_connection_config.py, because a REFUSED real socket is also a
#: `mysql.connector.Error` — only the message tells the two apart, so only the message can prove
#: the guard is the thing that answered.
BLOCKED_MYSQL_MESSAGE = (
    "MySQL blocked in unit tests by tests/unit/conftest.py. Patch get_db_connection with "
    "the fake_cursor factory, or state the fixture (e.g. `signed_in`) whose collaborators "
    "this test forgot to stub."
)

BLOCKED_CLIENT_MESSAGE = (
    "A new OpenAI client was constructed under the hermetic guard. Only the shared "
    "`cqc_lem.utilities.ai.client.client` is patched, so a second client would reach the network."
)


def _blocked_llm_call(*_args: Any, **_kwargs: Any) -> Any:
    from openai import APIConnectionError

    raise APIConnectionError(message=BLOCKED_LLM_MESSAGE,
                             request=httpx.Request("POST", _BLOCKED_URL))


@contextlib.contextmanager
def block_llm() -> Iterator[None]:
    """Fail every endpoint of the shared LLM client instantly, as an unreachable proxy would."""
    from cqc_lem.utilities.ai.client import client

    with patch.object(client.chat.completions, "create", side_effect=_blocked_llm_call), \
         patch.object(client.embeddings, "create", side_effect=_blocked_llm_call), \
         patch.object(client.images, "generate", side_effect=_blocked_llm_call), \
         patch.object(client.responses, "create", side_effect=_blocked_llm_call):
        yield


@contextlib.contextmanager
def block_new_llm_clients() -> Iterator[None]:
    """Refuse to build any NEW OpenAI client (e.g. `ai_helper.choose_post_reaction`'s fallback)."""
    import openai

    def _refuse(*_args: Any, **_kwargs: Any) -> None:
        raise openai.APIConnectionError(message=BLOCKED_CLIENT_MESSAGE,
                                        request=httpx.Request("POST", _BLOCKED_URL))

    with patch.object(openai.OpenAI, "__init__", _refuse):
        yield


@contextlib.contextmanager
def block_redis() -> Iterator[None]:
    """Keep every `_redis_client()` caller on its fails-open (None) path."""
    from cqc_lem.utilities.linkedin.rate_limit import reset_redis_client

    reset_redis_client()
    with patch("redis.Redis.from_url", side_effect=ConnectionError("redis blocked in unit tests")):
        yield
    reset_redis_client()


@contextlib.contextmanager
def block_celery_broker() -> Iterator[None]:
    """Answer a task dispatch with a queued EagerResult instead of reaching a broker."""
    from celery import states
    from celery.app.base import Celery
    from celery.result import EagerResult

    def _queued_without_a_broker(self: Any, name: str, *args: Any, **kwargs: Any) -> EagerResult:
        return EagerResult(kwargs.get("task_id") or f"test-task-{name}", None, states.PENDING)

    with patch.object(Celery, "send_task", _queued_without_a_broker):
        yield


@contextlib.contextmanager
def block_mysql() -> Iterator[None]:
    """Fail both routes to a MySQL socket with the `mysql.connector.Error` readers already handle."""
    import mysql.connector

    def _blocked(*_args: Any, **_kwargs: Any) -> None:
        raise mysql.connector.errors.InterfaceError(BLOCKED_MYSQL_MESSAGE)

    with patch("mysql.connector.connect", side_effect=_blocked), \
         patch("mysql.connector.pooling.connect", side_effect=_blocked):
        yield


@contextlib.contextmanager
def block_selenium() -> Iterator[None]:
    """Fail Grid readiness checks instantly instead of polling for 60s."""
    from cqc_lem.utilities import selenium_util

    def _blocked(host: str, port: int, timeout: int = 60) -> None:
        raise TimeoutError(
            f"Selenium not ready at http://{host}:{port}/wd/hub/status — blocked by "
            "tests/unit/conftest.py. Patch _wait_for_selenium_ready or get_docker_driver if "
            "this test needs a driver."
        )

    with patch.object(selenium_util, "_wait_for_selenium_ready", side_effect=_blocked):
        yield


@contextlib.contextmanager
def hermetic_io() -> Iterator[None]:
    """Install every guard above at once — what prompt capture runs under outside pytest."""
    with contextlib.ExitStack() as stack:
        for guard in (block_llm, block_new_llm_clients, block_redis, block_celery_broker,
                      block_mysql, block_selenium):
            stack.enter_context(guard())
        yield
