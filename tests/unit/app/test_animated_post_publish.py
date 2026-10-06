"""post_to_linkedin picks the animated loop ONLY when ANIMATED_POST_ENABLED is on (docs/animated-posts.md).

Patched on `app.engagement.posting` — the module whose globals the task reads (#1154).
"""
from contextlib import ExitStack
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_POST = "cqc_lem.app.engagement.posting"
_STILL = "https://api.example.com/api/assets?file_name=images/posts/10/a.png"

BASE_PATCHES = [
    (f"{_POST}.get_post_status", {"return_value": "approved"}),
    (f"{_POST}.get_post_manual_publish", {"return_value": False}),
    (f"{_POST}.get_user_password_pair_by_id", {"return_value": ("u@example.com", "pw")}),
    (f"{_POST}.get_post_content", {"return_value": "Post text"}),
    (f"{_POST}.insert_new_log", {}),
    (f"{_POST}.update_db_post_status", {}),
    (f"{_POST}.get_engagement_preferences", {"return_value": {"reply_check_mode": "event"}}),
    (f"{_POST}.sweep_reply_comments", {}),
    (f"{_POST}.auto_seed_comment_on_post", {}),
    (f"{_POST}.auto_second_wave_comment", {}),
]


def _run(monkeypatch, *, flag, loop_path):
    from cqc_lem.app.engagement.posting import post_to_linkedin
    from cqc_lem.utilities.db import PostType

    monkeypatch.setenv("ANIMATED_POST_ENABLED", "true" if flag else "false")
    with ExitStack() as stack:
        for target, kwargs in BASE_PATCHES:
            stack.enter_context(patch(target, **kwargs))
        stack.enter_context(patch(f"{_POST}.get_post_type", return_value=PostType.TEXT))
        stack.enter_context(patch("cqc_lem.utilities.db.get_post_image_url", return_value=_STILL))
        stack.enter_context(patch("cqc_lem.utilities.post_image.post_loop_abs_path",
                                  return_value=loop_path))
        still = stack.enter_context(
            patch(f"{_POST}.share_on_linkedin", return_value="urn:li:ugcPost:1"))
        animated = stack.enter_context(
            patch(f"{_POST}.share_animated_image_on_linkedin", return_value="urn:li:share:2"))
        post_to_linkedin.run(1, 10)
    return still, animated


def test_flag_on_with_a_stored_loop_publishes_the_loop(monkeypatch):
    still, animated = _run(monkeypatch, flag=True, loop_path="/assets/images/posts/10/a.loop.gif")
    animated.assert_called_once_with(1, "Post text", "/assets/images/posts/10/a.loop.gif", _STILL)
    still.assert_not_called()


def test_flag_off_publishes_the_still_exactly_as_before(monkeypatch):
    still, animated = _run(monkeypatch, flag=False, loop_path="/assets/images/posts/10/a.loop.gif")
    animated.assert_not_called()
    still.assert_called_once_with(1, "Post text", _STILL)


def test_flag_on_without_a_loop_publishes_the_still(monkeypatch):
    still, animated = _run(monkeypatch, flag=True, loop_path=None)
    animated.assert_not_called()
    still.assert_called_once_with(1, "Post text", _STILL)


def test_a_loop_lookup_fault_publishes_the_still(monkeypatch):
    from cqc_lem.app.engagement.posting import _animated_loop_for
    monkeypatch.setenv("ANIMATED_POST_ENABLED", "true")
    with patch("cqc_lem.utilities.post_image.post_loop_abs_path", side_effect=OSError("disk")), \
         patch(f"{_POST}.log_warning") as warn:
        assert _animated_loop_for(1, 10, _STILL) is None
    warn.assert_called_once()
    assert _animated_loop_for(1, 10, None) is None


def test_text_post_image_generation_hands_the_still_to_the_loop_producer(monkeypatch):
    from cqc_lem.app.run_content_plan import _generate_text_post_image
    with patch("cqc_lem.utilities.db.get_engagement_preferences",
               return_value={"text_post_images": True}), \
         patch("cqc_lem.utilities.db.update_db_post_image_url", return_value=True), \
         patch("cqc_lem.utilities.post_image.generate_image_for_post",
               return_value=(_STILL, None)), \
         patch("cqc_lem.utilities.animated_loop.produce_post_loop") as produce:
        assert _generate_text_post_image(7, "My post", 42) == _STILL
    produce.assert_called_once_with(7, 42, "My post", _STILL)
