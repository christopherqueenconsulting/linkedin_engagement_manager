"""Unit tests for the auto seed-comment-on-own-post feature."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_AI = "cqc_lem.utilities.ai.ai_helper"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


class TestGenerateSeedComment:
    def test_returns_stripped_comment(self):
        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content="  What surprised you most here?  "))]
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=resp):
            out = ai_helper.generate_seed_comment("my post body", prof, {"comment_length": "short"})
        assert out == "What surprised you most here?"


class TestPinOwnComment:
    def test_true_when_pin_clicked(self):
        from cqc_lem.app.run_automation import _pin_own_comment
        d = MagicMock(); d.execute_script.side_effect = [True, True]   # opened menu, clicked Pin
        assert _pin_own_comment(d) is True

    def test_false_when_no_overflow(self):
        from cqc_lem.app.run_automation import _pin_own_comment
        d = MagicMock(); d.execute_script.side_effect = [False]        # overflow not found
        assert _pin_own_comment(d) is False


class TestAutoSeedCommentOnPost:
    def test_posts_and_pins(self):
        from cqc_lem.app.run_automation import auto_seed_comment_on_post
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://x/feed/update/urn"), \
             patch(f"{_RA}.get_post_message_from_log_for_user", return_value="my post"), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.generate_seed_comment", return_value="Behind the scenes: … thoughts?"), \
             patch(f"{_RA}.find_first", return_value=MagicMock()), \
             patch(f"{_RA}.post_comment_inline", return_value=True) as pci, \
             patch(f"{_RA}.insert_new_log") as log, \
             patch(f"{_RA}._pin_own_comment", return_value=True), \
             patch(f"{_RA}.quit_gracefully"):
            result = auto_seed_comment_on_post.run(user_id=1, post_id=9)
        pci.assert_called_once()
        log.assert_called_once()
        assert "pinned=True" in result

    def test_bails_without_post_url(self):
        from cqc_lem.app.run_automation import auto_seed_comment_on_post
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value=None), \
             patch(f"{_RA}.get_post_message_from_log_for_user", return_value="x"), \
             patch(f"{_RA}.get_current_profile") as gp:
            result = auto_seed_comment_on_post.run(user_id=1, post_id=9)
        assert "No post URL" in result
        gp.assert_not_called()
