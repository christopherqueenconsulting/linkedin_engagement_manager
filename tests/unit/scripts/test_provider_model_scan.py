"""OpenAI + Perplexity provider scan (issue #2251).

Covers scripts/provider_model_scan.py and its `model_health_check.py --provider-*` CLI. Every fetch
is mocked or read from a fixture dir.
"""

import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = pathlib.Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import model_health_check as mhc  # noqa: E402
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
  - model_name: lem-complex
    litellm_params:
      model: openai/gpt-4o
  - model_name: lem-research
    litellm_params:
      model: perplexity/sonar
  - model_name: lem-image
    litellm_params:
      model: openai/gpt-image-2
  - model_name: lem-image
    litellm_params:
      model: openai/gpt-image-1
  - model_name: lem-vision
    litellm_params:
      model: openai/gpt-4o-mini
"""

DEPRECATIONS_HTML = """
<table><thead><tr><th>Shutdown date</th><th>Model snapshot</th><th>Substitute</th></tr></thead>
<tbody>
<tr><td>October 23, 2026</td><td><code>gpt-4.1-nano</code> | <code>gpt-4.1-nano-2025-04-14</code></td>
<td><code>gpt-5.6-luna</code></td></tr>
<tr><td>October 23, 2026</td><td><code>gpt-image-1</code></td>
<td><code>gpt-image-2.5-sunburst</code> or <code>gpt-image-2.5-flare</code></td></tr>
<tr><td>Jan 6, 2027</td><td><code>tts-1</code></td><td><code>gpt-realtime-2.1-mini</code></td></tr>
<tr><td>2024-10-28</td><td>New fine-tuning training on <code>babbage-002</code></td>
<td><code>gpt-4o-mini</code></td></tr>
<tr><td>not a date</td><td><code>gpt-4</code></td><td><code>gpt-5</code></td></tr>
<tr><td>Smarch 1, 2026</td><td><code>gpt-4</code></td><td><code>gpt-5</code></td></tr>
<tr><td>only two</td><td>cells</td></tr>
</tbody></table>
"""

OPENAI_IDS = ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-5-mini", "gpt-5.4-mini",
              "gpt-5-pro", "gpt-5.6-sol", "gpt-4o-2024-08-06", "gpt-image-1", "gpt-image-2",
              "gpt-image-2.5-flare", "gpt-image-1-mini", "gpt-4.1-nano", "text-embedding-3-small"]


def _deployments(text=CONFIG):
    return mhc.parse_deployments(mhc.load_config_text(text))


class TestOpenAIFamily:
    @pytest.mark.parametrize("model,family,version,variant", [
        ("gpt-4o", "gpt", (4, 0, 1), ""),
        ("openai/gpt-4o-mini", "gpt", (4, 0, 1), "mini"),
        ("gpt-4.1", "gpt", (4, 1, 0), ""),
        ("gpt-5.6-sol", "gpt", (5, 6, 0), "sol"),
        ("gpt-image-2.5-flare", "gpt-image", (2, 5), "flare"),
        ("gpt-image-1", "gpt-image", (1, 0), ""),
        ("text-embedding-3-small", "text-embedding", (3,), "small"),
        ("tts-1-hd", "tts", (1,), "hd"),
    ])
    def test_parses_the_families_compared(self, model, family, version, variant):
        assert pms.openai_family(model) == {"family": family, "version": version,
                                            "variant": variant}

    @pytest.mark.parametrize("model", ["gpt-4o-2024-08-06", "gpt-5-pro", "gpt-5.4-cyber",
                                       "gpt-image-1-pro", "whisper-1", "", None, "gpt-4o-0613"])
    def test_snapshots_excluded_variants_and_unknowns_are_none(self, model):
        assert pms.openai_family(model) is None

    def test_4o_sits_between_4_and_4_1(self):
        assert (pms.openai_family("gpt-4")["version"] < pms.openai_family("gpt-4o")["version"]
                < pms.openai_family("gpt-4.1")["version"])

    def test_a_mini_is_never_a_successor_to_a_full_model(self):
        assert not pms.is_family_successor(pms.openai_family("gpt-4o"),
                                           pms.openai_family("gpt-5-mini"))

    def test_a_codename_variant_succeeds_only_a_base_model(self):
        assert pms.is_family_successor(pms.openai_family("gpt-image-2"),
                                       pms.openai_family("gpt-image-2.5-flare"))
        assert not pms.is_family_successor(pms.openai_family("gpt-4o-mini"),
                                           pms.openai_family("gpt-5.6-sol"))

    def test_not_a_successor_across_families_or_backwards(self):
        assert not pms.is_family_successor(pms.openai_family("gpt-4o"),
                                           pms.openai_family("gpt-image-2"))
        assert not pms.is_family_successor(pms.openai_family("gpt-4.1"),
                                           pms.openai_family("gpt-4o"))
        assert not pms.is_family_successor(None, pms.openai_family("gpt-4o"))
        assert not pms.is_family_successor(pms.openai_family("text-embedding-3-small"),
                                           pms.openai_family("text-embedding-3-large"))


class TestPlans:
    def test_configured_models_exclude_ollama_transport(self):
        configured = pms.configured_provider_models(_deployments())
        assert ("openai", "gpt-oss:20b") not in configured
        assert configured[("openai", "gpt-4o-mini")] == ["lem-simple", "lem-vision"]
        assert ("perplexity", "sonar") in configured

    def test_provider_of_defaults_to_openai(self):
        assert pms.provider_of("gpt-4o") == "openai"
        assert pms.provider_of("perplexity/sonar") == "perplexity"

    def test_upgrades_pick_the_newest_same_variant_and_skip_sunsetting(self):
        deprecations = [{"provider": "openai", "model": "gpt-5.4-mini", "date": "2027-01-01"}]
        ups = {u["current"]: u for u in pms.plan_provider_upgrades(
            _deployments(), {"openai": OPENAI_IDS}, deprecations)}
        assert ups["gpt-4o-mini"]["candidate"] == "gpt-5-mini"
        assert ups["gpt-4o-mini"]["groups"] == ["lem-simple", "lem-vision"]
        assert ups["gpt-4o"]["candidate"] == "gpt-5.6-sol"
        assert "gpt-5-pro" not in ups["gpt-4o"]["candidates"]
        assert ups["gpt-image-2"]["candidate"] == "gpt-image-2.5-flare"
        assert "sonar" not in ups

    def test_no_upgrades_without_a_provider_list(self):
        assert pms.plan_provider_upgrades(_deployments(), {"openai": None}) == []

    def test_sunsets_merge_sources_on_the_same_date(self):
        rows = [{"provider": "openai", "model": "gpt-image-1", "date": "2026-10-23",
                 "replacement": [], "source": pms.SOURCE_LITELLM_MAP},
                {"provider": "openai", "model": "gpt-image-1", "date": "2026-10-23",
                 "replacement": ["gpt-image-2.5-flare"], "source": pms.SOURCE_DEPRECATIONS_PAGE,
                 "url": "u"},
                {"provider": "openai", "model": "gpt-image-1", "date": "2026-12-01",
                 "replacement": ["x"], "source": "later"},
                {"provider": "openai", "model": "gpt-4o", "date": None}]
        sunsets = pms.plan_provider_sunsets(_deployments(), rows, "2026-10-07")
        assert len(sunsets) == 1
        s = sunsets[0]
        assert (s["date"], s["days_until"], s["groups"]) == ("2026-10-23", 16, ["lem-image"])
        assert s["replacement"] == ["gpt-image-2.5-flare"]
        assert s["source"] == pms.SOURCE_DEPRECATIONS_PAGE and s["url"] == "u"

    def test_a_passed_sunset_counts_down_negative(self):
        rows = [dict(n, url=n["source"], source=pms.SOURCE_CURATED) for n in pms.PERPLEXITY_NOTICES]
        sunsets = pms.plan_provider_sunsets(_deployments(), rows, "2026-10-07")
        assert sunsets[0]["model"] == "sonar" and sunsets[0]["days_until"] == -10
        assert sunsets[0]["url"].startswith("https://docs.perplexity.ai")

    def test_bad_dates_count_down_as_none(self):
        assert pms._days_until("soon", "2026-10-07") is None

    def test_vanished_needs_an_authoritative_nonempty_list(self):
        listed = {"openai": ["gpt-4o", "gpt-image-2"], "perplexity": []}
        assert pms.plan_provider_vanished(_deployments(), listed, {"openai": False}) == []
        gone = pms.plan_provider_vanished(_deployments(), listed,
                                          {"openai": True, "perplexity": True})
        assert [g["model"] for g in gone] == ["gpt-4o-mini", "gpt-image-1"]


class TestParsing:
    def test_deprecations_page_reads_code_ids_only(self):
        rows = pms.parse_openai_deprecations(DEPRECATIONS_HTML)
        by_model = {r["model"]: r for r in rows}
        assert by_model["gpt-image-1"]["date"] == "2026-10-23"
        assert by_model["gpt-image-1"]["replacement"] == ["gpt-image-2.5-sunburst",
                                                         "gpt-image-2.5-flare"]
        assert by_model["gpt-4.1-nano-2025-04-14"]["date"] == "2026-10-23"
        assert by_model["tts-1"]["date"] == "2027-01-06"
        assert by_model["babbage-002"]["date"] == "2024-10-28"
        assert "gpt-4" not in by_model
        assert all(r["source"] == pms.SOURCE_DEPRECATIONS_PAGE for r in rows)

    @pytest.mark.parametrize("cell,expected", [
        ("Jan 6, 2027", "2027-01-06"), ("<b>October 23, 2026</b>", "2026-10-23"),
        ("2026-05-12", "2026-05-12"), ("Sept. 3, 2026", "2026-09-03"), ("Feb 30, 2026", None),
        ("Q3 2026", None), ("", None)])
    def test_date_cells(self, cell, expected):
        assert pms.parse_date_cell(cell) == expected

    def test_litellm_ids_are_undated_unprefixed_openai(self):
        cost_map = {"gpt-4o": {"litellm_provider": "openai", "mode": "chat"},
                    "gpt-4o-2024-08-06": {"litellm_provider": "openai", "mode": "chat"},
                    "azure/gpt-4o": {"litellm_provider": "azure", "mode": "chat"},
                    "whisper-1": {"litellm_provider": "openai", "mode": "audio_transcription"},
                    "gpt-image-2": {"litellm_provider": "openai", "mode": "image_generation"},
                    "sample_spec": "not a dict"}
        assert pms.litellm_openai_models(cost_map) == ["gpt-4o", "gpt-image-2"]

    def test_litellm_deprecations_prefer_the_prefixed_key(self):
        cost_map = {"openai/gpt-image-1": {"deprecation_date": "2026-10-23"},
                    "perplexity/sonar": {"deprecation_date": None}}
        rows = pms.litellm_deprecations(cost_map, [("openai", "gpt-image-1"),
                                                   ("perplexity", "sonar")])
        assert rows == [{"provider": "openai", "model": "gpt-image-1", "date": "2026-10-23",
                         "replacement": [], "source": pms.SOURCE_LITELLM_MAP}]

    def test_models_payload(self):
        assert pms.parse_models_payload({"data": [{"id": "b"}, {"id": "a"}, {}, "x"]}) == ["a", "b"]
        assert pms.parse_models_payload({"error": "401"}) is None
        assert pms.parse_models_payload(None) is None

    def test_snapshot_loader_tolerates_garbage(self):
        assert pms.load_provider_snapshot("not json") == {}
        assert pms.load_provider_snapshot("[1]") == {}
        assert pms.load_provider_snapshot(None) == {}
        assert pms.load_provider_snapshot('{"updated": "x"}') == {"updated": "x"}


def _plan(**kw):
    defaults = dict(today="2026-10-07", openai_models=OPENAI_IDS,
                    openai_source=pms.SOURCE_OPENAI_API, perplexity_models=["sonar", "sonar-pro"],
                    deprecations=pms.parse_openai_deprecations(DEPRECATIONS_HTML))
    defaults.update(kw)
    return pms.build_provider_plan(_deployments(), **defaults)


class TestBuildPlan:
    def test_findings_issue_specs_and_alerts(self):
        plan = _plan()
        assert [s["model"] for s in plan["sunsets"]] == ["gpt-image-1"]
        assert {u["current"] for u in plan["upgrades"]} == {"gpt-4o", "gpt-4o-mini", "gpt-image-2",
                                                          "gpt-image-1"}
        assert plan["vanished"] == []
        assert plan["alert"][0]["model"] == "gpt-image-1"
        assert "gpt-image-1: sunset 2026-10-23 (in 16 days)" in plan["alert_text"]
        sunset = plan["issues"]["provider-sunset"]
        assert sunset["markers"] == ["openai/gpt-image-1 sunset 2026-10-23"]
        assert sunset["title"].startswith(pms.SUNSET_TITLE_PREFIX)
        assert "never an entry in `.litellm/model_upgrades.yaml`" in sunset["body"]
        upgrade = plan["issues"]["provider-upgrade"]
        assert "openai/gpt-4o-mini -> gpt-5.4-mini" in upgrade["markers"]
        assert "also in the family" in upgrade["body"]
        assert plan["issues"]["provider-vanished"] == {"markers": [], "title": "", "body": ""}
        assert plan["snapshot_changed"] is True

    def test_unlisted_model_alerts(self):
        plan = _plan(openai_models=["gpt-4o", "gpt-4o-mini", "gpt-image-2"])
        assert [v["model"] for v in plan["vanished"]] == ["gpt-image-1"]
        assert plan["issues"]["provider-vanished"]["markers"] == ["openai/gpt-image-1 unlisted"]
        assert "no longer listed by the provider" in plan["alert_text"]
        assert "404s answers fastest" in plan["issues"]["provider-vanished"]["body"]

    def test_a_cost_map_list_never_reports_a_vanish(self):
        plan = _plan(openai_models=["gpt-4o"], openai_source=pms.SOURCE_LITELLM_MAP)
        assert plan["vanished"] == []

    def test_an_unreadable_source_keeps_the_previous_snapshot_section(self):
        first = _plan()
        previous = dict(first["snapshot"], updated="2026-10-01")
        again = _plan(openai_models=None, openai_source=None, perplexity_models=None,
                      deprecations=[], previous=previous)
        assert again["snapshot"]["openai"] == first["snapshot"]["openai"]
        assert again["snapshot"]["perplexity"] == first["snapshot"]["perplexity"]
        # the page's sunset rows carry forward, so the registry never silently drops a date
        assert [s["model"] for s in again["sunsets"]] == ["gpt-image-1"]
        assert again["snapshot_changed"] is False
        assert again["sources"] == {"openai": None, "perplexity": None, "deprecations_page": False}

    def test_a_far_sunset_is_an_issue_but_not_an_alert(self):
        plan = _plan(today="2026-01-01")
        assert plan["sunsets"] and plan["alert"] == []

    def test_passed_sunset_alert_text_says_passed(self):
        plan = _plan(today="2026-11-01")
        assert "PASSED 9 days ago" in plan["alert_text"]
        assert "9 days AGO" in plan["issues"]["provider-sunset"]["body"]

    def test_snapshot_render_is_stable_json(self):
        text = pms.render_provider_snapshot(_plan()["snapshot"], "2026-10-07")
        doc = json.loads(text)
        assert doc["updated"] == "2026-10-07" and text.endswith("\n")
        assert doc["openai"]["source"] == pms.SOURCE_OPENAI_API
        assert all((d["provider"], d["model"]) != ("openai", "babbage-002")
                   for d in doc["deprecations"])

    def test_print_plan_lines(self):
        lines = []
        pms.print_plan(_plan(openai_models=["gpt-4o", "gpt-4o-mini", "gpt-image-2"]), lines.append)
        text = "\n".join(lines)
        assert "SUNSET [lem-image]" in text and "UNLISTED [lem-image]" in text
        empty = []
        pms.print_plan(pms.build_provider_plan([], today="2026-10-07", openai_models=None,
                                               openai_source=None, perplexity_models=None,
                                               deprecations=[]), empty.append)
        assert "nothing to report" in empty[-1] and "UNAVAILABLE" in empty[0]


class TestIO:
    def test_scan_falls_back_to_the_cost_map_and_adds_curated_notices(self):
        sources = {"openai_payload": None,
                   "litellm_map": {"gpt-4o": {"litellm_provider": "openai", "mode": "chat"},
                                   "gpt-4.1": {"litellm_provider": "openai", "mode": "chat"}},
                   "deprecations_html": DEPRECATIONS_HTML, "perplexity_payload": None}
        plan = pms.scan(_deployments(), sources, today="2026-10-07")
        assert plan["sources"]["openai"] == pms.SOURCE_LITELLM_MAP
        assert {s["model"] for s in plan["sunsets"]} == {"gpt-image-1", "sonar"}
        assert plan["vanished"] == []

    def test_scan_prefers_the_live_api(self):
        sources = {"openai_payload": {"data": [{"id": "gpt-4o"}]}, "litellm_map": {},
                   "deprecations_html": "", "perplexity_payload": {"data": [{"id": "sonar"}]}}
        plan = pms.scan(_deployments(), sources, today="2026-10-07")
        assert plan["sources"]["openai"] == pms.SOURCE_OPENAI_API
        assert plan["sources"]["perplexity"] == pms.SOURCE_PERPLEXITY_API
        assert {v["model"] for v in plan["vanished"]} == {"gpt-4o-mini", "gpt-image-2",
                                                         "gpt-image-1"}

    def test_gather_sources_from_a_fixture_dir(self, tmp_path):
        (tmp_path / "openai_models.json").write_text(json.dumps({"data": [{"id": "gpt-4o"}]}))
        (tmp_path / "deprecations.html").write_text(DEPRECATIONS_HTML)
        got = pms.gather_sources(str(tmp_path))
        assert got["openai_payload"] == {"data": [{"id": "gpt-4o"}]}
        assert got["deprecations_html"] == DEPRECATIONS_HTML
        assert got["litellm_map"] is None and got["perplexity_payload"] is None

    def test_gather_sources_network_paths_are_mocked(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pk-test")
        seen = []

        def fake_get(url, headers=None, timeout=None):
            seen.append((url, dict(headers or {})))
            if "deprecations" in url:
                return "<html></html>"
            if "perplexity" in url:
                raise OSError("401")
            return json.dumps({"data": []})

        with patch.object(mhc, "http_get", side_effect=fake_get):
            got = pms.gather_sources()
        assert got["openai_payload"] == {"data": []}
        assert got["perplexity_payload"] is None
        assert got["deprecations_html"] == "<html></html>"
        assert ("Authorization", "Bearer sk-test") in seen[0][1].items()

    def test_gather_sources_without_an_openai_key_never_calls_v1_models(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with patch.object(mhc, "http_get", side_effect=OSError("down")) as get:
            got = pms.gather_sources()
        assert got == {"openai_payload": None, "litellm_map": None, "deprecations_html": None,
                       "perplexity_payload": None}
        assert all(pms.OPENAI_MODELS_URL != c.args[0] for c in get.call_args_list)


class TestCli:
    def _fixture(self, tmp_path):
        (tmp_path / "openai_models.json").write_text(json.dumps(
            {"data": [{"id": i} for i in OPENAI_IDS]}))
        (tmp_path / "deprecations.html").write_text(DEPRECATIONS_HTML)
        (tmp_path / "perplexity_models.json").write_text(json.dumps({"data": [{"id": "sonar"}]}))
        config = tmp_path / "config.yaml"
        config.write_text(CONFIG)
        return config

    def test_scan_apply_writes_the_snapshot_and_exits_3_on_a_near_sunset(self, tmp_path, capsys):
        config = self._fixture(tmp_path)
        snap = tmp_path / "snap.json"
        rc = mhc.main(["--provider-scan", "--provider-apply", "--provider-fixture", str(tmp_path),
                       "--config", str(config), "--provider-snapshot", str(snap),
                       "--today", "2026-10-07"])
        assert rc == 3
        assert json.loads(snap.read_text())["openai"]["source"] == pms.SOURCE_OPENAI_API
        assert "SUNSET [lem-image]" in capsys.readouterr().out

    def test_json_then_file_issues_from_the_plan(self, tmp_path, capsys):
        config = self._fixture(tmp_path)
        rc = mhc.main(["--provider-json", "--provider-fixture", str(tmp_path), "--config",
                       str(config), "--provider-snapshot", str(tmp_path / "none.json"),
                       "--today", "2026-01-01"])
        assert rc == 2
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(capsys.readouterr().out)
        github = MagicMock()
        github.open_issues.return_value = []
        github.create.return_value = "https://example/1"
        with patch.object(mhc, "GitHubIssues", return_value=github):
            rc = mhc.main(["--file-provider-issues", "--plan-file", str(plan_path)])
        assert rc == 2
        titles = [c.args[0] for c in github.create.call_args_list]
        assert any(t.startswith(pms.SUNSET_TITLE_PREFIX) for t in titles)
        assert any(t.startswith(pms.UPGRADE_TITLE_PREFIX) for t in titles)
        assert [c.args[0] for c in github.open_issues.call_args_list] == [
            pms.SUNSET_TITLE_PREFIX, pms.UPGRADE_TITLE_PREFIX]

    def test_a_clean_scan_exits_0(self, tmp_path):
        config = tmp_path / "c.yaml"
        config.write_text("model_list:\n  - model_name: lem-embedding\n    litellm_params:\n"
                          "      model: openai/text-embedding-3-small\n")
        (tmp_path / "openai_models.json").write_text(json.dumps(
            {"data": [{"id": "text-embedding-3-small"}]}))
        snap = tmp_path / "snap.json"
        args = ["--provider-scan", "--provider-fixture", str(tmp_path), "--config", str(config),
                "--provider-snapshot", str(snap), "--today", "2026-10-07"]
        assert mhc.main(args + ["--provider-apply"]) == 2  # first write: the snapshot changed
        assert mhc.main(args) == 0

    def test_plan_file_cannot_drive_apply(self, tmp_path):
        plan = tmp_path / "p.json"
        plan.write_text("{}")
        with pytest.raises(SystemExit):
            mhc.main(["--provider-apply", "--plan-file", str(plan)])

    def test_plan_from_stdin(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", MagicMock(read=lambda: json.dumps(
            {"sunsets": [], "upgrades": [], "vanished": [], "snapshot_changed": False,
             "alert": [], "today": "2026-10-07", "sources": {
                 "openai": None, "perplexity": None, "deprecations_page": False}})))
        assert mhc.main(["--provider-scan", "--plan-file", "-"]) == 0
        assert "nothing to report" in capsys.readouterr().out
