"""Guards for the Ollama Cloud roster in .litellm/config.yaml (issue #717).

The proxy config is not app code — nothing in this suite executes it, and a typo'd model tag shows
up in production as a 404 that latency-based routing happily keeps picking (that is exactly how the
retired ministral-3:8b kept winning the lem-simple race). These assertions are the only place the
roster is checked against the catalog we committed and against the tools that read it: the weekly
model-health cron (scripts/model_health_check.py) and the benchmark harness
(scripts/benchmark_models.py).
"""
import importlib.util
import json
import pathlib
import re
import sys

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG_TEXT = (REPO_ROOT / ".litellm" / "config.yaml").read_text()
SNAPSHOT = json.loads((REPO_ROOT / ".litellm" / "ollama_catalog_snapshot.json").read_text())
PRICES = json.loads((REPO_ROOT / ".litellm" / "model_prices_snapshot.json").read_text())["models"]


def _load(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))
    return module


mhc = _load("model_health_check")
DEPLOYMENTS = mhc.parse_deployments(mhc.load_config_text(CONFIG_TEXT))


def _ollama_models(group: str) -> list:
    return [d["bare"] for d in DEPLOYMENTS if d["group"] == group and d["is_ollama"]]


def _ollama_deployments() -> list:
    """Every Ollama Cloud deployment — LEM's serving tiers AND the agent-pipeline's `lem-agent-*`
    lane. The agent tiers were exempt until #844, when they carried a `:cloud` tag; ollama.com
    resolves that as a tag alias, so it never 404'd, which is exactly why nothing caught it."""
    return [d for d in DEPLOYMENTS if d["is_ollama"]]


class TestRoster:
    def test_deepseek_v4_flash_serves_both_the_medium_and_complex_tiers(self):
        assert "deepseek-v4-flash" in _ollama_models("lem-medium")
        assert "deepseek-v4-flash" in _ollama_models("lem-complex")

    def test_gemma4_serves_the_medium_tier(self):
        assert "gemma4:31b" in _ollama_models("lem-medium")

    def test_the_stale_minimax_family_is_gone_from_the_serving_tiers(self):
        """minimax-m2.7 was not retired, it was a version behind — following it to m3 would move
        this tier from a MEDIUM to a HIGH usage level, so the tier left the family instead."""
        for group in ("lem-simple", "lem-medium", "lem-complex", "lem-router"):
            assert not any(m.startswith("minimax-m2") for m in _ollama_models(group))

    def test_every_ollama_tag_exists_verbatim_in_the_committed_catalog(self):
        """A tag the catalog has never listed is a 404, and a 404 is the FASTEST answer in the
        group — latency routing then sends it MORE traffic (how retired ministral-3:8b kept winning
        lem-simple). The catalog snapshot is `ollama.com/api/tags`, i.e. exactly the ids the direct
        ollama.com/v1 API serves, so the comparison is verbatim: no `:cloud`/`-cloud` normalizing.
        `-cloud` is the local `ollama pull` form; `:cloud` is a tag alias the endpoint RESOLVES
        (probed on #844 — it answers 200, only a genuinely unknown tag 404s), which is worse than
        a typo: it serves, so only this assertion and the two below can see it."""
        catalog = SNAPSHOT["models"]
        for deployment in _ollama_deployments():
            bare = deployment["bare"]
            assert bare in catalog, f"{bare} ({deployment['group']}) is not in the catalog snapshot"

    def test_every_ollama_tag_is_priced_so_shadow_cost_never_silently_drops(self):
        """track_llm_call prices the SERVING model. estimate_shadow_cost_usd looks the id up
        EXACTLY (no substring fallback, unlike estimate_llm_cost_usd), so an Ollama deployment
        missing from the price snapshot reports no shadow cost at all — subscription traffic that
        the margin report cannot see. The `lem-agent-*` lane is covered for uniformity and for the
        promotion case (glm-5.2 / minimax-m3 are live candidates for lem-complex), NOT because it
        feeds this path: agent-pipeline traffic never reaches track_llm_call, its cost is LiteLLM's
        own `$ai_generation` callback (scripts/agent-pipeline/docs/agent-pipeline-routing.md)."""
        for deployment in _ollama_deployments():
            key = f"openai/{deployment['bare']}"
            spec = PRICES.get(key)
            assert spec, f"{key} ({deployment['group']}) has no model_prices_snapshot entry"
            assert spec.get("shadow_reference"), f"{key} has no shadow_reference"

    def test_every_serving_tier_keeps_more_than_one_ollama_deployment(self):
        """A single-deployment tier falls straight onto a paid OpenAI/Anthropic key — or onto
        nothing — the moment that one model has a bad day."""
        for group in ("lem-medium", "lem-complex"):
            assert len(_ollama_models(group)) >= 2

    def test_no_ollama_deployment_carries_a_tag_the_catalog_never_lists(self):
        """Companion to the catalog assertion above, for its ERROR MESSAGE. `glm-5.2:cloud` fails
        that one as "not in the catalog snapshot", which reads like a stale snapshot rather than the
        real fault: a tag ollama.com RESOLVES (probed on #844 — it answers 200, like `:latest`) but
        never lists. The `lem-agent-*` lane carried exactly that until #844.

        Matched by SHAPE, not by model name. scripts/weekly_model_check.sh rewrites `model:` lines
        in place when a configured id is retired — and since #844 that reaches the agent tiers too —
        so pinning the four current ids would fail CI on the cron's own correct swap."""
        for deployment in _ollama_deployments():
            bare = deployment["bare"]
            for tag in (":cloud", "-cloud", ":latest"):
                assert not bare.endswith(tag), (
                    f"{bare} ({deployment['group']}) carries a `{tag}` tag. The direct "
                    f"ollama.com/v1 API is keyed on the bare catalog id — write "
                    f"`{bare[:-len(tag)]}`.")

    def test_the_agent_lane_keeps_one_ollama_deployment_per_tier(self):
        """The `lem-agent-*` aliases back the agent-pipeline's Ollama lane (scripts/agent-pipeline),
        which pins a tier per issue and has no serving-tier redundancy to fall back on. Also the
        guard that keeps the assertions above from passing vacuously on an emptied group."""
        for group in ("lem-agent-tier1", "lem-agent-tier2",
                      "lem-agent-tier2-alt", "lem-agent-tier3"):
            assert len(_ollama_models(group)) == 1, f"{group} has no Ollama Cloud deployment"


class TestWeeklyModelCheck:
    def test_the_family_scan_has_nothing_left_to_file(self):
        """The cron re-files a family-upgrade issue every run while a configured model trails its
        own family. #717 exists because minimax-m2.7 did; after this roster nothing does."""
        assert mhc.plan_family_upgrades(DEPLOYMENTS, SNAPSHOT["models"]) == []

    def test_the_new_names_are_recognized_as_ollama_deployments(self):
        """The cron only probes deployments whose api_base points at Ollama Cloud — a new entry
        that missed the api_base line would never be checked for retirement at all."""
        new = [d for d in DEPLOYMENTS if d["bare"] in ("deepseek-v4-flash", "gemma4:31b")]
        assert new and all(d["is_ollama"] for d in new)

    def test_a_retirement_announcement_can_reach_the_new_names(self):
        """scan_retirements keys configured deployments by their bare id and matches the
        docs.ollama.com/cloud retirement table verbatim. An id spelled in a way that table never
        uses (a `:cloud` tag, say) makes that deployment invisible to the advance-retirement
        warning — the one thing this cron exists to give us. That is what the `lem-agent-*` lane
        was doing before #844."""
        announced = [{"model": name, "date": "2026-12-31", "alternative": None}
                     for name in sorted(SNAPSHOT["models"])]
        notices, _ = mhc.plan_retirement_notices(DEPLOYMENTS, announced, {}, today="2026-08-01")
        assert ({n["bare"] for n in notices}
                == {d["bare"] for d in _ollama_deployments()})


class TestBenchmarkChampions:
    def test_the_champions_are_still_the_incumbents(self):
        """scripts/benchmark_models.py reads the FIRST Ollama deployment of a tier as the champion
        a candidate must beat. Promoting a new model to the head of its group before the benchmark
        runs would make it its own baseline."""
        bm = _load("benchmark_models")
        champions = bm.champions_from_config(CONFIG_TEXT,
                                             ["lem-simple", "lem-medium", "lem-complex"])
        assert champions == {"lem-simple": "gpt-oss:20b", "lem-medium": "gpt-oss:120b",
                             "lem-complex": "qwen3.5:397b"}


class TestOllamaCompatLimits:
    def test_embeddings_never_move_to_ollama(self):
        """Ollama Cloud serves no embedding models — a well-meaning 'consistency' edit here breaks
        the comment similarity gate, feedback dedup and content-quality scoring at once."""
        assert _ollama_models("lem-embedding") == []

    def test_no_call_site_forces_a_tool_call(self):
        """Ollama's OpenAI-compat layer supports response_format but NOT forced tool_choice; a call
        site that sends it degrades silently on every Ollama deployment."""
        offenders = []
        for tree in ("src", "scripts"):
            for path in (REPO_ROOT / tree).rglob("*.py"):
                if re.search(r"\btool_choice\s*=", path.read_text(errors="ignore")):
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == []
