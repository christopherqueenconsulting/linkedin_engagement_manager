"""Issue #737 — the FTC disclosure gate at the ONE publish choke point.

The acceptance criterion is blunt: affiliate content without the disclosure is BLOCKED. These drive
the real `post_to_linkedin` task so the gate is proven where it actually runs, including the seam
that would otherwise defeat it — #392 carrying the referral link out of the body and into the first
comment before the gate ever sees it.
"""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_AFF = "cqc_lem.utilities.marketing.affiliate"
_ATTR = "cqc_lem.utilities.marketing.attribution"

DISCLOSURE = "#ad — I get free trial time when people sign up through my link."
REF_LINK = "https://app.lem.test/signup?utm_source=referral&ref=1"


def _post_patches(stack, content, prefs=None):
    from cqc_lem.utilities.db import PostType
    targets = {
        "get_post_status": {"return_value": "approved"},
        "get_user_password_pair_by_id": {"return_value": ("u@example.com", "pw")},
        "get_post_content": {"return_value": content},
        "get_engagement_preferences": {"return_value": prefs if prefs is not None else {}},
        "get_post_type": {"return_value": PostType.TEXT},
        "share_on_linkedin": {"return_value": "urn:li:ugcPost:1"},
        "insert_new_log": {},
        "update_db_post_status": {},
        "update_db_post_content": {},
        "update_db_post_first_comment_link": {},
        "sweep_reply_comments": {},
        "auto_seed_comment_on_post": {},
        "auto_second_wave_comment": {},
    }
    mocks = {name: stack.enter_context(patch(f"{_RA}.{name}", **kw)) for name, kw in targets.items()}
    stack.enter_context(patch(f"{_ATTR}.PUBLIC_BASE_URL", "https://app.lem.test"))
    stack.enter_context(patch(f"{_ATTR}.BRAND_SIGNUP_URL", "https://app.lem.test/signup"))
    stack.enter_context(patch(f"{_ATTR}.MARKETING_OWNED_DOMAINS", ""))
    stack.enter_context(patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", DISCLOSURE))
    stack.enter_context(patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True))
    return mocks


class TestAffiliateDisclosureGate:
    def test_undisclosed_affiliate_post_is_blocked_and_flagged(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        from cqc_lem.utilities.db import PostStatus
        with ExitStack() as stack:
            m = _post_patches(stack, f"This tool changed my week. {REF_LINK}")
            result = post_to_linkedin.run(1, 10)

        m["share_on_linkedin"].assert_not_called()
        m["update_db_post_status"].assert_called_once_with(10, PostStatus.ERROR)
        assert "missing_ftc_disclosure" in result
        # The user is told exactly what to do, in the log the post's error surfaces from.
        assert "disclosure" in m["insert_new_log"].call_args.kwargs["message"].lower()

    def test_disclosed_affiliate_post_publishes(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            m = _post_patches(stack, f"This tool changed my week. {REF_LINK}\n\n{DISCLOSURE}",
                              prefs={"link_in_first_comment": False})
            post_to_linkedin.run(1, 10)
        m["share_on_linkedin"].assert_called_once()

    def test_moving_the_link_to_the_first_comment_does_not_defeat_the_gate(self):
        """#392 strips the link out of the body before the gate runs. Grading only the trimmed body
        would let every affiliate post through by having its link carried — so the carried link is
        graded too.
        """
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            m = _post_patches(stack, f"This tool changed my week.\n\nHere: {REF_LINK}",
                              prefs={"link_in_first_comment": True})
            result = post_to_linkedin.run(1, 10)
        m["share_on_linkedin"].assert_not_called()
        assert "missing_ftc_disclosure" in result

    def test_a_normal_post_is_never_gated(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            m = _post_patches(stack, "Three lessons from the rebuild.")
            post_to_linkedin.run(1, 10)
        m["share_on_linkedin"].assert_called_once()
        m["update_db_post_status"].assert_called_once()   # POSTED, not ERROR

    def test_a_deployment_with_no_disclosure_configured_cannot_publish_affiliate_content(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            m = _post_patches(stack, f"Use my link: {REF_LINK}",
                              prefs={"link_in_first_comment": False})
            stack.enter_context(patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", ""))
            result = post_to_linkedin.run(1, 10)
        m["share_on_linkedin"].assert_not_called()
        assert "no_disclosure_configured" in result

    def test_a_published_affiliate_post_is_counted_as_published(self):
        """Issue #770: 'generated' and 'published' are different numbers — a promo post the author
        never approved is one and not the other, and that gap is the read on the program.
        """
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            _post_patches(stack, f"This tool changed my week. {REF_LINK}\n\n{DISCLOSURE}",
                          prefs={"link_in_first_comment": False})
            track = stack.enter_context(
                patch("cqc_lem.utilities.marketing.affiliate_content._track"))
            post_to_linkedin.run(1, 10)
        assert track.call_args[0][0] == "affiliate_promo_published"
        assert track.call_args[1]["post_id"] == 10

    def test_an_ordinary_published_post_is_not_counted_as_affiliate(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            _post_patches(stack, "Three lessons from the rebuild.")
            track = stack.enter_context(
                patch("cqc_lem.utilities.marketing.affiliate_content._track"))
            post_to_linkedin.run(1, 10)
        track.assert_not_called()

    def test_a_broken_gate_publishes_rather_than_blocking_every_post(self):
        """This is a compliance check on a rare shape of post, not a new way for posting to break."""
        from cqc_lem.app.run_automation import post_to_linkedin
        with ExitStack() as stack:
            m = _post_patches(stack, "Three lessons from the rebuild.")
            stack.enter_context(patch(f"{_AFF}.disclosure_report", side_effect=RuntimeError("boom")))
            post_to_linkedin.run(1, 10)
        m["share_on_linkedin"].assert_called_once()
