"""Issue #770 — where the (B) writer meets the content plan.

Two seams live here and neither is testable inside the writer itself:
  - the promo SLOT: affiliate promotion replaces the author's own case-study post, it never runs
    beside it, which is what keeps the 10% mix ceiling exact.
  - the APPROVAL hold: an endorsement published under the author's name is never auto-scheduled.
"""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_AC = "cqc_lem.utilities.marketing.affiliate_content"
_AFF = "cqc_lem.utilities.marketing.affiliate"
_ATTR = "cqc_lem.utilities.marketing.attribution"

DISCLOSURE = "#ad - I get free trial time when people sign up through my link."
REF_LINK = "https://app.lem.test/signup?ref=1"
PROMO_POST = f"I plan a month of posts at a time now.\n\n{REF_LINK}\n\n{DISCLOSURE}"


def _owned(stack):
    stack.enter_context(patch(f"{_ATTR}.PUBLIC_BASE_URL", "https://app.lem.test"))
    stack.enter_context(patch(f"{_ATTR}.BRAND_SIGNUP_URL", "https://app.lem.test/signup"))
    stack.enter_context(patch(f"{_ATTR}.MARKETING_OWNED_DOMAINS", ""))
    stack.enter_context(patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True))
    stack.enter_context(patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", DISCLOSURE))
    return stack


class TestPromoSlotClaim:
    def test_a_claimed_promo_slot_writes_the_affiliate_post_instead_of_the_case_study(self):
        from cqc_lem.app.run_content_plan import create_content
        with ExitStack() as stack:
            _owned(stack)
            stack.enter_context(patch(f"{_AC}.claims_promo_slot", return_value=True))
            stack.enter_context(patch(f"{_AC}.generate_promo_post", return_value=PROMO_POST))
            stack.enter_context(patch(f"{_RCP}._engagement_prefs_or_empty", return_value={}))
            stack.enter_context(patch(f"{_RCP}._profile_synthesis_or_none", return_value=None))
            text_post = stack.enter_context(patch(f"{_RCP}.create_text_post"))
            content, video_url = create_content(1, "text", "decision", post_id=10,
                                                content_mix="promo")
        assert content == PROMO_POST
        assert video_url is None
        # The point of claiming a slot rather than adding a post: exactly ONE post exists here.
        text_post.assert_not_called()

    def test_an_unclaimed_slot_writes_the_ordinary_post_and_costs_no_extra_reads(self):
        from cqc_lem.app.run_content_plan import create_content
        with ExitStack() as stack:
            _owned(stack)
            stack.enter_context(patch(f"{_AC}.claims_promo_slot", return_value=False))
            promo = stack.enter_context(patch(f"{_AC}.generate_promo_post"))
            prefs = stack.enter_context(patch(f"{_RCP}._engagement_prefs_or_empty"))
            stack.enter_context(patch(f"{_RCP}.create_text_post",
                                      return_value="  Ordinary post.  "))
            content, _ = create_content(1, "text", "decision", post_id=10, content_mix="promo")
        assert content == "Ordinary post."
        promo.assert_not_called()
        prefs.assert_not_called()

    def test_a_failing_writer_never_costs_the_user_the_post_they_would_have_had(self):
        from cqc_lem.app.run_content_plan import create_content
        with ExitStack() as stack:
            _owned(stack)
            stack.enter_context(patch(f"{_AC}.claims_promo_slot",
                                      side_effect=RuntimeError("consent read exploded")))
            text_post = stack.enter_context(patch(f"{_RCP}.create_text_post",
                                                  return_value="Ordinary post."))
            content, _ = create_content(1, "text", "decision", post_id=10, content_mix="promo")
        assert content == "Ordinary post."
        text_post.assert_called_once()

    def test_carousels_and_videos_are_untouched_by_the_writer(self):
        from cqc_lem.app.run_content_plan import create_content
        with ExitStack() as stack:
            _owned(stack)
            claims = stack.enter_context(patch(f"{_AC}.claims_promo_slot", return_value=True))
            stack.enter_context(patch(f"{_RCP}.create_carousel_content", return_value="Deck."))
            content, _ = create_content(1, "carousel", "decision", post_id=10, content_mix="promo")
        assert content == "Deck."
        claims.assert_not_called()


class TestApprovalHold:
    def _gates(self, stack, content, owner=1):
        from cqc_lem.app.run_content_plan import evaluate_post_gates
        _owned(stack)
        stack.enter_context(patch("cqc_lem.utilities.db.get_post_user_id", return_value=owner))
        stack.enter_context(patch(f"{_RCP}._post_missing_required_asset", return_value=False))
        return evaluate_post_gates(10, content, "text")

    def test_an_affiliate_post_is_held_for_the_authors_explicit_approval(self):
        from cqc_lem.utilities.quality_gates import GATE_AFFILIATE_PROMO, demoting_findings
        with ExitStack() as stack:
            findings = self._gates(stack, PROMO_POST)
        held = [f for f in demoting_findings(findings) if f["gate"] == GATE_AFFILIATE_PROMO]
        assert len(held) == 1
        assert "referral link" in held[0]["explanation"]

    def test_an_ordinary_post_is_never_held_by_this_gate(self):
        from cqc_lem.utilities.quality_gates import GATE_AFFILIATE_PROMO
        with ExitStack() as stack:
            findings = self._gates(stack, "Three lessons from the rebuild.")
        assert not [f for f in findings if f["gate"] == GATE_AFFILIATE_PROMO]

    def test_somebody_elses_referral_link_is_not_this_authors_endorsement(self):
        """Graded on the post OWNER's code, the same way the publish gate grades it."""
        from cqc_lem.utilities.quality_gates import GATE_AFFILIATE_PROMO
        with ExitStack() as stack:
            findings = self._gates(stack, PROMO_POST, owner=2)
        assert not [f for f in findings if f["gate"] == GATE_AFFILIATE_PROMO]

    def test_an_unresolvable_owner_is_never_read_as_this_authors_endorsement(self):
        """Falling through to `user_id=None` would match ANY member's referral code — holding
        somebody's post, over a link that is not theirs, with copy calling it their paid
        endorsement.
        """
        from cqc_lem.app.run_content_plan import evaluate_post_gates
        from cqc_lem.utilities.quality_gates import GATE_AFFILIATE_PROMO
        with ExitStack() as stack:
            _owned(stack)
            stack.enter_context(patch("cqc_lem.utilities.db.get_post_user_id", return_value=None))
            stack.enter_context(patch(f"{_RCP}._post_missing_required_asset", return_value=False))
            findings = evaluate_post_gates(10, PROMO_POST, "text")
        assert not [f for f in findings if f["gate"] == GATE_AFFILIATE_PROMO]

    def test_unreadable_ownership_leaves_the_publish_gate_as_the_enforcer(self):
        from cqc_lem.app.run_content_plan import evaluate_post_gates
        from cqc_lem.utilities.quality_gates import GATE_AFFILIATE_PROMO
        with ExitStack() as stack:
            _owned(stack)
            stack.enter_context(patch("cqc_lem.utilities.db.get_post_user_id",
                                      side_effect=RuntimeError("db down")))
            stack.enter_context(patch(f"{_RCP}._post_missing_required_asset", return_value=False))
            findings = evaluate_post_gates(10, PROMO_POST, "text")
        assert not [f for f in findings if f["gate"] == GATE_AFFILIATE_PROMO]
