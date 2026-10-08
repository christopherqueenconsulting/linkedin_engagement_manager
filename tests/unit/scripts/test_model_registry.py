"""The generated model registry (issue #2251): scripts/model_registry.py.

The freshness test at the bottom is the guard that makes "never hand-edited" true: the committed
block in docs/model-benchmarks/README.md must equal what the generator renders from the committed
config, snapshots and leaderboards.
"""

import json
import pathlib
import sys

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import model_health_check as mhc  # noqa: E402
import model_registry as reg  # noqa: E402
import provider_model_scan as pms  # noqa: E402

CONFIG = """\
model_list:
  - model_name: lem-simple
    litellm_params:
      model: openai/gpt-oss:20b
      api_base: os.environ/OLLAMA_CLOUD_URL
  - model_name: lem-simple
    litellm_params:
      model: openai/gpt-4o-mini
  - model_name: lem-research
    litellm_params:
      model: perplexity/sonar
  - model_name: lem-image
    litellm_params:
      model: openai/gpt-image-2
  - model_name: lem-tts
    litellm_params:
      model: openai/tts-1
  - model_name: lem-embedding
    litellm_params:
      model: openai/text-embedding-3-small
  - model_name: lem-vision
    litellm_params:
      model: openai/gpt-9-mystery
"""

PRICES = {
    "openai/gpt-oss:20b": {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0,
                           "shadow_reference": "openai/gpt-4o-mini"},
    "openai/gpt-4o-mini": {"input_cost_per_token": 1.5e-07, "output_cost_per_token": 6e-07},
    "perplexity/sonar": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06},
    "openai/gpt-image-2": {"input_cost_per_token": 5e-06, "output_cost_per_image_token": 3e-05},
    "openai/tts-1": {"input_cost_per_character": 1.5e-05, "deprecation_date": "2027-01-06"},
    "openai/text-embedding-3-small": {"input_cost_per_token": 2e-08, "output_cost_per_token": 0.0},
}

PROVIDER = {"updated": "2026-10-07",
            "openai": {"source": "openai /v1/models", "models": ["gpt-4o-mini", "gpt-5.4-mini"]},
            "perplexity": {"models": []},
            "deprecations": [{"provider": "perplexity", "model": "sonar", "date": "2026-09-27",
                              "source": "curated"},
                             {"provider": "openai", "model": "tts-1", "date": "2027-01-06",
                              "source": pms.SOURCE_DEPRECATIONS_PAGE}]}

README = """# Bench

<!-- LEADERBOARD:BEGIN -->
| Date | Run | Tier | Model | Role | Contract | First draft | Judge | p50 | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-30 | `bm-b` | lem-simple | `gpt-oss:20b` | champion | 90% | 60% | 67% | 1 ms | baseline |
| 2026-08-02 | `bm-a` | lem-simple | `gpt-oss:20b` | champion | 50% | 50% | n/a | 1 ms | reject |
<!-- LEADERBOARD:END -->

<!-- MEDIA-LEADERBOARD:BEGIN -->
| Date | Run | Tier | Model | Role | Score | Detail | Cost | Verdict |
|---|---|---|---|---|---|---|---|---|
| 2026-10-07 | `mm-a` | lem-image | `gpt-image-2` | champion | 100% | mean 4.5/5 | $0.1 | baseline |
<!-- MEDIA-LEADERBOARD:END -->
"""


def _deployments():
    return mhc.parse_deployments(mhc.load_config_text(CONFIG))


def _rows():
    return {(r["tier"], r["model"]): r for r in reg.build_registry(
        _deployments(), prices=PRICES, catalog={"gpt-oss:20b": {}}, provider_snapshot=PROVIDER,
        readme_text=README)}


class TestCells:
    def test_price_text_per_kind(self):
        rows = _rows()
        assert rows[("lem-simple", "gpt-oss:20b")]["price"] == "plan-metered (shadow `gpt-4o-mini`)"
        assert rows[("lem-simple", "gpt-4o-mini")]["price"] == "$0.15 / $0.60 per 1M in/out"
        assert rows[("lem-image", "gpt-image-2")]["price"].startswith("≤$0.055/image")
        assert rows[("lem-tts", "tts-1")]["price"] == "$15.00 per 1M chars"
        assert rows[("lem-embedding", "text-embedding-3-small")]["price"] == "$0.02 per 1M in"
        assert rows[("lem-vision", "gpt-9-mystery")]["price"] == "unpriced"
        assert reg.price_text({"model": "openai/x", "bare": "x", "is_ollama": True}, {}) == \
            "plan-metered"
        assert reg.price_text({"model": "openai/x", "bare": "x", "is_ollama": False},
                              {"openai/x": {"output_cost_per_image_token": 1e-05}}) == "unpriced"

    def test_verdict_sunset_and_candidate_columns(self):
        rows = _rows()
        assert rows[("lem-simple", "gpt-oss:20b")]["last verdict"] == "baseline · 2026-08-30"
        assert rows[("lem-image", "gpt-image-2")]["last verdict"] == "baseline · 2026-10-07"
        assert rows[("lem-simple", "gpt-4o-mini")]["last verdict"] == "never measured"
        assert rows[("lem-research", "sonar")]["sunset"] == "**2026-09-27** (curated)"
        assert rows[("lem-tts", "tts-1")]["sunset"] == \
            f"**2027-01-06** ({pms.SOURCE_DEPRECATIONS_PAGE})"
        assert rows[("lem-simple", "gpt-oss:20b")]["sunset"] == "none published"
        assert rows[("lem-simple", "gpt-4o-mini")]["newer candidate?"] == "yes: `gpt-5.4-mini`"
        assert rows[("lem-research", "sonar")]["newer candidate?"] == "n/a (unversioned ids)"
        assert rows[("lem-simple", "gpt-oss:20b")]["newer candidate?"] == "no"
        assert rows[("lem-simple", "gpt-4o-mini")]["order"] == 2

    def test_unscanned_provider_says_so(self):
        rows = reg.build_registry(_deployments(), prices=PRICES, catalog={},
                                  provider_snapshot={}, readme_text="")
        by = {(r["tier"], r["model"]): r for r in rows}
        assert by[("lem-simple", "gpt-4o-mini")]["newer candidate?"] == "not scanned"
        assert by[("lem-simple", "gpt-oss:20b")]["newer candidate?"] == "not scanned"

    def test_last_verdicts_skip_headers_and_missing_blocks(self):
        assert reg.last_verdicts("no tables") == {}
        verdicts = reg.last_verdicts(README)
        assert verdicts[("lem-simple", "gpt-oss:20b")] == ("2026-08-30", "baseline")


class TestBlock:
    def test_render_and_replace(self):
        block = reg.render_registry(reg.build_registry(
            _deployments(), prices=PRICES, catalog={}, provider_snapshot=PROVIDER,
            readme_text=README), "note")
        assert block.splitlines()[2] == "| " + " | ".join(reg.REGISTRY_COLUMNS) + " |"
        inserted = reg.update_registry_block("# t\n\n## Leaderboard\nx\n", block)
        assert inserted.index("## Model registry") < inserted.index("## Leaderboard")
        replaced = reg.update_registry_block(inserted, "new")
        assert f"{reg.REGISTRY_BEGIN}\nnew\n{reg.REGISTRY_END}" in replaced
        assert "note" not in replaced
        appended = reg.update_registry_block("# only a title", "b")
        assert appended.endswith(f"{reg.REGISTRY_END}\n\n")

    def test_sources_note_names_the_pin(self):
        note = reg.sources_note({"_pinned": {"fetched": "2026-10-07", "source": "URL"}},
                                "2026-10-04", PROVIDER)
        assert "pinned 2026-10-07 (URL)" in note and "do not hand-edit" in note
        assert "OpenAI ids from openai /v1/models" in note


class TestPricePin:
    UPSTREAM = {"gpt-4o-mini": {"input_cost_per_token": 1.5e-07, "output_cost_per_token": 6e-07,
                                "mode": "chat", "max_tokens": 16384,
                                "input_cost_per_token_batches": 7.5e-08},
                "perplexity/sonar": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06,
                                     "mode": "chat"}}

    def test_refresh_copies_only_price_fields_and_records_the_source(self):
        doc = {"_comment": "c", "models": {"openai/gpt-oss:20b": {"shadow_reference": "x"}},
               "_pinned": {"models": ["openai/old"]}}
        out, missing = reg.refresh_price_snapshot(
            doc, self.UPSTREAM, [("openai", "gpt-4o-mini"), ("perplexity", "sonar"),
                                 ("openai", "gpt-nope")], fetched="2026-10-07", source="URL")
        assert missing == ["openai/gpt-nope"]
        assert out["models"]["openai/gpt-4o-mini"] == {
            "input_cost_per_token": 1.5e-07, "output_cost_per_token": 6e-07,
            "deprecation_date": None, "mode": "chat"}
        assert out["models"]["openai/gpt-oss:20b"] == {"shadow_reference": "x"}
        assert out["_pinned"] == {"source": "URL", "fetched": "2026-10-07",
                                  "models": ["openai/gpt-4o-mini", "openai/old",
                                             "perplexity/sonar"]}
        assert doc["models"].keys() == {"openai/gpt-oss:20b"}  # input not mutated

    def test_models_to_pin_adds_the_newest_candidate(self):
        assert reg.models_to_pin(_deployments(), PROVIDER) == [
            ("openai", "gpt-4o-mini"), ("openai", "gpt-5.4-mini"), ("openai", "gpt-9-mystery"),
            ("openai", "gpt-image-2"), ("openai", "text-embedding-3-small"), ("openai", "tts-1"),
            ("perplexity", "sonar")]


class TestCli:
    def _files(self, tmp_path):
        paths = {"config": tmp_path / "c.yaml", "prices": tmp_path / "p.json",
                 "catalog": tmp_path / "cat.json", "provider": tmp_path / "prov.json",
                 "readme": tmp_path / "README.md"}
        paths["config"].write_text(CONFIG)
        paths["prices"].write_text(json.dumps({"models": PRICES}))
        paths["catalog"].write_text(json.dumps({"updated": "2026-10-04", "models": {}}))
        paths["provider"].write_text(json.dumps(PROVIDER))
        paths["readme"].write_text(README)
        args = ["--config", str(paths["config"]), "--prices", str(paths["prices"]),
                "--catalog", str(paths["catalog"]), "--provider-snapshot", str(paths["provider"]),
                "--readme", str(paths["readme"])]
        return paths, args

    def test_check_write_check(self, tmp_path, capsys):
        paths, args = self._files(tmp_path)
        assert reg.main(["--check"] + args) == 1
        assert reg.main(["--write"] + args) == 0
        assert "registry updated" in capsys.readouterr().out
        assert reg.main(["--check"] + args) == 0
        assert reg.main(["--write"] + args) == 0
        assert "registry unchanged" in capsys.readouterr().out

    def test_dry_run_prints_table_and_spend_without_writing(self, tmp_path, capsys):
        paths, args = self._files(tmp_path)
        before = paths["readme"].read_text()
        assert reg.main(["--dry-run", "--max-spend-usd", "3"] + args) == 0
        out = capsys.readouterr().out
        assert "| lem-research | 1 | perplexity | `sonar` |" in out
        assert "Planned media-benchmark spend (cap $3.00" in out
        assert "UNPRICED" in out  # gpt-9-mystery has no pinned price
        assert paths["readme"].read_text() == before

    def test_print_only(self, tmp_path, capsys):
        _, args = self._files(tmp_path)
        assert reg.main(["--print"] + args) == 0
        assert "Planned" not in capsys.readouterr().out

    def test_refresh_prices_from_a_file(self, tmp_path, capsys):
        paths, args = self._files(tmp_path)
        upstream = tmp_path / "up.json"
        upstream.write_text(json.dumps(TestPricePin.UPSTREAM))
        assert reg.main(["--refresh-prices", "--upstream-file", str(upstream), "--today",
                         "2026-10-07"] + args) == 0
        doc = json.loads(paths["prices"].read_text())
        assert doc["_pinned"]["source"] == str(upstream)
        assert "is not in the LiteLLM cost map" in capsys.readouterr().err
        assert reg.main(["--refresh-prices", "--upstream-file", str(upstream), "--write"]
                        + args) == 0

    def test_refresh_prices_network_failure_leaves_the_file(self, tmp_path, monkeypatch):
        paths, args = self._files(tmp_path)
        before = paths["prices"].read_text()
        monkeypatch.setattr(pms, "_get_json", lambda *a, **k: None)
        assert reg.main(["--refresh-prices"] + args) == 1
        assert paths["prices"].read_text() == before


def test_the_committed_registry_is_fresh():
    """docs/model-benchmarks/README.md's registry block equals the generator's output.

    Failing? Run `poetry run python scripts/model_registry.py --write` and commit the README. The
    block is generated from .litellm/config.yaml, the pinned price / catalog / provider snapshots
    and the leaderboards - editing any of those without regenerating is what this catches.
    """
    inputs = reg.load_inputs(str(_ROOT / reg.DEFAULT_CONFIG), str(_ROOT / reg.DEFAULT_PRICES),
                             str(_ROOT / reg.DEFAULT_CATALOG), str(_ROOT / reg.DEFAULT_PROVIDER),
                             str(_ROOT / reg.DEFAULT_README))
    current = inputs["readme"]
    assert reg.REGISTRY_BEGIN in current
    assert reg.update_registry_block(current, reg.generate(inputs)) == current, (
        "model registry is stale - run: poetry run python scripts/model_registry.py --write")


def test_every_configured_provider_model_is_priced():
    """A configured OpenAI/Perplexity model with no pinned price makes every media run refuse."""
    inputs = reg.load_inputs(str(_ROOT / reg.DEFAULT_CONFIG), str(_ROOT / reg.DEFAULT_PRICES),
                             str(_ROOT / reg.DEFAULT_CATALOG), str(_ROOT / reg.DEFAULT_PROVIDER),
                             str(_ROOT / reg.DEFAULT_README))
    prices = inputs["prices_doc"]["models"]
    missing = [f"{p}/{m}" for p, m in pms.configured_provider_models(inputs["deployments"])
               if reg.price_text({"model": f"{p}/{m}", "bare": m, "is_ollama": False},
                                 prices) == "unpriced"]
    assert missing == [], f"pin with scripts/model_registry.py --refresh-prices: {missing}"


class TestResearchPresetsAndMeteredVerdicts:
    """#2255 presets and #2256 provider-qualified leaderboard rows."""

    _CONFIG = ("model_list:\n  - model_name: lem-research\n    litellm_params:\n"
               "      model: perplexity/preset/fast\n"
               "  - model_name: lem-complex\n    litellm_params:\n      model: openai/gpt-4o\n")
    _PRICES = {"perplexity/preset/fast": {"input_cost_per_token": 2e-07,
                                          "output_cost_per_token": 1.2e-06,
                                          "cost_per_request": 0.0025},
               "openai/gpt-4o": {"input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05}}
    _README = README.replace(
        "<!-- LEADERBOARD:END -->",
        "| 2026-10-08 | `bm-c` | lem-complex | `openai/gpt-4o` | champion | 100% | 90% | 80% | "
        "1 ms | baseline |\n<!-- LEADERBOARD:END -->")

    def _rows(self):
        deployments = mhc.parse_deployments(mhc.load_config_text(self._CONFIG))
        return {(r["tier"], r["model"]): r for r in reg.build_registry(
            deployments, prices=self._PRICES, catalog={}, provider_snapshot=PROVIDER,
            readme_text=self._README)}

    def test_a_preset_prices_its_per_call_search_fee(self):
        row = self._rows()[("lem-research", "preset/fast")]
        assert row["price"] == "$0.20 / $1.20 per 1M in/out + $0.0025/call"
        assert row["newer candidate?"] == "n/a (preset: Perplexity re-points it)"
        assert row["sunset"] == "none published"

    def test_a_metered_run_row_is_read_under_its_qualified_id(self):
        assert self._rows()[("lem-complex", "gpt-4o")]["last verdict"] == "baseline · 2026-10-08"

    def test_the_committed_snapshot_prices_both_research_presets(self):
        prices = json.loads((_ROOT / reg.DEFAULT_PRICES).read_text())["models"]
        for preset in ("perplexity/preset/fast", "perplexity/preset/low"):
            assert prices[preset]["cost_per_request"] > 0
        assert "perplexity/sonar" not in prices
