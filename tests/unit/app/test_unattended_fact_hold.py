"""Issue #2098: story-bank fact grounding is HARD on newsletters and group posts.

Both surfaces publish unattended — `auto_publish_newsletters` ships a draft edition, and silence
ships a READY group draft — and both shipped invented client work in the 2026-09 audit. The two
fixtures below are the published sentences quoted in that audit.
"""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.platform.db.enums import GroupPostDraftStatus

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_FEED = "cqc_lem.app.engagement.feed"
_NL = "cqc_lem.app.engagement.newsletter"
_DB = "cqc_lem.utilities.db"
_REPO = "cqc_lem.platform.db.repositories.newsletter"

# Newsletter edition 14 (approved, due to auto-publish 2026-09-29).
NL14 = ("I audited a pipeline last quarter where 80% of tokens went to a frontier model. "
        "That's a real number from a real client.")
# Group post draft 6 (published 2026-09-22).
GROUP_POST_6 = ("I've seen a 40% drop in per-call cost by routing 80% of queries to a distilled "
                "LLM. What are you seeing in your pipelines?")
# The author's real material: no client work, no savings percentage.
BANK_ENTRY = {"kind": "artifact", "title": "Built LEM",
              "body": "I built a 3-agent content pipeline on LiteLLM."}


@pytest.fixture(autouse=True)
def _no_severity_env(monkeypatch):
    for name in ("FACT_GROUNDING_SEVERITY", "FACT_GROUNDING_SEVERITY_NEWSLETTER",
                 "FACT_GROUNDING_SEVERITY_GROUP_POST"):
        monkeypatch.delenv(name, raising=False)


class TestSeverity:
    def test_both_surfaces_are_hard(self):
        from cqc_lem.utilities.ai import story_bank
        assert story_bank.fact_grounding_severity("newsletter") == "hard"
        assert story_bank.fact_grounding_severity("group_post") == "hard"

    def test_nl14_is_blocked_on_the_newsletter_surface(self):
        from cqc_lem.utilities.ai import story_bank
        sources = story_bank.fact_sources(BANK_ENTRY)
        assert story_bank.fact_hold_specifics(NL14, sources, "newsletter") == ["80"]

    def test_group_post_6_is_blocked(self):
        from cqc_lem.utilities.ai import story_bank
        sources = story_bank.fact_sources(BANK_ENTRY)
        assert story_bank.fact_hold_specifics(GROUP_POST_6, sources, "group_post") == ["40", "80"]

    def test_a_number_from_the_bank_does_not_hold(self):
        from cqc_lem.utilities.ai import story_bank
        text = "I built a 3-agent pipeline last spring and it still runs every morning."
        assert story_bank.fact_hold_specifics(
            text, story_bank.fact_sources(BANK_ENTRY), "newsletter") == []

    @pytest.mark.parametrize("value", ["warn", "off"])
    def test_ops_can_lower_a_surface_without_a_deploy(self, monkeypatch, value):
        from cqc_lem.utilities.ai import story_bank
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY_NEWSLETTER", value)
        assert story_bank.fact_hold_specifics(NL14, [], "newsletter") == []


class TestUnattendedFactHold:
    def test_the_bank_is_read_only_after_the_cheap_sources_fail(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._story_bank_sources") as bank:
            assert ai_helper.unattended_fact_hold("No numbers here at all.", "newsletter", 1,
                                                  "brief") == []
        bank.assert_not_called()

    def test_a_bank_number_releases_the_draft(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_DB}.get_story_bank_entries",
                   return_value=[{"title": "Audit", "body": "80% of tokens went to one model"}]):
            assert ai_helper.unattended_fact_hold(NL14, "newsletter", 1) == []

    def test_an_unreadable_bank_still_holds(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_DB}.get_story_bank_entries", side_effect=RuntimeError("db down")), \
             patch(f"{_AI}.log_warning") as warned:
            assert ai_helper.unattended_fact_hold(GROUP_POST_6, "group_post", 1) == ["40", "80"]
        assert warned.call_args.kwargs["action_type"] == "group_post"


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


class TestNewsletterGeneration:
    def _generate(self, body, **kwargs):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        payload = json.dumps({"title": "Where the tokens go", "subtitle": "S", "body": body})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)), \
             patch(f"{_AI}._humanize_text", side_effect=lambda text, **_: text), \
             patch(f"{_AI}._humanize_title", side_effect=lambda text, **_: text), \
             patch(f"{_AI}.current_llm_attribution", return_value=(7, "newsletter")), \
             patch(f"{_DB}.get_story_bank_entries", return_value=[BANK_ENTRY]):
            return ai_helper.generate_newsletter_edition(prof, **kwargs)

    def test_nl14_edition_carries_a_hold(self):
        out = self._generate(NL14)
        assert out["fact_hold"] == ["80"]

    def test_research_findings_never_ground_a_first_person_claim(self):
        out = self._generate(NL14, research={"findings": "80% of tokens go to frontier models."})
        assert out["fact_hold"] == ["80"]

    def test_the_authors_blog_grounds_it(self):
        out = self._generate(NL14, blog_content="Last quarter 80% of tokens hit a frontier model.")
        assert out["fact_hold"] == []

    def test_the_profile_json_grounds_it_when_no_synthesis_exists(self):
        """With no synthesis the writer is shown the full profile, so its numbers are the author's."""
        with patch(f"{_AI}._voice_reference", return_value="80% of tokens went to a frontier model"):
            out = self._generate(NL14)
        assert out["fact_hold"] == []


class TestGroupPostDraft:
    def _draft(self, text, synthesis="brief", profile_json="{}"):
        from cqc_lem.app.engagement.feed import auto_draft_group_post
        profile = MagicMock()
        profile.model_dump_json.return_value = profile_json
        with patch(f"{_FEED}.get_open_group_post_draft", return_value=None), \
             patch(f"{_FEED}.load_profile_for_user", return_value=profile), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_or_create_profile_synthesis", return_value=synthesis), \
             patch(f"{_FEED}.generate_group_post", return_value=text), \
             patch(f"{_DB}.get_story_bank_entries", return_value=[BANK_ENTRY]), \
             patch(f"{_FEED}.create_group_post_draft", return_value=6) as create:
            result = auto_draft_group_post.run(user_id=1, group_id="g1", group_name="AI")
        return result, create

    def test_group_post_6_is_stored_skipped_for_approval(self):
        result, create = self._draft(GROUP_POST_6)
        assert result == "Held group post 6 for approval"
        assert create.call_args.kwargs["status"] == GroupPostDraftStatus.SKIPPED

    def test_the_profile_json_grounds_it_when_no_synthesis_exists(self):
        result, create = self._draft(
            GROUP_POST_6, synthesis=None,
            profile_json='{"summary": "a 40% drop in per-call cost routing 80% of queries"}')
        assert result == "Drafted group post 6"
        assert create.call_args.kwargs["status"] == GroupPostDraftStatus.READY

    def test_a_grounded_draft_stays_ready(self):
        result, create = self._draft("What is the one pipeline check you never skip?")
        assert result == "Drafted group post 6"
        assert create.call_args.kwargs["status"] == GroupPostDraftStatus.READY


class TestNewsletterPublishGates:
    def test_the_due_query_excludes_a_held_draft(self):
        from cqc_lem.platform.db.repositories.newsletter import get_editions_due_to_publish
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor
        with patch(f"{_REPO}.db_cursor", return_value=ctx):
            get_editions_due_to_publish(datetime(2026, 9, 29, 13, 0))
        sql = cursor.execute.call_args.args[0]
        assert "e.fact_hold IS NULL" in sql

    def test_create_stores_the_hold_in_the_same_insert(self):
        from cqc_lem.platform.db.repositories.newsletter import create_newsletter_edition
        cursor = MagicMock(lastrowid=14)
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor
        with patch(f"{_REPO}.db_cursor", return_value=ctx):
            assert create_newsletter_edition(1, "T", "S", "B", datetime(2026, 9, 29),
                                             fact_hold=["80"]) == 14
        assert cursor.execute.call_args.args[1][-1] == "80"

    def test_set_hold_clears_with_null(self):
        from cqc_lem.platform.db.repositories.newsletter import set_edition_fact_hold
        cursor = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor
        with patch(f"{_REPO}.db_cursor", return_value=ctx):
            assert set_edition_fact_hold(14, 1, []) is True
        assert cursor.execute.call_args.args[1] == (None, 14, 1)

    def test_set_hold_fails_soft(self):
        import mysql.connector

        from cqc_lem.platform.db.repositories.newsletter import set_edition_fact_hold
        with patch(f"{_REPO}.db_cursor", side_effect=mysql.connector.Error("down")), \
             patch(f"{_REPO}.log_error"):
            assert set_edition_fact_hold(14, 1, ["80"]) is False

    def test_a_held_draft_waits_even_under_auto_publish(self):
        from cqc_lem.app import run_scheduler as rs
        pending = [{"id": 14, "status": "draft", "fact_hold": "80",
                    "scheduled_for": datetime(2026, 9, 29, 13, 0)}]
        dispatch = MagicMock()
        with patch(f"{_DB}.get_pending_newsletter_editions", return_value=pending), \
             patch(f"{_DB}.get_newsletter_settings", return_value={"auto_publish_newsletters": True}):
            assert rs._publish_next_due_edition_for_user(1, datetime(2026, 9, 30), dispatch) == 0
        dispatch.assert_not_called()

    def test_approving_a_held_edition_releases_it(self):
        from cqc_lem.app import run_scheduler as rs
        pending = [{"id": 14, "status": "approved", "fact_hold": "80",
                    "scheduled_for": datetime(2026, 9, 29, 13, 0)}]
        dispatch = MagicMock()
        with patch(f"{_DB}.get_pending_newsletter_editions", return_value=pending), \
             patch(f"{_DB}.get_newsletter_settings", return_value={"auto_publish_newsletters": True}):
            assert rs._publish_next_due_edition_for_user(1, datetime(2026, 9, 30), dispatch) == 1
        dispatch.assert_called_once_with(14)

    def test_the_worker_refuses_a_held_draft(self):
        from cqc_lem.app.engagement.newsletter import auto_publish_edition
        edition = {"id": 14, "user_id": 1, "status": "draft", "fact_hold": "80", "body": NL14}
        with patch(f"{_NL}.get_newsletter_edition", return_value=edition), \
             patch(f"{_NL}.get_newsletter_settings", return_value={"auto_publish_newsletters": True}), \
             patch(f"{_NL}.get_current_profile") as gp:
            result = auto_publish_edition.run(edition_id=14)
        assert "held for approval" in result
        gp.assert_not_called()

    def test_generate_and_publish_path_refuses_a_hold(self):
        from cqc_lem.app.engagement.newsletter import auto_publish_newsletter_edition
        driver = MagicMock()
        with patch(f"{_NL}.get_newsletter_settings", return_value={"enabled": True}), \
             patch(f"{_NL}.get_current_profile", return_value=(driver, MagicMock(), "e", MagicMock())), \
             patch(f"{_NL}.resolve_blog_source", return_value=None), \
             patch(f"{_NL}.generate_newsletter_edition",
                   return_value={"title": "T", "body": NL14, "fact_hold": ["80"]}), \
             patch(f"{_NL}._fill_and_publish_article") as fill, \
             patch(f"{_NL}.quit_gracefully"):
            result = auto_publish_newsletter_edition.run(user_id=1)
        assert "held" in result
        fill.assert_not_called()
        driver.get.assert_not_called()


class TestSchedulerPersistsTheHold:
    def test_topup_stores_the_hold_and_says_the_draft_waits_on_the_author(self):
        from tests.unit.app.test_newsletter_publish import _run_generate, _settings
        _, create, notify = _run_generate(
            settings=_settings(auto_publish_newsletters=True), pending=0,
            edition={"title": "T", "subtitle": "S", "body": NL14, "fact_hold": ["80"]})
        assert create.call_args.kwargs["fact_hold"] == ["80"]
        assert notify.call_args.kwargs["auto_publish"] is False

    def test_regenerate_regrades_the_hold(self):
        from tests.unit.app.test_newsletter_publish import _run_regenerate
        _, _, captured = _run_regenerate(
            edition={"id": 14, "user_id": 1, "status": "approved", "subject": "Tokens"},
            new_ed={"title": "T", "subtitle": "S", "subject": "Tokens", "body": "B",
                    "fact_hold": []})
        captured["hold"].assert_called_once_with(14, 1, [])

    def test_regenerate_refuses_to_land_a_held_body_it_could_not_hold(self):
        from tests.unit.app.test_newsletter_publish import _run_regenerate
        result, upd, _ = _run_regenerate(
            edition={"id": 14, "user_id": 1, "status": "draft", "subject": "Tokens"},
            new_ed={"title": "T", "subtitle": "S", "subject": "Tokens", "body": NL14,
                    "fact_hold": ["80"]},
            hold_stored=False)
        assert "not saved" in result
        upd.assert_not_called()

    def test_regenerate_proceeds_past_a_failed_release(self):
        from tests.unit.app.test_newsletter_publish import _run_regenerate
        result, upd, _ = _run_regenerate(
            edition={"id": 14, "user_id": 1, "status": "draft", "subject": "Tokens"},
            new_ed={"title": "T", "subtitle": "S", "subject": "Tokens", "body": "B",
                    "fact_hold": []},
            hold_stored=False)
        assert "Regenerated" in result
        upd.assert_called_once()
