"""The drafter refuses a post whose body never arrived (issue #1833).

A hand audit of `comment_generation` traces found the prompt's `<content>` block carrying nothing
but `https://lnkd.in/gKabw7UJ` — the scrape had produced no body. The model said so in its own
reasoning ("Without seeing the post, impossible...") and then invented a statistic, which it quoted
back at the post's author under the user's name.

`generate_ai_response`'s docstring already promised the comment is grounded in the target post.
These tests are what enforces it: an empty, whitespace-only or URL-only `post_content` never
reaches the model, and the function returns None so the caller SKIPS the post — the same contract
the #617 quality gate uses when no draft can be repaired.

The last test is the one that keeps the guard from becoming over-broad: a REAL body that happens to
contain a URL is a perfectly groundable post and must still generate.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_FEED = "cqc_lem.app.engagement.feed"

# A real post plus the link it cites, and a draft grounded in its specifics (no validation-filler
# opener, one lived value-add with a number, closing question) so it clears the quality contract.
_POST_WITH_URL = ("We cut our warehouse pick times by moving the fast movers to the front aisle. "
                  "Nobody talks about how much layout beats software here. "
                  "Full write-up: https://lnkd.in/gKabw7UJ")
_GOOD = ("The front-aisle move is the part most layout posts skip. We tried it across two "
         "warehouses last year and pick times fell 18% before we touched the software. "
         "How long did the fast movers stay stable for you?")

# Every shape a failed scrape has been seen to produce.
_UNREADABLE = {
    "empty": "",
    "none": None,
    "whitespace": "   \n\t  ",
    "bare_shortlink": "https://lnkd.in/gKabw7UJ",
    "shortlink_trailing_punctuation": "https://lnkd.in/gKabw7UJ.",
    "shortlink_wrapped": "  (https://lnkd.in/gKabw7UJ)  ",
    "scheme_less_shortlink": "lnkd.in/gKabw7UJ",
}


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _profile():
    p = MagicMock()
    p.model_dump_json.return_value = '{"full_name": "Jane", "job_title": "COO"}'
    return p


class TestUngroundedCommentGuard:
    """No post in the prompt means no comment — never an invented one."""

    @pytest.mark.parametrize("shape", sorted(_UNREADABLE))
    def test_unreadable_post_body_never_reaches_the_model(self, shape):
        from cqc_lem.utilities.ai import ai_helper
        # A perfectly good draft is on offer — the point is that it is never asked for, because the
        # only thing the model could ground it in is a link.
        with patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)) as llm, \
             patch(f"{_AI}.log_warning") as warn:
            out = ai_helper.generate_ai_response(_UNREADABLE[shape], _profile(), user_id=7)
        # None is the caller's SKIP signal — the same one the #617 quality gate returns.
        assert out is None
        assert llm.call_count == 0, "the model was handed a post that never arrived"
        # A scrape that produced no body is a scraper defect, not an expected no-op, so it warns
        # once per occurrence (utilities/CLAUDE.md), never at DEBUG.
        assert warn.call_count == 1
        assert warn.call_args.kwargs["user_id"] == 7

    def test_the_warning_names_which_post_scraped_empty(self):
        # `post_id` is what makes the warning actionable for the scraper defect behind it.
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)) as llm, \
             patch(f"{_AI}.log_warning") as warn:
            ai_helper.generate_ai_response("https://lnkd.in/gKabw7UJ", _profile(), user_id=7,
                                           post_id="urn:li:activity:1")
        assert warn.call_args.kwargs["post_id"] == "urn:li:activity:1"
        assert llm.call_count == 0

    def test_guard_runs_before_any_research_spend(self):
        # Paired with a positive control: research IS reachable from this argument set, so the zero
        # above is the guard short-circuiting rather than a call that never happens anyway.
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)), patch(f"{_AI}.log_warning"), \
             patch(f"{_AI}.research_topic", return_value={}) as research:
            assert ai_helper.generate_ai_response("https://lnkd.in/gKabw7UJ", _profile()) is None
            assert research.call_count == 0
            assert ai_helper.generate_ai_response(_POST_WITH_URL, _profile()) == _GOOD
            assert research.call_count == 1

    def test_an_attached_image_does_not_exempt_a_bodiless_post(self):
        # Deliberate, not an oversight: the contract asks the model to quote a specific point back at
        # the author, which an image cannot supply — that gap is what the invented statistic filled.
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)) as llm, \
             patch(f"{_AI}.log_warning"):
            out = ai_helper.generate_ai_response("https://lnkd.in/gKabw7UJ", _profile(),
                                                 "https://media.licdn.com/x.jpg")
        assert out is None
        assert llm.call_count == 0

    def test_reply_drafting_is_a_separate_contract_and_is_not_gated(self):
        # Replying to a specific comment is grounded in that comment, so a thin post body is not a
        # missing subject there. Explicitly out of scope for #1833.
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Appreciate that — here is why.")) as llm:
            out = ai_helper.generate_ai_response("https://lnkd.in/gKabw7UJ", _profile(),
                                                 post_comment="What did you change first?")
        assert out == "Appreciate that — here is why."
        assert llm.call_count == 1

    def test_a_real_body_that_contains_a_url_still_generates(self):
        # The test that stops the guard being over-broad: the post has words as well as a link.
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)) as llm:
            out = ai_helper.generate_ai_response(_POST_WITH_URL, _profile())
        assert out == _GOOD
        assert llm.call_count == 1
        assert "front aisle" in llm.call_args.kwargs["messages"][1]["content"][0]["text"]


class TestReadableBodyPredicate:
    """The predicate itself, so the boundary is pinned independently of the prompt plumbing."""

    @pytest.mark.parametrize("shape", sorted(_UNREADABLE))
    def test_failed_scrape_shapes_are_unreadable(self, shape):
        from cqc_lem.utilities.ai.ai_helper import _has_readable_post_body
        assert _has_readable_post_body(_UNREADABLE[shape]) is False

    @pytest.mark.parametrize("text", [
        _POST_WITH_URL,
        "Layout beats software.",
        "See https://example.com/report — the pick-time numbers are on page 4.",
        "仓库拣货时间下降了百分之十八。",  # a non-Latin body is still a body
    ])
    def test_real_bodies_are_readable(self, text):
        from cqc_lem.utilities.ai.ai_helper import _has_readable_post_body
        assert _has_readable_post_body(text) is True


class TestTheCallerSkipsRatherThanPublishing:
    """Acceptance criterion 2, end to end through the surface that publishes unattended.

    None has to reach a caller that treats it as SKIP, rather than crashing on it or posting an
    empty comment.
    """

    def test_the_feed_walk_posts_nothing_when_the_body_never_arrived(self):
        from cqc_lem.app.engagement import feed
        from cqc_lem.domain.models import FeedRunContext
        ctx = FeedRunContext(driver=MagicMock(), wait=MagicMock(), my_profile=_profile(),
                             user_id=1, profile_synthesis="synth", prefs={})
        with patch(f"{_FEED}.claim_post_for_comment", return_value=True), \
             patch(f"{_FEED}.select_blueprint", return_value={"format": "expander"}), \
             patch(f"{_FEED}.release_post_claim") as release, \
             patch(f"{_FEED}.post_comment_inline") as posted, \
             patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)) as llm, \
             patch(f"{_AI}.log_warning") as warn:
            engaged = feed._engage_card(ctx, MagicMock(), "feedurn://x",
                                        "https://lnkd.in/gKabw7UJ", "Jane")
        assert engaged is False
        assert posted.call_count == 0, "an ungrounded comment was published"
        assert llm.call_count == 0
        release.assert_called_once()  # the claim is handed back, so a re-scrape can retry the post
        # The caller's own identifier reaches the warning, so the scraper defect is attributable.
        assert warn.call_args.kwargs["post_id"] == "feedurn://x"
