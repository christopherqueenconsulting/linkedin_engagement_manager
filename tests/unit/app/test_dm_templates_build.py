"""Unit tests for build_dm_from_template (templated, voice-aligned DMs)."""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"


class TestBuildDmFromTemplate:
    def test_renders_and_refines(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        prof = MagicMock(); prof.job_title = "AI Engineer"
        with patch(f"{_OUT}.get_dm_template",
                   return_value={"template_text": "Hi {first_name}, re {headline}", "delay_hours": 0, "step": 0}), \
             patch(f"{_OUT}.get_ai_message_refinement", return_value="Hi Jane, about AI Engineer (refined)"):
            msg = build_dm_from_template(1, "connection_accepted", "Jane", prof)
        assert msg == "Hi Jane, about AI Engineer (refined)"

    def test_none_when_no_template(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        with patch(f"{_OUT}.get_dm_template", return_value=None):
            assert build_dm_from_template(1, "x", "Jane", MagicMock()) is None

    def test_bad_placeholder_degrades_gracefully(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        prof = MagicMock(); prof.job_title = None
        with patch(f"{_OUT}.get_dm_template",
                   return_value={"template_text": "Hi {first_name} {unknown_field}", "delay_hours": 0, "step": 0}), \
             patch(f"{_OUT}.get_ai_message_refinement", side_effect=lambda m, character_limit=300: m):
            msg = build_dm_from_template(1, "connection_accepted", "Jane", prof)
        assert "Jane" in msg  # didn't crash on the stray placeholder

    def test_refinement_failure_falls_back_to_rendered(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        prof = MagicMock(); prof.job_title = "Eng"
        with patch(f"{_OUT}.get_dm_template",
                   return_value={"template_text": "Hi {first_name}", "delay_hours": 0, "step": 0}), \
             patch(f"{_OUT}.get_ai_message_refinement", side_effect=Exception("llm down")):
            msg = build_dm_from_template(1, "connection_accepted", "Jane", prof)
        assert msg == "Hi Jane"


class TestLinkPlaceholderNeverLeaves:
    """A template that promises a link must never render a link-shaped hole (#2061).

    Four DMs in one 24h window were refused by `send_dm_now` for an invented `[link]`, and a fifth
    was sent reading "Here's a quick breakdown for when you've a moment:" with nothing after the
    colon — all from the follow-up ladder calling `build_dm_from_template` with no `blog_url`.
    """

    _LINK_TEMPLATE = "No problem if the timing missed. Here's a quick breakdown: {blog_url}"

    @staticmethod
    def _patches(refine, blog_url="https://blog.example.com/x"):
        return (
            patch(f"{_OUT}.get_dm_template",
                  return_value={"template_text": TestLinkPlaceholderNeverLeaves._LINK_TEMPLATE,
                                "delay_hours": 0, "step": 1}),
            patch(f"{_OUT}.get_user_blog_url", return_value=blog_url),
            patch(f"{_OUT}.get_ai_message_refinement", side_effect=refine),
            patch(f"{_OUT}.humanize_text", side_effect=lambda t, **kw: t),
            patch(f"{_OUT}.lint_repaired", side_effect=lambda t, *a, **kw: t),
        )

    def test_unpassed_blog_url_is_resolved_from_the_user(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        seen = {}

        def _refine(message, character_limit=300, extra_directive=""):
            seen["message"] = message
            return message

        with ExitStack() as stack:
            for p in self._patches(_refine):
                stack.enter_context(p)
            msg = build_dm_from_template(1, "connection_accepted", "Jane", MagicMock(), step=1)
        assert "https://blog.example.com/x" in seen["message"]
        assert "{blog_url}" not in msg

    def test_no_blog_url_anywhere_drops_the_clause_instead_of_leaving_a_hole(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        with ExitStack() as stack:
            for p in self._patches(lambda m, character_limit=300, extra_directive="": m,
                                   blog_url=None):
                stack.enter_context(p)
            msg = build_dm_from_template(1, "connection_accepted", "Jane", MagicMock(), step=1)
        assert msg == "No problem if the timing missed."

    def test_template_read_is_skipped_when_the_template_wants_no_link(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        with patch(f"{_OUT}.get_dm_template",
                   return_value={"template_text": "Hi {first_name}", "delay_hours": 0, "step": 0}), \
             patch(f"{_OUT}.get_user_blog_url") as blog, \
             patch(f"{_OUT}.get_ai_message_refinement",
                   side_effect=lambda m, character_limit=300, extra_directive="": m), \
             patch(f"{_OUT}.humanize_text", side_effect=lambda t, **kw: t), \
             patch(f"{_OUT}.lint_repaired", side_effect=lambda t, *a, **kw: t):
            assert build_dm_from_template(1, "connection_accepted", "Jane", MagicMock()) == "Hi Jane"
        blog.assert_not_called()

    def test_link_only_template_with_no_url_sends_nothing(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        with patch(f"{_OUT}.get_dm_template",
                   return_value={"template_text": "Here you go: {blog_url}", "delay_hours": 0,
                                 "step": 1}), \
             patch(f"{_OUT}.get_user_blog_url", return_value=None), \
             patch(f"{_OUT}.get_ai_message_refinement") as refine:
            assert build_dm_from_template(1, "connection_accepted", "Jane", MagicMock(), step=1) is None
        refine.assert_not_called()

    def test_a_rewrite_that_invents_a_placeholder_falls_back_to_the_rendered_template(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        invented = "No problem if the timing missed. Here's a quick breakdown: [link]"
        with ExitStack() as stack:
            for p in self._patches(lambda m, character_limit=300, extra_directive="": invented):
                stack.enter_context(p)
            msg = build_dm_from_template(1, "connection_accepted", "Jane", MagicMock(), step=1)
        assert msg == ("No problem if the timing missed. Here's a quick breakdown: "
                       "https://blog.example.com/x")

    def test_a_rendered_template_that_is_itself_unsendable_keeps_the_rewrite(self):
        # The fallback is only better than the rewrite when the template is actually sendable; a
        # template the user wrote with its own `[link]` in it must not be preferred.
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        with patch(f"{_OUT}.get_dm_template",
                   return_value={"template_text": "Here you go {first_name}: [link]",
                                 "delay_hours": 0, "step": 1}), \
             patch(f"{_OUT}.get_ai_message_refinement",
                   side_effect=lambda m, character_limit=300, extra_directive="": "Here you go [link]"), \
             patch(f"{_OUT}.humanize_text", side_effect=lambda t, **kw: t), \
             patch(f"{_OUT}.lint_repaired", side_effect=lambda t, *a, **kw: t):
            msg = build_dm_from_template(1, "connection_accepted", "Jane", MagicMock(), step=1)
        assert msg == "Here you go [link]"
