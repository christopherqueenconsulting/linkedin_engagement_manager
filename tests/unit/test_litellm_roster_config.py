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


class TestRoster:
    def test_deepseek_v4_flash_serves_both_the_medium_and_complex_tiers(self):
        assert "deepseek-v4-flash:cloud" in _ollama_models("lem-medium")
        assert "deepseek-v4-flash:cloud" in _ollama_models("lem-complex")

    def test_gemma4_serves_the_medium_tier(self):
        assert "gemma4:31b" in _ollama_models("lem-medium")

    def test_the_stale_minimax_family_is_gone_from_the_serving_tiers(self):
        """minimax-m2.7 was not retired, it was a version behind — following it to m3 would move
        this tier from a MEDIUM to a HIGH usage level, so the tier left the family instead."""
        for group in ("lem-simple", "lem-medium", "lem-complex", "lem-router"):
            assert not any(m.startswith("minimax-m2") for m in _ollama_models(group))

    def test_every_ollama_tag_exists_in_the_committed_catalog(self):
        """A tag the catalog has never listed is a 404 the router treats as a fast deployment.
        Cloud-only models are configured with a `:cloud` tag; the catalog lists them bare."""
        catalog = SNAPSHOT["models"]
        for deployment in DEPLOYMENTS:
            if not deployment["is_ollama"]:
                continue
            bare = deployment["bare"]
            name = bare[: -len(":cloud")] if bare.endswith(":cloud") else bare
            assert name in catalog, f"{bare} ({deployment['group']}) is not in the catalog snapshot"

    def test_every_serving_tier_keeps_more_than_one_ollama_deployment(self):
        """A single-deployment tier falls straight onto a paid OpenAI/Anthropic key — or onto
        nothing — the moment that one model has a bad day."""
        for group in ("lem-medium", "lem-complex"):
            assert len(_ollama_models(group)) >= 2


class TestWeeklyModelCheck:
    def test_the_family_scan_has_nothing_left_to_file(self):
        """The cron re-files a family-upgrade issue every run while a configured model trails its
        own family. #717 exists because minimax-m2.7 did; after this roster nothing does."""
        assert mhc.plan_family_upgrades(DEPLOYMENTS, SNAPSHOT["models"]) == []

    def test_the_new_names_are_recognized_as_ollama_deployments(self):
        """The cron only probes deployments whose api_base points at Ollama Cloud — a new entry
        that missed the api_base line would never be checked for retirement at all."""
        new = [d for d in DEPLOYMENTS if d["bare"] in ("deepseek-v4-flash:cloud", "gemma4:31b")]
        assert new and all(d["is_ollama"] for d in new)


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
