"""`lem_get_setup_checklist` and the `align_my_account` prompt (#2802)."""

import re
from typing import Any

import anyio
import pytest

from cqc_lem.api.mcp import server, tools as registry
from cqc_lem.utilities.db import DEFAULT_POSTING_DAYS, DEFAULT_POSTS_PER_WEEK

from ._admitted import admitted, rpc
from .test_mcp_tool_runners import FakeLoopback, _down

pytestmark = pytest.mark.unit

_READY = {"ready": True, "items": [
    {"key": "linkedin_oauth", "label": "LinkedIn connected (posting)", "ok": True, "required": True,
     "hint": "Connect LinkedIn in your account."},
    {"key": "location", "label": "Login location set", "ok": False, "required": False,
     "hint": "Set your login location."}]}
_PREFS_DONE = {"icp_definition": {"roles": ["cto"]}, "focus_topics": ["ai"], "tone": "warm",
               "comment_style": None, "posting_days": [0, 2], "posts_per_week": 4,
               "session_windows": [{"start": "09:00", "end": "11:00"}],
               "session_timezone": "Europe/Paris"}
_ALIGNMENT_KEYS = ("icp_defined", "focus_topics", "voice", "story_bank", "lead_magnet", "cadence",
                   "session_windows", "newsletter_topic")
_PREFS = "/user/engagement-preferences"
_STORIES = "/user/story-bank"


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    """The rollout flag on (env fallback) and the in-memory Redis behind every limit."""
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")


def _answers() -> dict[str, Any]:
    return {"/user/account-readiness": _READY, _PREFS: dict(_PREFS_DONE),
            _STORIES: {"entries": [{"id": 1}], "target_entries": 5},
            "/user/lead-magnet": {"enabled": True, "keyword": "GUIDE", "message": "m"},
            "/user/newsletter-settings": {"enabled": False, "topic": None},
            "/user/timezone": {"timezone": "America/New_York"}}


async def _run(answers: dict[str, Any]) -> dict[str, Any]:
    out = await registry._setup_checklist(FakeLoopback(answers), registry.NoArgs())
    return {"raw": out, **{i["key"]: i for i in out["items"]}}


async def test_shape_every_item_has_the_contract_keys() -> None:
    out = (await _run(_answers()))["raw"]
    assert out["items"]
    for item in out["items"]:
        assert set(item) >= {"key", "label", "ok", "why_it_matters", "fix"}
        assert item["ok"] in (True, False, None)
        fix = item["fix"]
        assert ("tool" in fix and fix["fields"]) or "open_in_lem" in fix
        if "tool" in fix:
            assert fix["tool"] == "lem_update_settings"
    assert out["summary"]["missing"] == sum(1 for i in out["items"] if i["ok"] is False)


async def test_a_fully_set_up_account_has_no_alignment_gap() -> None:
    got = await _run(_answers())
    for key in _ALIGNMENT_KEYS:
        assert got[key]["ok"] is True, key
    assert got["readiness_linkedin_oauth"]["ok"] is True
    assert got["readiness_location"]["ok"] is False


async def test_each_gap_reads_false() -> None:
    got = await _run({
        **_answers(),
        _PREFS: {"icp_definition": None, "focus_topics": [], "tone": " ", "comment_style": None,
                 "posting_days": list(DEFAULT_POSTING_DAYS), "posts_per_week": DEFAULT_POSTS_PER_WEEK,
                 "session_windows": None, "session_timezone": None},
        _STORIES: {"entries": []},
        "/user/lead-magnet": {"enabled": False, "keyword": ""},
        "/user/newsletter-settings": {"enabled": True, "topic": "  "}})
    for key in _ALIGNMENT_KEYS:
        assert got[key]["ok"] is False, key
    assert got["story_bank"]["count"] == 0


async def test_voice_is_set_by_either_tone_or_style() -> None:
    prefs = {**_PREFS_DONE, "tone": None, "comment_style": "plain"}
    assert (await _run({**_answers(), _PREFS: prefs}))["voice"]["ok"] is True


async def test_one_changed_cadence_value_is_not_the_default() -> None:
    prefs = {**_PREFS_DONE, "posting_days": list(DEFAULT_POSTING_DAYS),
             "posts_per_week": DEFAULT_POSTS_PER_WEEK + 1}
    assert (await _run({**_answers(), _PREFS: prefs}))["cadence"]["ok"] is True


async def test_an_unreadable_preferences_section_is_unknown_never_missing() -> None:
    got = await _run({**_answers(), _PREFS: _down()})
    for key in ("icp_defined", "focus_topics", "voice", "cadence", "session_windows"):
        assert got[key]["ok"] is None, key
    assert got["lead_magnet"]["ok"] is True
    assert got["raw"]["summary"]["unknown"] == 5


async def test_each_other_unreadable_section_is_unknown() -> None:
    got = await _run({**_answers(), _STORIES: _down(), "/user/lead-magnet": _down(),
                      "/user/newsletter-settings": _down(), "/user/account-readiness": _down()})
    assert [got[k]["ok"] for k in ("story_bank", "lead_magnet", "newsletter_topic")] == [None] * 3
    assert got["account_readiness"]["ok"] is None and "readiness_location" not in got


async def test_windows_without_any_readable_timezone_is_unknown() -> None:
    prefs = {**_PREFS_DONE, "session_timezone": None}
    got = await _run({**_answers(), _PREFS: prefs, "/user/timezone": _down()})
    assert got["session_windows"]["ok"] is None
    assert (await _run({**_answers(), _PREFS: prefs}))["session_windows"]["ok"] is True


async def test_story_bank_counts_only_verified_entries_when_the_api_says_so() -> None:
    entries = [{"verified": True}, {"verified": False}, {"verified": False}]
    got = await _run({**_answers(), _STORIES: {"entries": entries}})
    assert got["story_bank"]["count"] == 1 and got["story_bank"]["ok"] is True
    assert not any("every entry is counted" in n for n in got["raw"]["notes"])
    only_drafts = await _run({**_answers(), _STORIES: {"entries": entries[1:]}})
    assert only_drafts["story_bank"]["ok"] is False


async def test_without_a_verified_flag_all_entries_count_and_it_is_noted() -> None:
    got = await _run(_answers())
    assert got["story_bank"]["count"] == 1
    assert any("every entry is counted" in n for n in got["raw"]["notes"])


async def test_no_user_or_third_party_text_is_returned() -> None:
    canary = "zz-private-text-91"
    got = await _run({
        **_answers(),
        _STORIES: {"entries": [{"title": canary, "body": canary}]},
        "/user/lead-magnet": {"enabled": True, "keyword": canary, "message": canary},
        "/user/newsletter-settings": {"enabled": True, "topic": canary},
        _PREFS: {**_PREFS_DONE, "tone": canary, "focus_topics": [canary]},
        "/user/account-readiness": {"items": [{"key": "k", "label": "L", "ok": True,
                                               "hint": "h" * 500}]}})
    assert canary not in str(got["raw"])
    assert len(got["readiness_k"]["why_it_matters"]) == 200


async def test_malformed_readiness_rows_are_skipped_and_a_non_bool_is_unknown() -> None:
    got = await _run({**_answers(), "/user/account-readiness": {"items": [
        "junk", {"label": "no key"}, {"key": "a", "ok": "yes", "hint": None}]}})
    assert got["readiness_a"]["ok"] is None
    assert got["readiness_a"]["label"] == "a"
    assert "readiness_junk" not in got


def test_the_spec_declares_exactly_the_gets_the_runner_makes() -> None:
    spec = registry.TOOLS_BY_NAME["lem_get_setup_checklist"]
    assert spec.scopes == registry.BOTH_SCOPES and not spec.write
    assert spec.args_model is registry.NoArgs
    lb = FakeLoopback(_answers())

    async def _go() -> None:
        await spec.run(lb, registry.NoArgs())

    anyio.run(_go)
    assert {("GET", path) for path, _ in lb.calls} == set(spec.calls)


# --- the prompt -------------------------------------------------------------------------------------

_TOOL_NAME = re.compile(r"\blem_[a-z_]+\b")
_CONDITIONAL = re.compile(r"\bif (lem_[a-z_]+) is available\b")
# Tools a later sub-issue adds: the prompt may name them only as "if NAME is available".
_FUTURE_TOOLS = {"lem_request_settings_change", "lem_add_story_draft"}


def _prompt_text() -> str:
    return server.align_my_account()


def test_unconditional_tool_names_exist_in_the_registry() -> None:
    text = _prompt_text()
    conditional = set(_CONDITIONAL.findall(text))
    names = set(_TOOL_NAME.findall(text))
    assert {"lem_get_setup_checklist", "lem_open_in_lem"} <= names
    for name in names - conditional:
        assert name in registry.TOOLS_BY_NAME, name
    assert conditional - set(registry.TOOLS_BY_NAME) <= _FUTURE_TOOLS


def test_the_prompt_keeps_its_safety_instructions() -> None:
    text = _prompt_text()
    for needle in ("goals, ideal customer, voice, topics, cadence, assets", "never invent",
                   "ONE batched", "diff", "acknowledge_risk", "never applied by you",
                   "unverified drafts", "third_party", "never say it is applied"):
        assert needle in text, needle
    assert text.rindex("lem_get_setup_checklist") > text.index("lem_update_settings")


@pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
def test_the_prompt_is_listed_and_served_to_both_scopes(api_client: Any, resolver: Any,
                                                        enabled: None, scope: str) -> None:
    with admitted(resolver, scope):
        names = {p["name"] for p in rpc(api_client, "prompts/list").json()["result"]["prompts"]}
        got = rpc(api_client, "prompts/get", {"name": "align_my_account"}).json()
    assert "align_my_account" in names
    assert got["result"]["messages"][0]["content"]["text"] == _prompt_text()


def test_the_prompt_is_not_an_outreach_only_prompt() -> None:
    assert "align_my_account" not in server.OUTREACH_PROMPTS
