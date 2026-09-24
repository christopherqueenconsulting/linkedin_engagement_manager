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

    def test_an_empty_rewrite_still_falls_back_to_the_rendered_template(self):
        # `empty_body` is a refusal like any other: short-circuiting it would return "", which every
        # caller reads as "no template for this step" and answers by stopping the sequence.
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        with ExitStack() as stack:
            for p in self._patches(lambda m, character_limit=300, extra_directive="": "   "):
                stack.enter_context(p)
            msg = build_dm_from_template(1, "connection_accepted", "Jane", MagicMock(), step=1)
        assert msg == ("No problem if the timing missed. Here's a quick breakdown: "
                       "https://blog.example.com/x")

    def test_a_link_only_template_never_warns(self):
        # One user's template plus one missing blog URL is a steady state, not an event: every
        # contact on that drip renders the same empty body, so a WARNING here would clear the
        # 3-in-24h escalation and re-file the very defect #2061 is.
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        with patch(f"{_OUT}.get_dm_template",
                   return_value={"template_text": "Here you go: {blog_url}", "delay_hours": 0,
                                 "step": 1}), \
             patch(f"{_OUT}.get_user_blog_url", return_value=None), \
             patch(f"{_OUT}.log_warning") as warn:
            assert build_dm_from_template(1, "connection_accepted", "Jane", MagicMock(), step=1) is None
        warn.assert_not_called()


_DM_37 = ("Your cybernetic AI work brings to mind a DoD LLM rollout that cut false positives 30%. "
          "Do you've 15 minutes for a quick call to swap ideas?")


class TestDmContentGate:
    """#2099: every templated DM passes the content gate or is not sent."""

    @staticmethod
    def _build(template: str, rewrites: list, **kwargs):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        with patch(f"{_OUT}.get_dm_template", return_value={"template_text": template}), \
             patch(f"{_OUT}.get_story_bank_entries", return_value=[]), \
             patch(f"{_OUT}.get_ai_message_refinement", side_effect=rewrites) as refine, \
             patch(f"{_OUT}.humanize_text", side_effect=lambda t, **k: t):
            return build_dm_from_template(1, "connection_accepted", "Jane", MagicMock(), **kwargs), refine

    def test_a_blocked_rewrite_gets_one_steered_retry(self):
        msg, refine = self._build("Hi {first_name}", [_DM_37, "Good to connect, Jane."])
        assert msg == "Good to connect, Jane."
        directive = refine.call_args_list[1].kwargs["extra_directive"]
        assert "meeting_ask" in directive and "30%" in directive

    def test_a_still_blocked_rewrite_falls_back_to_the_rendered_template(self):
        msg, _ = self._build("Hi {first_name}", [_DM_37, _DM_37])
        assert msg == "Hi Jane"

    def test_a_cold_call_ask_in_the_template_itself_is_never_sent(self):
        msg, _ = self._build("Hi {first_name}, got 20 minutes for a call?", [_DM_37, _DM_37])
        assert msg is None

    def test_the_same_template_may_ask_once_the_contact_replied(self):
        tmpl = "Hi {first_name}, got 20 minutes for a call?"
        msg, _ = self._build(tmpl, ["Hi Jane, got 20 minutes for a call?"], thread_replied=True)
        assert msg == "Hi Jane, got 20 minutes for a call?"

    def test_a_number_from_the_story_bank_passes(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        entry = {"title": "Triage", "body": "We cut false positives 30% at a client."}
        with patch(f"{_OUT}.get_dm_template", return_value={"template_text": "Hi {first_name}"}), \
             patch(f"{_OUT}.get_story_bank_entries", return_value=[entry]), \
             patch(f"{_OUT}.get_ai_message_refinement",
                   return_value="Hi Jane, we cut false positives 30% on triage."), \
             patch(f"{_OUT}.humanize_text", side_effect=lambda t, **k: t):
            msg = build_dm_from_template(1, "connection_accepted", "Jane", MagicMock())
        assert msg == "Hi Jane, we cut false positives 30% on triage."

    def test_an_unreadable_story_bank_grades_strictly(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        with patch(f"{_OUT}.get_dm_template", return_value={"template_text": "Hi {first_name}"}), \
             patch(f"{_OUT}.get_story_bank_entries", side_effect=RuntimeError("down")), \
             patch(f"{_OUT}.get_ai_message_refinement", return_value="We cut costs 30%."), \
             patch(f"{_OUT}.humanize_text", side_effect=lambda t, **k: t), \
             patch(f"{_OUT}.log_warning") as warn:
            msg = build_dm_from_template(1, "connection_accepted", "Jane", MagicMock())
        assert msg == "Hi Jane"
        assert "Story bank unreadable" in warn.call_args.args[0]
