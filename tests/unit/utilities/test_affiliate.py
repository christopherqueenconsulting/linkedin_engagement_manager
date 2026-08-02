"""Unit tests for the affiliate/ambassador program's policy core (issue #737).

The acceptance criteria this file owns:
  - (A) and (B) are independently toggleable, and (B) is OFF unless consent was RECORDED.
  - affiliate content without the FTC disclosure is BLOCKED.
  - self-referral and duplicate referrals are rejected.
  - the per-user reward cap holds.
"""
from unittest.mock import patch

import pytest

from cqc_lem.utilities.marketing import affiliate

_AFF = "cqc_lem.utilities.marketing.affiliate"
_ATTR = "cqc_lem.utilities.marketing.attribution"

DISCLOSURE = "#ad — I get free LinkedIn Engagement Manager trial time when people sign up through my link."


@pytest.fixture
def owned_site():
    """PUBLIC_BASE_URL/BRAND_SIGNUP_URL pointing at a host we own, so referral links are tagged."""
    with patch(f"{_ATTR}.PUBLIC_BASE_URL", "https://app.lem.test"), \
         patch(f"{_ATTR}.BRAND_SIGNUP_URL", "https://app.lem.test/signup"), \
         patch(f"{_ATTR}.MARKETING_OWNED_DOMAINS", ""):
        yield


# --- (A)/(B) separation ---------------------------------------------------------------------------

def test_enrollment_defaults_on_and_is_not_promo_consent():
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True), \
         patch(f"{_AFF}.AFFILIATE_DEFAULT_ENROLLED", True):
        assert affiliate.enrollment_default() == affiliate.STATUS_ENROLLED
    # An enrolled user with no recorded consent may NOT have promo content published from their
    # account — enrollment never implies (B).
    enrolled = {"status": affiliate.STATUS_ENROLLED, "promo_content_opt_in": False,
                "promo_consent_at": None}
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True):
        assert affiliate.promo_content_allowed(enrolled) is False


@pytest.mark.parametrize("enrollment,expected", [
    ({"status": "enrolled", "promo_content_opt_in": True, "promo_consent_at": "2026-07-27"}, True),
    # The flag set but no consent timestamp: something other than the user said yes. Refuse.
    ({"status": "enrolled", "promo_content_opt_in": True, "promo_consent_at": None}, False),
    ({"status": "opted_out", "promo_content_opt_in": True, "promo_consent_at": "2026-07-27"}, False),
    ({"status": "enrolled", "promo_content_opt_in": False, "promo_consent_at": "2026-07-27"}, False),
    (None, False),
])
def test_promo_content_allowed(enrollment, expected):
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True):
        assert affiliate.promo_content_allowed(enrollment) is expected


def test_program_disabled_blocks_everything():
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", False):
        assert affiliate.program_enabled() is False
        assert affiliate.is_eligible(True) is False
        assert affiliate.enrollment_default() == affiliate.STATUS_OPTED_OUT
        assert affiliate.promo_content_allowed(
            {"status": "enrolled", "promo_content_opt_in": True, "promo_consent_at": "x"}) is False


def test_company_page_boundary_is_opt_in():
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True):
        with patch(f"{_AFF}.AFFILIATE_REQUIRE_COMPANY_PAGE", False):
            assert affiliate.is_eligible(has_company_page=False) is True
        with patch(f"{_AFF}.AFFILIATE_REQUIRE_COMPANY_PAGE", True):
            assert affiliate.is_eligible(has_company_page=False) is False
            assert affiliate.is_eligible(has_company_page=True) is True


# --- FTC disclosure -------------------------------------------------------------------------------

def test_referral_link_makes_content_affiliate(owned_site):
    body = "Been using this for 3 months. https://app.lem.test/signup?ref=42 — worth a look."
    assert affiliate.carries_referral_link(body) is True
    assert affiliate.carries_referral_link(body, user_id=42) is True
    # Somebody ELSE's link is not this user's material connection.
    assert affiliate.carries_referral_link(body, user_id=7) is False


def test_third_party_and_bare_links_are_not_affiliate(owned_site):
    assert affiliate.carries_referral_link("See https://example.com/x?ref=42") is False
    assert affiliate.carries_referral_link("See https://app.lem.test/signup") is False
    assert affiliate.carries_referral_link("") is False


def test_affiliate_content_without_disclosure_is_blocked(owned_site):
    with patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", DISCLOSURE):
        report = affiliate.disclosure_report("Try it: https://app.lem.test/signup?ref=42")
    assert report["is_affiliate"] is True
    assert report["ok"] is False
    assert report["reason"] == "missing_ftc_disclosure"


def test_affiliate_content_with_disclosure_passes(owned_site):
    with patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", DISCLOSURE):
        body = f"Try it: https://app.lem.test/signup?ref=42\n\n{DISCLOSURE}"
        assert affiliate.disclosure_report(body)["ok"] is True


@pytest.mark.parametrize("marker", ["#ad", "#sponsored", "#affiliate",
                                    "I'm an affiliate and earn trial time"])
def test_reworded_disclosures_are_accepted(owned_site, marker):
    """An author editing their own draft must not be blocked for not using our exact sentence."""
    with patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", DISCLOSURE):
        body = f"Great tool. {marker}\nhttps://app.lem.test/signup?ref=42"
        assert affiliate.disclosure_report(body)["ok"] is True


def test_non_affiliate_content_is_never_gated(owned_site):
    with patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", DISCLOSURE):
        report = affiliate.disclosure_report("A normal post about hiring engineers.")
    assert report["is_affiliate"] is False
    assert report["ok"] is True


def test_tagged_promo_content_needs_disclosure_even_without_a_link(owned_site):
    """(B) content is affiliate promotion whether or not the #392 split left a link in the body."""
    with patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", DISCLOSURE):
        report = affiliate.disclosure_report("LEM writes my LinkedIn posts now.", tagged=True)
    assert report["ok"] is False and report["reason"] == "missing_ftc_disclosure"


def test_blank_disclosure_config_blocks_rather_than_waives(owned_site):
    with patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", ""):
        report = affiliate.disclosure_report("https://app.lem.test/signup?ref=42")
    assert report["ok"] is False and report["reason"] == "no_disclosure_configured"


def test_apply_disclosure_is_idempotent_and_scoped(owned_site):
    with patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", DISCLOSURE):
        body = "Try it: https://app.lem.test/signup?ref=42"
        stamped = affiliate.apply_disclosure(body)
        assert DISCLOSURE in stamped
        assert affiliate.apply_disclosure(stamped) == stamped
        assert affiliate.disclosure_report(stamped)["ok"] is True
        # Nothing is appended to a post that is not affiliate promotion.
        assert affiliate.apply_disclosure("Ordinary post.") == "Ordinary post."


# --- reward sizing --------------------------------------------------------------------------------

def test_reward_cap_grants_the_remainder_rather_than_refusing():
    with patch(f"{_AFF}.AFFILIATE_MAX_REWARD_DAYS", 90):
        assert affiliate.grantable_days(0, 14) == 14
        assert affiliate.grantable_days(80, 14) == 10
        assert affiliate.grantable_days(90, 14) == 0
        assert affiliate.grantable_days(120, 14) == 0


def test_negative_env_values_never_produce_negative_rewards():
    with patch(f"{_AFF}.AFFILIATE_ENROLLMENT_BONUS_DAYS", -5), \
         patch(f"{_AFF}.AFFILIATE_REFERRAL_BONUS_DAYS", -1), \
         patch(f"{_AFF}.AFFILIATE_MAX_REWARD_DAYS", -1):
        assert affiliate.enrollment_bonus_days() == 0
        assert affiliate.referral_bonus_days() == 0
        assert affiliate.max_reward_days() == 0


@pytest.mark.parametrize("raw,expected", [("42", 42), (" 42 ", 42), ("abc", None), ("", None),
                                          (None, None), ("0", None), ("-3", None)])
def test_parse_referrer_id(raw, expected):
    assert affiliate.parse_referrer_id(raw) == expected


# --- referral attribution -------------------------------------------------------------------------

def _patch_db(**overrides):
    """Patch the db functions `attribute_referral` reaches for, returning the recorded calls."""
    calls = {}

    def _record(referrer, referred, code, status="pending", reject_reason=None):
        calls["record"] = {"referrer": referrer, "referred": referred, "code": code,
                           "status": status, "reason": reject_reason}
        return overrides.get("referral_id", 1)

    return calls, [
        patch("cqc_lem.utilities.db.record_affiliate_referral", side_effect=_record),
        patch("cqc_lem.utilities.db.get_affiliate_enrollment",
              return_value=overrides.get("enrollment", {"status": "enrolled"})),
        patch("cqc_lem.utilities.db.get_user_email", return_value=overrides.get("email", "a@b.c")),
        patch("cqc_lem.utilities.db.ensure_affiliate_enrollment",
              return_value=overrides.get("backfilled", {"status": "enrolled"})),
        patch("cqc_lem.utilities.db.grant_affiliate_trial_days",
              return_value={"granted": True, "reason": "granted", "days": 7}),
        patch("cqc_lem.utilities.db.get_company_linked_in_url_for_user", return_value="https://li/c"),
        patch("cqc_lem.utilities.observability.track_affiliate_event"),
    ]


def _run_attribute(referred_id, attribution, **overrides):
    calls, patches = _patch_db(**overrides)
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True):
        for p in patches:
            p.start()
        try:
            result = affiliate.attribute_referral(referred_id, attribution)
        finally:
            for p in patches:
                p.stop()
    return result, calls


def test_self_referral_is_rejected_and_recorded():
    result, calls = _run_attribute(42, {"ref": "42"})
    assert result["status"] == affiliate.REFERRAL_REJECTED
    assert result["reason"] == affiliate.REJECT_SELF_REFERRAL
    # Recorded, not dropped: a fraud signal we can count.
    assert calls["record"]["status"] == affiliate.REFERRAL_REJECTED


def test_duplicate_referral_is_rejected():
    result, _ = _run_attribute(9, {"ref": "42"}, referral_id=None)
    assert result["status"] == affiliate.REFERRAL_REJECTED
    assert result["reason"] == affiliate.REJECT_DUPLICATE


def test_referral_from_an_opted_out_member_pays_nothing():
    result, calls = _run_attribute(9, {"ref": "42"}, enrollment={"status": "opted_out"})
    assert result["reason"] == affiliate.REJECT_NOT_ENROLLED
    assert calls["record"]["status"] == affiliate.REFERRAL_REJECTED


def test_referral_from_an_unknown_referrer_is_rejected():
    result, _ = _run_attribute(9, {"ref": "999"}, enrollment=None, email=None)
    assert result["reason"] == affiliate.REJECT_UNKNOWN_REFERRER


def test_a_referrer_who_predates_the_program_is_enrolled_not_rejected():
    """The whole existing user base has no enrollment row until they open the Account page. Their
    links have to work, or the program launches with every referral silently zeroed."""
    result, calls = _run_attribute(9, {"ref": "42"}, enrollment=None)
    assert result["status"] == affiliate.REFERRAL_PENDING
    assert calls["record"]["status"] == affiliate.REFERRAL_PENDING


def test_a_backfill_that_lands_opted_out_still_pays_nothing():
    result, _ = _run_attribute(9, {"ref": "42"}, enrollment=None,
                               backfilled={"status": "opted_out"})
    assert result["reason"] == affiliate.REJECT_NOT_ENROLLED


def test_valid_referral_is_pending_not_converted():
    """A signup earns nothing on its own — the reward waits for activation."""
    result, calls = _run_attribute(9, {"ref": "42"})
    assert result["status"] == affiliate.REFERRAL_PENDING
    assert result["reason"] is None
    assert calls["record"]["status"] == affiliate.REFERRAL_PENDING


def test_signup_without_a_ref_attributes_nothing():
    result, calls = _run_attribute(9, {"utm_source": "linkedin"})
    assert result is None
    assert "record" not in calls


# --- edge-case coverage for uncovered branches ------------------------------------------------------

def test_program_disabled_short_circuits_everything():
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", False):
        assert affiliate.enroll_user(1) == {}
        assert affiliate.attribute_referral(1, {"ref": "2"}) is None
        assert affiliate.convert_referral(1) is None


def test_has_disclosure_returns_false_for_empty_text():
    assert affiliate.has_disclosure(None) is False
    assert affiliate.has_disclosure("") is False


def test_apply_disclosure_is_a_no_op_without_configured_sentence(owned_site):
    with patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", ""):
        body = "Try it: https://app.lem.test/signup?ref=42"
        assert affiliate.apply_disclosure(body) == body


def test_company_page_query_only_runs_when_boundary_is_on():
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True), \
         patch(f"{_AFF}.AFFILIATE_REQUIRE_COMPANY_PAGE", True), \
         patch("cqc_lem.utilities.db.get_company_linked_in_url_for_user", return_value="https://li/c") as mock_url:
        assert affiliate._company_page(1) is True
        mock_url.assert_called_once_with(1)

    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True), \
         patch(f"{_AFF}.AFFILIATE_REQUIRE_COMPANY_PAGE", True), \
         patch("cqc_lem.utilities.db.get_company_linked_in_url_for_user", side_effect=RuntimeError("db")):
        assert affiliate._company_page(1) is False

    # Boundary off: no query at all, regardless of what the DB would do.
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True), \
         patch(f"{_AFF}.AFFILIATE_REQUIRE_COMPANY_PAGE", False), \
         patch("cqc_lem.utilities.db.get_company_linked_in_url_for_user") as mock_url:
        assert affiliate._company_page(1) is True
        mock_url.assert_not_called()


def test_enroll_user_is_a_no_op_when_ineligible():
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True), \
         patch(f"{_AFF}.AFFILIATE_REQUIRE_COMPANY_PAGE", True), \
         patch("cqc_lem.utilities.db.get_company_linked_in_url_for_user", return_value=None):
        assert affiliate.enroll_user(1) == {}


def _enroll(bonus_days, created, grant=None):
    """Run `enroll_user` against a stubbed db and return the emitted affiliate events."""
    row = {"status": affiliate.STATUS_ENROLLED, "created": created}
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True), \
         patch(f"{_AFF}.AFFILIATE_DEFAULT_ENROLLED", True), \
         patch(f"{_AFF}.AFFILIATE_REQUIRE_COMPANY_PAGE", False), \
         patch(f"{_AFF}.AFFILIATE_ENROLLMENT_BONUS_DAYS", bonus_days), \
         patch("cqc_lem.utilities.db.ensure_affiliate_enrollment", return_value=row), \
         patch("cqc_lem.utilities.db.grant_affiliate_trial_days",
               return_value=grant or {"granted": True, "reason": "granted", "days": bonus_days}) as grant_mock, \
         patch("cqc_lem.utilities.observability.track_affiliate_event") as event:
        affiliate.enroll_user(1)
    return event.call_args_list, grant_mock


def test_joining_is_counted_even_when_no_join_bonus_is_configured():
    """The shipped policy pays nothing for joining, so `affiliate_enrolled` cannot hang off a grant —
    the marketing funnel still has to count the enrollment."""
    events, grant = _enroll(bonus_days=0, created=True)
    assert grant.call_count == 0
    assert [c.args[0] for c in events] == ["affiliate_enrolled"]
    assert events[0].kwargs["bonus_days"] == 0


def test_a_repeat_page_load_does_not_re_emit_the_enrollment_event():
    events, grant = _enroll(bonus_days=0, created=False)
    assert grant.call_count == 0
    assert events == []


def test_a_configured_join_bonus_is_still_granted_and_reported():
    events, grant = _enroll(bonus_days=7, created=True)
    grant.assert_called_once()
    assert events[0].kwargs["bonus_days"] == 7


def test_affiliate_state_reports_ineligible_when_company_page_required_and_missing():
    with patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True), \
         patch(f"{_AFF}.AFFILIATE_REQUIRE_COMPANY_PAGE", True), \
         patch("cqc_lem.utilities.db.get_company_linked_in_url_for_user", return_value=None), \
         patch("cqc_lem.utilities.db.get_affiliate_enrollment", return_value=None), \
         patch("cqc_lem.utilities.db.get_affiliate_referral_counts", return_value={}), \
         patch("cqc_lem.utilities.db.get_affiliate_reward_totals", return_value={}):
        state = affiliate.affiliate_state(1)
        assert state["status"] == affiliate.STATUS_INELIGIBLE
        assert state["eligible"] is False
