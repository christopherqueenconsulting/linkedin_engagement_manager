"""Unit tests for scripts/model_health_check.py — the pure planning/rewriting logic."""

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

# The tool lives under scripts/ (not an importable package) — load it by path.
_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "model_health_check.py"
_spec = importlib.util.spec_from_file_location("model_health_check", _PATH)
mhc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mhc)


def _cfg():
    return {"model_list": [
        {"model_name": "lem-simple", "litellm_params": {
            "model": "openai/gpt-oss:20b", "api_base": "os.environ/OLLAMA_CLOUD_URL"}},
        {"model_name": "lem-simple", "litellm_params": {
            "model": "openai/gpt-4o-mini", "api_key": "os.environ/OPENAI_API_KEY"}},  # not ollama
        {"model_name": "lem-medium", "litellm_params": {
            "model": "openai/minimax-m2.5", "api_base": "os.environ/OLLAMA_CLOUD_URL"}},
    ]}


class TestParse:
    def test_flags_ollama_vs_direct_and_strips_prefix(self):
        rows = mhc.parse_deployments(_cfg())
        by_model = {r["model"]: r for r in rows}
        assert by_model["openai/gpt-oss:20b"]["is_ollama"] is True
        assert by_model["openai/gpt-oss:20b"]["bare"] == "gpt-oss:20b"
        assert by_model["openai/gpt-4o-mini"]["is_ollama"] is False  # OpenAI, not Ollama Cloud

    def test_load_map(self):
        assert mhc.load_map({"upgrades": {"a": "b"}}) == {"a": "b"}
        assert mhc.load_map({}) == {}


class TestTextLoaders:
    def test_load_config_text_extracts_models_and_ollama_flag(self):
        text = (
            "model_list:\n"
            "  - model_name: lem-simple\n"
            "    litellm_params:\n"
            "      model: openai/gpt-oss:20b\n"
            "      api_base: os.environ/OLLAMA_CLOUD_URL\n"
            "  - model_name: lem-simple\n"
            "    litellm_params:\n"
            "      model: openai/gpt-4o-mini\n"
            "      api_key: os.environ/OPENAI_API_KEY\n"
            "router_settings:\n"
            "  routing_strategy: latency-based-routing\n"
        )
        rows = mhc.parse_deployments(mhc.load_config_text(text))
        assert len(rows) == 2
        assert rows[0]["bare"] == "gpt-oss:20b" and rows[0]["is_ollama"] is True
        assert rows[1]["bare"] == "gpt-4o-mini" and rows[1]["is_ollama"] is False

    def test_load_map_text_handles_colon_in_key(self):
        text = (
            "upgrades:\n"
            '  "minimax-m2.5": "minimax-m2.7"\n'
            '  "kimi-k2:1t": "REMOVE"   # comment\n'
            "other_section:\n"
            '  "ignored": "value"\n'
        )
        up = mhc.load_map(mhc.load_map_text(text))
        assert up == {"minimax-m2.5": "minimax-m2.7", "kimi-k2:1t": "REMOVE"}

    def test_real_repo_files_parse(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        cfg = mhc.load_config_text((root / ".litellm" / "config.yaml").read_text())
        rows = mhc.parse_deployments(cfg)
        assert any(r["group"] == "lem-simple" for r in rows)
        # No live ministral deployment remains (only the explanatory comment).
        assert not any(r["bare"] == "ministral-3:8b" for r in rows)
        up = mhc.load_map(mhc.load_map_text((root / ".litellm" / "model_upgrades.yaml").read_text()))
        assert up.get("minimax-m2.5") == "minimax-m2.7"
        assert up.get("kimi-k2.5") == "kimi-k2.6"


class TestSwapPrefix:
    def test_preserves_provider_prefix(self):
        assert mhc._swap_prefix("openai/minimax-m2.5", "minimax-m2.7") == "openai/minimax-m2.7"
        assert mhc._swap_prefix("bare-model", "other") == "other"


class TestPlanActions:
    def _rows(self):
        return mhc.parse_deployments(_cfg())

    def test_scheduled_swap_when_replacement_ok(self):
        # minimax-m2.5 is in the map (->m2.7); not 410 yet, but swapped proactively.
        actions = mhc.plan_actions(self._rows(), retired=set(),
                                   upgrades={"minimax-m2.5": "minimax-m2.7"},
                                   replacement_ok=lambda b: True)
        swaps = [a for a in actions if a["kind"] == "SWAP"]
        assert len(swaps) == 1
        assert swaps[0]["old"] == "openai/minimax-m2.5"
        assert swaps[0]["new"] == "openai/minimax-m2.7"
        assert swaps[0]["reason"] == "scheduled"

    def test_retired_swap_reason(self):
        actions = mhc.plan_actions(self._rows(), retired={"minimax-m2.5"},
                                   upgrades={"minimax-m2.5": "minimax-m2.7"},
                                   replacement_ok=lambda b: True)
        assert [a for a in actions if a["kind"] == "SWAP"][0]["reason"] == "retired"

    def test_alert_when_replacement_also_fails(self):
        actions = mhc.plan_actions(self._rows(), retired={"minimax-m2.5"},
                                   upgrades={"minimax-m2.5": "minimax-m2.7"},
                                   replacement_ok=lambda b: False)
        assert actions and actions[0]["kind"] == "ALERT"
        assert "failed to verify" in actions[0]["reason"]

    def test_retired_no_mapping_alerts(self):
        actions = mhc.plan_actions(self._rows(), retired={"gpt-oss:20b"},
                                   upgrades={}, replacement_ok=lambda b: True)
        assert any(a["kind"] == "ALERT" and "no mapping" in a["reason"] for a in actions)

    def test_retired_remove_alerts_not_autodeletes(self):
        actions = mhc.plan_actions(self._rows(), retired={"gpt-oss:20b"},
                                   upgrades={"gpt-oss:20b": "REMOVE"}, replacement_ok=lambda b: True)
        assert actions and actions[0]["kind"] == "ALERT" and "REMOVE" in actions[0]["reason"]

    def test_non_ollama_never_swapped(self):
        # gpt-4o-mini is OpenAI-direct; even if it were mapped, it must not be touched.
        actions = mhc.plan_actions(self._rows(), retired={"gpt-4o-mini"},
                                   upgrades={"gpt-4o-mini": "x"}, replacement_ok=lambda b: True)
        assert actions == []

    def test_healthy_model_no_action(self):
        actions = mhc.plan_actions(self._rows(), retired=set(), upgrades={},
                                   replacement_ok=lambda b: True)
        assert actions == []


class TestApplySwaps:
    def test_line_swap_preserves_comments(self):
        text = (
            "model_list:\n"
            "  # keep this comment\n"
            "  - model_name: lem-medium\n"
            "    litellm_params:\n"
            "      model: openai/minimax-m2.5\n"
            "      api_base: os.environ/OLLAMA_CLOUD_URL\n"
        )
        swaps = [{"old": "openai/minimax-m2.5", "new": "openai/minimax-m2.7"}]
        new, n = mhc.apply_swaps(text, swaps)
        assert n == 1
        assert "model: openai/minimax-m2.7" in new
        assert "openai/minimax-m2.5" not in new
        assert "# keep this comment" in new  # untouched

    def test_no_partial_or_substring_match(self):
        # A model whose name is a prefix of another must not be swapped by the shorter key.
        text = "      model: openai/minimax-m2.75\n"
        new, n = mhc.apply_swaps(text, [{"old": "openai/minimax-m2.7", "new": "openai/x"}])
        assert n == 0 and new == text

    def test_swaps_all_occurrences(self):
        text = ("      model: openai/gpt-oss:20b\n"
                "      model: openai/gpt-oss:20b\n")
        new, n = mhc.apply_swaps(text, [{"old": "openai/gpt-oss:20b", "new": "openai/gpt-oss:21b"}])
        assert n == 2 and "20b" not in new
