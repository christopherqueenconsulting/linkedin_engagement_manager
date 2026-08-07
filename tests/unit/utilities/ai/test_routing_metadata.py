"""`_call_llm` tells the LiteLLM proxy which (user, feature) bucket a call belongs to, so the
complexity router can look up its cost-aware routing policy (issue #494).
"""
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"


def _response():
    response = MagicMock()
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 5
    return response


def _call(model, **kwargs):
    from cqc_lem.utilities.ai.ai_helper import _call_llm
    create = MagicMock(return_value=_response())
    with patch(f"{_AI}.client") as client:
        client.chat.completions.create = create
        _call_llm(model=model, messages=[{"role": "user", "content": "hi"}], **kwargs)
    return create.call_args[1]


class TestRoutingMetadata:
    def test_tier_calls_carry_the_attribution_bucket(self):
        with patch("cqc_lem.utilities.observability.current_llm_attribution",
                   return_value=(7, "content")):
            sent = _call("lem-complex")
        assert sent["extra_body"]["metadata"] == {"feature": "content", "user_id": 7}

    def test_explicit_track_kwargs_win_over_the_ambient_scope(self):
        with patch("cqc_lem.utilities.observability.current_llm_attribution",
                   return_value=(1, "system")):
            sent = _call("lem-medium", _track_user_id=9, _track_feature="newsletter")
        assert sent["extra_body"]["metadata"] == {"feature": "newsletter", "user_id": 9}
        # The tracking kwargs are still popped — they must never reach the proxy as request fields.
        assert "_track_user_id" not in sent and "_track_feature" not in sent

    def test_unattributed_calls_carry_the_system_sentinel(self):
        """LiteLLM's PostHog logger uses metadata.user_id as the $ai_generation distinct_id, so an
        unattributed call must still name one — otherwise every housekeeping call mints its own
        anonymous person (issue #647).
        """
        with patch("cqc_lem.utilities.observability.current_llm_attribution",
                   return_value=(None, None)):
            sent = _call("lem-simple")
        assert sent["extra_body"]["metadata"] == {"feature": "system", "user_id": "system"}

    def test_raw_provider_models_are_not_tagged(self):
        """Only tier aliases are routable, so nothing is attached to a raw provider model."""
        assert "extra_body" not in _call("gpt-4o-mini")

    def test_a_callers_own_metadata_is_not_overwritten(self):
        sent = _call("lem-simple", extra_body={"metadata": {"feature": "custom"}})
        assert sent["extra_body"]["metadata"] == {"feature": "custom"}

    def test_attribution_failure_never_breaks_the_call(self):
        with patch("cqc_lem.utilities.observability.current_llm_attribution",
                   side_effect=RuntimeError("boom")):
            sent = _call("lem-complex")
        assert "metadata" not in sent.get("extra_body", {})
