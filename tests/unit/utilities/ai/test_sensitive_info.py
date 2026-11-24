"""Sensitive-info check + its wiring and the `lem-voice` tier (#2750)."""
import inspect
import pathlib
import re
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import ai_helper, content_framework
from cqc_lem.utilities.ai.sensitive_info import (
    REDACTED_CARD,
    REDACTED_SSN,
    contains_sensitive,
    luhn_valid,
    redact_messages,
    redact_sensitive,
)

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


class TestRedaction:
    @pytest.mark.parametrize("text", ["my ssn is 123-45-6789 ok", "123 45 6789", "123.45.6789"])
    def test_ssn_redacted(self, text):
        out = redact_sensitive(text)
        assert REDACTED_SSN in out and "6789" not in out

    @pytest.mark.parametrize("card", ["4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111",
                                      "378282246310005"])
    def test_luhn_valid_card_redacted(self, card):
        out = redact_sensitive(f"pay with {card} thanks")
        assert out == f"pay with {REDACTED_CARD} thanks"

    @pytest.mark.parametrize("text", [
        "call me at 415-555-0134", "+1 (415) 555-0134", "mail jo@example.com",
        "order 4111111111111112", "id 123456789", "000-12-3456", "666-12-3456", "900-12-3456",
        "version 2026-11-24", "I made 12,500,000 views",
    ])
    def test_false_positives_pass(self, text):
        assert redact_sensitive(text) == text
        assert not contains_sensitive(text)

    def test_empty_and_none_like(self):
        assert redact_sensitive("") == ""
        assert not contains_sensitive("")

    def test_luhn(self):
        assert luhn_valid("4111111111111111")
        assert not luhn_valid("4111111111111112")

    def test_messages_string_and_parts_and_untouched(self):
        msgs = [
            {"role": "user", "content": "ssn 123-45-6789"},
            {"role": "user", "content": [{"type": "text", "text": "card 4111111111111111"}, {"type": "image"}]},
            "odd",
        ]
        out = redact_messages(msgs)
        assert REDACTED_SSN in out[0]["content"]
        assert REDACTED_CARD in out[1]["content"][0]["text"]
        assert out[1]["content"][1] == {"type": "image"}
        assert out[2] == "odd"
        assert "123-45-6789" in msgs[0]["content"]  # input not mutated
        assert redact_messages(None) is None


class TestWiring:
    def test_call_llm_redacts_before_the_client(self):
        client = MagicMock()
        with patch.object(ai_helper, "client", client):
            ai_helper._call_llm(model="lem-voice", messages=[{"role": "user", "content": "ssn 123-45-6789"}])
        sent = client.chat.completions.create.call_args.kwargs["messages"]
        assert sent[0]["content"] == f"ssn {REDACTED_SSN}"

    def test_embedding_batch_redacted(self):
        client = MagicMock()
        client.embeddings.create.return_value = MagicMock(data=[])
        with patch("cqc_lem.utilities.ai.client.client", client):
            content_framework.embed_comments(["card 4111111111111111"])
        assert client.embeddings.create.call_args.kwargs["input"] == [f"card {REDACTED_CARD}"]

    def test_every_outbound_llm_path_goes_through_the_check(self):
        """Call-site enumeration: the one raw chat call in ai_helper is inside `_call_llm`.

        It redacts, and so does the embedding batcher.
        """
        src = inspect.getsource(ai_helper)
        raw = [m.start() for m in re.finditer(r"client\.chat\.completions\.create\(", src)]
        in_wrapper = inspect.getsource(ai_helper._call_llm)
        assert in_wrapper.count("client.chat.completions.create(") == len(raw) == 1
        assert "redact_messages" in in_wrapper
        assert "redact_sensitive" in inspect.getsource(content_framework.embed_comments)

    @pytest.mark.parametrize("fn", [
        ai_helper.generate_seed_comment, ai_helper.generate_second_wave_comment,
        ai_helper.generate_thread_reply, ai_helper.generate_comment_reply_followup,
        ai_helper.generate_lead_response, ai_helper.generate_nurture_dm,
    ])
    def test_writers_use_voice_tier(self, fn):
        src = inspect.getsource(fn)
        assert "VOICE_TIER" in src and '"lem-medium"' not in src
        assert ai_helper.VOICE_TIER == "lem-voice"


class TestVoiceTierConfig:
    CONFIG = (REPO_ROOT / ".litellm" / "config.yaml").read_text()

    def _deployments(self):
        blocks = re.split(r"\n  - model_name: ", self.CONFIG)[1:]
        rows = []
        for b in blocks:
            if b.startswith("lem-voice\n"):
                model = re.search(r"model: (\S+)", b).group(1)
                order = int(re.search(r"order: (\d+)", b).group(1))
                rows.append((order, model))
        return sorted(rows)

    def test_ordered_not_latency_raced(self):
        assert self._deployments() == [(1, "openai/gpt-5.4-mini"), (2, "openai/gpt-oss:120b")]

    def test_no_removed_deployment(self):
        for _, model in self._deployments():
            assert not re.search(r"gemma4|deepseek-v4-flash|minimax-m2|gpt-4o-mini", model)
