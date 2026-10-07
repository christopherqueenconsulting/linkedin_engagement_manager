"""Showcase round 7: hooks and closes vary across the batch.

6 of 14 posts opened on a question; "share with someone hiring their first operations person" closed
141 and 142; "share your insights below" closed 143 and 144. docs/content-core.md, "Showcase
round 7".
"""

from unittest.mock import MagicMock

import pytest

from cqc_lem.app import run_content_plan as rcp
from cqc_lem.domain.models import PostDraftContext
from cqc_lem.utilities.ai import content_framework as cf

pytestmark = pytest.mark.unit

Q = "Is your AI stack paying for itself?\n\nBody.\n\nClose."
S = "AI spend hides in seat licences.\n\nBody.\n\nClose."
POST_141 = ("Hook.\n\nThe insight.\n\nIf you're hiring your first operations person, share this "
            "insight with peers who face the same challenges.")
POST_142 = ("Hook.\n\nAnother insight.\n\nShare this with someone hiring their first operations "
            "person.")


def _ctx(**overrides) -> PostDraftContext:
    fields = dict(user_id=1, stage="awareness", post_type="thought_leadership",
                  user_profile=MagicMock(), prefs={}, profile_synthesis="voice",
                  blueprint={"format": "personal_lesson", "cta_type": cf.CTA_TYPE_SHARE},
                  post_id=40, history_directive="", story_directive="")
    fields.update(overrides)
    return PostDraftContext(**fields)


class TestQuestionCap:
    def test_two_questions_in_the_window_cap_the_next(self):
        assert cf.question_hooks_capped([Q, S, Q])
        assert not cf.question_hooks_capped([Q, S, S])
        # The window is the previous five posts: a question six back no longer counts.
        assert not cf.question_hooks_capped([Q, S, S, S, S, Q])
        assert not cf.question_hooks_capped(None)

    def test_the_rotation_never_assigns_a_third(self):
        recent = [S, Q, Q]
        shapes = {cf.select_hook_shape(recent, allowed=(cf.HOOK_SHAPE_QUESTION,
                                                         cf.HOOK_SHAPE_NUMBER))}
        assert shapes == {cf.HOOK_SHAPE_NUMBER}

    def test_only_a_question_allowed_still_returns_one(self):
        assert cf.select_hook_shape([S, Q, Q], allowed=(cf.HOOK_SHAPE_QUESTION,)) == \
            cf.HOOK_SHAPE_QUESTION

    def test_a_third_question_is_a_violation(self):
        reason = cf.hook_shape_violation(Q, [S, Q, Q])
        assert reason and "question" in reason
        assert cf.hook_shape_violation(Q, [S, Q, S]) is None


class TestCtaPhrase:
    def test_141_and_142_repeat(self):
        assert "first operations" in cf.repeated_cta_phrase(POST_142, [POST_141])

    def test_143_and_144_repeat(self):
        a = "Hook.\n\nBody.\n\nWhat do you use? Share your insights below."
        b = "Hook.\n\nBody.\n\nHow do you decide? Share your insights below."
        assert "your insights" in cf.repeated_cta_phrase(b, [a])

    def test_different_wording_and_the_window(self):
        a = "Hook.\n\nBody.\n\nSave this for your next vendor review."
        assert cf.repeated_cta_phrase(POST_142, [a]) == ""
        assert cf.repeated_cta_phrase(POST_142, ["x"] * 5 + [POST_141]) == ""
        assert cf.repeated_cta_phrase("Hook.\n\nNo ask here.", [POST_141]) == ""

    def test_phrases_need_two_content_words(self):
        # "in the comments" is one content word: never a phrase on its own.
        assert "in the comments" not in cf.cta_phrases("Body.\n\nTell me in the comments?")
        assert all(len([w for w in p.split() if w not in cf._STOPWORDS and w != "POSS"]) >= 2
                   for p in cf.cta_phrases(POST_141))


class TestWiring:
    def test_a_repeated_wording_is_cut_after_the_type_rotation(self):
        out = rcp._enforce_cta_rotation(_ctx(), POST_142, [POST_141])
        assert out == "Hook.\n\nAnother insight."

    def test_the_artifact_close_is_never_cut(self):
        artifact = "Hook.\n\nBody.\n\nComment AUDIT and I'll send it."
        assert rcp._cut_repeated_cta_phrase(1, 2, artifact, [artifact]) == artifact
        assert rcp._cut_repeated_cta_phrase(1, 2, None, [artifact]) is None

    def test_a_deck_caption_gets_both_rules(self):
        caption = "Is your inbox lying to you?\n\nThe insight.\n\nMore.\n\n" \
                  "Share this with someone hiring their first operations person."
        out = rcp._enforce_batch_variety(1, 2, caption, [POST_141, Q, Q])
        assert out == "The insight.\n\nMore."

    def test_no_history_changes_nothing(self):
        assert rcp._enforce_batch_variety(1, 2, Q, None) == Q
        assert rcp._enforce_batch_variety(1, 2, "", []) == ""
