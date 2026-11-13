"""`_call_llm` treats a proxy connection error as a warning, any other failure as an error (#2557)."""
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from cqc_lem.utilities.ai import ai_helper


def _conn_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "http://litellm:4000/chat"))


def test_connection_error_warns_and_still_raises():
    with patch.object(ai_helper.client.chat.completions, "create", side_effect=_conn_error()), \
            patch.object(ai_helper, "log_warning") as warn, patch.object(ai_helper, "log_error") as err:
        with pytest.raises(openai.APIConnectionError):
            ai_helper._call_llm(model="lem-simple", messages=[])
    warn.assert_called_once()
    err.assert_not_called()


def test_other_failure_still_errors():
    with patch.object(ai_helper.client.chat.completions, "create", side_effect=RuntimeError("boom")), \
            patch.object(ai_helper, "log_warning") as warn, patch.object(ai_helper, "log_error") as err:
        with pytest.raises(RuntimeError):
            ai_helper._call_llm(model="lem-simple", messages=[])
    err.assert_called_once()
    warn.assert_not_called()


def test_post_is_relevant_fails_open_on_connection_error():
    with patch.object(ai_helper, "_call_llm", side_effect=_conn_error()):
        assert ai_helper.post_is_relevant("text", ["ai"]) is True
