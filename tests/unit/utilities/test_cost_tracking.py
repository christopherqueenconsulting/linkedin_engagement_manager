"""Unit tests for media-cost tracking and the daily LLM cost rollup (issue #490)."""

from datetime import date

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.observability"
_REDIS = "cqc_lem.utilities.linkedin.rate_limit._redis_client"


class TestImageCostUsd:
    def test_defaults_to_list_price(self, monkeypatch):
        monkeypatch.delenv("IMAGE_COST_PER_IMAGE", raising=False)
        from cqc_lem.utilities.observability import image_cost_usd
        assert image_cost_usd(1) == 0.08
        assert image_cost_usd(3) == pytest.approx(0.24)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("IMAGE_COST_PER_IMAGE", "0.12")
        from cqc_lem.utilities.observability import image_cost_usd
        assert image_cost_usd(2) == pytest.approx(0.24)

    def test_malformed_rate_falls_back(self, monkeypatch):
        monkeypatch.setenv("IMAGE_COST_PER_IMAGE", "free")
        from cqc_lem.utilities.observability import image_cost_usd
        assert image_cost_usd(1) == 0.08


class TestTrackMediaCost:
    def test_emits_event_and_ledger_row(self):
        with patch(f"{_MOD}.posthog") as mock_ph, patch(f"{_MOD}._write_cost_ledger") as ledger:
            from cqc_lem.utilities.observability import track_media_cost
            track_media_cost("video", "runway", 0.25, user_id=3, post_id=9, qty=5,
                             model="gen4_turbo", meta={"ratio": "960:960"})

        props = mock_ph.capture.call_args[1]["properties"]
        assert mock_ph.capture.call_args[1]["event"] == "media_cost"
        assert props["kind"] == "video" and props["cost_usd"] == 0.25 and props["qty"] == 5
        assert props["ratio"] == "960:960"

        row = ledger.call_args[1]
        assert row["category"] == "media" and row["provider"] == "runway"
        assert row["user_id"] == 3 and row["post_id"] == 9 and row["model_tier"] == "gen4_turbo"
        assert row["feature"] == "content"

    def test_falls_back_to_attribution_scope(self):
        from cqc_lem.utilities.observability import llm_attribution, track_media_cost, FEATURE_NEWSLETTER
        with patch(f"{_MOD}.posthog"), patch(f"{_MOD}._write_cost_ledger") as ledger:
            with llm_attribution(user_id=12, feature=FEATURE_NEWSLETTER):
                track_media_cost("image", "openai", 0.08, qty=1, model="dall-e-3", feature=None)

        row = ledger.call_args[1]
        assert row["user_id"] == 12 and row["feature"] == "newsletter"

    def test_ledger_failure_never_propagates(self):
        with patch(f"{_MOD}.posthog"), \
             patch("cqc_lem.utilities.db.insert_cost_ledger_entry", side_effect=RuntimeError("db down")):
            from cqc_lem.utilities.observability import track_media_cost
            track_media_cost("image", "openai", 0.08)  # must not raise


class TestDallEImageCost:
    def test_generated_image_is_billed_per_image(self):
        from types import SimpleNamespace
        response = SimpleNamespace(data=[SimpleNamespace(url="https://img/1.png")])
        with patch("cqc_lem.utilities.ai.client.client.images.generate", return_value=response), \
             patch(f"{_MOD}.track_media_cost") as track:
            from cqc_lem.utilities.ai.ai_helper import generate_dall_e_image_from_prompt
            assert generate_dall_e_image_from_prompt("a robot") == response.data

        args, kwargs = track.call_args
        assert args[0] == "image" and args[1] == "openai" and args[2] == pytest.approx(0.08)
        assert kwargs["qty"] == 1 and kwargs["model"] == "dall-e-3"


class TestLlmRollupAccrual:
    def test_track_llm_call_accrues_to_todays_bucket(self):
        client = MagicMock()
        with patch(f"{_MOD}.posthog"), patch(_REDIS, return_value=client):
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-complex", prompt_tokens=1000, completion_tokens=500,
                           latency_ms=10, user_id=4, feature="content")

        key, field, usd = client.hincrbyfloat.call_args_list[0][0]
        assert key.startswith("lem:cost:llm:")
        assert field == "4|content|lem-complex"
        assert usd == pytest.approx(0.003 + 0.0075)
        # tokens accrue to the parallel qty bucket
        qty_call = client.hincrbyfloat.call_args_list[1][0]
        assert qty_call[0].endswith(":qty") and qty_call[2] == 1500

    def test_cache_hit_costs_nothing_and_is_not_accrued(self):
        client = MagicMock()
        with patch(f"{_MOD}.posthog"), patch(_REDIS, return_value=client):
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-complex", prompt_tokens=1000, completion_tokens=500,
                           latency_ms=10, cached=True)
        client.hincrbyfloat.assert_not_called()

    def test_no_redis_is_a_noop(self):
        with patch(f"{_MOD}.posthog"), patch(_REDIS, return_value=None):
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-simple", prompt_tokens=10, completion_tokens=10, latency_ms=5)

    def test_redis_error_does_not_break_the_call(self):
        client = MagicMock()
        client.hincrbyfloat.side_effect = RuntimeError("redis gone")
        with patch(f"{_MOD}.posthog"), patch(_REDIS, return_value=client):
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-simple", prompt_tokens=10, completion_tokens=10, latency_ms=5)


class TestFlushLlmCostRollup:
    def _client(self, keys, costs, quantities):
        client = MagicMock()
        client.scan_iter.return_value = iter(keys)
        client.hgetall.side_effect = lambda k: costs if not k.endswith(":qty") else quantities
        return client

    def test_writes_one_row_per_bucket_field_and_clears_the_key(self):
        client = self._client(
            [b"lem:cost:llm:2026-07-24", b"lem:cost:llm:2026-07-24:qty"],
            {b"4|content|lem-complex": b"0.0105", b"|system|lem-simple": b"0.0002"},
            {b"4|content|lem-complex": b"1500"},
        )
        with patch(_REDIS, return_value=client), patch(f"{_MOD}._write_cost_ledger") as ledger:
            from cqc_lem.utilities.observability import flush_llm_cost_rollup
            assert flush_llm_cost_rollup(today="2026-07-25") == 2

        rows = {c[1]["model_tier"]: c[1] for c in ledger.call_args_list}
        assert rows["lem-complex"]["user_id"] == 4
        assert rows["lem-complex"]["feature"] == "content"
        assert rows["lem-complex"]["qty"] == 1500
        assert rows["lem-complex"]["incurred_on"] == date(2026, 7, 24)
        assert rows["lem-complex"]["category"] == "llm"
        # a system-wide call has no user and no token bucket entry
        assert rows["lem-simple"]["user_id"] is None and rows["lem-simple"]["qty"] is None
        client.delete.assert_called_once_with("lem:cost:llm:2026-07-24", "lem:cost:llm:2026-07-24:qty")

    def test_todays_bucket_is_left_alone(self):
        client = self._client([b"lem:cost:llm:2026-07-25"], {b"1|dm|lem-medium": b"0.01"}, {})
        with patch(_REDIS, return_value=client), patch(f"{_MOD}._write_cost_ledger") as ledger:
            from cqc_lem.utilities.observability import flush_llm_cost_rollup
            assert flush_llm_cost_rollup(today="2026-07-25") == 0
        ledger.assert_not_called()
        client.delete.assert_not_called()

    def test_ignores_malformed_keys_and_values(self):
        client = self._client([b"lem:cost:llm:not-a-date", b"lem:cost:llm:2026-07-01"],
                              {b"1|dm|lem-medium": b"NaN-ish"}, {})
        with patch(_REDIS, return_value=client), patch(f"{_MOD}._write_cost_ledger") as ledger:
            from cqc_lem.utilities.observability import flush_llm_cost_rollup
            assert flush_llm_cost_rollup(today="2026-07-25") == 0
        ledger.assert_not_called()

    def test_no_redis_returns_zero(self):
        with patch(_REDIS, return_value=None):
            from cqc_lem.utilities.observability import flush_llm_cost_rollup
            assert flush_llm_cost_rollup() == 0

    def test_scan_error_returns_zero(self):
        client = MagicMock()
        client.scan_iter.side_effect = RuntimeError("redis gone")
        with patch(_REDIS, return_value=client):
            from cqc_lem.utilities.observability import flush_llm_cost_rollup
            assert flush_llm_cost_rollup() == 0
