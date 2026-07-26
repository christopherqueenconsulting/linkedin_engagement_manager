"""Unit tests for the golden-hour second wave (issue #622 / G7) — the 6–8h self-comment that adds
a substantive insight, and the self-comment cap that keeps it from colliding with the #344 seed."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_AI = "cqc_lem.utilities.ai.ai_helper"

_URL = "https://www.linkedin.com/feed/update/urn:li:ugcPost:7479519458164695040/"


@pytest.fixture(autouse=True)
def _second_wave_on(monkeypatch):
    monkeypatch.delenv("SECOND_WAVE_COMMENT_ENABLED", raising=False)
    monkeypatch.delenv("SELF_COMMENT_MAX_PER_POST", raising=False)


@pytest.fixture
def _happy_path():
    """Every collaborator of the task stubbed for the success case; individual tests patch over."""
    with patch(f"{_RA}.is_automation_paused", return_value=False), \
         patch(f"{_RA}.get_post_age_minutes", return_value=9 * 60), \
         patch(f"{_RA}.get_post_url_from_log_for_user", return_value=_URL), \
         patch(f"{_RA}.count_user_comments_on_post_url", return_value=1), \
         patch(f"{_RA}.get_post_content", return_value="the real post body"), \
         patch(f"{_RA}.load_profile_for_user", return_value=MagicMock()), \
         patch(f"{_RA}.get_engagement_preferences", return_value={}), \
         patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
         patch(f"{_RA}.get_story_bank_entries", return_value=[]), \
         patch(f"{_RA}.get_recent_comment_texts", return_value=[]), \
         patch(f"{_RA}.record_story_bank_use") as story_use, \
         patch(f"{_RA}.record_action") as action, \
         patch(f"{_RA}._record_golden_hour_report") as report, \
         patch(f"{_RA}.insert_new_log") as log:
        yield {"story_use": story_use, "action": action, "report": report, "log": log}


class TestSecondWaveComment:
    def test_posts_via_api_and_reports(self, _happy_path):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.generate_second_wave_comment", return_value="The number behind it: 40%."), \
             patch(f"{_RA}.comment_on_linkedin_post", return_value="urn:li:comment:(x,2)") as api:
            result = auto_second_wave_comment.run(user_id=1, post_id=9)
        assert api.call_args.args[1] == "urn:li:ugcPost:7479519458164695040"
        assert api.call_args.args[2] == "The number behind it: 40%."
        _happy_path["log"].assert_called_once()
        _happy_path["action"].assert_called_once()          # tracked, not charged to the envelope
        assert _happy_path["report"].call_args.kwargs["phase"] == "second_wave"
        assert "posted via API" in result

    def test_no_selenium_session_is_ever_opened(self, _happy_path):
        """Like the seed comment, the second wave is API-only — that is what makes it 429-immune."""
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.generate_second_wave_comment", return_value="insight"), \
             patch(f"{_RA}.comment_on_linkedin_post", return_value="urn:li:comment:(x,2)"), \
             patch(f"{_RA}.get_current_profile") as gcp:
            auto_second_wave_comment.run(user_id=1, post_id=9)
        gcp.assert_not_called()

    def test_self_comment_cap_blocks_a_third_comment(self, _happy_path):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.count_user_comments_on_post_url", return_value=2), \
             patch(f"{_RA}.generate_second_wave_comment") as gen, \
             patch(f"{_RA}.comment_on_linkedin_post") as api:
            result = auto_second_wave_comment.run(user_id=1, post_id=9)
        assert "cap reached" in result
        gen.assert_not_called()   # no LLM spend once the post is full
        api.assert_not_called()

    def test_cap_is_env_tunable(self, _happy_path, monkeypatch):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        monkeypatch.setenv("SELF_COMMENT_MAX_PER_POST", "3")
        with patch(f"{_RA}.count_user_comments_on_post_url", return_value=2), \
             patch(f"{_RA}.generate_second_wave_comment", return_value="insight"), \
             patch(f"{_RA}.comment_on_linkedin_post", return_value="urn:li:comment:(x,3)") as api:
            auto_second_wave_comment.run(user_id=1, post_id=9)
        api.assert_called_once()

    def test_a_draft_that_fails_the_gate_ships_nothing(self, _happy_path):
        """generate_second_wave_comment returns None when no draft clears the quality contract —
        an empty second wave beats a filler one."""
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.generate_second_wave_comment", return_value=None), \
             patch(f"{_RA}.comment_on_linkedin_post") as api, \
             patch(f"{_RA}.log_warning") as warn:
            result = auto_second_wave_comment.run(user_id=1, post_id=9)
        api.assert_not_called()
        _happy_path["log"].assert_not_called()
        warn.assert_called_once()
        assert "quality gate" in result
        assert _happy_path["report"].call_args.kwargs["phase"] == "second_wave"

    def test_stands_down_while_automation_is_paused(self, _happy_path):
        """The second wave is discretionary amplification, so the #629 suppression pause stops it —
        unlike the seed comment, which is part of publishing."""
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.is_automation_paused", return_value=True), \
             patch(f"{_RA}.automation_pause_reason", return_value="suppression:user:1"), \
             patch(f"{_RA}.generate_second_wave_comment") as gen, \
             patch(f"{_RA}.comment_on_linkedin_post") as api:
            result = auto_second_wave_comment.run(user_id=1, post_id=9)
        assert "suppression" in result
        gen.assert_not_called()
        api.assert_not_called()

    def test_re_arms_itself_until_the_post_is_due(self, _happy_path):
        """The 6-8h wait is served in hops: a single long countdown sits unacked past the broker's
        visibility timeout and gets redelivered — which would post several self-comments."""
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities import golden_hour as g
        with patch(f"{_RA}.get_post_age_minutes", return_value=30.0), \
             patch(f"{_RA}.generate_second_wave_comment") as gen, \
             patch(f"{_RA}.comment_on_linkedin_post") as api, \
             patch.object(ra.auto_second_wave_comment, "apply_async") as rearm:
            result = ra.auto_second_wave_comment.run(user_id=1, post_id=9)
        assert "re-armed" in result
        gen.assert_not_called()
        api.assert_not_called()
        assert rearm.call_args.kwargs["kwargs"] == {"user_id": 1, "post_id": 9}
        assert 0 < rearm.call_args.kwargs["countdown"] <= g.second_wave_hop_cap_seconds()

    def test_a_pause_during_the_wait_does_not_cost_the_second_wave(self, _happy_path):
        """The pause is checked at the DUE time, not on every hop — a pause that lifts in the
        meantime must not silently cancel the amplification."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.get_post_age_minutes", return_value=30.0), \
             patch(f"{_RA}.is_automation_paused", return_value=True), \
             patch.object(ra.auto_second_wave_comment, "apply_async") as rearm:
            result = ra.auto_second_wave_comment.run(user_id=1, post_id=9)
        assert "re-armed" in result
        rearm.assert_called_once()

    def test_an_unknown_publish_time_acts_rather_than_hopping_forever(self, _happy_path):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.get_post_age_minutes", return_value=None), \
             patch(f"{_RA}.generate_second_wave_comment", return_value="insight"), \
             patch(f"{_RA}.comment_on_linkedin_post", return_value="urn:li:comment:(x,2)") as api, \
             patch.object(ra.auto_second_wave_comment, "apply_async") as rearm:
            ra.auto_second_wave_comment.run(user_id=1, post_id=9)
        api.assert_called_once()
        rearm.assert_not_called()

    def test_kill_switch(self, _happy_path, monkeypatch):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        monkeypatch.setenv("SECOND_WAVE_COMMENT_ENABLED", "false")
        with patch(f"{_RA}.comment_on_linkedin_post") as api:
            result = auto_second_wave_comment.run(user_id=1, post_id=9)
        assert "disabled" in result
        api.assert_not_called()

    def test_bails_without_a_post_url(self, _happy_path):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value=None), \
             patch(f"{_RA}.comment_on_linkedin_post") as api:
            result = auto_second_wave_comment.run(user_id=1, post_id=9)
        assert "No post URL" in result
        api.assert_not_called()

    def test_bails_when_urn_underivable(self, _happy_path):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://example.com/x"), \
             patch(f"{_RA}.comment_on_linkedin_post") as api:
            result = auto_second_wave_comment.run(user_id=1, post_id=9)
        assert "object URN" in result
        api.assert_not_called()

    def test_bails_without_post_content(self, _happy_path):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.get_post_content", return_value=None), \
             patch(f"{_RA}.get_post_message_from_log_for_user", return_value=None), \
             patch(f"{_RA}.comment_on_linkedin_post") as api:
            result = auto_second_wave_comment.run(user_id=1, post_id=9)
        assert "No post content" in result
        api.assert_not_called()

    def test_records_the_story_entry_it_used(self, _happy_path):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        entry = {"id": 4, "kind": "number", "title": "cut build time",
                 "body": "we cut CI from 22 to 8 minutes", "active": True}
        with patch(f"{_RA}.get_post_content", return_value="what our CI pipeline taught us"), \
             patch(f"{_RA}.get_story_bank_entries", return_value=[entry]), \
             patch(f"{_RA}.generate_second_wave_comment", return_value="insight") as gen, \
             patch(f"{_RA}.comment_on_linkedin_post", return_value="urn:li:comment:(x,2)"):
            auto_second_wave_comment.run(user_id=1, post_id=9)
        # The bank entry rides into the prompt as the ONLY permitted personal specifics...
        assert "22 to 8 minutes" in gen.call_args.kwargs["story_directive"]
        # ...and its rotation counter moves, so the next post doesn't reuse the same anecdote.
        _happy_path["story_use"].assert_called_once_with(1, 4)

    def test_empty_bank_uses_the_no_fabrication_fallback(self, _happy_path):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.get_story_bank_entries", return_value=[]), \
             patch(f"{_RA}.generate_second_wave_comment", return_value="insight") as gen, \
             patch(f"{_RA}.comment_on_linkedin_post", return_value="urn:li:comment:(x,2)"):
            auto_second_wave_comment.run(user_id=1, post_id=9)
        directive = gen.call_args.kwargs["story_directive"]
        assert directive and "invent" in directive.lower()
        _happy_path["story_use"].assert_not_called()

    def test_a_failed_api_post_is_not_logged_as_a_comment(self, _happy_path):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.generate_second_wave_comment", return_value="insight"), \
             patch(f"{_RA}.comment_on_linkedin_post", return_value=None):
            result = auto_second_wave_comment.run(user_id=1, post_id=9)
        assert "failed to post" in result
        _happy_path["log"].assert_not_called()   # never claim a comment the API rejected

    def test_errors_are_swallowed_into_a_result_string(self, _happy_path):
        from cqc_lem.app.run_automation import auto_second_wave_comment
        with patch(f"{_RA}.load_profile_for_user", side_effect=RuntimeError("boom")), \
             patch(f"{_RA}.log_error") as err:
            result = auto_second_wave_comment.run(user_id=1, post_id=9)
        assert "error" in result.lower()
        err.assert_called_once()


class TestSecondWaveDispatch:
    def test_publishing_schedules_the_second_wave_within_one_hop(self):
        """post_to_linkedin dispatches it alongside the seed comment and the golden-hour sweeps —
        but only for one hop: the task re-arms itself the rest of the way to its 6-8h offset, so the
        broker never holds a message past its visibility timeout (which would redeliver it)."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.get_post_content", return_value="body"), \
             patch(f"{_RA}.get_post_status", return_value="approved"), \
             patch(f"{_RA}.get_user_password_pair_by_id", return_value=("u@example.com", "pw")), \
             patch(f"{_RA}.get_post_type", return_value=ra.PostType.TEXT), \
             patch(f"{_RA}.get_engagement_preferences", return_value={"reply_check_mode": "off"}), \
             patch(f"{_RA}.split_link_for_first_comment", return_value=("body", [])), \
             patch(f"{_RA}.share_on_linkedin", return_value="urn:li:share:1"), \
             patch(f"{_RA}.update_db_post_status"), \
             patch(f"{_RA}.insert_new_log"), \
             patch(f"{_RA}.auto_seed_comment_on_post"), \
             patch(f"{_RA}.auto_second_wave_comment") as second, \
             patch(f"{_RA}.get_post_video_url", return_value=None):
            ra.post_to_linkedin.run(user_id=1, post_id=9)
        from cqc_lem.utilities import golden_hour as g
        countdown = second.apply_async.call_args.kwargs["countdown"]
        assert 0 < countdown <= g.second_wave_hop_cap_seconds()
        assert second.apply_async.call_args.kwargs["kwargs"] == {"user_id": 1, "post_id": 9}

    def test_kill_switch_stops_the_dispatch(self, monkeypatch):
        from cqc_lem.app import run_automation as ra
        monkeypatch.setenv("SECOND_WAVE_COMMENT_ENABLED", "false")
        with patch(f"{_RA}.get_post_content", return_value="body"), \
             patch(f"{_RA}.get_post_status", return_value="approved"), \
             patch(f"{_RA}.get_user_password_pair_by_id", return_value=("u@example.com", "pw")), \
             patch(f"{_RA}.get_post_type", return_value=ra.PostType.TEXT), \
             patch(f"{_RA}.get_engagement_preferences", return_value={"reply_check_mode": "off"}), \
             patch(f"{_RA}.split_link_for_first_comment", return_value=("body", [])), \
             patch(f"{_RA}.share_on_linkedin", return_value="urn:li:share:1"), \
             patch(f"{_RA}.update_db_post_status"), \
             patch(f"{_RA}.insert_new_log"), \
             patch(f"{_RA}.auto_seed_comment_on_post"), \
             patch(f"{_RA}.auto_second_wave_comment") as second, \
             patch(f"{_RA}.get_post_video_url", return_value=None):
            ra.post_to_linkedin.run(user_id=1, post_id=9)
        second.apply_async.assert_not_called()


class TestGenerateSecondWaveComment:
    def _profile(self):
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        return prof

    def _response(self, text):
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content=text))]
        return resp

    def test_a_passing_draft_is_returned(self):
        from cqc_lem.utilities.ai import ai_helper
        draft = ("The 40% figure in the post came from one migration, not the whole fleet. "
                 "We measured it over six weeks and the second cluster only moved 9%. "
                 "Which part of your stack would you measure first?")
        with patch(f"{_AI}._call_llm", return_value=self._response(draft)), \
             patch(f"{_AI}._humanize_text", side_effect=lambda t, **k: t):
            out = ai_helper.generate_second_wave_comment(
                "our migration cut build cost 40% across the fleet in six weeks",
                self._profile(), recent_comments=[])
        assert out == draft

    def test_filler_draft_is_rejected_rather_than_shipped(self):
        """The second wave runs the SAME quality contract as a feed comment: a 'thanks for reading'
        one-liner never reaches the post."""
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=self._response("Great post! Thanks for reading.")), \
             patch(f"{_AI}._humanize_text", side_effect=lambda t, **k: t), \
             patch(f"{_AI}.log_warning"):
            out = ai_helper.generate_second_wave_comment("our migration cut build cost 40%",
                                                         self._profile(), recent_comments=[])
        assert out is None

    def test_story_directive_rides_into_the_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        draft = ("The post skipped the caveat: our CI drop from 22 to 8 minutes only held after we "
                 "pinned the cache. It regressed twice before that. What broke your cache first?")
        with patch(f"{_AI}._call_llm", return_value=self._response(draft)) as llm, \
             patch(f"{_AI}._humanize_text", side_effect=lambda t, **k: t):
            ai_helper.generate_second_wave_comment("we cut CI build minutes", self._profile(),
                                                   story_directive="\nSTORY: cut CI 22→8 minutes\n",
                                                   recent_comments=[])
        user_message = llm.call_args.kwargs["messages"][1]["content"]
        assert "cut CI 22→8 minutes" in user_message
