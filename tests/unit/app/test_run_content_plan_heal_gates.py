"""Unit tests for the gated asset heal — issue #2100.

The carousel and video heals used to promote any non-POSTED post straight to APPROVED once its
media was re-rendered, so a post the gates HELD at PENDING published (post 108), and a rewritten
carousel caption shipped without ever being graded. `_heal_errored_post` now moves ONLY an ERROR
post, and only as far as the post-surface gates re-run on the new content allow.
"""
from unittest.mock import patch

import pytest

from cqc_lem.utilities.db import PostApprover, PostStatus, PostType
from cqc_lem.utilities.quality_gates import slop_finding

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_DB = "cqc_lem.utilities.db"

_FAILING = [slop_finding(["canned scaffold"], [])]


def _heal(status: str, findings: list, *, auto_schedule: bool = True, may_approve=None,
          user_id=1):
    """Run the heal against a post at `status` whose re-gated content yields `findings`."""
    from cqc_lem.app.run_content_plan import _heal_errored_post
    approve = patch(f"{_RCP}._may_auto_approve", return_value=may_approve) \
        if may_approve is not None else patch(f"{_RCP}.get_post_ever_gate_demoted",
                                              return_value=False)
    with patch(f"{_DB}.get_post_status", return_value=status), \
         patch(f"{_DB}.get_post_type", return_value=PostType.CAROUSEL), \
         patch(f"{_DB}.get_post_video_url", return_value=None), \
         patch(f"{_RCP}._gate_findings_for_post", return_value=findings) as gates, \
         patch(f"{_RCP}.update_db_post_gate_reason") as gate_reason, \
         patch(f"{_RCP}.get_user_preferences",
               return_value={"auto_schedule_posts": auto_schedule}), \
         approve, \
         patch(f"{_RCP}.update_db_post_status") as set_status:
        result = _heal_errored_post(user_id, 7, "new caption", task_name="heal_test",
                                    approved_by=PostApprover.CAROUSEL_HEAL)
    return result, gates, gate_reason, set_status


class TestHealErroredPost:
    def test_a_pending_post_stays_pending(self):
        result, gates, gate_reason, set_status = _heal(PostStatus.PENDING.value, [])
        assert result is None
        set_status.assert_not_called()
        gates.assert_not_called()
        gate_reason.assert_not_called()

    @pytest.mark.parametrize("status", [PostStatus.APPROVED.value, PostStatus.POSTED.value,
                                        "planning", "scheduled"])
    def test_no_status_but_error_is_touched(self, status):
        result, _, _, set_status = _heal(status, [])
        assert result is None
        set_status.assert_not_called()

    def test_an_error_post_whose_new_content_fails_a_gate_goes_to_pending(self):
        result, gates, _, set_status = _heal(PostStatus.ERROR.value, _FAILING)
        assert result is PostStatus.PENDING
        set_status.assert_called_once_with(7, PostStatus.PENDING,
                                           approved_by=PostApprover.CAROUSEL_HEAL)
        # The gates are graded on the NEW content, with the post's own type.
        gates.assert_called_once_with(1, 7, "new caption", PostType.CAROUSEL.value, None)

    def test_gate_reason_is_rewritten_with_the_failing_findings(self):
        _, _, gate_reason, _ = _heal(PostStatus.ERROR.value, _FAILING)
        gate_reason.assert_called_once_with(7, _FAILING)

    def test_an_error_post_that_passes_every_gate_is_approved_and_its_reason_cleared(self):
        result, _, gate_reason, set_status = _heal(PostStatus.ERROR.value, [])
        assert result is PostStatus.APPROVED
        set_status.assert_called_once_with(7, PostStatus.APPROVED,
                                           approved_by=PostApprover.CAROUSEL_HEAL)
        gate_reason.assert_called_once_with(7, [])

    def test_a_passing_post_waits_for_its_author_when_auto_scheduling_is_off(self):
        result, _, _, set_status = _heal(PostStatus.ERROR.value, [], auto_schedule=False)
        assert result is PostStatus.PENDING
        set_status.assert_called_once_with(7, PostStatus.PENDING,
                                           approved_by=PostApprover.CAROUSEL_HEAL)

    def test_a_repaired_post_held_by_the_shared_approve_decision_goes_to_pending(self):
        result, _, _, _ = _heal(PostStatus.ERROR.value, [], may_approve=False)
        assert result is PostStatus.PENDING

    def test_an_unresolvable_owner_leaves_the_post_at_error(self):
        result, gates, _, set_status = _heal(PostStatus.ERROR.value, [], user_id=None)
        assert result is None
        gates.assert_not_called()
        set_status.assert_not_called()


class TestBothHealsAreGated:
    """The PENDING hold survives both asset heals end to end (issue #2100, post 108)."""

    def test_carousel_heal_leaves_a_pending_post_pending(self):
        from cqc_lem.app.run_content_plan import regenerate_post_carousel_task
        with patch(f"{_DB}.get_post_user_id", return_value=1), \
             patch(f"{_DB}.get_post_buyer_stage", return_value="awareness"), \
             patch(f"{_RCP}.create_carousel_content", return_value="caption"), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_DB}.update_db_post_content"), \
             patch(f"{_DB}.get_post_carousel_slides", return_value=["https://x/s1.png"]), \
             patch(f"{_DB}.get_post_status", return_value=PostStatus.PENDING.value), \
             patch(f"{_RCP}.update_db_post_status") as set_status, \
             patch(f"{_DB}.update_db_post_status") as set_status_db:
            regenerate_post_carousel_task(7)
        set_status.assert_not_called()
        set_status_db.assert_not_called()

    def test_carousel_heal_falls_back_to_the_stored_caption(self):
        from cqc_lem.app.run_content_plan import regenerate_post_carousel_task
        with patch(f"{_DB}.get_post_user_id", return_value=1), \
             patch(f"{_DB}.get_post_buyer_stage", return_value="awareness"), \
             patch(f"{_RCP}.create_carousel_content", return_value=None), \
             patch(f"{_DB}.get_post_carousel_slides", return_value=["https://x/s1.png"]), \
             patch(f"{_RCP}.get_post_content", return_value="stored caption"), \
             patch(f"{_RCP}._heal_errored_post") as heal:
            regenerate_post_carousel_task(7)
        heal.assert_called_once_with(1, 7, "stored caption",
                                     task_name="regenerate_post_carousel_task",
                                     approved_by=PostApprover.CAROUSEL_HEAL)

    def test_video_heal_regates_an_error_post_instead_of_approving_it(self):
        from cqc_lem.app.run_content_plan import regenerate_video_for_post
        with patch(f"{_RCP}.get_post_content", return_value="text"), \
             patch(f"{_DB}.get_post_user_id", return_value=1), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}._generate_video_src", return_value="http://runway/v.mp4"), \
             patch(f"{_RCP}._store_video_asset", return_value="https://x/v.mp4"), \
             patch(f"{_RCP}._heal_errored_post") as heal:
            assert regenerate_video_for_post(7) == "https://x/v.mp4"
        heal.assert_called_once_with(1, 7, "text", task_name="regenerate_post_video_task",
                                     approved_by=PostApprover.VIDEO_HEAL)

    def test_video_heal_leaves_a_pending_post_pending(self):
        from cqc_lem.app.run_content_plan import regenerate_video_for_post
        with patch(f"{_RCP}.get_post_content", return_value="text"), \
             patch(f"{_DB}.get_post_user_id", return_value=1), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}._generate_video_src", return_value="http://runway/v.mp4"), \
             patch(f"{_RCP}._store_video_asset", return_value="https://x/v.mp4"), \
             patch(f"{_DB}.get_post_status", return_value=PostStatus.PENDING.value), \
             patch(f"{_RCP}.update_db_post_status") as set_status:
            regenerate_video_for_post(7)
        set_status.assert_not_called()
