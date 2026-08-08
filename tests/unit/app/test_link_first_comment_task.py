"""Unit tests for the link-in-first-comment mechanic at the task layer (issue #392 — C3):
post_to_linkedin holds the link back, auto_seed_comment_on_post delivers it.
"""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_URL = "https://www.linkedin.com/feed/update/urn:li:ugcPost:7479519458164695040/"
_BODY_WITH_LINK = "Three lessons from the rebuild.\n\nFull breakdown: https://example.com/rebuild"


def _post_patches(stack, content, prefs=None, share_urn="urn:li:ugcPost:1"):
    from cqc_lem.utilities.db import PostType
    targets = {
        "get_post_status": {"return_value": "approved"},
        # Issue #1074: the publish task refuses a native-publish draft before anything else.
        "get_post_manual_publish": {"return_value": False},
        "get_user_password_pair_by_id": {"return_value": ("u@example.com", "pw")},
        "get_post_content": {"return_value": content},
        "get_engagement_preferences": {"return_value": prefs if prefs is not None else {}},
        "get_post_type": {"return_value": PostType.TEXT},
        "share_on_linkedin": {"return_value": share_urn},
        "insert_new_log": {},
        "update_db_post_status": {},
        "sweep_reply_comments": {},
        "auto_seed_comment_on_post": {},
        "auto_second_wave_comment": {},
    }
    return {name: stack.enter_context(patch(f"{_RA}.{name}", **kw)) for name, kw in targets.items()}


class TestPostToLinkedinHoldsLinkBack:
    def test_publishes_link_free_body_and_stashes_the_link(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            m = _post_patches(stack, _BODY_WITH_LINK)
            content_upd = stack.enter_context(patch(f"{_RA}.update_db_post_content"))
            link_upd = stack.enter_context(patch(f"{_RA}.update_db_post_first_comment_link"))
            post_to_linkedin.run(1, 10)

        published = m["share_on_linkedin"].call_args[0][1]
        assert "https://example.com/rebuild" not in published
        assert "Three lessons from the rebuild." in published
        link_upd.assert_called_once_with(10, "https://example.com/rebuild")
        # The stored post is kept in sync with what actually published.
        content_upd.assert_called_once_with(10, published)
        # The success log records the published (link-free) body.
        assert m["insert_new_log"].call_args.kwargs["message"] == published

    def test_pref_off_publishes_the_link_inline(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            m = _post_patches(stack, _BODY_WITH_LINK, prefs={"link_in_first_comment": False})
            link_upd = stack.enter_context(patch(f"{_RA}.update_db_post_first_comment_link"))
            post_to_linkedin.run(1, 10)

        assert "https://example.com/rebuild" in m["share_on_linkedin"].call_args[0][1]
        link_upd.assert_not_called()

    def test_link_free_post_touches_nothing(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            m = _post_patches(stack, "No links in this one.")
            content_upd = stack.enter_context(patch(f"{_RA}.update_db_post_content"))
            link_upd = stack.enter_context(patch(f"{_RA}.update_db_post_first_comment_link"))
            post_to_linkedin.run(1, 10)

        assert m["share_on_linkedin"].call_args[0][1] == "No links in this one."
        content_upd.assert_not_called()
        link_upd.assert_not_called()

    def test_seed_comment_is_dispatched_after_publish(self):
        """The seed task is dispatched HERE (not by the scheduler) because it needs the published
        post's URL — and it is what delivers the held-back link.
        """
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            m = _post_patches(stack, _BODY_WITH_LINK)
            stack.enter_context(patch(f"{_RA}.update_db_post_content"))
            stack.enter_context(patch(f"{_RA}.update_db_post_first_comment_link"))
            post_to_linkedin.run(1, 10)

        m["auto_seed_comment_on_post"].apply_async.assert_called_once()
        kwargs = m["auto_seed_comment_on_post"].apply_async.call_args.kwargs
        assert kwargs["kwargs"] == {"user_id": 1, "post_id": 10}
        assert kwargs["countdown"] > 0

    def test_no_seed_dispatch_when_publishing_failed(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            m = _post_patches(stack, "No links in this one.", share_urn=None)
            post_to_linkedin.run(1, 10)
        m["auto_seed_comment_on_post"].apply_async.assert_not_called()

    def test_failed_publish_leaves_the_stored_post_intact(self):
        """The split is in-memory until the share succeeds — a failed publish must not strand the
        post in the link-held-back state, or a retry would publish a body whose link is gone.
        """
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            _post_patches(stack, _BODY_WITH_LINK, share_urn=None)
            content_upd = stack.enter_context(patch(f"{_RA}.update_db_post_content"))
            link_upd = stack.enter_context(patch(f"{_RA}.update_db_post_first_comment_link"))
            post_to_linkedin.run(1, 10)

        content_upd.assert_not_called()
        link_upd.assert_not_called()

    def test_failed_carousel_leaves_the_stored_post_intact(self):
        """Same for the carousel early-return path (no usable slide images)."""
        from cqc_lem.app.run_automation import post_to_linkedin
        from cqc_lem.utilities.db import PostType
        with ExitStack() as stack:
            m = _post_patches(stack, _BODY_WITH_LINK)
            m["get_post_type"].return_value = PostType.CAROUSEL
            stack.enter_context(patch(f"{_RA}.get_carousel_slides", return_value=[]))
            stack.enter_context(patch(f"{_RA}.log_error"))
            content_upd = stack.enter_context(patch(f"{_RA}.update_db_post_content"))
            link_upd = stack.enter_context(patch(f"{_RA}.update_db_post_first_comment_link"))
            post_to_linkedin.run(1, 10)

        content_upd.assert_not_called()
        link_upd.assert_not_called()


class TestSeedCommentDeliversTheLink:
    def _seed(self, stack, held_link, seed_text="What surprised you most?", already_commented=False):
        from unittest.mock import MagicMock

        from cqc_lem.app.run_automation import auto_seed_comment_on_post
        stack.enter_context(patch(f"{_RA}.get_post_url_from_log_for_user", return_value=_URL))
        stack.enter_context(patch(f"{_RA}.has_user_commented_on_post_url", return_value=already_commented))
        stack.enter_context(patch(f"{_RA}.get_post_content", return_value="Three lessons."))
        stack.enter_context(patch(f"{_RA}.load_profile_for_user", return_value=MagicMock()))
        stack.enter_context(patch(f"{_RA}.get_engagement_preferences", return_value={}))
        stack.enter_context(patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"))
        stack.enter_context(patch(f"{_RA}.generate_seed_comment", return_value=seed_text))
        stack.enter_context(patch(f"{_RA}.get_post_first_comment_link", return_value=held_link))
        stack.enter_context(patch(f"{_RA}.insert_new_log"))
        api = stack.enter_context(patch(f"{_RA}.comment_on_linkedin_post",
                                        return_value="urn:li:comment:(x,1)"))
        result = auto_seed_comment_on_post.run(user_id=1, post_id=9)
        return api, result

    def test_held_link_is_appended_to_the_seed_comment(self):
        with ExitStack() as stack:
            api, _ = self._seed(stack, "https://example.com/rebuild")
        comment = api.call_args.args[2]
        assert "What surprised you most?" in comment
        assert "https://example.com/rebuild" in comment

    def test_multiple_links_all_ship(self):
        with ExitStack() as stack:
            api, _ = self._seed(stack, "https://example.com/a\nhttps://example.com/b")
        comment = api.call_args.args[2]
        assert "https://example.com/a" in comment and "https://example.com/b" in comment

    def test_link_still_ships_when_generation_returns_nothing(self):
        with ExitStack() as stack:
            api, _ = self._seed(stack, "https://example.com/rebuild", seed_text=None)
        assert "https://example.com/rebuild" in api.call_args.args[2]

    def test_no_held_link_leaves_the_comment_unchanged(self):
        with ExitStack() as stack:
            api, _ = self._seed(stack, None)
        assert api.call_args.args[2] == "What surprised you most?"

    def test_bails_when_a_comment_already_exists(self):
        """Idempotency: a re-dispatched seed must not leave a second comment on the same post."""
        with ExitStack() as stack:
            api, result = self._seed(stack, "https://example.com/a", already_commented=True)
        api.assert_not_called()
        assert "already exists" in result
