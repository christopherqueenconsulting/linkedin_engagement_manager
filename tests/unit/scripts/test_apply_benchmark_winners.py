"""Benchmark winners -> a reviewable config diff (issue #2256): scripts/apply_benchmark_winners.py.

Nothing here touches the committed config: every run reads and writes copies under tmp_path.
"""

import json
import pathlib
import shutil
import sys

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import apply_benchmark_winners as apply  # noqa: E402
import model_health_check as mhc  # noqa: E402

CONFIG = """\
model_list:
  - model_name: lem-simple
    litellm_params:
      model: openai/gpt-oss:20b
      api_base: os.environ/OLLAMA_CLOUD_URL
      api_key: os.environ/OLLAMA_CLOUD_API_KEY
      request_timeout: 40
      rpm: 100

  # OpenAI fallback
  - model_name: lem-simple
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY

  # OpenAI fallback
  - model_name: lem-complex
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
      request_timeout: 120

  - model_name: lem-vision
    litellm_params:
      model: openai/gpt-4.1
      api_key: os.environ/OPENAI_API_KEY
      order: 1

  - model_name: lem-vision
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
      order: 2

router_settings:
  routing_strategy: latency-based-routing
"""


def _rec(tier, model, champion, decision="adopt"):
    return {"tier": tier, "model": model, "champion": champion,
            "quota_policy": {"decision": decision}}


def _promote(rec, kind="text", text=CONFIG):
    return apply.promote(text, apply.load_recommendations([rec])["recommendations"][0]
                         if kind == "text" else rec, kind, run_id="bm-x", date="2026-10-08")


def _groups(text):
    return [(r["group"], r["model"]) for r in mhc.parse_deployments(mhc.load_config_text(text))]


class TestLoad:
    def test_every_benchmark_output_shape(self):
        text = apply.load_recommendations({"run_id": "bm-1", "date": "d", "recommendations": [
            _rec("lem-complex", "openai/gpt-6.1-sol", "openai/gpt-4o")]})
        assert text["kind"] == "text" and text["run_id"] == "bm-1"
        assert text["recommendations"][0]["policy"] == "adopt"
        media = apply.load_recommendations({"kind": "media", "run_id": "mm-1", "gates": [
            {"tier": "lem-vision", "model": "gpt-6.1-sol", "champion": "gpt-4.1",
             "verdict": "recommend"},
            {"tier": "lem-vision", "model": "gpt-4o-mini", "champion": "gpt-4.1",
             "verdict": "reject"}]})
        assert [r["model"] for r in media["recommendations"]] == ["gpt-6.1-sol"]
        assert media["recommendations"][0]["policy"] == "adopt"
        bare = apply.load_recommendations([{"tier": "t", "model": "m"}, {"model": "no tier"}, 3])
        assert bare["recommendations"] == [{"tier": "t", "model": "m", "champion": "",
                                            "policy": "hold"}]
        with pytest.raises(ValueError):
            apply.load_recommendations("nope")


class TestPromote:
    def test_a_lone_champion_becomes_the_ordered_fallback(self):
        text, note, refusal = _promote(_rec("lem-complex", "openai/gpt-6.1-sol", "openai/gpt-4o"))
        assert refusal is None and "takes order 1" in note
        block = text.split("  - model_name: lem-complex", 1)[1]
        assert block.index("openai/gpt-6.1-sol") < block.index("openai/gpt-4o")
        assert "      request_timeout: 120\n      order: 1\n" in text  # carried, then ordered
        assert "      model: openai/gpt-4o\n      api_key: os.environ/OPENAI_API_KEY\n" \
               "      request_timeout: 120\n      order: 2\n" in text
        # The champion's own comment stays attached to the champion.
        assert "      order: 1\n\n  # OpenAI fallback\n  - model_name: lem-complex" in text
        assert "earned `recommend` over `openai/gpt-4o` on lem-complex" in text
        assert _groups(text).count(("lem-complex", "openai/gpt-6.1-sol")) == 1

    def test_an_ordered_group_shifts_down_from_the_champion(self):
        rec = {"tier": "lem-vision", "model": "gpt-6.1-sol", "champion": "gpt-4.1",
               "policy": "adopt"}
        text, _, refusal = _promote(rec, kind="media")
        assert refusal is None
        vision = [b for b in apply.parse_blocks(text) if b["tier"] == "lem-vision"]
        assert [(apply._model(b), apply._order(b)) for b in vision] == [
            ("openai/gpt-6.1-sol", 1), ("openai/gpt-4.1", 2), ("openai/gpt-4o-mini", 3)]

    def test_a_latency_group_gets_a_peer_and_no_order(self):
        text, note, refusal = _promote(_rec("lem-simple", "openai/gpt-5.4-mini",
                                            "openai/gpt-4o-mini"))
        assert refusal is None and "latency peer" in note
        simple = [b for b in apply.parse_blocks(text) if b["tier"] == "lem-simple"]
        assert [apply._model(b) for b in simple] == [
            "openai/gpt-oss:20b", "openai/gpt-5.4-mini", "openai/gpt-4o-mini"]
        assert all(apply._order(b) is None for b in simple)

    def test_an_ollama_candidate_carries_the_ollama_transport(self):
        text, _, refusal = _promote(_rec("lem-simple", "glm-5.3", "gpt-oss:20b"))
        assert refusal is None
        new = next(b for b in apply.parse_blocks(text) if apply._model(b) == "openai/glm-5.3")
        assert apply._is_ollama(new)
        assert new["params"]["rpm"][1] == "100" and new["params"]["request_timeout"][1] == "40"

    @pytest.mark.parametrize("rec,reason", [
        (_rec("lem-complex", "anthropic/claude-sonnet-5", "openai/gpt-4o"),
         "no first-party production route"),
        (_rec("lem-complex", "openai/gpt-6.1-sol", "openai/gpt-3"), "no longer deployed"),
        (_rec("lem-simple", "openai/gpt-4o-mini", "gpt-oss:20b"), "already deployed"),
        (_rec("lem-simple", "openai/gpt-5.4-mini", ""), "no champion named"),
        # A qualified champion never matches an Ollama deployment's `openai/` transport prefix.
        (_rec("lem-simple", "openai/gpt-5.4-mini", "openai/gpt-oss:20b"), "no longer deployed"),
    ])
    def test_refusals_leave_the_text_untouched(self, rec, reason):
        text, note, refusal = _promote(rec)
        assert text == CONFIG and note is None and reason in refusal

    def test_parse_blocks_stops_at_the_router_settings(self):
        blocks = apply.parse_blocks(CONFIG)
        assert [b["tier"] for b in blocks] == ["lem-simple", "lem-simple", "lem-complex",
                                               "lem-vision", "lem-vision"]
        assert "routing_strategy" not in blocks[-1]["params"]


class TestCli:
    @pytest.fixture
    def repo(self, tmp_path):
        """Copies of the committed inputs, so --write can never touch the real tree."""
        for rel in (".litellm/config.yaml", ".litellm/model_prices_snapshot.json",
                    ".litellm/ollama_catalog_snapshot.json",
                    ".litellm/provider_models_snapshot.json", "docs/model-benchmarks/README.md"):
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(_ROOT / rel, dest)
        return tmp_path

    def _args(self, repo, results, *extra):
        return [str(results), "--config", str(repo / ".litellm/config.yaml"),
                "--readme", str(repo / "docs/model-benchmarks/README.md"),
                "--prices", str(repo / ".litellm/model_prices_snapshot.json"),
                "--catalog", str(repo / ".litellm/ollama_catalog_snapshot.json"),
                "--provider-snapshot", str(repo / ".litellm/provider_models_snapshot.json"),
                *extra]

    def _results(self, repo, recs):
        path = repo / "results.json"
        path.write_text(json.dumps({"run_id": "bm-20261008-abc", "date": "2026-10-08",
                                    "recommendations": recs}))
        return path

    def test_prints_a_diff_and_writes_nothing_without_write(self, repo, capsys):
        before = (repo / ".litellm/config.yaml").read_text()
        results = self._results(repo, [
            _rec("lem-complex", "openai/gpt-6.1-sol", "openai/gpt-4o"),
            _rec("lem-medium", "openai/gpt-5.4-mini", "openai/gpt-4o-mini", "hold")])
        rc = apply.main(self._args(repo, results))
        out = capsys.readouterr().out
        assert rc == 2
        assert "+      model: openai/gpt-6.1-sol" in out
        assert "+| lem-complex | 1 | openai | `gpt-6.1-sol` |" in out  # regenerated registry
        assert "SKIP [lem-medium]" in out and "`hold`" in out
        assert (repo / ".litellm/config.yaml").read_text() == before

    def test_write_updates_the_config_and_registry(self, repo, capsys):
        results = self._results(repo, [
            _rec("lem-medium", "openai/gpt-5.4-mini", "openai/gpt-4o-mini", "hold")])
        rc = apply.main(self._args(repo, results, "--write", "--include-held"))
        assert rc == 2
        config = (repo / ".litellm/config.yaml").read_text()
        assert ("lem-medium", "openai/gpt-5.4-mini") in _groups(config)
        readme = (repo / "docs/model-benchmarks/README.md").read_text()
        assert "| lem-medium | 3 | openai | `gpt-5.4-mini` |" in readme

    def test_nothing_to_apply(self, repo, capsys):
        empty = self._results(repo, [])
        assert apply.main(self._args(repo, empty)) == 0
        held = self._results(repo, [_rec("lem-medium", "openai/gpt-5.4-mini",
                                         "openai/gpt-4o-mini", "hold")])
        assert apply.main(self._args(repo, held)) == 0
        assert "nothing to apply" in capsys.readouterr().out

    def test_an_unreadable_input_exits_1(self, repo, capsys):
        bad = repo / "bad.json"
        bad.write_text("{not json")
        assert apply.main(self._args(repo, bad)) == 1
        assert apply.main(self._args(repo, repo / "missing.json")) == 1
