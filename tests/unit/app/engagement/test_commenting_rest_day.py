"""The golden-hour commenting loop opens no session once today's comment budget is spent (issue #2327).

On a pacing rest day `remaining_actions` is 0, yet `automate_commenting` logged in, found the budget
spent inside `comment_on_feed_inline`, and re-queued itself 60 s later — about 16 sessions in ~24
minutes that spent nothing.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_FEED = "cqc_lem.app.engagement.feed"


def _run(allowance, **kwargs):
    from cqc_lem.app.engagement import feed as mod

    with patch(f"{_FEED}.is_commenting_held", return_value=False), \
         patch(f"{_FEED}._comment_allowance_today", return_value=allowance), \
         patch(f"{_FEED}.acquire_run_lock", return_value="tok") as lock, \
         patch(f"{_FEED}.release_run_lock"), \
         patch(f"{_FEED}.get_current_profile",
               return_value=(MagicMock(), MagicMock(), "e", MagicMock())) as session, \
         patch(f"{_FEED}.navigate_to_feed"), \
         patch(f"{_FEED}.comment_on_feed_inline", return_value=2) as walk, \
         patch(f"{_FEED}.quit_gracefully"), \
         patch.object(mod.automate_commenting, "apply_async") as requeue:
        result = mod.automate_commenting.run(user_id=1, **kwargs)
    return result, {"lock": lock, "session": session, "walk": walk, "requeue": requeue}


class TestAutomateCommentingRestDay:
    def test_a_spent_budget_opens_no_session_and_does_not_requeue(self):
        result, m = _run(0, loop_for_duration=900)

        assert result.startswith("Skipped")
        m["session"].assert_not_called()
        m["requeue"].assert_not_called()
        m["lock"].assert_not_called()

    def test_a_positive_allowance_keeps_the_loop(self):
        result, m = _run(3, loop_for_duration=900)

        m["session"].assert_called_once()
        m["walk"].assert_called_once()
        m["requeue"].assert_called_once()
        assert m["requeue"].call_args.kwargs["countdown"] == 60
        assert "Commented on 2 posts" in result

    def test_an_unreadable_allowance_fails_open(self):
        _result, m = _run(None, loop_for_duration=900)

        m["session"].assert_called_once()
        m["requeue"].assert_called_once()


class TestCommentAllowanceToday:
    def test_reads_the_same_budget_the_feed_walk_enforces(self):
        from cqc_lem.app.engagement import feed as mod

        prefs = {"max_comments_per_day": 12}
        with patch(f"{_FEED}.get_engagement_preferences", return_value=prefs), \
             patch(f"{_FEED}.count_comments_today", return_value=4), \
             patch(f"{_FEED}.engagement_caps_from_prefs", return_value={"comment": 12}) as caps, \
             patch(f"{_FEED}.remaining_actions", return_value=0) as remaining:
            assert mod._comment_allowance_today(1) == 0

        caps.assert_called_once_with(prefs)
        remaining.assert_called_once_with(1, mod.ACTION_COMMENT, 12, 4, caps={"comment": 12})

    def test_defaults_the_cap_to_20(self):
        from cqc_lem.app.engagement import feed as mod

        with patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.count_comments_today", return_value=0), \
             patch(f"{_FEED}.engagement_caps_from_prefs", return_value={}), \
             patch(f"{_FEED}.remaining_actions", return_value=7) as remaining:
            assert mod._comment_allowance_today(1) == 7

        assert remaining.call_args.args[2] == 20

    def test_a_read_fault_returns_none(self):
        from cqc_lem.app.engagement import feed as mod

        with patch(f"{_FEED}.get_engagement_preferences", side_effect=RuntimeError("db down")):
            assert mod._comment_allowance_today(1) is None
