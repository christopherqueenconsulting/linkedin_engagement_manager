"""Unit tests for the 70/20/10 content-mix governor and the artifact-CTA policy (issue #618):
the deterministic class assignment, the mix directive injected into post prompts, the meeting-ask
detector + deterministic repair, and the compliance summary the analytics dashboard renders."""

from collections import Counter

import pytest

pytestmark = pytest.mark.unit

_LM = {"enabled": True, "keyword": "AUDIT", "message": "the churn audit checklist"}
_NEWSLETTER = {"enabled": True, "title": "The Retention Brief"}


class TestAssignContentMix:
    def test_thirty_day_plan_is_seventy_twenty_ten(self):
        from cqc_lem.utilities.ai.content_alignment import assign_content_mix
        counts = Counter(assign_content_mix(30))
        assert counts == {"value": 21, "authority": 6, "promo": 3}

    def test_thirty_day_plan_promo_within_ten_percent(self):
        """The acceptance criterion: a 30-day plan is at most 10% promo."""
        from cqc_lem.utilities.ai.content_alignment import assign_content_mix, PROMO_MAX_RATIO
        mixes = assign_content_mix(30)
        assert mixes.count("promo") / len(mixes) <= PROMO_MAX_RATIO

    @pytest.mark.parametrize("count", [1, 5, 7, 9, 12, 28, 31, 60])
    def test_promo_never_exceeds_the_ceiling_for_any_plan_length(self, count):
        from cqc_lem.utilities.ai.content_alignment import assign_content_mix, PROMO_MAX_RATIO
        mixes = assign_content_mix(count)
        assert len(mixes) == count
        assert mixes.count("promo") / count <= PROMO_MAX_RATIO

    def test_promo_slots_are_at_least_ten_posts_apart(self):
        from cqc_lem.utilities.ai.content_alignment import assign_content_mix
        idx = [i for i, m in enumerate(assign_content_mix(60)) if m == "promo"]
        assert all(b - a >= 10 for a, b in zip(idx, idx[1:]))

    def test_offset_continues_the_cadence_across_plans(self):
        """A second plan must not restart the rotation — that could place two promo posts back to
        back across the plan boundary."""
        from cqc_lem.utilities.ai.content_alignment import assign_content_mix
        first = assign_content_mix(12)
        second = assign_content_mix(12, offset=12)
        combined = first + second
        idx = [i for i, m in enumerate(combined) if m == "promo"]
        assert all(b - a >= 10 for a, b in zip(idx, idx[1:]))

    def test_empty_plan(self):
        from cqc_lem.utilities.ai.content_alignment import assign_content_mix
        assert assign_content_mix(0) == []

    def test_deterministic(self):
        from cqc_lem.utilities.ai.content_alignment import assign_content_mix
        assert assign_content_mix(30) == assign_content_mix(30)


class TestPromoEveryN:
    def test_default(self, monkeypatch):
        from cqc_lem.utilities.ai.content_alignment import promo_every_n
        monkeypatch.delenv("PROMO_EVERY_N_POSTS", raising=False)
        assert promo_every_n() == 10

    def test_denser_than_one_in_ten_is_clamped(self, monkeypatch):
        """1-in-7 would put promo at ~14% — over the ceiling the same audit sets."""
        from cqc_lem.utilities.ai.content_alignment import promo_every_n
        monkeypatch.setenv("PROMO_EVERY_N_POSTS", "3")
        assert promo_every_n() == 10

    def test_rarer_promo_is_honored_and_capped(self, monkeypatch):
        from cqc_lem.utilities.ai.content_alignment import promo_every_n
        monkeypatch.setenv("PROMO_EVERY_N_POSTS", "20")
        assert promo_every_n() == 20
        monkeypatch.setenv("PROMO_EVERY_N_POSTS", "500")
        assert promo_every_n() == 30

    def test_garbage_falls_back_to_default(self, monkeypatch):
        from cqc_lem.utilities.ai.content_alignment import promo_every_n
        monkeypatch.setenv("PROMO_EVERY_N_POSTS", "sometimes")
        assert promo_every_n() == 10

    def test_explicit_argument_wins_over_env(self, monkeypatch):
        from cqc_lem.utilities.ai.content_alignment import assign_content_mix
        monkeypatch.delenv("PROMO_EVERY_N_POSTS", raising=False)
        assert Counter(assign_content_mix(30, every_n=15))["promo"] == 2


class TestMixDirective:
    def test_promo_demands_case_study_and_no_pressure(self):
        from cqc_lem.utilities.ai.content_alignment import mix_directive
        text = mix_directive("promo").lower()
        assert "case study" in text and "no-pressure" in text
        assert "no meeting ask" in text

    @pytest.mark.parametrize("mix", ["value", "authority"])
    def test_value_and_authority_sell_nothing(self, mix):
        from cqc_lem.utilities.ai.content_alignment import mix_directive
        text = mix_directive(mix).lower()
        assert "sell nothing" in text and "no pitch" in text

    def test_unclassified_adds_nothing(self):
        from cqc_lem.utilities.ai.content_alignment import mix_directive
        assert mix_directive(None) == "" and mix_directive("whatever") == ""

    def test_alignment_directive_carries_the_class(self):
        from cqc_lem.utilities.ai.content_alignment import alignment_directive
        assert "SOFT PROMO" in alignment_directive({}, "", "promo")
        assert "MIX CLASS" not in alignment_directive({}, "")

    def test_cta_policy_directive_bans_the_meeting_ask(self):
        from cqc_lem.utilities.ai.content_framework import cta_policy_directive
        assert "book a call" in cta_policy_directive().lower()


class TestMeetingAskDetector:
    @pytest.mark.parametrize("text", [
        "Want the same result? Book a call and we'll map it out.",
        "Happy to schedule a demo if that's useful.",
        "Let's hop on a quick call this week.",
        "Want to set up a time to talk it through?",
        "Let's find a time next week.",
        "Grab a free 15-minute call with me.",
        "My Calendly is in my profile.",
        "DM me to discuss your funnel.",
        "Reach out and we can discuss the details.",
        "Booking a consult is the fastest way in.",
        # 'an' is the ONLY article "intro"/"introductory" ever takes — an offer-verb pattern that
        # omits it cannot match the most natural phrasing of the ask it exists to catch.
        "Book an intro call with me this week.",
        "Schedule an introductory call if that helps.",
        "Grab an intro session on my calendar.",
        # The same ask framed as the reader's interest, or as a bare headline offer with a booking
        # marker — no offer verb, still unambiguously a meeting ask.
        "Want a free strategy session? Comment below.",
        "Interested in a discovery call?",
        "Free discovery call for the first 5 people who reply.",
        "Free strategy session — link in comments.",
    ])
    def test_flags_meeting_asks(self, text):
        from cqc_lem.utilities.ai.content_alignment import contains_meeting_ask
        assert contains_meeting_ask(text) is True

    @pytest.mark.parametrize("text", [
        "Let's talk about why dwell time beats reach.",
        "We scheduled the migration for a Tuesday in March and it still slipped.",
        "I called the vendor twice before anyone answered.",
        "Comment AUDIT and I'll DM you the checklist.",
        "What would you have done differently? Curious how others handle this.",
        # First-person NARRATIVE about calls/sessions is often the story-bank anecdote itself —
        # the repair deletes flagged sentences, so a bare noun phrase must never match (#620 seam).
        "I ran a discovery call with them last week and it changed the scope.",
        "Our last strategy session surfaced three gaps in the funnel.",
        "The 30-minute call with their CTO was where the real problem showed up.",
        "I book discovery calls with new clients every Friday.",
        "I wanted a quick call but they were booked solid.",
        "That discovery call opened my eyes to the real bottleneck.",
        "",
        None,
    ])
    def test_leaves_ordinary_prose_alone(self, text):
        from cqc_lem.utilities.ai.content_alignment import contains_meeting_ask
        assert contains_meeting_ask(text) is False

    def test_excerpts_name_the_offending_phrase(self):
        from cqc_lem.utilities.ai.content_alignment import meeting_ask_excerpts
        assert meeting_ask_excerpts("Book a call today. Or book a call tomorrow.") == ["Book a call"]
        assert meeting_ask_excerpts("nothing here") == []


class TestReplaceMeetingAskCta:
    _DRAFT = ("We cut their churn from 9% to 4% in one quarter.\n\n"
              "The fix was boring: one onboarding call in week one.\n\n"
              "Want the same? Book a call with me this week.")

    def test_routes_to_the_lead_magnet_when_configured(self):
        from cqc_lem.utilities.ai.content_alignment import (contains_meeting_ask,
                                                            replace_meeting_ask_cta)
        out = replace_meeting_ask_cta(self._DRAFT, lead_magnet=_LM, post_id=3)
        assert contains_meeting_ask(out) is False
        assert "AUDIT" in out and "comment" in out.lower()
        assert "churn from 9% to 4%" in out

    def test_routes_to_the_newsletter_when_there_is_no_lead_magnet(self):
        from cqc_lem.utilities.ai.content_alignment import replace_meeting_ask_cta
        out = replace_meeting_ask_cta(self._DRAFT, newsletter=_NEWSLETTER, post_id=1)
        assert "The Retention Brief" in out and "subscribe" in out.lower()

    def test_drops_the_ask_when_the_user_has_no_artifact(self):
        from cqc_lem.utilities.ai.content_alignment import (contains_meeting_ask,
                                                            replace_meeting_ask_cta)
        out = replace_meeting_ask_cta(self._DRAFT)
        assert contains_meeting_ask(out) is False
        assert "subscribe" not in out.lower() and "comment" not in out.lower()

    def test_unchanged_when_there_is_no_meeting_ask(self):
        from cqc_lem.utilities.ai.content_alignment import replace_meeting_ask_cta
        clean = "We cut churn in half.\n\nWhat's worked for you?"
        assert replace_meeting_ask_cta(clean, lead_magnet=_LM, post_id=1) == clean

    def test_does_not_double_an_existing_keyword_ask(self):
        from cqc_lem.utilities.ai.content_alignment import replace_meeting_ask_cta
        draft = ("Churn halved in a quarter.\n\nBook a call if you want the walkthrough.\n\n"
                 "Comment AUDIT and I'll DM you the checklist.")
        out = replace_meeting_ask_cta(draft, lead_magnet=_LM, post_id=1)
        assert out.upper().count("AUDIT") == 1

    def test_keeps_the_surviving_body_lines(self):
        from cqc_lem.utilities.ai.content_alignment import replace_meeting_ask_cta
        out = replace_meeting_ask_cta(self._DRAFT, lead_magnet=_LM, post_id=0)
        assert "The fix was boring" in out


class TestContentMixCompliance:
    def test_on_policy_plan(self):
        from cqc_lem.utilities.ai.content_alignment import content_mix_compliance
        out = content_mix_compliance({"value": 21, "authority": 6, "promo": 3, "unclassified": 4})
        assert out["compliant"] is True
        assert out["total"] == 30 and out["counts"]["unclassified"] == 4
        assert out["ratios"] == {"value": 0.7, "authority": 0.2, "promo": 0.1}
        assert out["target"]["promo"] == 0.1 and out["promo_every_n"] == 10

    def test_over_the_promo_ceiling(self):
        from cqc_lem.utilities.ai.content_alignment import content_mix_compliance
        out = content_mix_compliance({"value": 4, "authority": 3, "promo": 3})
        assert out["compliant"] is False

    def test_empty_plan_is_not_a_violation(self):
        from cqc_lem.utilities.ai.content_alignment import content_mix_compliance
        out = content_mix_compliance(None)
        assert out["compliant"] is True and out["total"] == 0
        assert out["ratios"]["promo"] is None

    def test_unknown_classes_are_ignored(self):
        from cqc_lem.utilities.ai.content_alignment import content_mix_compliance
        out = content_mix_compliance({"value": 2, "sales": 5})
        assert out["total"] == 2 and "sales" not in out["counts"]


class TestNormalizeContentMix:
    @pytest.mark.parametrize("raw,expected", [
        ("promo", "promo"), ("PROMO", "promo"), (" value ", "value"),
        ("authority", "authority"), ("sales", None), (None, None), ("", None),
    ])
    def test_normalizes(self, raw, expected):
        from cqc_lem.utilities.ai.content_alignment import normalize_content_mix
        assert normalize_content_mix(raw) == expected
