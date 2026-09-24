"""The pre-post window runs each lane at most once per post (issue #2093).

Both pre-post lanes used to self-requeue every `future_forward` seconds across their window, and
every pass opened a fresh LinkedIn session — a new session every ~85 s, 41 in one 30-minute bucket.
Viewers were revisited 5-9 times in one window, because the per-viewer dedupe only counted a
SUCCESS row written after the attempt. And "Daily comment budget spent (cap 20)" printed while the
pacing draw was the real limit.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_WIN = "cqc_lem.utilities.engagement_window"
_FEED = "cqc_lem.app.engagement.feed"
_OUT = "cqc_lem.app.engagement.outreach"


class _FakeRedis:
    """Just enough of `SET NX EX` to exercise a claim twice."""

    def __init__(self):
        self.keys: dict = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.fixture
def redis():
    client = _FakeRedis()
    with patch(f"{_WIN}.shared_redis_client", return_value=client):
        yield client


class TestClaimPrePostLane:
    def test_the_second_claim_for_the_same_post_and_lane_loses(self, redis):
        from cqc_lem.utilities.engagement_window import PRE_POST_TASK_COMMENTING, claim_pre_post_lane

        assert claim_pre_post_lane(1, 42, PRE_POST_TASK_COMMENTING) is True
        assert claim_pre_post_lane(1, 42, PRE_POST_TASK_COMMENTING) is False

    def test_lanes_and_posts_claim_independently(self, redis):
        from cqc_lem.utilities.engagement_window import (
            PRE_POST_TASK_COMMENTING,
            PRE_POST_TASK_VIEWER,
            claim_pre_post_lane,
        )

        assert claim_pre_post_lane(1, 42, PRE_POST_TASK_COMMENTING) is True
        assert claim_pre_post_lane(1, 42, PRE_POST_TASK_VIEWER) is True
        assert claim_pre_post_lane(1, 43, PRE_POST_TASK_COMMENTING) is True
        assert claim_pre_post_lane(2, 42, PRE_POST_TASK_COMMENTING) is True

    def test_no_redis_fails_open(self):
        from cqc_lem.utilities.engagement_window import claim_pre_post_lane

        with patch(f"{_WIN}.shared_redis_client", return_value=None):
            assert claim_pre_post_lane(1, 42, "lane") is True

    def test_a_redis_error_fails_open_and_warns(self):
        from cqc_lem.utilities.engagement_window import claim_pre_post_lane

        client = MagicMock()
        client.set.side_effect = RuntimeError("down")
        with patch(f"{_WIN}.shared_redis_client", return_value=client), \
             patch(f"{_WIN}.log_warning") as warn:
            assert claim_pre_post_lane(1, 42, "lane") is True
        warn.assert_called_once()


class TestClaimProfileViewerTouch:
    def test_one_touch_per_viewer_per_day(self, redis):
        from cqc_lem.utilities.engagement_window import claim_profile_viewer_touch

        url = "https://www.linkedin.com/in/jane/"
        assert claim_profile_viewer_touch(1, url, day="2026-09-24") is True
        assert claim_profile_viewer_touch(1, url.rstrip("/"), day="2026-09-24") is False
        assert claim_profile_viewer_touch(1, url, day="2026-09-25") is True
        assert claim_profile_viewer_touch(2, url, day="2026-09-24") is True

    def test_defaults_to_today(self, redis):
        from cqc_lem.utilities.engagement_window import claim_profile_viewer_touch

        assert claim_profile_viewer_touch(1, "https://www.linkedin.com/in/jane/") is True
        assert claim_profile_viewer_touch(1, "https://www.linkedin.com/in/jane/") is False

    def test_no_redis_or_a_redis_error_fails_open(self):
        from cqc_lem.utilities.engagement_window import claim_profile_viewer_touch

        with patch(f"{_WIN}.shared_redis_client", return_value=None):
            assert claim_profile_viewer_touch(1, "https://www.linkedin.com/in/jane/") is True
        client = MagicMock()
        client.set.side_effect = RuntimeError("down")
        with patch(f"{_WIN}.shared_redis_client", return_value=client), \
             patch(f"{_WIN}.log_warning"):
            assert claim_profile_viewer_touch(1, "https://www.linkedin.com/in/jane/") is True


class TestCommentingWindowIsOnePass:
    def _run(self, claimed: bool, post_id=42):
        from cqc_lem.app.engagement import feed as mod

        with patch(f"{_FEED}.is_commenting_held", return_value=False), \
             patch(f"{_FEED}.acquire_run_lock", return_value="tok"), \
             patch(f"{_FEED}.release_run_lock") as release, \
             patch(f"{_FEED}.claim_pre_post_lane", return_value=claimed) as claim, \
             patch(f"{_FEED}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), "e", MagicMock())) as session, \
             patch(f"{_FEED}.navigate_to_feed"), \
             patch(f"{_FEED}.comment_on_feed_inline", return_value=0), \
             patch(f"{_FEED}.record_pre_post_run"), \
             patch(f"{_FEED}.quit_gracefully"), \
             patch.object(mod.automate_commenting, "apply_async") as requeue:
            result = mod.automate_commenting.run(user_id=1, loop_for_duration=900, post_id=post_id)
        return result, {"claim": claim, "session": session, "release": release, "requeue": requeue}

    def test_a_second_dispatch_for_the_same_post_is_a_no_op_without_a_session(self):
        result, m = self._run(claimed=False)

        assert result.startswith("no_op")
        m["session"].assert_not_called()
        m["requeue"].assert_not_called()
        m["release"].assert_called_once_with("automate_commenting:1", "tok")

    def test_the_first_dispatch_claims_its_lane_and_runs(self):
        from cqc_lem.utilities.engagement_window import PRE_POST_TASK_COMMENTING

        result, m = self._run(claimed=True)

        m["claim"].assert_called_once_with(1, 42, PRE_POST_TASK_COMMENTING)
        m["session"].assert_called_once()
        m["requeue"].assert_not_called()
        assert not result.startswith("no_op")

    def test_a_run_with_no_post_takes_no_claim(self):
        _result, m = self._run(claimed=False, post_id=None)

        m["claim"].assert_not_called()
        m["session"].assert_called_once()

    def test_losing_the_run_lock_does_not_burn_the_claim(self):
        """A run that loses the per-user lock never reached the claim, so the post keeps its pass."""
        from cqc_lem.app.engagement import feed as mod

        with patch(f"{_FEED}.is_commenting_held", return_value=False), \
             patch(f"{_FEED}.acquire_run_lock", return_value=None), \
             patch(f"{_FEED}.claim_pre_post_lane") as claim, \
             patch(f"{_FEED}.get_current_profile") as session:
            mod.automate_commenting.run(user_id=1, post_id=42)

        claim.assert_not_called()
        session.assert_not_called()


def _viewer_driver(rows: list) -> MagicMock:
    driver = MagicMock(name="driver")

    def _execute(script, *args):
        if "Profile viewers" in script or "scrollTop" in script:
            return None
        return rows

    driver.execute_script.side_effect = _execute
    return driver


def _viewer_run(claimed: bool = True, touch=lambda *_a, **_k: True, rows=None, **kwargs):
    from cqc_lem.app.engagement import outreach as mod

    profile = MagicMock(name="profile")
    profile.email = "user@example.com"
    driver = _viewer_driver(rows or [])
    with patch(f"{_OUT}.claim_pre_post_lane", return_value=claimed) as claim, \
         patch(f"{_OUT}.claim_profile_viewer_touch", side_effect=touch) as touched, \
         patch(f"{_OUT}.get_current_profile",
               return_value=(driver, MagicMock(), "user@example.com", profile)) as session, \
         patch(f"{_OUT}.quit_gracefully"), \
         patch(f"{_OUT}.get_user_id", return_value=1), \
         patch(f"{_OUT}.time.sleep"), \
         patch(f"{_OUT}._PROFILE_VIEWER_RENDER_WAIT_SECONDS", 0), \
         patch(f"{_OUT}.engage_with_profile_viewer") as engage, \
         patch.object(mod.automate_profile_viewer_engagement, "apply_async") as requeue:
        result = mod.automate_profile_viewer_engagement.run(user_id=1, **kwargs)
    return result, {"claim": claim, "touch": touched, "session": session, "engage": engage,
                    "requeue": requeue}


def _row(slug: str) -> dict:
    return {"href": f"https://www.linkedin.com/in/{slug}/", "name": slug.title(), "viewed": "Viewed 1h ago"}


class TestViewerWindowIsOnePass:
    def test_a_second_dispatch_for_the_same_post_is_a_no_op_without_a_session(self):
        result, m = _viewer_run(claimed=False, loop_for_duration=600, post_id=42)

        assert result.startswith("no_op")
        m["session"].assert_not_called()
        m["requeue"].assert_not_called()

    def test_a_window_run_claims_its_lane_and_never_requeues(self):
        from cqc_lem.utilities.engagement_window import PRE_POST_TASK_VIEWER

        _result, m = _viewer_run(claimed=True, loop_for_duration=600, post_id=42)

        m["claim"].assert_called_once_with(1, 42, PRE_POST_TASK_VIEWER)
        m["session"].assert_called_once()
        m["requeue"].assert_not_called()

    def test_a_run_with_no_post_takes_no_claim_and_keeps_its_loop(self):
        _result, m = _viewer_run(claimed=False, loop_for_duration=600)

        m["claim"].assert_not_called()
        m["requeue"].assert_called_once()


class TestViewerTouchedTodayIsSkipped:
    def test_a_viewer_already_touched_today_is_not_dispatched(self):
        rows = [_row("ann"), _row("bob")]
        result, m = _viewer_run(rows=rows, touch=lambda _uid, url: "bob" not in url)

        dispatched = [c.kwargs["kwargs"]["viewer_url"] for c in m["engage"].apply_async.call_args_list]
        assert dispatched == ["https://www.linkedin.com/in/ann/"]
        assert m["touch"].call_count == 2
        assert "Engaged with 1 viewers" in result


class TestBudgetMessageNamesTheLimiter:
    def _spent(self, comments_today: int, cap: int = 20):
        from cqc_lem.app.engagement import feed as mod

        with patch(f"{_FEED}.count_comments_today", return_value=comments_today), \
             patch(f"{_FEED}.remaining_actions", return_value=0), \
             patch(f"{_FEED}.get_recent_engagers", return_value=set()), \
             patch(f"{_FEED}.log_info") as info:
            posted = mod.comment_on_feed_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1,
                                                prefs={"max_comments_per_day": cap})
        assert posted == 0
        return info.call_args[0][0]

    def test_names_pacing_when_the_pacing_draw_is_exhausted(self):
        message = self._spent(comments_today=3)

        assert "pacing" in message
        assert "limited by cap" not in message

    def test_names_the_cap_when_the_cap_is_reached(self):
        message = self._spent(comments_today=20)

        assert "limited by cap" in message
