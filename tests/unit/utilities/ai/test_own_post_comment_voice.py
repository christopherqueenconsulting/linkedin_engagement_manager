"""The author's own comments must not address the author (issue #2020).

The #344 seed comment and the #622 second wave are the AUTHOR commenting on the AUTHOR's post. Both
system prompts say so plainly ("You are the AUTHOR of the post below"), but both also pasted in the
user's free-form `comment_style` preference, which users write for commenting on OTHER people —
production's real value opens "Lead with a specific point from THEIR post". The style guidance won,
and what shipped in the first-comment slot of our own posts was:

    "Your tip to health-check each LiteLLM alias hit home…"
    "I hit the same silent retirement when a Cohere model vanished…"

That preference also bans the phrase "hits home", which is the tell that it was steering the draft
rather than the system prompt. Two halves are pinned here: the directive stops carrying a
third-party instruction onto an author-voice surface, and a HARD lint check catches it anyway if a
prompt ever drifts back.
"""

import pytest

from cqc_lem.utilities.ai.content_alignment import OWN_POST_COMMENT, style_directive
from cqc_lem.utilities.ai.slop_lint import (
    CHECK_SECOND_PERSON_AUTHOR,
    SEVERITY_HARD,
    lint_report,
    second_person_author_hits,
)

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"

# User 1's real preference, verbatim from production on 2026-09-10.
PREFS = {
    "tone": "direct, warm, credible, plainspoken - a practitioner, not a pitch",
    "comment_length": "medium",
    "comment_style": ("Lead with a specific point from their post. Add one concrete insight from "
                      "real LLM-ops experience. Ask a genuine question. No buzzwords (i.e "
                      '"hits home"), no "great post," never pitch.'),
    "use_emojis": True,
    "use_hashtags": False,
}

# What actually shipped on post 98, one of our own.
SHIPPED = ("Your tip to health-check each LiteLLM alias hit home—I learned the hard way a missing "
           "recovery path can freeze the whole router. When LinkedIn started spitting out 429s on "
           "July 20 2026, my circuit-breaker stayed latched until I added adaptive back-off and a "
           "manual pause. Have you added a self-heal step for similar rate-limit issues?")

# The same substance, written by the person who wrote the post — including a question to readers,
# which is not the problem and must stay allowed.
AUTHOR_VOICE = ("One thing I left out of the post: the breaker stayed latched for six hours before "
                "I noticed, because nothing alerted on a latched breaker, only on the 429s "
                "themselves. What would you have instrumented first?")


class TestStyleDirective:
    def test_own_post_surface_drops_the_third_party_style_guidance(self):
        own = style_directive(PREFS, OWN_POST_COMMENT)
        assert "their post" not in own.lower()
        assert "Style guidance:" not in own

    def test_it_still_carries_the_surface_neutral_preferences(self):
        own = style_directive(PREFS, OWN_POST_COMMENT)
        assert PREFS["tone"] in own                  # tone is not about who wrote the post
        assert "tasteful emoji" in own               # nor is the emoji rule
        assert "substantive comment of at least" in own   # nor the comment-length contract

    def test_it_says_whose_post_this_is(self):
        assert "YOUR OWN post" in style_directive(PREFS, OWN_POST_COMMENT)

    def test_the_third_party_comment_surface_is_unchanged(self):
        other = style_directive(PREFS, "comment")
        assert "Lead with a specific point from their post" in other
        assert "YOUR OWN post" not in other

    def test_a_post_surface_is_untouched(self):
        assert "Style guidance:" in style_directive(PREFS, "post")


class TestSecondPersonAuthorCheck:
    def test_it_catches_what_shipped(self):
        # Lower-cased: the hits come off `_plain()`, which normalises before matching.
        assert second_person_author_hits(SHIPPED) == ["your tip"]

    def test_it_is_hard_on_our_own_post(self):
        report = lint_report(SHIPPED, content_type=OWN_POST_COMMENT)
        blocking = [v for v in report["violations"]
                    if v["check"] == CHECK_SECOND_PERSON_AUTHOR and v["severity"] == SEVERITY_HARD]
        assert blocking, "a second-person own-post comment must be held, not merely recorded"
        assert not report["passes"]

    def test_author_voice_passes(self):
        report = lint_report(AUTHOR_VOICE, content_type=OWN_POST_COMMENT)
        assert not [v for v in report["violations"] if v["check"] == CHECK_SECOND_PERSON_AUTHOR]

    def test_a_question_to_readers_is_not_a_second_person_address(self):
        """A "you" aimed at the AUDIENCE is fine.

        That is the whole point of a seed comment — only "your post" and its friends name the
        author of the post.
        """
        assert second_person_author_hits("What would you do differently here?") == []
        assert second_person_author_hits("If you have shipped this, you know the tradeoff.") == []

    @pytest.mark.parametrize("text", [
        "Your post nails the tradeoff.",
        "Great point about idempotency.",
        "Well said.",
        "Thanks for sharing this.",
        "You're right that vigilance decays.",
    ])
    def test_the_vocabulary(self, text):
        assert second_person_author_hits(text), text

    def test_it_is_off_on_every_other_surface(self):
        """The check must not leak onto the surfaces where second person is correct.

        Addressing "your post" is right on a comment, a reply and a DM — it is only wrong when we
        wrote the post ourselves.
        """
        for surface in ("comment", "post", "newsletter", "dm"):
            report = lint_report(SHIPPED, content_type=surface)
            assert not [v for v in report["violations"]
                        if v["check"] == CHECK_SECOND_PERSON_AUTHOR], surface


class TestTheWiring:
    """The two generators must actually GRADE on the own-post surface.

    The directive and the check are each correct in isolation; what shipped the defect was the
    plumbing between them — the seed lint-repaired on "comment" and the second wave's gate
    hard-coded "comment", so the HARD check never ran on the surface it exists for.
    """

    def _llm(self, text):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content=text))]
        return resp

    def _profile(self):
        from unittest.mock import MagicMock
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        return prof

    def test_the_seed_comment_grades_on_the_own_post_surface(self):
        from unittest.mock import patch

        from cqc_lem.utilities.ai import ai_helper

        with patch(f"{_AI}._call_llm", return_value=self._llm(AUTHOR_VOICE)), \
             patch(f"{_AI}.lint_repaired", return_value=AUTHOR_VOICE) as lint:
            ai_helper.generate_seed_comment("my post body", self._profile(), PREFS)
        assert lint.call_args.args[1] == OWN_POST_COMMENT

    def test_the_second_wave_grades_on_the_own_post_surface(self):
        from unittest.mock import patch

        from cqc_lem.utilities.ai import ai_helper

        with patch(f"{_AI}._call_llm", return_value=self._llm(AUTHOR_VOICE)), \
             patch(f"{_AI}._gated_comment", return_value=AUTHOR_VOICE) as gate:
            ai_helper.generate_second_wave_comment("my post body", self._profile(), PREFS)
        assert gate.call_args.kwargs["surface"] == OWN_POST_COMMENT

    def test_the_gate_lints_against_the_surface_it_is_handed(self):
        """`_gated_comment` used to hard-code "comment" — the line that let the defect through."""
        from unittest.mock import patch

        from cqc_lem.utilities.ai import ai_helper

        seen = []

        def _spy(candidate, content_type, **kw):
            seen.append(content_type)
            return {"passes": True, "violations": []}

        with patch(f"{_AI}._slop.lint_report", side_effect=_spy), \
             patch(f"{_AI}._framework.comment_contract_report",
                   return_value={"passes": True, "failures": []}), \
             patch(f"{_AI}._framework.comment_similarity_report",
                   return_value={"too_similar": False}):
            out = ai_helper._gated_comment(lambda fix="": AUTHOR_VOICE, "post body",
                                           surface=OWN_POST_COMMENT)
        assert out == AUTHOR_VOICE
        assert seen == [OWN_POST_COMMENT]

    def test_the_gate_still_defaults_to_the_third_party_comment_surface(self):
        from unittest.mock import patch

        from cqc_lem.utilities.ai import ai_helper

        seen = []

        def _spy(candidate, content_type, **kw):
            seen.append(content_type)
            return {"passes": True, "violations": []}

        with patch(f"{_AI}._slop.lint_report", side_effect=_spy), \
             patch(f"{_AI}._framework.comment_contract_report",
                   return_value={"passes": True, "failures": []}), \
             patch(f"{_AI}._framework.comment_similarity_report",
                   return_value={"too_similar": False}):
            ai_helper._gated_comment(lambda fix="": "some comment", "post body")
        assert seen == ["comment"]

    def test_the_second_wave_refuses_a_draft_that_keeps_failing(self):
        """The second wave BLOCKS: `_gated_comment` returns None rather than ship a HARD failure."""
        from unittest.mock import patch

        from cqc_lem.utilities.ai import ai_helper

        with patch(f"{_AI}._call_llm", return_value=self._llm(SHIPPED)), \
             patch(f"{_AI}._humanize_text", side_effect=lambda text, **kw: text), \
             patch(f"{_AI}._framework.comment_contract_report",
                   return_value={"passes": True, "failures": []}), \
             patch(f"{_AI}._framework.comment_similarity_report",
                   return_value={"too_similar": False}):
            out = ai_helper.generate_second_wave_comment("my post body", self._profile(), PREFS)
        assert out is None

    def test_the_seed_repairs_and_warns_rather_than_blocking(self):
        """The seed REPAIRS: it retries against the fix directive, then ships with a warning.

        Not an oversight, and deliberately pinned so nobody "fixes" it into a block. `lint_repaired`
        ships the best draft it got because these surfaces have no review queue to hold one in — and
        for the seed specifically, blocking would strand the #392 held-back link, which the seed
        comment is the delivery mechanism for. The asymmetry with the second wave above is the
        design: the wave adds optional value and can be skipped, the seed carries the link.

        A model that CAN repair does; this mock never can, which is what exercises the give-up path.
        The repeated warning is the safety net — under the escalation contract a recurring one is
        re-emitted at ERROR and files a grouped issue.
        """
        from unittest.mock import patch

        from cqc_lem.utilities.ai import ai_helper

        with patch(f"{_AI}._call_llm", return_value=self._llm(SHIPPED)), \
             patch(f"{_AI}._humanize_text", side_effect=lambda text, **kw: text), \
             patch(f"{_AI}.log_warning") as warn:
            out = ai_helper.generate_seed_comment("my post body", self._profile(), PREFS)
        assert out == SHIPPED, "the seed ships its best draft; it has no review queue"
        assert warn.called, "and it must say so loudly enough to escalate"
        assert "second_person_author" in warn.call_args.args[0]

    def test_a_repairable_draft_is_repaired(self):
        """The path that matters in production: the retry clears the check and the clean text ships."""
        from unittest.mock import patch

        from cqc_lem.utilities.ai import ai_helper

        drafts = iter([self._llm(SHIPPED), self._llm(AUTHOR_VOICE)])
        with patch(f"{_AI}._call_llm", side_effect=lambda **kw: next(drafts)), \
             patch(f"{_AI}._humanize_text", side_effect=lambda text, **kw: text):
            out = ai_helper.generate_seed_comment("my post body", self._profile(), PREFS)
        assert out == AUTHOR_VOICE
