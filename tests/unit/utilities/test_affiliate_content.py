"""Issue #770 — the (B) writer: LEM-promotional content from the user's own account.

The acceptance criteria this file owns:
  - With (B) consent, a post that promotes LEM is produced and it ALREADY carries the disclosure.
  - With (B) off or consent missing, NOTHING promotional is generated.
  - The writer respects the 10% promo mix cap (it claims a promo slot, it never adds a post) and the
    human-pacing daily budget.
  - Consent-off / consent-on / disclosure-present / disclosure-missing are all covered.
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.marketing import affiliate_content

pytestmark = pytest.mark.unit

_AC = "cqc_lem.utilities.marketing.affiliate_content"
_AFF = "cqc_lem.utilities.marketing.affiliate"
_ATTR = "cqc_lem.utilities.marketing.attribution"
_HP = "cqc_lem.utilities.human_pacing"
_DB = "cqc_lem.utilities.db"
_AI = "cqc_lem.utilities.ai.ai_helper"

DISCLOSURE = "#ad - I get free trial time when people sign up through my link."
CONSENTED = {"status": "enrolled", "promo_content_opt_in": True,
             "promo_consent_at": "2026-07-27 10:00:00"}
NO_CONSENT = {"status": "enrolled", "promo_content_opt_in": False, "promo_consent_at": None}

# Plain copy with no banned lexicon, no contrastive framing and no reflex closer, so the slop lint
# passes and the tests are exercising the affiliate rules rather than the linter.
DRAFT = ("I schedule my LinkedIn posts a month at a time now.\n\n"
         "The tool writes the drafts and puts them out when my audience is around. I read every one "
         "before it goes.\n\n"
         "If keeping up on here is a chore for you too, it has a free trial.")


def _llm(text):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
    return MagicMock(return_value=response)


def _env(stack, *, enrollment=CONSENTED, disclosure=DISCLOSURE, enabled=True, every_n=1,
         per_day=1, budget=1, used=0):
    """Everything outside the writer: consent, the owned signup domain, the disclosure, pacing."""
    stack.enter_context(patch(f"{_ATTR}.PUBLIC_BASE_URL", "https://app.lem.test"))
    stack.enter_context(patch(f"{_ATTR}.BRAND_SIGNUP_URL", "https://app.lem.test/signup"))
    stack.enter_context(patch(f"{_ATTR}.MARKETING_OWNED_DOMAINS", ""))
    stack.enter_context(patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True))
    stack.enter_context(patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", disclosure))
    stack.enter_context(patch(f"{_AC}.AFFILIATE_PROMO_CONTENT_ENABLED", enabled))
    stack.enter_context(patch(f"{_AC}.AFFILIATE_PROMO_EVERY_N_PROMO_SLOTS", every_n))
    stack.enter_context(patch(f"{_AC}.AFFILIATE_PROMO_MAX_PER_DAY", per_day))
    stack.enter_context(patch(f"{_DB}.get_affiliate_enrollment", return_value=enrollment))
    stack.enter_context(patch(f"{_HP}.daily_budget", return_value=budget))
    stack.enter_context(patch(f"{_HP}.actions_used_today", return_value=used))
    stack.enter_context(patch(f"{_HP}.record_action"))
    return stack


def _generate(stack, draft=DRAFT, post_id=10, content_mix="promo", user_id=1):
    call = _llm(draft)
    stack.enter_context(patch(f"{_AI}._call_llm", call))
    content = affiliate_content.generate_promo_post(user_id, post_id=post_id,
                                                    content_mix=content_mix)
    return content, call


# --- consent is the only authorisation ------------------------------------------------------------

class TestConsentGate:
    def test_consent_on_produces_a_disclosed_promotional_post(self):
        from cqc_lem.utilities.marketing.affiliate import has_disclosure
        with ExitStack() as stack:
            _env(stack)
            content, call = _generate(stack)
            assert content
            call.assert_called_once()
            # It promotes LEM with the member's OWN referral link, already disclosed.
            assert "ref=1" in content
            assert affiliate_content.is_affiliate_post(content, user_id=1)
            assert has_disclosure(content)

    def test_no_consent_writes_nothing_and_never_calls_the_model(self):
        with ExitStack() as stack:
            _env(stack, enrollment=NO_CONSENT)
            content, call = _generate(stack)
        assert content is None
        call.assert_not_called()

    def test_opted_out_user_writes_nothing(self):
        with ExitStack() as stack:
            _env(stack, enrollment={"status": "opted_out", "promo_content_opt_in": True,
                                    "promo_consent_at": "2026-07-27"})
            content, _ = _generate(stack)
        assert content is None

    def test_unreadable_consent_is_a_no(self):
        with ExitStack() as stack:
            _env(stack)
            stack.enter_context(patch(f"{_DB}.get_affiliate_enrollment",
                                      side_effect=RuntimeError("db down")))
            content, call = _generate(stack)
        assert content is None
        call.assert_not_called()

    def test_generator_kill_switch_stops_the_writer_without_touching_consent(self):
        with ExitStack() as stack:
            _env(stack, enabled=False)
            content, call = _generate(stack)
        assert content is None
        call.assert_not_called()
        # Consent itself is untouched — flipping the switch back writes again.
        with ExitStack() as stack:
            _env(stack, enabled=True)
            content, _ = _generate(stack)
        assert content


# --- the disclosure is stamped at generation, not left to the publish gate -------------------------

class TestDisclosure:
    def test_a_draft_with_no_disclosure_is_disclosed_before_it_is_returned(self):
        """The model writing an undisclosed draft must not produce an undisclosed post."""
        with ExitStack() as stack:
            _env(stack)
            content, _ = _generate(stack, draft="Great tool. https://app.lem.test/signup?ref=1")
        assert content
        from cqc_lem.utilities.marketing.affiliate import disclosure_report
        assert disclosure_report(content, user_id=1, tagged=True)["ok"] is True

    def test_a_draft_that_already_discloses_is_not_disclosed_twice(self):
        with ExitStack() as stack:
            _env(stack)
            content, _ = _generate(stack, draft=f"{DRAFT}\n\n{DISCLOSURE}")
        assert content.count("free trial time when people sign up") == 1

    def test_a_deployment_with_no_disclosure_configured_writes_nothing(self):
        """Blank means 'affiliate content cannot be published here', never 'no disclosure needed' —
        so there is no point spending a generation on copy the publish gate would refuse."""
        with ExitStack() as stack:
            _env(stack, disclosure="")
            content, call = _generate(stack)
        assert content is None
        call.assert_not_called()

    def test_a_deployment_that_cannot_publish_affiliate_content_says_so(self):
        """The one refusal that is a MISCONFIGURATION rather than the healthy common case, and the
        content plan short-circuits on `claims_promo_slot` — so it has to be reported from there or
        the deployment silently never promotes."""
        with ExitStack() as stack:
            _env(stack, disclosure="")
            warn = stack.enter_context(patch("cqc_lem.utilities.logger.log_warning"))
            assert affiliate_content.claims_promo_slot(1, 10, "promo") is False
        assert warn.call_count == 1
        assert "AFFILIATE_DISCLOSURE_TEXT" in warn.call_args[0][0]

    def test_declining_an_ordinary_slot_is_not_a_warning(self):
        """A user who never consented plans promo slots forever; one warning per slot is noise."""
        with ExitStack() as stack:
            _env(stack, enrollment=NO_CONSENT)
            warn = stack.enter_context(patch("cqc_lem.utilities.logger.log_warning"))
            assert affiliate_content.claims_promo_slot(1, 10, "promo") is False
        warn.assert_not_called()

    def test_copy_that_cannot_be_made_compliant_is_blocked_not_shipped(self):
        with ExitStack() as stack:
            _env(stack)
            stack.enter_context(patch(f"{_AC}.apply_disclosure", side_effect=lambda t, **kw: t))
            track = stack.enter_context(patch(f"{_AC}._track"))
            content, _ = _generate(stack, draft="Great tool. https://app.lem.test/signup?ref=1")
        assert content is None
        assert track.call_args[0][0] == "affiliate_promo_blocked"
        assert track.call_args[1]["reason"] == affiliate_content.REASON_UNDISCLOSED


# --- the referral link is the one URL that has to survive verbatim --------------------------------

class TestReferralLinkIntegrity:
    def test_the_link_the_model_reproduced_survives_the_markdown_pass(self):
        """The prompt tells the model to reproduce the link, and `sanitize_for_linkedin` strips
        markdown italics — `_word_` being exactly the shape of `utm_source=…&utm_medium=…`. Left
        un-repaired the post publishes `utmsource=`, a dead link, AND a second clean copy beside it
        because the mangled one no longer matches."""
        from cqc_lem.utilities.marketing.affiliate import link_for_user
        with ExitStack() as stack:
            _env(stack)
            link = link_for_user(1)
            assert "utm_source=" in link  # the exact shape the italic pass eats
            content, _ = _generate(stack, draft=f"{DRAFT}\n\n{link}")
        assert link in content
        assert content.count("https://") == 1
        assert "utmsource" not in content

    def test_a_draft_that_omitted_the_link_still_gets_exactly_one(self):
        from cqc_lem.utilities.marketing.affiliate import link_for_user
        with ExitStack() as stack:
            _env(stack)
            link = link_for_user(1)
            content, _ = _generate(stack)
        assert content.count(link) == 1


# --- the writer takes a slot, it never adds one ---------------------------------------------------

class TestPromoMixCap:
    @pytest.mark.parametrize("mix", ["value", "authority", None, "", "unknown"])
    def test_only_the_governors_promo_slot_can_be_claimed(self, mix):
        """The 10% ceiling holds by construction: affiliate promotion replaces the author's own
        case-study post in a slot the mix governor already allowed, so it can never raise the ratio."""
        with ExitStack() as stack:
            _env(stack)
            content, call = _generate(stack, content_mix=mix)
        assert content is None
        call.assert_not_called()

    def test_cadence_makes_lem_promotion_rarer_than_the_promo_slot_itself(self):
        with ExitStack() as stack:
            _env(stack, every_n=3)
            claimed = [affiliate_content.claims_promo_slot(1, pid, "promo") for pid in range(9)]
        assert claimed == [True, False, False, True, False, False, True, False, False]

    def test_a_misconfigured_cadence_never_means_every_slot(self):
        with ExitStack() as stack:
            _env(stack, every_n=0)
            assert affiliate_content.promo_slot_cadence() == 1

    def test_no_referral_link_means_nothing_to_promote(self):
        with ExitStack() as stack:
            _env(stack)
            stack.enter_context(patch(f"{_ATTR}.BRAND_SIGNUP_URL", ""))
            stack.enter_context(patch(f"{_ATTR}.PUBLIC_BASE_URL", ""))
            content, call = _generate(stack)
        assert content is None
        call.assert_not_called()


# --- human pacing ----------------------------------------------------------------------------------

class TestPacing:
    def test_the_days_paced_budget_is_a_ceiling(self):
        with ExitStack() as stack:
            _env(stack, budget=1, used=1)
            content, call = _generate(stack)
        assert content is None
        call.assert_not_called()

    def test_a_pacing_rest_day_writes_nothing(self):
        """`daily_budget` returns 0 on a rest day — the writer inherits that with no rule of its own."""
        with ExitStack() as stack:
            _env(stack, budget=0, used=0)
            content, _ = _generate(stack)
        assert content is None

    def test_a_generated_post_is_recorded_against_the_days_budget(self):
        with ExitStack() as stack:
            _env(stack)
            recorded = stack.enter_context(patch(f"{_HP}.record_action"))
            content, _ = _generate(stack)
        assert content
        recorded.assert_called_once_with(1, "affiliate_promo")

    def test_pacing_failure_fails_open(self):
        """Consent and the promo slot are the hard limits; neither of them lives in Redis."""
        with ExitStack() as stack:
            _env(stack)
            stack.enter_context(patch(f"{_HP}.daily_budget", side_effect=RuntimeError("no redis")))
            content, _ = _generate(stack)
        assert content

    def test_a_zero_per_day_cap_writes_nothing(self):
        with ExitStack() as stack:
            _env(stack, per_day=0)
            content, call = _generate(stack)
        assert content is None
        call.assert_not_called()


# --- the slop lint applies to promotional copy too -------------------------------------------------

class TestQualityGates:
    def test_a_slopped_draft_is_regenerated_then_blocked(self):
        slop = ("Let's dive into this game changer. It's not just a tool, it's a revolution. "
                "Thoughts?")
        with ExitStack() as stack:
            _env(stack)
            track = stack.enter_context(patch(f"{_AC}._track"))
            stack.enter_context(patch("cqc_lem.utilities.ai.slop_lint.slop_max_attempts",
                                      return_value=2))
            content, call = _generate(stack, draft=slop)
        assert content is None
        assert call.call_count == 2
        assert track.call_args[0][0] == "affiliate_promo_blocked"
        assert track.call_args[1]["reason"] == affiliate_content.REASON_SLOP

    def test_an_empty_model_response_blocks_rather_than_shipping_a_bare_link(self):
        with ExitStack() as stack:
            _env(stack)
            track = stack.enter_context(patch(f"{_AC}._track"))
            content, _ = _generate(stack, draft="")
        assert content is None
        assert track.call_args[1]["reason"] == affiliate_content.REASON_EMPTY_DRAFT

    def test_a_model_error_never_takes_the_ordinary_promo_post_down_with_it(self):
        with ExitStack() as stack:
            _env(stack)
            stack.enter_context(patch(f"{_AI}._call_llm", side_effect=RuntimeError("502")))
            content = affiliate_content.generate_promo_post(1, post_id=10, content_mix="promo")
        assert content is None

    def test_the_writer_states_nothing_about_the_product_it_was_not_given(self):
        """The prompt's allow-list is the product-fact list and nothing else — the same posture the
        story bank takes for personal specifics."""
        with ExitStack() as stack:
            _env(stack)
            content, call = _generate(stack)
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert all(fact in prompt for fact in affiliate_content.PRODUCT_FACTS)
        system = call.call_args.kwargs["messages"][0]["content"]
        assert "no metrics" in system.lower() or "no results" in system.lower()


# --- published telemetry ---------------------------------------------------------------------------

class TestPublishedEvent:
    def test_publishing_an_affiliate_post_emits_the_event(self):
        with ExitStack() as stack:
            _env(stack)
            track = stack.enter_context(patch(f"{_AC}._track"))
            emitted = affiliate_content.record_promo_published(
                1, 10, f"Great tool.\n\n{DISCLOSURE}",
                first_comment_links=["https://app.lem.test/signup?ref=1"],
                post_url="https://linkedin.com/feed/update/urn:1/")
        assert emitted is True
        assert track.call_args[0][0] == "affiliate_promo_published"
        assert track.call_args[1]["link_in_first_comment"] is True

    def test_an_ordinary_post_emits_nothing(self):
        with ExitStack() as stack:
            _env(stack)
            track = stack.enter_context(patch(f"{_AC}._track"))
            emitted = affiliate_content.record_promo_published(1, 10, "Three lessons from the rebuild.")
        assert emitted is False
        track.assert_not_called()

    def test_analytics_never_fails_a_published_post(self):
        with ExitStack() as stack:
            _env(stack)
            stack.enter_context(patch(f"{_AC}._track", side_effect=RuntimeError("posthog down")))
            assert affiliate_content.record_promo_published(
                1, 10, f"Use my link https://app.lem.test/signup?ref=1 {DISCLOSURE}") is False
