"""Where curated sources meet the two shared paths: the content plan and `post_to_linkedin`.

Acceptance (#2260): a curated draft is NEVER auto-approved; no curated post publishes without a
human approval; a curated slot replaces the ordinary post rather than adding one; an ordinary post
(or an unreadable curated context) publishes exactly as before.
"""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_POST = "cqc_lem.app.engagement.posting"

BASE_PATCHES = [
    (f"{_POST}.get_post_status", {"return_value": "scheduled"}),
    (f"{_POST}.get_post_manual_publish", {"return_value": False}),
    (f"{_POST}.get_user_password_pair_by_id", {"return_value": ("u@example.com", "pw")}),
    (f"{_POST}.get_post_content", {"return_value": "My take on Jane's post."}),
    (f"{_POST}.get_engagement_preferences", {"return_value": {"reply_check_mode": "off"}}),
    (f"{_POST}.sweep_reply_comments", {}),
    (f"{_POST}.auto_seed_comment_on_post", {}),
    (f"{_POST}.auto_second_wave_comment", {}),
]
CONTEXT = {"post_id": 10, "post_user_id": 1, "post_status": "scheduled",
           "approved_by": "user:1", "source_treatment": "reshare", "image_url": None,
           "curated_source_id": 5,
           "source": {"id": 5, "platform": "linkedin", "canonical_id": "urn:li:share:9",
                      "url": "https://www.linkedin.com/feed/update/urn:li:share:9/",
                      "author": "Jane Doe", "publisher": "LinkedIn", "excerpt": "A good point.",
                      "licence": "linkedin_native", "link_only": False}}


def _publish(context, *, share_urn="urn:li:share:77"):
    from cqc_lem.app.engagement.posting import post_to_linkedin
    from cqc_lem.utilities.db import PostType

    with ExitStack() as stack:
        for target, kwargs in BASE_PATCHES:
            stack.enter_context(patch(target, **kwargs))
        stack.enter_context(patch(f"{_POST}.get_post_type", return_value=PostType.TEXT))
        stack.enter_context(patch(f"{_POST}.get_post_curated_context", return_value=context))
        status = stack.enter_context(patch(f"{_POST}.update_db_post_status"))
        log = stack.enter_context(patch(f"{_POST}.insert_new_log"))
        ordinary = stack.enter_context(patch(f"{_POST}.share_on_linkedin",
                                             return_value="urn:li:ugcPost:1"))
        reshare = stack.enter_context(patch(
            "cqc_lem.utilities.linkedin.reshare.share_reshare_on_linkedin",
            return_value=share_urn))
        stack.enter_context(patch("cqc_lem.app.run_curated_sources.update_curated_source_status"))
        stack.enter_context(patch("cqc_lem.app.run_curated_sources.track_curated_source"))
        stack.enter_context(patch("cqc_lem.utilities.db.get_post_image_url", return_value=None))
        result = post_to_linkedin.run(1, 10)
    return result, status, log, ordinary, reshare


class TestPublish:
    def test_an_owner_approved_curated_post_reshares_with_its_credit(self):
        from cqc_lem.utilities.db import PostStatus
        _, status, log, ordinary, reshare = _publish(CONTEXT)
        ordinary.assert_not_called()
        body = reshare.call_args.args[1]
        assert body.startswith("My take on Jane's post.")
        assert "Source: Jane Doe" in body
        status.assert_any_call(10, PostStatus.POSTED)
        assert "Source: Jane Doe" in log.call_args.kwargs["message"]

    @pytest.mark.parametrize("approver", ["system:auto_schedule", "system:rescore", None])
    def test_an_automatic_approval_never_publishes(self, approver):
        from cqc_lem.utilities.db import PostStatus
        result, status, _, ordinary, reshare = _publish({**CONTEXT, "approved_by": approver})
        assert "refused" in result
        reshare.assert_not_called()
        ordinary.assert_not_called()
        status.assert_called_once_with(10, PostStatus.PENDING)

    def test_an_unconfirmed_curated_publish_is_held_at_error(self):
        from cqc_lem.utilities.db import PostStatus
        result, status, _, ordinary, _ = _publish(CONTEXT, share_urn=None)
        assert "error" in result
        status.assert_called_once_with(10, PostStatus.ERROR)
        ordinary.assert_not_called()

    def test_an_ordinary_post_is_unchanged(self):
        _, _, _, ordinary, reshare = _publish(None)
        ordinary.assert_called_once_with(1, "My take on Jane's post.")
        reshare.assert_not_called()

    def test_an_unreadable_context_publishes_as_an_ordinary_post(self):
        _, _, _, ordinary, reshare = _publish({"unreadable": True})
        ordinary.assert_called_once()
        reshare.assert_not_called()


class TestNeverAutoApproved:
    def _decide(self, monkeypatch, *, flag, curated):
        from cqc_lem.app import run_content_plan as rcp
        monkeypatch.setenv("CURATED_SOURCES_ENABLED", "true" if flag else "false")
        with patch(f"{_RCP}.get_post_ever_gate_demoted", return_value=False), \
             patch(f"{_RCP}.post_is_curated", return_value=curated) as read:
            return rcp._may_auto_approve(1, 77, True, []), read

    def test_a_curated_draft_is_held_for_its_owner(self, monkeypatch):
        decided, _ = self._decide(monkeypatch, flag=True, curated=True)
        assert decided is False

    def test_an_unreadable_answer_holds_for_a_flag_on_user(self, monkeypatch):
        decided, _ = self._decide(monkeypatch, flag=True, curated=None)
        assert decided is False

    def test_an_ordinary_post_still_auto_approves(self, monkeypatch):
        decided, _ = self._decide(monkeypatch, flag=True, curated=False)
        assert decided is True

    def test_flag_off_reads_nothing(self, monkeypatch):
        decided, read = self._decide(monkeypatch, flag=False, curated=True)
        assert decided is True
        read.assert_not_called()

    def test_no_post_id(self):
        from cqc_lem.app import run_content_plan as rcp
        assert rcp._curated_post_needs_review(1, None) is False


class TestSlotClaim:
    def test_a_curated_slot_replaces_the_ordinary_post_and_its_photo(self):
        from cqc_lem.app.run_content_plan import create_content
        with patch(f"{_RCP}._affiliate_promo_content", return_value=None), \
             patch("cqc_lem.app.run_curated_sources.curated_content_for_slot",
                   return_value="  Curated take.  ") as claim, \
             patch(f"{_RCP}.create_text_post") as text_post, \
             patch(f"{_RCP}._generate_text_post_image") as photo, \
             patch(f"{_RCP}._engagement_prefs_or_empty", return_value={"a": 1}), \
             patch(f"{_RCP}._profile_synthesis_or_none", return_value="voice"):
            content, video_url = create_content(1, "text", "awareness", post_id=10,
                                                content_mix="authority")
            assert claim.call_args.kwargs["load_voice"]() == ({"a": 1}, "voice")
        assert (content, video_url) == ("Curated take.", None)
        text_post.assert_not_called()
        photo.assert_not_called()

    def test_an_unclaimed_slot_writes_the_ordinary_post(self):
        from cqc_lem.app.run_content_plan import create_content
        with patch(f"{_RCP}._affiliate_promo_content", return_value=None), \
             patch("cqc_lem.app.run_curated_sources.curated_content_for_slot", return_value=None), \
             patch(f"{_RCP}.create_text_post", return_value="Ordinary."), \
             patch(f"{_RCP}._generate_text_post_image"):
            content, _ = create_content(1, "text", "awareness", post_id=10, content_mix="value")
        assert content == "Ordinary."
