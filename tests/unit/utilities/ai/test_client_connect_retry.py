"""A LiteLLM proxy that is not accepting connections is ridden out, not lost (issue #986).

The proxy is a container LEM restarts on its own schedule, and while it comes back up every call
gets `[Errno 111] Connection refused`. The OpenAI SDK's own retries are spent in ~1.5s, so a single
restart used to abort whatever generation was in flight and file an `APIConnectionError` defect for
working infrastructure.

These drive REAL requests through the SDK (same approach as test_client_attribution.py): the retry
hangs off an SDK method, so an upgrade that moves it must fail here rather than silently give the
proxy back its one-blip-loses-the-call behaviour.
"""
from unittest.mock import patch

import httpx
import pytest

pytestmark = pytest.mark.unit

_CHAT_RESPONSE = {
    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
}


class _RefusingTransport(httpx.BaseTransport):
    """Refuses the first `refusals` connections the way a starting container does, then serves."""

    def __init__(self, refusals: int, status: int = 200, timeout: bool = False):
        self.refusals = refusals
        self.status = status
        self.timeout = timeout
        self.attempts = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.attempts <= self.refusals:
            if self.timeout:
                raise httpx.ConnectTimeout("timed out", request=request)
            raise httpx.ConnectError("[Errno 111] Connection refused", request=request)
        return httpx.Response(self.status, json=_CHAT_RESPONSE)


def _client(transport: _RefusingTransport):
    # Imported here, not at module scope: importing the module CONSTRUCTS the shared client, which
    # needs the API key the session-scoped env fixture only sets after collection.
    from cqc_lem.utilities.ai.client import AttributedOpenAI
    # max_retries=0 so the transport's attempt count is exactly our retry budget, not the SDK's.
    return AttributedOpenAI(api_key="k", base_url="http://litellm:4000", max_retries=0,
                            http_client=httpx.Client(transport=transport))


def _chat(client):
    return client.chat.completions.create(model="lem-medium",
                                          messages=[{"role": "user", "content": "hi"}])


class TestRidingOutARestartingProxy:
    def test_a_refused_connection_is_retried_until_the_proxy_is_back(self):
        transport = _RefusingTransport(refusals=2)
        with patch("cqc_lem.utilities.ai.client.time.sleep") as sleep:
            response = _chat(_client(transport))
        assert response.choices[0].message.content == "hi"
        assert transport.attempts == 3
        # Exponential, so a slow container start is still covered by the last wait.
        assert [call.args[0] for call in sleep.call_args_list] == [8.0, 16.0]

    def test_a_proxy_that_stays_down_still_fails_the_call(self, monkeypatch):
        """The budget is bounded — a real outage must still reach error tracking."""
        from openai import APIConnectionError
        monkeypatch.setenv("LLM_CONNECT_RETRY_ATTEMPTS", "2")
        transport = _RefusingTransport(refusals=99)
        with patch("cqc_lem.utilities.ai.client.time.sleep"):
            with pytest.raises(APIConnectionError):
                _chat(_client(transport))
        assert transport.attempts == 2

    def test_one_wait_is_capped_so_a_mistyped_budget_cannot_hang_a_worker(self, monkeypatch):
        """Attempts is operator-tunable and every wait doubles. Uncapped, `ATTEMPTS=10` would sleep
        for over an hour inside a Celery task that sets no time limit — the cap keeps the total
        linear without touching the default 8s/16s schedule.
        """
        from openai import APIConnectionError
        monkeypatch.setenv("LLM_CONNECT_RETRY_ATTEMPTS", "6")
        transport = _RefusingTransport(refusals=99)
        with patch("cqc_lem.utilities.ai.client.time.sleep") as sleep:
            with pytest.raises(APIConnectionError):
                _chat(_client(transport))
        assert [call.args[0] for call in sleep.call_args_list] == [8.0, 16.0, 32.0, 60.0, 60.0]

    def test_one_attempt_turns_the_wait_off(self, monkeypatch):
        from openai import APIConnectionError
        monkeypatch.setenv("LLM_CONNECT_RETRY_ATTEMPTS", "1")
        transport = _RefusingTransport(refusals=99)
        with patch("cqc_lem.utilities.ai.client.time.sleep") as sleep:
            with pytest.raises(APIConnectionError):
                _chat(_client(transport))
        assert transport.attempts == 1
        sleep.assert_not_called()

    def test_an_unparseable_setting_falls_back_to_the_default(self, monkeypatch):
        """A typo in `.env` must not take LLM traffic down, in either direction."""
        monkeypatch.setenv("LLM_CONNECT_RETRY_ATTEMPTS", "lots")
        monkeypatch.setenv("LLM_CONNECT_RETRY_BACKOFF_SECONDS", "soon")
        transport = _RefusingTransport(refusals=2)
        with patch("cqc_lem.utilities.ai.client.time.sleep") as sleep:
            _chat(_client(transport))
        assert transport.attempts == 3
        assert [call.args[0] for call in sleep.call_args_list] == [8.0, 16.0]

    def test_the_retry_is_debug_not_a_warning(self):
        """A proxy restart happens on every deploy — warning on it would escalate and file a defect
        for working behaviour (the #917 lesson). Only the exhausted budget is an error, and the
        caller logs that.
        """
        transport = _RefusingTransport(refusals=1)
        with patch("cqc_lem.utilities.ai.client.time.sleep"), \
             patch("cqc_lem.utilities.ai.client.log_debug") as debug:
            _chat(_client(transport))
        assert debug.call_count == 1


class TestWhatIsDeliberatelyNotRetried:
    def test_a_timeout_is_not_retried(self):
        """A connect timeout is not a refusal: the SDK reports every timeout as APITimeoutError, and
        a read timeout may already have been served and billed. Retrying would double the spend.
        """
        from openai import APITimeoutError
        transport = _RefusingTransport(refusals=99, timeout=True)
        with patch("cqc_lem.utilities.ai.client.time.sleep") as sleep:
            with pytest.raises(APITimeoutError):
                _chat(_client(transport))
        assert transport.attempts == 1
        sleep.assert_not_called()

    def test_a_server_error_is_not_retried(self):
        """A 5xx is the proxy answering — that is a real failure, not an unreachable proxy."""
        from openai import InternalServerError
        transport = _RefusingTransport(refusals=0, status=500)
        with patch("cqc_lem.utilities.ai.client.time.sleep") as sleep:
            with pytest.raises(InternalServerError):
                _chat(_client(transport))
        assert transport.attempts == 1
        sleep.assert_not_called()


class TestEveryEndpointIsCovered:
    def test_embeddings_ride_it_out_too(self, monkeypatch):
        """Embeddings (comment dedup, feedback clustering) never go through `_call_llm`, so the
        client is the only place their connection failure can be retried.
        """
        from openai import APIConnectionError
        monkeypatch.setenv("LLM_CONNECT_RETRY_ATTEMPTS", "3")
        transport = _RefusingTransport(refusals=99)
        with patch("cqc_lem.utilities.ai.client.time.sleep"):
            with pytest.raises(APIConnectionError):
                _client(transport).embeddings.create(model="lem-embedding", input=["a"])
        assert transport.attempts == 3

    def test_the_shared_client_carries_the_guard(self):
        """The tests above build their own client; pin the ONE instance every helper imports."""
        from cqc_lem.utilities.ai.client import AttributedOpenAI, client
        assert type(client).post is AttributedOpenAI.post
