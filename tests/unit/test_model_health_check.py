"""Unit tests for scripts/model_health_check.py — the pure planning/rewriting logic."""

import importlib.util
import io
import json
import pathlib
import sys

import pytest

pytestmark = pytest.mark.unit

# The tool lives under scripts/ (not an importable package) — load it by path.
_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PATH = _ROOT / "scripts" / "model_health_check.py"
_spec = importlib.util.spec_from_file_location("model_health_check", _PATH)
mhc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mhc)

# Real captures of the two sources the proactive scan reads (issue #716).
FIXTURES = _ROOT / "tests" / "unit" / "fixtures" / "ollama"


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


# ══════════════════ issue #716 — proactive scan (advance retirements, catalog, families) ═════════

def _cloud_md():
    return (FIXTURES / "cloud.md").read_text()


def _tags():
    return json.loads((FIXTURES / "tags.json").read_text())


def _ollama_cfg(*models):
    return {"model_list": [
        {"model_name": "lem-medium", "litellm_params": {
            "model": f"openai/{m}", "api_base": "os.environ/OLLAMA_CLOUD_URL"}}
        for m in models]}


class TestParseRetirements:
    def test_real_cloud_md_upcoming_rows(self):
        rows = mhc.parse_retirements(_cloud_md())
        upcoming = {r["model"]: r for r in rows if r["upcoming"]}
        assert upcoming["minimax-m2.5"] == {"model": "minimax-m2.5", "alternative": "minimax-m2.7",
                                            "date": "2026-07-31", "upcoming": True}
        assert upcoming["kimi-k2.5"]["alternative"] == "kimi-k2.6"

    def test_past_accordion_rows_inherit_the_accordion_date(self):
        rows = {r["model"]: r for r in mhc.parse_retirements(_cloud_md()) if not r["upcoming"]}
        assert rows["kimi-k2:1t"]["date"] == "2026-06-16"       # June 16, 2026 accordion
        assert rows["ministral-3:8b"]["date"] == "2026-07-15"   # July 15, 2026 accordion
        assert rows["qwen3-coder:480b"]["alternative"] == "qwen3.5:397b"

    def test_missing_alternative_is_none_not_empty_string(self):
        rows = {r["model"]: r for r in mhc.parse_retirements(_cloud_md())}
        assert rows["devstral-small-2:24b"]["alternative"] is None

    def test_header_and_separator_rows_are_not_models(self):
        names = {r["model"] for r in mhc.parse_retirements(_cloud_md())}
        assert not any(n.lower() in {"model", "retirement date", "recommended alternative"}
                       for n in names)
        assert not any(set(n) <= {"-", ":"} for n in names)

    def test_tables_outside_the_retirements_section_are_ignored(self):
        md = ("## Something else\n\n| Model | Recommended alternative |\n"
              "| ----- | ----------------------- |\n| `not-a-retirement` | `x` |\n"
              "\n## Retirements\n\n### Upcoming retirements\n\n"
              "| Retirement date | Model | Recommended alternative |\n"
              "| --- | --- | --- |\n| March 3, 2027 | `real-1` | `real-2` |\n")
        rows = mhc.parse_retirements(md)
        assert [r["model"] for r in rows] == ["real-1"]
        assert rows[0]["date"] == "2027-03-03"

    def test_unparseable_or_empty_input_is_empty(self):
        assert mhc.parse_retirements("") == []
        assert mhc.parse_retirements(None) == []

    def test_parse_doc_date_rejects_garbage(self):
        assert mhc.parse_doc_date("Smarch 3, 2026") is None
        assert mhc.parse_doc_date("February 31, 2026") is None
        assert mhc.parse_doc_date("") is None

    def test_a_cell_that_is_not_a_model_tag_is_dropped(self):
        """cloud.md is remote and its cells become model_upgrades.yaml values, which the reactive
        half applies to the live config — a quote or newline there must never reach that writer."""
        md = ('## Retirements\n\n### Upcoming retirements\n\n'
              "| Retirement date | Model | Recommended alternative |\n| --- | --- | --- |\n"
              '| March 3, 2027 | `real-1` | `evil" injected: "yes` |\n'
              '| March 3, 2027 | `see the migration guide` | `real-2` |\n'
              '| March 3, 2027 | `real-3` | `real-4` |\n')
        rows = mhc.parse_retirements(md)
        assert [(r["model"], r["alternative"]) for r in rows] == [("real-1", None),
                                                                  ("real-3", "real-4")]

    def test_is_model_tag(self):
        for good in ("minimax-m2.7", "kimi-k2:1t", "qwen3.5:397b", "REMOVE", "gpt-oss:20b"):
            assert mhc.is_model_tag(good)
        for bad in ("", None, 'a" b', "a\nb", "see the docs", "-leading-dash", "a`b"):
            assert not mhc.is_model_tag(bad)


class TestRetirementNotices:
    def _rows(self, *models):
        return mhc.parse_deployments(_ollama_cfg(*models))

    def test_future_retirement_of_a_configured_model_alerts_and_maps(self):
        notices, additions = mhc.plan_retirement_notices(
            self._rows("minimax-m2.5"), mhc.parse_retirements(_cloud_md()), {}, "2026-07-20",
            mhc.parse_catalog(_tags()))
        assert [n["bare"] for n in notices] == ["minimax-m2.5"]
        assert notices[0]["date"] == "2026-07-31" and notices[0]["days_until"] == 11
        assert notices[0]["mapped"] is False
        assert additions == {"minimax-m2.5": "minimax-m2.7"}

    def test_already_past_retirement_is_the_410_probes_job(self):
        notices, additions = mhc.plan_retirement_notices(
            self._rows("minimax-m2.5"), mhc.parse_retirements(_cloud_md()), {}, "2026-08-01", None)
        assert notices == [] and additions == {}

    def test_same_day_is_not_future(self):
        notices, _ = mhc.plan_retirement_notices(
            self._rows("minimax-m2.5"), mhc.parse_retirements(_cloud_md()), {}, "2026-07-31", None)
        assert notices == []

    def test_already_mapped_still_notices_but_adds_nothing(self):
        notices, additions = mhc.plan_retirement_notices(
            self._rows("minimax-m2.5"), mhc.parse_retirements(_cloud_md()),
            {"minimax-m2.5": "minimax-m2.7"}, "2026-07-20", None)
        assert notices[0]["mapped"] is True
        assert additions == {}

    def test_alternative_missing_from_the_catalog_is_never_auto_mapped(self):
        retirements = [{"model": "minimax-m2.5", "alternative": "ghost-model",
                        "date": "2026-07-31", "upcoming": True}]
        notices, additions = mhc.plan_retirement_notices(
            self._rows("minimax-m2.5"), retirements, {}, "2026-07-20", {"minimax-m2.7": {}})
        assert notices[0]["alternative_in_catalog"] is False
        assert additions == {}

    def test_no_published_alternative_alerts_without_writing_remove(self):
        retirements = [{"model": "minimax-m2.5", "alternative": None,
                        "date": "2026-07-31", "upcoming": True}]
        notices, additions = mhc.plan_retirement_notices(
            self._rows("minimax-m2.5"), retirements, {}, "2026-07-20", None)
        assert notices and notices[0]["alternative"] is None
        assert additions == {}

    def test_unconfigured_models_are_not_our_problem(self):
        notices, additions = mhc.plan_retirement_notices(
            self._rows("gpt-oss:20b"), mhc.parse_retirements(_cloud_md()), {}, "2026-07-20", None)
        assert notices == [] and additions == {}

    def test_non_ollama_deployment_is_never_noticed(self):
        cfg = {"model_list": [{"model_name": "lem-medium", "litellm_params": {
            "model": "openai/minimax-m2.5", "api_key": "os.environ/OPENAI_API_KEY"}}]}
        notices, _ = mhc.plan_retirement_notices(
            mhc.parse_deployments(cfg), mhc.parse_retirements(_cloud_md()), {}, "2026-07-20", None)
        assert notices == []


class TestAppendUpgradeMappings:
    MAP = ('# header comment\nupgrades:\n  # existing\n  "kimi-k2:1t": "REMOVE"\n')

    def test_appends_inside_the_block_and_keeps_comments(self):
        text, added = mhc.append_upgrade_mappings(self.MAP, {"minimax-m2.5": "minimax-m2.7"},
                                                  "2026-07-20")
        assert added == ["minimax-m2.5"]
        assert "# header comment" in text and '"kimi-k2:1t": "REMOVE"' in text
        assert '  "minimax-m2.5": "minimax-m2.7"' in text
        assert mhc.load_map(mhc.load_map_text(text)) == {"kimi-k2:1t": "REMOVE",
                                                         "minimax-m2.5": "minimax-m2.7"}

    def test_existing_key_is_never_duplicated(self):
        text, added = mhc.append_upgrade_mappings(self.MAP, {"kimi-k2:1t": "kimi-k2.6"},
                                                  "2026-07-20")
        assert added == [] and text == self.MAP

    def test_is_idempotent(self):
        once, _ = mhc.append_upgrade_mappings(self.MAP, {"a": "b"}, "2026-07-20")
        twice, added = mhc.append_upgrade_mappings(once, {"a": "b"}, "2026-07-21")
        assert twice == once and added == []

    def test_creates_the_block_when_the_file_has_none(self):
        text, added = mhc.append_upgrade_mappings("# only a comment\n", {"a": "b"}, "2026-07-20")
        assert added == ["a"]
        assert mhc.load_map(mhc.load_map_text(text)) == {"a": "b"}

    def test_does_not_append_past_the_block_into_a_later_section(self):
        src = 'upgrades:\n  "a": "b"\n\nother:\n  "x": "y"\n'
        text, _ = mhc.append_upgrade_mappings(src, {"c": "d"}, "2026-07-20")
        assert text.index('"c": "d"') < text.index("other:")
        assert mhc.load_map(mhc.load_map_text(text)) == {"a": "b", "c": "d"}

    def test_a_non_tag_value_can_never_inject_extra_mappings(self):
        """Last line of defence: the writer refuses anything it hasn't checked, so a poisoned
        cell that reached this far still cannot add a mapping of its own."""
        text, added = mhc.append_upgrade_mappings(
            self.MAP, {"good": "fine", "victim": 'evil"\n  "injected": "yes'}, "2026-07-20")
        assert added == ["good"]
        assert mhc.load_map(mhc.load_map_text(text)) == {"kimi-k2:1t": "REMOVE", "good": "fine"}


class TestCatalogSnapshot:
    def test_parse_catalog_from_the_real_tags_payload(self):
        catalog = mhc.parse_catalog(_tags())
        assert "minimax-m3" in catalog and "gpt-oss:20b" in catalog
        assert catalog["gpt-oss:20b"]["size"] > 0

    def test_parse_catalog_tolerates_junk_rows(self):
        assert mhc.parse_catalog({"models": [None, {}, {"name": "  "}, {"model": "x"}]}) == {
            "x": {"modified_at": "", "size": 0, "parameter_size": "", "family": ""}}
        assert mhc.parse_catalog(None) == {}

    def test_snapshot_round_trip(self):
        catalog = mhc.parse_catalog(_tags())
        assert mhc.load_snapshot_text(mhc.render_snapshot(catalog, "2026-07-27")) == catalog

    def test_snapshot_of_unreadable_json_is_empty_not_an_exception(self):
        assert mhc.load_snapshot_text("{not json") == {}

    def test_diff_reports_added_and_removed(self):
        assert mhc.diff_catalog({"a": {}, "b": {}}, {"b": {}, "c": {}}) == {"added": ["c"],
                                                                            "removed": ["a"]}

    def test_committed_snapshot_matches_the_real_catalog_shape(self):
        snapshot = mhc.load_snapshot_text((_ROOT / ".litellm"
                                           / "ollama_catalog_snapshot.json").read_text())
        assert snapshot, "the committed snapshot must not be empty — an empty one files every tag"
        assert "gpt-oss:20b" in snapshot  # a model config.yaml actually runs


def _ollama_deployments(*models, group="lem-medium"):
    return mhc.parse_deployments({"model_list": [
        {"model_name": group, "litellm_params": {"model": f"openai/{m}",
                                                 "api_base": "os.environ/OLLAMA_CLOUD_URL"}}
        for m in models]})


_OLD_BUILD = {"modified_at": "2026-04-24T00:00:00Z", "size": 140_000_000_000}
_NEW_BUILD = {"modified_at": "2026-07-31T00:00:00Z", "size": 167_000_000_000}


class TestPlanRepoints:
    """Issue #925 — the catalog move a tag-NAME diff cannot see: the same id now serves a different
    build, so a live tier changed model with no repo change and no benchmark."""

    def test_a_configured_tag_whose_build_moved_is_reported(self):
        out = mhc.plan_repoints(_ollama_deployments("deepseek-v4-flash"),
                                {"deepseek-v4-flash": dict(_OLD_BUILD)},
                                {"deepseek-v4-flash": dict(_NEW_BUILD)})
        assert len(out) == 1
        assert out[0]["tag"] == "deepseek-v4-flash"
        assert out[0]["model"] == "openai/deepseek-v4-flash"
        assert out[0]["groups"] == ["lem-medium"]
        assert {c["field"] for c in out[0]["changes"]} == {"size", "modified_at"}

    def test_a_timestamp_only_move_still_counts(self):
        out = mhc.plan_repoints(_ollama_deployments("minimax-m3"),
                                {"minimax-m3": {"modified_at": "2026-06-01T00:00:00Z", "size": 0}},
                                {"minimax-m3": {"modified_at": "2026-08-01T00:00:00Z", "size": 0}})
        assert [c["field"] for c in out[0]["changes"]] == ["modified_at"]

    def test_an_unreferenced_tags_repoint_stays_silent(self):
        """The catalog carries hundreds of tags LEM does not run — those moves are noise."""
        assert mhc.plan_repoints(_ollama_deployments("gpt-oss:20b"),
                                 {"deepseek-v4-flash": dict(_OLD_BUILD)},
                                 {"deepseek-v4-flash": dict(_NEW_BUILD)}) == []

    def test_an_unchanged_build_is_not_a_repoint(self):
        assert mhc.plan_repoints(_ollama_deployments("deepseek-v4-flash"),
                                 {"deepseek-v4-flash": dict(_OLD_BUILD)},
                                 {"deepseek-v4-flash": dict(_OLD_BUILD)}) == []

    def test_a_brand_new_or_vanished_tag_is_never_a_repoint(self):
        """Those are the added/removed paths' job — reporting them twice files two issues."""
        assert mhc.plan_repoints(_ollama_deployments("deepseek-v4-flash"), {},
                                 {"deepseek-v4-flash": dict(_NEW_BUILD)}) == []
        assert mhc.plan_repoints(_ollama_deployments("deepseek-v4-flash"),
                                 {"deepseek-v4-flash": dict(_OLD_BUILD)}, {}) == []

    def test_a_missing_baseline_field_is_not_a_build_swap(self):
        """A snapshot written before a field was captured has NO baseline for it. Reading that as a
        re-point would file an issue for every configured tag at once."""
        assert mhc.plan_repoints(_ollama_deployments("deepseek-v4-flash"),
                                 {"deepseek-v4-flash": {"size": 0, "modified_at": ""}},
                                 {"deepseek-v4-flash": dict(_NEW_BUILD)}) == []

    def test_a_non_ollama_deployment_is_never_checked(self):
        deployments = mhc.parse_deployments({"model_list": [
            {"model_name": "lem-medium", "litellm_params": {"model": "openai/deepseek-v4-flash",
                                                            "api_key": "os.environ/OPENAI_API_KEY"}}]})
        assert mhc.plan_repoints(deployments, {"deepseek-v4-flash": dict(_OLD_BUILD)},
                                 {"deepseek-v4-flash": dict(_NEW_BUILD)}) == []

    def test_every_tier_the_tag_serves_is_named_once(self):
        deployments = (_ollama_deployments("deepseek-v4-flash", group="lem-medium")
                       + _ollama_deployments("deepseek-v4-flash", group="lem-complex"))
        out = mhc.plan_repoints(deployments, {"deepseek-v4-flash": dict(_OLD_BUILD)},
                                {"deepseek-v4-flash": dict(_NEW_BUILD)})
        assert len(out) == 1 and out[0]["groups"] == ["lem-complex", "lem-medium"]


class TestRepointIssue:
    def _repoint(self, new=None):
        return mhc.plan_repoints(_ollama_deployments("deepseek-v4-flash"),
                                 {"deepseek-v4-flash": dict(_OLD_BUILD)},
                                 {"deepseek-v4-flash": {**_NEW_BUILD, **(new or {})}})[0]

    def test_marker_carries_the_new_build_not_just_the_tag(self):
        marker = mhc.repoint_marker(self._repoint())
        assert marker == "deepseek-v4-flash @ 167000000000/2026-07-31T00:00:00Z"

    def test_a_second_repoint_of_the_same_tag_is_not_deduped_away(self):
        """The acceptance criterion the tag name alone would fail: once #925's issue is filed for
        build A, a later move to build B must still reach a human."""
        first = mhc.repoint_marker(self._repoint())
        second = mhc.repoint_marker(self._repoint({"modified_at": "2026-09-02T00:00:00Z",
                                                   "size": 171_000_000_000}))
        open_issues = [{"number": 12, "title": mhc.REPOINT_TITLE_PREFIX + " deepseek-v4-flash",
                        "body": f"markers: `{first}`", "comments": []}]
        assert mhc.plan_issue(open_issues, [first],
                              title_prefix=mhc.REPOINT_TITLE_PREFIX)["action"] == "none"
        assert mhc.plan_issue(open_issues, [second],
                              title_prefix=mhc.REPOINT_TITLE_PREFIX) == {
            "action": "append", "markers": [second], "number": 12}

    def test_title_and_body_name_the_live_deployment_and_the_move(self):
        repoint = self._repoint()
        assert mhc.build_repoint_issue_title([repoint]) == (
            f"{mhc.REPOINT_TITLE_PREFIX} deepseek-v4-flash")
        body = mhc.build_repoint_issue_body([repoint], "2026-08-02")
        assert "`lem-medium`" in body  # the reader has to know a live tier moved
        assert "model: openai/deepseek-v4-flash" in body
        assert "`140GB` → `167GB`" in body
        assert "2026-04-24T00:00:00Z" in body and "2026-07-31T00:00:00Z" in body
        assert mhc.repoint_marker(repoint) in body  # dedup marker survives into the issue
        assert "model_upgrades.yaml" in body  # never the retirement map, which auto-swaps


class TestParseModelVersion:
    @pytest.mark.parametrize("name,family,version,size_tag", [
        ("minimax-m2.7", "minimax", (2, 7), ""),
        ("minimax-m3", "minimax", (3,), ""),
        ("qwen3.5:397b", "qwen", (3, 5), "397b"),
        ("glm-5.1", "glm", (5, 1), ""),
        ("glm-5.2", "glm", (5, 2), ""),
        ("kimi-k2.6", "kimi", (2, 6), ""),
        ("kimi-k2.7-code", "kimi-code", (2, 7), ""),
        ("kimi-k2:1t", "kimi", (2,), "1t"),
        ("gemma4:31b", "gemma", (4,), "31b"),
        ("gemma3:12b", "gemma", (3,), "12b"),
        ("deepseek-v4-flash", "deepseek-flash", (4,), ""),
        ("deepseek-v4-pro", "deepseek-pro", (4,), ""),
        ("nemotron-3-nano:30b", "nemotron-nano", (3,), "30b"),
        ("nemotron-3-super", "nemotron-super", (3,), ""),
        ("nemotron-3-ultra", "nemotron-ultra", (3,), ""),
        ("mistral-large-3:675b", "mistral-large", (3,), "675b"),
        ("qwen3-coder:480b", "qwen-coder", (3,), "480b"),
        ("ministral-3:8b", "ministral", (3,), "8b"),
        ("gpt-oss:20b", "gpt-oss", None, "20b"),      # size-tagged, not versioned
        ("gpt-oss:120b", "gpt-oss", None, "120b"),
    ])
    def test_real_catalog_names(self, name, family, version, size_tag):
        assert mhc.parse_model_version(name) == (family, version, size_tag)

    def test_every_live_catalog_tag_parses(self):
        for name in mhc.parse_catalog(_tags()):
            family, _version, _tag = mhc.parse_model_version(name)
            assert family, name

    def test_empty_input(self):
        assert mhc.parse_model_version("") == ("", None, "")
        assert mhc.parse_model_version(None) == ("", None, "")

    def test_version_str(self):
        assert mhc.version_str((2, 7)) == "2.7"
        assert mhc.version_str(None) == "unversioned"
        assert mhc.version_str(()) == "unversioned"


class TestPlanFamilyUpgrades:
    def _rows(self, *models):
        return mhc.parse_deployments(_ollama_cfg(*models))

    def test_major_bump_against_the_real_catalog(self):
        out = mhc.plan_family_upgrades(self._rows("minimax-m2.7"), mhc.parse_catalog(_tags()))
        assert len(out) == 1
        assert out[0]["candidate"] == "minimax-m3" and out[0]["urgency"] == "major"
        assert out[0]["groups"] == ["lem-medium"]

    def test_minor_bump_is_reported_at_lower_urgency(self):
        out = mhc.plan_family_upgrades(self._rows("glm-5.1"), mhc.parse_catalog(_tags()))
        assert out[0]["candidate"] == "glm-5.2" and out[0]["urgency"] == "minor"

    def test_current_newest_produces_nothing(self):
        assert mhc.plan_family_upgrades(self._rows("minimax-m3"), mhc.parse_catalog(_tags())) == []
        assert mhc.plan_family_upgrades(self._rows("qwen3.5:397b"),
                                        mhc.parse_catalog(_tags())) == []

    def test_a_variant_is_never_offered_as_the_base_models_upgrade(self):
        # kimi-k2.7-code is a coding variant, not a newer kimi-k2.6.
        assert mhc.plan_family_upgrades(self._rows("kimi-k2.6"), mhc.parse_catalog(_tags())) == []

    def test_size_tagged_unversioned_model_with_no_versioned_sibling_is_skipped(self):
        assert mhc.plan_family_upgrades(self._rows("gpt-oss:20b"), mhc.parse_catalog(_tags())) == []

    def test_unversioned_current_with_a_versioned_sibling_is_a_major(self):
        out = mhc.plan_family_upgrades(self._rows("gpt-oss:20b"), {"gpt-oss2": {}})
        assert out[0]["urgency"] == "major" and out[0]["current_version"] == "unversioned"

    def test_a_retiring_candidate_is_never_recommended(self):
        catalog = {"minimax-m2.5": {}, "minimax-m2.7": {}}
        out = mhc.plan_family_upgrades(self._rows("minimax-m2.5"), catalog,
                                       retiring={"minimax-m2.7"})
        assert out == []

    def test_non_ollama_deployments_are_ignored(self):
        cfg = {"model_list": [{"model_name": "lem-medium", "litellm_params": {
            "model": "openai/minimax-m2.7", "api_key": "os.environ/OPENAI_API_KEY"}}]}
        assert mhc.plan_family_upgrades(mhc.parse_deployments(cfg),
                                        mhc.parse_catalog(_tags())) == []

    def test_duplicate_deployments_report_once_with_every_tier(self):
        cfg = {"model_list": [
            {"model_name": "lem-medium", "litellm_params": {
                "model": "openai/minimax-m2.7", "api_base": "os.environ/OLLAMA_CLOUD_URL"}},
            {"model_name": "lem-complex", "litellm_params": {
                "model": "openai/minimax-m2.7", "api_base": "os.environ/OLLAMA_CLOUD_URL"}}]}
        out = mhc.plan_family_upgrades(mhc.parse_deployments(cfg), mhc.parse_catalog(_tags()))
        assert len(out) == 1 and out[0]["groups"] == ["lem-complex", "lem-medium"]

    def test_usage_levels_ride_along_and_flag_a_tier_jump(self):
        levels = {"minimax-m2.7": {"level": 2, "label": "medium"},
                  "minimax-m3": {"level": 3, "label": "high"}}
        out = mhc.plan_family_upgrades(self._rows("minimax-m2.7"), mhc.parse_catalog(_tags()),
                                       usage_lookup=lambda m: levels.get(m))
        assert out[0]["usage_jump"] is True
        assert out[0]["candidate_usage"]["label"] == "high"

    def test_an_unreadable_usage_page_is_none_not_no_jump(self):
        def boom(_model):
            raise RuntimeError("ollama.com down")

        out = mhc.plan_family_upgrades(self._rows("minimax-m2.7"), mhc.parse_catalog(_tags()),
                                       usage_lookup=boom)
        assert out[0]["usage_jump"] is None


class TestParseUsageLevel:
    def test_real_model_pages(self):
        assert mhc.parse_usage_level((FIXTURES / "usage" / "minimax-m2.7.html").read_text()) == {
            "level": 2, "label": "medium"}
        assert mhc.parse_usage_level((FIXTURES / "usage" / "minimax-m3.html").read_text()) == {
            "level": 3, "label": "high"}
        assert mhc.parse_usage_level((FIXTURES / "usage" / "deepseek-v4-pro.html").read_text()) == {
            "level": 4, "label": "extra high"}

    def test_missing_usage_block(self):
        assert mhc.parse_usage_level("<html><body>nothing</body></html>") == {"level": None,
                                                                              "label": None}
        assert mhc.parse_usage_level("") == {"level": None, "label": None}

    def test_reading_stops_before_the_next_stat(self):
        # "low" appears only AFTER the Context label — it must not be read as the usage level.
        html = ('<div>Usage</div><span class="bg-neutral-900"></span>'
                '<div>Context</div><span>low</span>')
        assert mhc.parse_usage_level(html) == {"level": 1, "label": None}


class TestIssueRendering:
    UPGRADES = [{"family": "minimax", "groups": ["lem-medium"], "current": "minimax-m2.7",
                 "current_version": "2.7", "candidate": "minimax-m3", "candidate_version": "3",
                 "urgency": "major", "current_usage": {"level": 2, "label": "medium"},
                 "candidate_usage": {"level": 3, "label": "high"}, "usage_jump": True}]

    def test_upgrade_marker_and_title(self):
        assert mhc.upgrade_marker(self.UPGRADES[0]) == "minimax-m2.7 -> minimax-m3"
        title = mhc.build_upgrade_issue_title(self.UPGRADES)
        assert title.startswith(mhc.UPGRADE_TITLE_PREFIX)
        assert "minimax-m2.7 -> minimax-m3" in title

    def test_upgrade_body_names_the_usage_jump_and_the_never_auto_swap_rule(self):
        body = mhc.build_upgrade_issue_body(self.UPGRADES, "2026-07-27")
        assert "minimax-m2.7 -> minimax-m3" in body
        assert "medium (level 2)" in body and "high (level 3)" in body
        assert "usage level goes UP" in body
        assert "model_upgrades.yaml" in body  # says explicitly not to add it there

    def test_unknown_usage_is_worded_as_unknown_not_as_no_jump(self):
        upgrade = {**self.UPGRADES[0], "usage_jump": None,
                   "current_usage": {"level": None, "label": None},
                   "candidate_usage": {"level": None, "label": None}}
        assert "usage level unknown" in mhc.build_upgrade_issue_body([upgrade], "2026-07-27")

    def test_minor_upgrades_get_their_own_section(self):
        minor = {**self.UPGRADES[0], "urgency": "minor"}
        body = mhc.build_upgrade_issue_body([minor], "2026-07-27")
        assert "Minor/patch behind" in body and "Major version behind" not in body

    def test_eval_title_and_body_from_the_real_catalog(self):
        catalog = mhc.parse_catalog(_tags())
        title = mhc.build_eval_issue_title(["minimax-m3", "glm-5.2"])
        assert title == f"{mhc.EVAL_TITLE_PREFIX} minimax-m3, glm-5.2"
        body = mhc.build_eval_issue_body(["minimax-m3"], catalog, "2026-07-27")
        assert "`minimax-m3`" in body and "family `minimax`" in body

    def test_long_title_is_truncated_but_the_body_keeps_every_marker(self):
        names = [f"some-rather-long-model-name-{i}" for i in range(12)]
        title = mhc.build_eval_issue_title(names)
        assert len(title) <= mhc.MAX_TITLE_CHARS
        assert "more)" in title
        body = mhc.build_eval_issue_body(names, {}, "2026-07-27")
        assert all(n in body for n in names)


class TestPlanIssue:
    def test_creates_when_nothing_open(self):
        assert mhc.plan_issue([], ["a -> b"], title_prefix=mhc.UPGRADE_TITLE_PREFIX) == {
            "action": "create", "markers": ["a -> b"], "number": None}

    def test_no_markers_is_never_an_issue(self):
        assert mhc.plan_issue([], [], title_prefix=mhc.UPGRADE_TITLE_PREFIX)["action"] == "none"

    def test_marker_already_in_an_open_issue_body_is_not_refiled(self):
        open_issues = [{"number": 9, "title": mhc.UPGRADE_TITLE_PREFIX + " x",
                        "body": "markers: `a -> b`", "comments": []}]
        out = mhc.plan_issue(open_issues, ["a -> b"], title_prefix=mhc.UPGRADE_TITLE_PREFIX)
        assert out == {"action": "none", "markers": [], "number": 9}

    def test_marker_in_a_comment_counts_as_filed(self):
        open_issues = [{"number": 9, "title": mhc.UPGRADE_TITLE_PREFIX + " x", "body": "",
                        "comments": [{"body": "found more: `a -> b`"}]}]
        assert mhc.plan_issue(open_issues, ["a -> b"],
                              title_prefix=mhc.UPGRADE_TITLE_PREFIX)["action"] == "none"

    def test_new_marker_appends_to_the_open_issue(self):
        open_issues = [{"number": 9, "title": mhc.UPGRADE_TITLE_PREFIX + " x",
                        "body": "`a -> b`", "comments": []}]
        out = mhc.plan_issue(open_issues, ["a -> b", "c -> d"],
                             title_prefix=mhc.UPGRADE_TITLE_PREFIX)
        assert out == {"action": "append", "markers": ["c -> d"], "number": 9}

    def test_an_unrelated_open_issue_does_not_count(self):
        open_issues = [{"number": 9, "title": "Something else entirely", "body": "`a -> b`",
                        "comments": []}]
        assert mhc.plan_issue(open_issues, ["a -> b"],
                              title_prefix=mhc.UPGRADE_TITLE_PREFIX)["action"] == "create"

    def test_append_comment_carries_the_markers_and_the_detail(self):
        text = mhc.build_append_comment(["c -> d"], "FULL BODY")
        assert "`c -> d`" in text and "FULL BODY" in text


class _FakeGitHub:
    def __init__(self, open_issues=None, fail_lookup=False):
        self._open = open_issues or []
        self._fail = fail_lookup
        self.created = []
        self.comments = []

    def open_issues(self, title_prefix):
        if self._fail:
            raise RuntimeError("gh exploded")
        return [i for i in self._open if str(i.get("title", "")).startswith(title_prefix)]

    def create(self, title, body, labels=mhc.ISSUE_LABELS):
        self.created.append((title, body))
        return "https://github.com/x/y/issues/1"

    def comment(self, number, body):
        self.comments.append((number, body))
        return True


def _boom(*a, **k):
    raise AssertionError("the catalog must not be re-scanned when a plan was supplied")


def _plan_with_issues(upgrade_markers=(), eval_markers=(), repoint_markers=()):
    return {"issues": {
        "upgrade": {"markers": list(upgrade_markers), "title": "T-up", "body": "B-up"},
        "evaluation": {"markers": list(eval_markers), "title": "T-ev", "body": "B-ev"},
        "repoint": {"markers": list(repoint_markers), "title": "T-rp", "body": "B-rp"}}}


class TestFileIssues:
    def test_files_both_issues(self):
        gh = _FakeGitHub()
        assert mhc._file_issues(_plan_with_issues(["a -> b"], ["new-tag"]), gh) == 2
        assert [t for t, _ in gh.created] == ["T-up", "T-ev"]

    def test_files_the_repoint_issue_on_its_own_title_prefix(self):
        gh = _FakeGitHub()
        assert mhc._file_issues(_plan_with_issues(repoint_markers=["t @ 1/x"]), gh) == 1
        assert [t for t, _ in gh.created] == ["T-rp"]

    def test_a_plan_saved_before_a_kind_existed_files_the_rest(self):
        """--plan-file may carry a plan written by an older copy of this tool; a missing kind is
        nothing to file, never a KeyError that drops the issues that WERE planned."""
        plan = _plan_with_issues(["a -> b"])
        del plan["issues"]["repoint"]
        gh = _FakeGitHub()
        assert mhc._file_issues(plan, gh) == 1
        assert [t for t, _ in gh.created] == ["T-up"]

    def test_nothing_to_file(self):
        gh = _FakeGitHub()
        assert mhc._file_issues(_plan_with_issues(), gh) == 0
        assert gh.created == []

    def test_appends_instead_of_refiling(self):
        gh = _FakeGitHub([{"number": 7, "title": mhc.UPGRADE_TITLE_PREFIX + " a -> b",
                           "body": "`a -> b`", "comments": []}])
        assert mhc._file_issues(_plan_with_issues(["a -> b", "c -> d"]), gh) == 1
        assert gh.created == [] and gh.comments[0][0] == 7

    def test_a_failed_dedup_lookup_skips_rather_than_duplicating(self):
        gh = _FakeGitHub(fail_lookup=True)
        assert mhc._file_issues(_plan_with_issues(["a -> b"]), gh) == 0
        assert gh.created == []


class TestGitHubIssuesClient:
    def _proc(self, returncode=0, stdout="", stderr=""):
        class P:
            pass
        p = P()
        p.returncode, p.stdout, p.stderr = returncode, stdout, stderr
        return p

    def test_open_issues_filters_by_title_prefix(self, monkeypatch):
        gh = mhc.GitHubIssues("o/n")
        payload = json.dumps([{"number": 1, "title": mhc.UPGRADE_TITLE_PREFIX + " x"},
                              {"number": 2, "title": "unrelated"}])
        monkeypatch.setattr(gh, "_run", lambda args: self._proc(stdout=payload))
        assert [i["number"] for i in gh.open_issues(mhc.UPGRADE_TITLE_PREFIX)] == [1]

    def test_open_issues_fails_closed_on_a_gh_error(self, monkeypatch):
        gh = mhc.GitHubIssues("o/n")
        monkeypatch.setattr(gh, "_run", lambda args: self._proc(returncode=1, stderr="boom"))
        with pytest.raises(RuntimeError):
            gh.open_issues(mhc.UPGRADE_TITLE_PREFIX)

    def test_open_issues_fails_closed_on_unparseable_json(self, monkeypatch):
        gh = mhc.GitHubIssues("o/n")
        monkeypatch.setattr(gh, "_run", lambda args: self._proc(stdout="{nope"))
        with pytest.raises(RuntimeError):
            gh.open_issues(mhc.UPGRADE_TITLE_PREFIX)

    def test_create_passes_every_label_and_returns_the_url(self, monkeypatch):
        gh = mhc.GitHubIssues("o/n")
        seen = {}

        def run(args):
            seen["args"] = args
            return self._proc(stdout="https://github.com/o/n/issues/5\n")

        monkeypatch.setattr(gh, "_run", run)
        assert gh.create("T", "B") == "https://github.com/o/n/issues/5"
        for label in mhc.ISSUE_LABELS:
            assert label in seen["args"]

    def test_create_failure_is_none_not_a_raise(self, monkeypatch):
        gh = mhc.GitHubIssues("o/n")
        monkeypatch.setattr(gh, "_run", lambda args: self._proc(returncode=1, stderr="nope"))
        assert gh.create("T", "B") is None

    def test_comment_reports_success_and_failure(self, monkeypatch):
        gh = mhc.GitHubIssues("o/n")
        monkeypatch.setattr(gh, "_run", lambda args: self._proc())
        assert gh.comment(3, "body") is True
        monkeypatch.setattr(gh, "_run", lambda args: self._proc(returncode=1))
        assert gh.comment(3, "body") is False


class TestFetchDegradation:
    def test_docs_fetch_failure_is_none(self, monkeypatch):
        monkeypatch.setattr(mhc, "http_get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        assert mhc.fetch_cloud_docs() is None

    def test_catalog_fetch_failure_is_none(self, monkeypatch):
        monkeypatch.setattr(mhc, "http_get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        assert mhc.fetch_catalog("key") is None

    def test_catalog_sends_the_bearer_token_when_one_is_set(self, monkeypatch):
        seen = {}

        def fake(url, headers=None, timeout=None):
            seen["headers"] = headers
            return '{"models": []}'

        monkeypatch.setattr(mhc, "http_get", fake)
        mhc.fetch_catalog("sekret")
        assert seen["headers"] == {"Authorization": "Bearer sekret"}
        mhc.fetch_catalog("")
        assert seen["headers"] == {}

    def test_usage_fetch_failure_is_unknown_not_a_raise(self, monkeypatch):
        monkeypatch.setattr(mhc, "http_get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        assert mhc.fetch_usage_level("minimax-m3") == {"level": None, "label": None}

    def test_usage_fetch_strips_the_size_tag_from_the_page_url(self, monkeypatch):
        seen = {}

        def fake(url, headers=None, timeout=None):
            seen["url"] = url
            return (FIXTURES / "usage" / "minimax-m3.html").read_text()

        monkeypatch.setattr(mhc, "http_get", fake)
        assert mhc.fetch_usage_level("qwen3.5:397b")["label"] == "high"
        assert seen["url"].endswith("/library/qwen3.5")


class TestCatalogScanCLI:
    """The dry-run proof required by the issue: a future-dated retirement of a CONFIGURED model
    produces the alert + the map addition, and a new tag renders the evaluation issue — all with
    the network replaced by the committed fixtures."""

    def _workspace(self, tmp_path, model="minimax-m2.5", drop=("minimax-m3", "glm-5.2"),
                   stale=None):
        (tmp_path / "config.yaml").write_text(
            "model_list:\n"
            "  - model_name: lem-medium\n"
            "    litellm_params:\n"
            f"      model: openai/{model}\n"
            "      api_base: os.environ/OLLAMA_CLOUD_URL\n")
        (tmp_path / "map.yaml").write_text('upgrades:\n  "kimi-k2:1t": "REMOVE"\n')
        catalog = mhc.parse_catalog(_tags())
        snapshot = {k: v for k, v in catalog.items() if k not in drop}
        # `stale` backdates a tag's build IN THE SNAPSHOT, i.e. the catalog re-pointed it since.
        for tag, patch in (stale or {}).items():
            snapshot[tag] = {**snapshot[tag], **patch}
        (tmp_path / "snapshot.json").write_text(mhc.render_snapshot(snapshot, "2026-07-01"))
        return [f"--config={tmp_path / 'config.yaml'}", f"--map={tmp_path / 'map.yaml'}",
                f"--snapshot={tmp_path / 'snapshot.json'}", f"--catalog-fixture={FIXTURES}",
                "--today=2026-07-20"]

    def test_dry_run_reports_everything_and_writes_nothing(self, tmp_path, capsys):
        args = self._workspace(tmp_path)
        before = (tmp_path / "map.yaml").read_text(), (tmp_path / "snapshot.json").read_text()
        code = mhc.main(["--catalog-scan", *args])
        out = capsys.readouterr().out
        assert code == 3  # a configured model is scheduled to retire — alert-worthy
        assert "RETIRING [lem-medium] minimax-m2.5 on 2026-07-31 (11d) -> minimax-m2.7" in out
        assert 'MAP + "minimax-m2.5": "minimax-m2.7"' in out
        assert "NEW TAG minimax-m3" in out and "NEW TAG glm-5.2" in out
        assert mhc.EVAL_TITLE_PREFIX in out and mhc.UPGRADE_TITLE_PREFIX in out
        assert ((tmp_path / "map.yaml").read_text(),
                (tmp_path / "snapshot.json").read_text()) == before

    def test_catalog_apply_writes_the_map_and_snapshot(self, tmp_path, capsys):
        args = self._workspace(tmp_path)
        mhc.main(["--catalog-apply", *args])
        assert mhc.load_map(mhc.load_map_text((tmp_path / "map.yaml").read_text())) == {
            "kimi-k2:1t": "REMOVE", "minimax-m2.5": "minimax-m2.7"}
        snapshot = mhc.load_snapshot_text((tmp_path / "snapshot.json").read_text())
        assert "minimax-m3" in snapshot and "glm-5.2" in snapshot
        # Re-running changes nothing further.
        second = (tmp_path / "map.yaml").read_text(), (tmp_path / "snapshot.json").read_text()
        mhc.main(["--catalog-apply", *args])
        assert ((tmp_path / "map.yaml").read_text(),
                (tmp_path / "snapshot.json").read_text()) == second

    def test_catalog_json_is_machine_readable_and_hides_the_raw_catalog(self, tmp_path, capsys):
        args = self._workspace(tmp_path)
        mhc.main(["--catalog-json", *args])
        plan = json.loads(capsys.readouterr().out)
        assert "_catalog" not in plan
        assert plan["repo_changes"] is True
        assert [n["bare"] for n in plan["notices"]] == ["minimax-m2.5"]
        assert plan["catalog"]["added"] == ["glm-5.2", "minimax-m3"]

    def test_clean_config_is_exit_zero(self, tmp_path, capsys):
        args = self._workspace(tmp_path, model="minimax-m3", drop=())
        assert mhc.main(["--catalog-scan", *args]) == 0
        assert "nothing to do" in capsys.readouterr().out

    def test_a_repointed_configured_tag_is_reported_and_filed(self, tmp_path, capsys, monkeypatch):
        args = self._workspace(tmp_path, model="gpt-oss:20b", drop=(),
                               stale={"gpt-oss:20b": {"size": 9_000_000_000,
                                                      "modified_at": "2026-01-01T00:00:00Z"}})
        code = mhc.main(["--catalog-scan", *args])
        out = capsys.readouterr().out
        assert code == 2  # an evaluation is planned, even though no tag NAME changed
        assert "NEW TAG" not in out
        assert "REPOINTED gpt-oss:20b [lem-medium]" in out
        assert mhc.REPOINT_TITLE_PREFIX in out

        mhc.main(["--catalog-json", *args])
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(capsys.readouterr().out)
        monkeypatch.setattr(mhc, "_catalog_scan", _boom)
        gh = _FakeGitHub()
        monkeypatch.setattr(mhc, "GitHubIssues", lambda repo: gh)
        mhc.main(["--file-issues", f"--plan-file={plan_path}"])
        assert any(t.startswith(mhc.REPOINT_TITLE_PREFIX) for t, _ in gh.created)

    def test_an_unreferenced_tags_repoint_is_not_reported(self, tmp_path, capsys):
        args = self._workspace(tmp_path, model="gpt-oss:20b", drop=(),
                               stale={"glm-5.2": {"size": 9_000_000_000,
                                                  "modified_at": "2026-01-01T00:00:00Z"}})
        code = mhc.main(["--catalog-scan", *args])
        out = capsys.readouterr().out
        assert "REPOINTED" not in out and "nothing to do" in out
        assert code == 2  # the snapshot still needs refreshing — but nobody is paged for it

    def test_a_missing_snapshot_is_seeded_not_reported_as_a_catalog_of_new_tags(self, tmp_path,
                                                                                capsys):
        args = self._workspace(tmp_path, model="minimax-m3", drop=())
        (tmp_path / "snapshot.json").unlink()
        code = mhc.main(["--catalog-scan", *args])
        out = capsys.readouterr().out
        assert "NEW TAG" not in out and "seeding it this run" in out
        assert "REPOINTED" not in out  # no snapshot is no baseline, not a catalog of build swaps
        assert code == 2  # the snapshot itself still needs writing

    def test_file_issues_acts_on_a_saved_plan_without_rescanning(self, tmp_path, capsys,
                                                                 monkeypatch):
        """The orchestrator files from the plan it already read. A second scan would re-fetch
        docs/api-tags, and an outage there would file NOTHING and say nothing — while the snapshot
        PR opened in the same run makes those tags no longer 'new', losing the issue for good."""
        args = self._workspace(tmp_path)
        mhc.main(["--catalog-json", *args])
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(capsys.readouterr().out)

        monkeypatch.setattr(mhc, "_catalog_scan", _boom)  # a re-scan would be a bug
        gh = _FakeGitHub()
        monkeypatch.setattr(mhc, "GitHubIssues", lambda repo: gh)
        assert mhc.main(["--file-issues", f"--plan-file={plan_path}"]) == 3
        titles = [t for t, _ in gh.created]
        assert any(t.startswith(mhc.EVAL_TITLE_PREFIX) for t in titles)
        assert any(t.startswith(mhc.UPGRADE_TITLE_PREFIX) for t in titles)

    def test_a_saved_plan_reads_from_stdin_too(self, tmp_path, capsys, monkeypatch):
        args = self._workspace(tmp_path)
        mhc.main(["--catalog-json", *args])
        monkeypatch.setattr(sys, "stdin", io.StringIO(capsys.readouterr().out))
        monkeypatch.setattr(mhc, "_catalog_scan", _boom)
        gh = _FakeGitHub()
        monkeypatch.setattr(mhc, "GitHubIssues", lambda repo: gh)
        assert mhc.main(["--file-issues", "--plan-file=-"]) == 3
        assert gh.created

    def test_a_saved_plan_can_never_drive_the_snapshot_write(self, tmp_path, capsys):
        """--catalog-json strips the raw catalog, so applying from a saved plan would silently skip
        the snapshot refresh. Refuse instead."""
        args = self._workspace(tmp_path)
        mhc.main(["--catalog-json", *args])
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(capsys.readouterr().out)
        with pytest.raises(SystemExit):
            mhc.main(["--catalog-apply", f"--plan-file={plan_path}"])

    def test_the_repo_config_and_committed_snapshot_are_clean_today(self, tmp_path, capsys):
        """Guards the shipped state: no configured model is scheduled to retire, the committed
        snapshot matches the catalog fixture, and — since the #717 roster refresh dropped the
        trailing minimax-m2.7 — no configured model is a version behind its own family either.
        A fresh run therefore files nothing at all."""
        code = mhc.main(["--catalog-scan", "--no-usage-levels",
                         f"--config={_ROOT / '.litellm' / 'config.yaml'}",
                         f"--map={_ROOT / '.litellm' / 'model_upgrades.yaml'}",
                         f"--snapshot={_ROOT / '.litellm' / 'ollama_catalog_snapshot.json'}",
                         f"--catalog-fixture={FIXTURES}", "--today=2026-07-27"])
        out = capsys.readouterr().out
        assert "RETIRING" not in out and "NEW TAG" not in out and "UPGRADE" not in out
        assert "REPOINTED" not in out  # every configured tag still points at the build we measured
        assert "nothing to do" in out
        assert code == 0
