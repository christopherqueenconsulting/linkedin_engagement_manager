"""Unit tests for the shared alignment core: the ONE place the self-promo policy, LEM engagement
purpose, and per-type style rules are expressed for newsletters, posts, and comments."""

import pytest

from cqc_lem.utilities.ai import content_alignment as ca

pytestmark = pytest.mark.unit


class TestPromoPolicy:
    def test_newsletter_gets_light_soft_promo(self):
        assert ca.promo_policy("newsletter") == ca.NEWSLETTER_SOFT_PROMO_NOTE
        assert "OWN newsletter" in ca.promo_policy("newsletter")

    @pytest.mark.parametrize("content_type", ["comment", "post", "anything_else"])
    def test_everything_else_gets_hard_guardrail(self, content_type):
        assert ca.promo_policy(content_type) == ca.NO_SELF_PROMO_GUARDRAIL
        assert "LEM" in ca.promo_policy(content_type)

    def test_ai_helper_consumes_the_same_objects(self):
        # The aliases in ai_helper must BE these definitions — one source, no drift.
        from cqc_lem.utilities.ai import ai_helper
        assert ai_helper._NO_SELF_PROMO_GUARDRAIL is ca.NO_SELF_PROMO_GUARDRAIL
        assert ai_helper._NEWSLETTER_SOFT_PROMO_NOTE is ca.NEWSLETTER_SOFT_PROMO_NOTE
        assert ai_helper._DEFAULT_ENGAGEMENT_INTENTION is ca.DEFAULT_ENGAGEMENT_INTENTION


class TestEngagementPurpose:
    def test_each_type_has_a_distinct_relationship_purpose(self):
        comment = ca.engagement_purpose("comment")
        post = ca.engagement_purpose("post")
        newsletter = ca.engagement_purpose("newsletter")
        assert len({comment, post, newsletter}) == 3
        assert "REPLY" in comment and "relationship" in comment
        assert "conversation" in post.lower()
        assert "relationship" in newsletter

    def test_unknown_type_falls_back_to_post_purpose(self):
        assert ca.engagement_purpose("carrier_pigeon") == ca.engagement_purpose("post")

    def test_post_alignment_directive_carries_purpose_and_guardrail(self):
        d = ca.alignment_directive(None)
        assert ca.NO_SELF_PROMO_GUARDRAIL in d
        assert ca.engagement_purpose("post") in d


class TestStyleDirectivePerType:
    _PREFS = {"tone": "warm", "comment_length": "long", "use_emojis": False, "use_hashtags": False}

    def test_comment_keeps_length_cap(self):
        d = ca.style_directive(self._PREFS, "comment")
        assert "550" in d and "warm" in d

    @pytest.mark.parametrize("content_type", ["post", "newsletter"])
    def test_long_form_types_drop_comment_length_cap(self, content_type):
        d = ca.style_directive(self._PREFS, content_type)
        assert "550" not in d and "brevity beats length" not in d
        assert "warm" in d and "Do not use any hashtags" in d

    def test_empty_prefs_yield_empty_directive(self):
        assert ca.style_directive(None) == ""
        assert ca.style_directive({}) == ""


class TestLeadMagnetCTA:
    _ON = {"enabled": True, "keyword": "AUDIT", "message": "A free 12-point LinkedIn profile audit PDF."}
    _OFF = {"enabled": False, "keyword": "AUDIT", "message": "audit"}
    _NO_KEYWORD = {"enabled": True, "keyword": "  ", "message": "audit"}

    def test_enabled_requires_on_and_keyword(self):
        assert ca.lead_magnet_enabled(self._ON) is True
        assert ca.lead_magnet_enabled(self._OFF) is False
        assert ca.lead_magnet_enabled(self._NO_KEYWORD) is False
        assert ca.lead_magnet_enabled(None) is False

    def test_directive_included_only_when_enabled_and_selected(self):
        d = ca.lead_magnet_cta_directive(self._ON, include=True)
        assert "AUDIT" in d
        assert "SANCTIONED" in d
        assert "OVERRIDES the no-self-promo guardrail" in d
        # resource value is threaded in for the model to paraphrase in voice
        assert "12-point LinkedIn profile audit" in d

    def test_directive_absent_when_not_selected(self):
        assert ca.lead_magnet_cta_directive(self._ON, include=False) == ""

    def test_directive_absent_when_disabled(self):
        assert ca.lead_magnet_cta_directive(self._OFF, include=True) == ""
        assert ca.lead_magnet_cta_directive(self._NO_KEYWORD, include=True) == ""

    def test_selection_off_when_lead_magnet_off_or_no_index(self):
        assert ca.should_include_lead_magnet_cta(self._OFF, 3) is False
        assert ca.should_include_lead_magnet_cta(self._ON, None) is False

    def test_selection_is_deterministic_and_roughly_one_in_n(self):
        n = 3
        selected = [i for i in range(300)
                    if ca.should_include_lead_magnet_cta(self._ON, i, every_n=n)]
        # exactly the multiples of N → deterministic 1-in-N
        assert selected == list(range(0, 300, n))
        assert len(selected) == 100
        # stable across repeated calls (no per-call randomness)
        assert all(ca.should_include_lead_magnet_cta(self._ON, i, every_n=n) for i in selected)
        assert not any(ca.should_include_lead_magnet_cta(self._ON, i, every_n=n)
                       for i in range(300) if i % n != 0)

    def test_every_n_of_one_selects_all(self):
        assert all(ca.should_include_lead_magnet_cta(self._ON, i, every_n=1) for i in range(10))

    def test_alignment_directive_appends_cta_when_given(self):
        cta = ca.lead_magnet_cta_directive(self._ON, include=True)
        d = ca.alignment_directive(None, lead_magnet_cta=cta)
        assert ca.NO_SELF_PROMO_GUARDRAIL in d
        assert "AUDIT" in d and "SANCTIONED" in d
        # default (no CTA) leaves the directive unchanged
        assert "SANCTIONED" not in ca.alignment_directive(None)
