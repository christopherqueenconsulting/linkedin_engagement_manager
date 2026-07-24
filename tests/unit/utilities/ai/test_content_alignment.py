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

    def test_comment_enforces_substantive_word_floor(self):
        # Issue #394: every comment directive must carry the ≥15-word substantive floor,
        # regardless of the length preference (short still clears the floor).
        for length in ("short", "medium", "long"):
            d = ca.style_directive({"comment_length": length}, "comment")
            assert f"at least {ca.COMMENT_MIN_WORDS} words" in d
            assert length in d

    def test_comment_length_defaults_to_medium(self):
        d = ca.style_directive({"use_emojis": False}, "comment")
        assert "medium" in d and "320" in d

    @pytest.mark.parametrize("content_type", ["post", "newsletter"])
    def test_long_form_types_drop_comment_length_cap(self, content_type):
        d = ca.style_directive(self._PREFS, content_type)
        assert "550" not in d and "brevity beats length" not in d
        assert "warm" in d and "Do NOT include any hashtags" in d

    def test_empty_prefs_yield_empty_directive(self):
        assert ca.style_directive(None) == ""
        assert ca.style_directive({}) == ""


class TestLeadMagnetCTA:
    _ON = {"enabled": True, "keyword": "AUDIT", "message": "A free 12-point LinkedIn profile audit PDF."}
    _OFF = {"enabled": False, "keyword": "AUDIT", "message": "audit"}
    _NO_KEYWORD = {"enabled": True, "keyword": "  ", "message": "audit"}
    _NO_MESSAGE = {"enabled": True, "keyword": "AUDIT", "message": "  "}

    def test_enabled_requires_on_keyword_and_message(self):
        assert ca.lead_magnet_enabled(self._ON) is True
        assert ca.lead_magnet_enabled(self._OFF) is False
        assert ca.lead_magnet_enabled(self._NO_KEYWORD) is False
        # No message → the automation DM dispatch gate can never fire; the CTA must not run either.
        assert ca.lead_magnet_enabled(self._NO_MESSAGE) is False
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
        # n == 1 is an explicit operator choice meaning every post.
        assert all(ca.should_include_lead_magnet_cta(self._ON, i, every_n=1) for i in range(10))

    @pytest.mark.parametrize("bad_n", [0, -1, -5])
    def test_invalid_every_n_falls_back_to_default_cadence(self, bad_n):
        # A misconfigured cadence (0/negative) must never mean 'every post' — default 1-in-3.
        selected = [i for i in range(30)
                    if ca.should_include_lead_magnet_cta(self._ON, i, every_n=bad_n)]
        assert selected == list(range(0, 30, 3))

    def test_invalid_env_cadence_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(ca, "LEAD_MAGNET_CTA_EVERY_N", 0)
        selected = [i for i in range(30) if ca.should_include_lead_magnet_cta(self._ON, i)]
        assert selected == list(range(0, 30, 3))

    def test_alignment_directive_appends_cta_when_given(self):
        cta = ca.lead_magnet_cta_directive(self._ON, include=True)
        d = ca.alignment_directive(None, lead_magnet_cta=cta)
        assert ca.NO_SELF_PROMO_GUARDRAIL in d
        assert "AUDIT" in d and "SANCTIONED" in d
        # default (no CTA) leaves the directive unchanged
        assert "SANCTIONED" not in ca.alignment_directive(None)


class TestCtaMechanicDetector:
    """The detector decides whether a FUNCTIONING comment-keyword ask survived the LLM refinement
    passes. A false positive here ships a post with a broken auto-DM mechanic, so precision matters
    more than recall (a miss just appends one soft-ask line)."""

    @pytest.mark.parametrize("content", [
        "Comment AUDIT below and I'll DM you the checklist.",
        "comment audit and it's yours",  # case-insensitive
        "COMMENT AUDIT AND I WILL SEND IT",
        "Drop AUDIT in the comments and I'll send it over.",
        "drop audit in the comments",
        "Comment 'AUDIT' and I'll DM it.",
        'Just comment the word "AUDIT" and I will DM you.',
        "Want it? Comment AUDIT, and it lands in your DMs.",
    ])
    def test_true_positives(self, content):
        assert ca.has_lead_magnet_cta_mechanic(content, "AUDIT") is True

    @pytest.mark.parametrize("content", [
        "We reviewed the audit trails last week.",  # incidental keyword, no mechanic
        "Most bias-audit tools miss this.",  # hyphenated compound must not count
        "AUDIT season is coming. Prepare now.",  # keyword without comment mechanic
        "Happy to comment on your audit process.",  # 'comment on X' = discuss, not type keyword
        "Comment below with your thoughts. Our audit found gaps.",  # sentence boundary between them
        "reach out for my AI Ops Readiness Checklist",  # prod post 30 failure mode: keyword gone
        "The auditors commented on our books.",  # neither token is the exact keyword/ask
    ])
    def test_true_negatives(self, content):
        assert ca.has_lead_magnet_cta_mechanic(content, "AUDIT") is False

    def test_keyword_must_be_exact_word(self):
        # Plural/substring forms are not the configured trigger and must not count.
        assert ca.has_lead_magnet_cta_mechanic("Comment AUDITS below and I'll DM it.", "AUDIT") is False

    def test_empty_inputs(self):
        assert ca.has_lead_magnet_cta_mechanic("", "AUDIT") is False
        assert ca.has_lead_magnet_cta_mechanic(None, "AUDIT") is False
        assert ca.has_lead_magnet_cta_mechanic("Comment AUDIT below", "") is False
        assert ca.has_lead_magnet_cta_mechanic("Comment AUDIT below", None) is False


class TestEnsureLeadMagnetCta:
    _LM = {"enabled": True, "keyword": "AUDIT", "message": "A free 12-point LinkedIn profile audit PDF."}
    _BODY = "Six months of testing taught us where acquisition cost really hides."

    def test_repair_appends_working_mechanic(self):
        out = ca.ensure_lead_magnet_cta(self._BODY, self._LM, post_id=3)
        assert out.startswith(self._BODY)
        assert ca.has_lead_magnet_cta_mechanic(out, "AUDIT")
        assert "AUDIT" in out  # exact configured casing, verbatim

    def test_repair_variant_selected_deterministically_by_post_id(self):
        n = len(ca.LEAD_MAGNET_CTA_REPAIR_MENU)
        for post_id in range(n * 2):
            out = ca.ensure_lead_magnet_cta(self._BODY, self._LM, post_id, every_n=1)
            expected = ca.LEAD_MAGNET_CTA_REPAIR_MENU[post_id % n]
            skeleton = expected.format(keyword="AUDIT",
                                       resource="a free 12-point LinkedIn profile audit PDF")
            assert out.endswith(skeleton)
            # stable across repeated calls — no per-call randomness
            assert out == ca.ensure_lead_magnet_cta(self._BODY, self._LM, post_id, every_n=1)

    def test_consecutive_selected_posts_get_different_phrasings(self):
        # Rotation selects every N posts; adjacent selections must not be word-for-word identical.
        outs = [ca.ensure_lead_magnet_cta(self._BODY, self._LM, pid)
                for pid in (3, 6, 9, 12, 15, 18)]
        assert len(set(outs)) == len(outs)

    @pytest.mark.parametrize("idx", range(len(ca.LEAD_MAGNET_CTA_REPAIR_MENU)))
    def test_every_variant_is_a_valid_soft_ask(self, idx):
        out = ca.ensure_lead_magnet_cta(self._BODY, self._LM, idx, every_n=1)
        appended = out[len(self._BODY):]
        assert "AUDIT" in appended  # exact keyword verbatim
        assert ca.has_lead_magnet_cta_mechanic(appended, "AUDIT")
        assert "dm" in appended.lower()  # states DM delivery after the comment
        assert "http" not in appended.lower() and "www." not in appended.lower()  # never a link
        assert "#" not in appended  # never a hashtag

    @pytest.mark.parametrize("idx", range(len(ca.LEAD_MAGNET_CTA_REPAIR_MENU)))
    def test_emoji_only_when_use_emojis(self, idx):
        plain = ca.ensure_lead_magnet_cta(self._BODY, self._LM, idx, use_emojis=False, every_n=1)
        assert plain.isascii()
        with_emoji = ca.ensure_lead_magnet_cta(self._BODY, self._LM, idx, use_emojis=True, every_n=1)
        emoji_glyphs = [c for c in with_emoji if not c.isascii() and c != "\ufe0f"]
        assert len(emoji_glyphs) == 1  # exactly one tasteful emoji, variation selector aside

    def test_noop_when_valid_cta_already_present(self):
        content = "Real value here.\n\nComment AUDIT and I'll DM you the 12-point checklist."
        assert ca.ensure_lead_magnet_cta(content, self._LM, post_id=3) == content

    def test_noop_when_not_selected(self):
        assert ca.ensure_lead_magnet_cta(self._BODY, self._LM, post_id=4) == self._BODY

    def test_noop_when_disabled_or_unkeyworded_or_no_post_id(self):
        off = {"enabled": False, "keyword": "AUDIT", "message": "x"}
        no_kw = {"enabled": True, "keyword": "  ", "message": "x"}
        no_msg = {"enabled": True, "keyword": "AUDIT", "message": ""}
        assert ca.ensure_lead_magnet_cta(self._BODY, off, post_id=3) == self._BODY
        assert ca.ensure_lead_magnet_cta(self._BODY, no_kw, post_id=3) == self._BODY
        assert ca.ensure_lead_magnet_cta(self._BODY, no_msg, post_id=3) == self._BODY
        assert ca.ensure_lead_magnet_cta(self._BODY, None, post_id=3) == self._BODY
        assert ca.ensure_lead_magnet_cta(self._BODY, self._LM, post_id=None) == self._BODY
        assert ca.ensure_lead_magnet_cta("", self._LM, post_id=3) == ""
        assert ca.ensure_lead_magnet_cta(None, self._LM, post_id=3) is None

    def test_repaired_output_is_normalized(self):
        # The appended result goes through normalize_public_text so typography stays clean.
        out = ca.ensure_lead_magnet_cta("Fancy—dash insight…", self._LM, post_id=3)
        assert "—" not in out and "…" not in out
        assert ca.has_lead_magnet_cta_mechanic(out, "AUDIT")

    def test_repair_is_idempotent(self):
        once = ca.ensure_lead_magnet_cta(self._BODY, self._LM, post_id=3)
        assert ca.ensure_lead_magnet_cta(once, self._LM, post_id=3) == once

    def test_resource_label_from_message(self):
        out = ca.ensure_lead_magnet_cta(self._BODY, self._LM, post_id=0, every_n=1)
        assert "a free 12-point LinkedIn profile audit PDF" in out

    @pytest.mark.parametrize("message", [
        "https://example.com/get-it #freebie",  # link/hashtag-only message must not leak in
        "I made a checklist that helps founders audit their ops end to end and more words",  # sentence-shaped
        "This is a checklist for auditing your LinkedIn profile",  # multi-word sentence start
        "We built an audit template you can copy",  # sentence start, plural
    ])
    def test_resource_label_falls_back_to_generic(self, message):
        lm = dict(self._LM, message=message)
        out = ca.ensure_lead_magnet_cta(self._BODY, lm, post_id=0, every_n=1)
        assert "the resource" in out
        assert "This is a" not in out  # never a garbled 'my This is a checklist...' label
        assert "http" not in out.lower() and "#" not in out


class TestPersonalProofDirective:
    """A2: the proof requirement, sourced from the profile synthesis, appended on a regeneration."""

    def test_directive_always_non_empty(self):
        assert ca.personal_proof_directive().strip()
        assert ca.personal_proof_directive(None).strip()
        assert ca.personal_proof_directive("").strip()

    def test_directive_demands_first_person_specificity(self):
        text = ca.personal_proof_directive()
        assert "FIRST-PERSON PROOF" in text
        assert "first person" in text.lower()
        assert "generic" in text.lower()

    def test_directive_sources_from_synthesis_when_provided(self):
        synth = "Founder who scaled a fintech from 0 to 12,000 users; led a 9-person team."
        text = ca.personal_proof_directive(synth)
        assert synth in text

    def test_synthesis_is_length_capped(self):
        synth = "x" * 5000
        text = ca.personal_proof_directive(synth)
        assert "x" * 800 in text
        assert "x" * 900 not in text

    def test_directive_is_not_self_promo(self):
        # Proof of experience, never a plug — stays inside NO_SELF_PROMO_GUARDRAIL.
        assert "not self-promotion" in ca.personal_proof_directive()
