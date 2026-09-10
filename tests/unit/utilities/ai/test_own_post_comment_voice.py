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
