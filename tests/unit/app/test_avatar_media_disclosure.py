"""The AI disclosure must cover avatar IMAGES, not just video (issue #744).

A synthetic likeness of a real person in a carousel slide shipped with neither a caption
disclosure nor C2PA provenance — §2.4 of docs/AVATAR_FIDELITY_AND_VIDEO_LANGUAGE.md.
"""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"


class TestPostUsedAvatarMedia:
    def test_reads_the_durable_flag(self):
        from cqc_lem.app.run_content_plan import _post_used_avatar_media
        with patch("cqc_lem.utilities.db.post_used_avatar_media", return_value=True):
            assert _post_used_avatar_media(9) is True

    def test_unreadable_flag_fails_soft(self):
        """The video branch still triggers the disclosure independently — this must not raise."""
        from cqc_lem.app.run_content_plan import _post_used_avatar_media
        with patch("cqc_lem.utilities.db.post_used_avatar_media",
                   side_effect=RuntimeError("db down")):
            assert _post_used_avatar_media(9) is False


class TestDisclosureOnAvatarImagePosts:
    def _create(self, *, video_url, avatar_media):
        from cqc_lem.app.run_content_plan import _create_content_for_planned_post
        post = {"id": 9, "user_id": 7, "post_type": "carousel", "buyer_stage": "awareness",
                "content_mix": "value", "scheduled_time": None}
        with patch(f"{_RCP}.create_content", return_value=("caption body", video_url)), \
             patch(f"{_RCP}._post_used_avatar_media", return_value=avatar_media), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}._gate_findings_for_post", return_value=[]), \
             patch(f"{_RCP}.demoting_findings", return_value=[]), \
             patch(f"{_RCP}._persist_gate_findings"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.update_db_post_content") as store:
            assert _create_content_for_planned_post(post, {"auto_schedule_posts": True}) is True
        return store.call_args[0][1]

    def test_avatar_image_post_gets_the_disclosure(self):
        content = self._create(video_url=None, avatar_media=True)
        assert "Visuals created with AI" in content

    def test_plain_post_does_not(self):
        content = self._create(video_url=None, avatar_media=False)
        assert "Visuals created with AI" not in content
