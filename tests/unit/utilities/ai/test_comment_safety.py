"""Unit tests for the comment scam/hostility filter (`utilities/ai/comment_safety.py`).

Every check family has positive examples, and every family that keys on a word a finance or
marketing audience uses legitimately has a false-positive guard next to it.
"""

import re

import pytest

from cqc_lem.utilities.ai import comment_safety as cs
from cqc_lem.utilities.ai.comment_safety import (
    LABEL_HOSTILE,
    LABEL_SAFE,
    LABEL_SCAM,
    CommentVerdict,
    classify_comment,
    is_safe_to_engage,
)

pytestmark = pytest.mark.unit


SCAM_CASES = [
    # off-platform contact asks
    ("Reach me on WhatsApp for details", cs.CHECK_OFF_PLATFORM_CONTACT),
    ("Message me via Telegram", cs.CHECK_OFF_PLATFORM_CONTACT),
    ("WhatsApp me now", cs.CHECK_OFF_PLATFORM_CONTACT),
    ("Telegram: @fastprofits", cs.CHECK_OFF_PLATFORM_CONTACT),
    ("my whatsapp number is below", cs.CHECK_OFF_PLATFORM_CONTACT),
    ("Interested? Text me", cs.CHECK_OFF_PLATFORM_CONTACT),
    ("DM me on the other app", cs.CHECK_OFF_PLATFORM_CONTACT),
    ("Find me on Signal for the details", cs.CHECK_OFF_PLATFORM_CONTACT),
    # money for nothing
    ("I earn $500 per day working part time", cs.CHECK_MONEY_FOR_NOTHING),
    ("Start earning $1,200 a week now", cs.CHECK_MONEY_FOR_NOTHING),
    ("She earns big money from the comfort of your home", cs.CHECK_MONEY_FOR_NOTHING),
    ("Guaranteed returns of 30% monthly", cs.CHECK_MONEY_FOR_NOTHING),
    ("guaranteed profit every trade", cs.CHECK_MONEY_FOR_NOTHING),
    ("Double your money in two weeks", cs.CHECK_MONEY_FOR_NOTHING),
    # crypto / forex recovery pitches
    ("Join for free trading signals", cs.CHECK_RECOVERY_PITCH),
    ("My forex account manager changed my life", cs.CHECK_RECOVERY_PITCH),
    ("Thanks to my account manager I made huge profits", cs.CHECK_RECOVERY_PITCH),
    ("Best crypto recovery service out there", cs.CHECK_RECOVERY_PITCH),
    ("He recovered my lost funds in days", cs.CHECK_RECOVERY_PITCH),
    # links
    ("See https://example.com/offer", cs.CHECK_LINK_PUSH),
    ("visit www.example.com today", cs.CHECK_LINK_PUSH),
    ("Grab it at bit.ly/abc123", cs.CHECK_LINK_PUSH),
    ("Join t.me/somechannel", cs.CHECK_LINK_PUSH),
    ("Chat at wa.me/15551234567", cs.CHECK_LINK_PUSH),
    ("Great post. Click the link below", cs.CHECK_LINK_PUSH),
    ("learned so much from cryptofx-pro.io", cs.CHECK_LINK_PUSH),
    ("details at quickreturns.xyz/join", cs.CHECK_LINK_PUSH),
    ("my page is linkedin.com/in/fxqueen", cs.CHECK_LINK_PUSH),
    # profile redirects
    ("Check my profile for more", cs.CHECK_PROFILE_REDIRECT),
    ("Check out my bio", cs.CHECK_PROFILE_REDIRECT),
    ("The link is in my bio", cs.CHECK_PROFILE_REDIRECT),
    # hacked-account pitches
    ("We can recover your hacked account fast", cs.CHECK_ACCOUNT_RECOVERY),
    ("We restore your account today", cs.CHECK_ACCOUNT_RECOVERY),
    ("Hacked page? We fix it", cs.CHECK_ACCOUNT_RECOVERY),
    # prize / giveaway
    ("Congratulations, you have been selected for our reward", cs.CHECK_PRIZE_CLAIM),
    ("You've won a free iPhone", cs.CHECK_PRIZE_CLAIM),
    ("Claim your prize now", cs.CHECK_PRIZE_CLAIM),
]

HOSTILE_CASES = [
    # profanity
    ("This is fucking wrong", cs.CHECK_PROFANITY),
    ("What a load of shit!", cs.CHECK_PROFANITY),
    # insults aimed at the reader
    ("You're an idiot", cs.CHECK_INSULT),
    ("you are such a clown", cs.CHECK_INSULT),
    ("You're a fraud and everyone knows it", cs.CHECK_INSULT),
    ("Shut up already", cs.CHECK_INSULT),
    ("Go to hell", cs.CHECK_INSULT),
    ("Nobody cares", cs.CHECK_INSULT),
    ("This is garbage", cs.CHECK_INSULT),
    ("this is total BS", cs.CHECK_INSULT),
    ("This is B.S.", cs.CHECK_INSULT),
    # threats
    ("I will find you", cs.CHECK_THREAT),
    ("I'm going to report you", cs.CHECK_THREAT),
    ("Watch your back", cs.CHECK_THREAT),
    ("You'll regret this post", cs.CHECK_THREAT),
    # disinterest / hostility to the account
    ("Stop spamming", cs.CHECK_DISINTEREST),
    ("Leave me alone", cs.CHECK_DISINTEREST),
    ("Unfollowed you, bye", cs.CHECK_DISINTEREST),
    ("I'm unfollowing", cs.CHECK_DISINTEREST),
    ("This is spam", cs.CHECK_DISINTEREST),
    ("Spam.", cs.CHECK_DISINTEREST),
    ("such a spammer", cs.CHECK_DISINTEREST),
]

BENIGN = [
    "Great point on investment strategy — diversification is underrated.",
    "As an investment manager, I see this play out every quarter.",
    "Our account manager walked us through onboarding, very smooth.",
    "We helped clients earn $10k a month in new MRR with this playbook.",
    "Telegram's new moderation policy is a big deal for founders.",
    "The signal: the market is pricing in two cuts.",
    "Signal: churn drops when onboarding is shorter.",
    "Good tips on staying out of the spam folder and spam filters.",
    "Crypto adoption numbers here are fascinating.",
    "This framework doubled our reach — double your reach by posting at 8am.",
    "I clicked the link in your post and loved the guide.",
    "Scrapbook-style carousels work; so does a Dickens quote about assessment.",
    "We moved our office to Scunthorpe last year.",
    "Garbage in, garbage out is the real lesson for data teams.",
    "You're not stupid for asking — this confuses everyone.",
    "I won't unfollow anyone over a disagreement, good thread.",
    "Congrats on the launch! Thanks for sharing.",
    "Call volume hit 2024 levels, up 15% from 2023.",
    # Bare-domain guards: a code suffix, an abbreviation, a capitalised brand named in prose, a
    # missing space after a full stop on a common word.
    "We rebuilt the API in Node.js and Next.js, e.g. the auth layer.",
    "Booking.com and Amazon.com both did this early.",
    "Agreed.to be fair the data was thin.",
    # Decision: naming the platform itself, with no path, is not a link push.
    "I found you on linkedin.com last year.",
]


class TestScamChecks:
    @pytest.mark.parametrize("text,check", SCAM_CASES)
    def test_scam_family_fires(self, text, check):
        verdict = classify_comment(text)
        assert verdict.label == LABEL_SCAM
        assert check in verdict.reasons

    @pytest.mark.parametrize("text", [
        "Reach out +1 555 123 4567",
        "call (555) 123-4567 today",
        "my line 555.123.4567",
        "+44 20 7946 0958 for details",
    ])
    def test_a_phone_number_shape_is_a_contact_ask(self, text):
        verdict = classify_comment(text)
        assert verdict.label == LABEL_SCAM and cs.CHECK_OFF_PLATFORM_CONTACT in verdict.reasons

    @pytest.mark.parametrize("text", [
        "Revenue grew from 2,500,000 to 4,100,000 in 2024-2025.",
        "Q3 2024 vs Q3 2025: 12% lift",
        "Ticket 1234567890 is fixed",
    ])
    def test_numbers_that_are_not_phone_shaped_do_not_fire(self, text):
        assert classify_comment(text).is_safe


class TestHostileChecks:
    @pytest.mark.parametrize("text,check", HOSTILE_CASES)
    def test_hostile_family_fires(self, text, check):
        verdict = classify_comment(text)
        assert verdict.label == LABEL_HOSTILE
        assert check in verdict.reasons


class TestFalsePositiveGuards:
    @pytest.mark.parametrize("text", BENIGN)
    def test_benign_comment_is_safe(self, text):
        verdict = classify_comment(text)
        assert verdict == CommentVerdict(LABEL_SAFE), verdict.reasons
        assert is_safe_to_engage(text)

    def test_profanity_is_whole_word_only(self):
        assert classify_comment("Scunthorpe scrapbook dickens").is_safe
        assert not classify_comment("crap.").is_safe


class TestVerdictShape:
    @pytest.mark.parametrize("text", ["", "   ", "\n\t", None])
    def test_empty_text_is_safe(self, text):
        assert classify_comment(text) == CommentVerdict(LABEL_SAFE, ())
        assert is_safe_to_engage(text)

    @pytest.mark.parametrize("text", ["WHATSAPP ME", "whatsapp me", "WhatsApp Me", "SHUT UP"])
    def test_case_insensitive(self, text):
        assert not classify_comment(text).is_safe

    def test_curly_apostrophes_are_folded(self):
        assert cs.CHECK_INSULT in classify_comment("You’re an idiot").reasons

    def test_scam_wins_and_keeps_every_reason(self):
        verdict = classify_comment("Shut up and text me")
        assert verdict.label == LABEL_SCAM
        assert verdict.reasons == (cs.CHECK_OFF_PLATFORM_CONTACT, cs.CHECK_INSULT)

    def test_reasons_never_carry_the_text(self):
        verdict = classify_comment("Earn $900 per day, see https://x.example/'quoted'")
        assert verdict.reasons
        for reason in verdict.reasons:
            assert re.fullmatch(r"[a-z_]+", reason)

    def test_reason_names_are_stable(self):
        """The names are a log and test contract; renaming one is a deliberate act."""
        assert {
            cs.CHECK_OFF_PLATFORM_CONTACT, cs.CHECK_MONEY_FOR_NOTHING, cs.CHECK_RECOVERY_PITCH,
            cs.CHECK_LINK_PUSH, cs.CHECK_PROFILE_REDIRECT, cs.CHECK_ACCOUNT_RECOVERY,
            cs.CHECK_PRIZE_CLAIM, cs.CHECK_PROFANITY, cs.CHECK_INSULT, cs.CHECK_THREAT,
            cs.CHECK_DISINTEREST,
        } == {
            "off_platform_contact", "money_for_nothing", "recovery_pitch", "link_push",
            "profile_redirect", "account_recovery", "prize_claim", "profanity", "insult", "threat",
            "disinterest",
        }

    def test_verdict_is_frozen(self):
        verdict = classify_comment("hello")
        with pytest.raises(AttributeError):
            verdict.label = LABEL_SCAM  # type: ignore[misc]

    def test_plain_linkedin_mention_is_safe_but_a_linkedin_path_is_a_link(self):
        assert classify_comment("Saw this on linkedin.com").is_safe
        assert cs.CHECK_LINK_PUSH in classify_comment("see linkedin.com/in/someone").reasons

    def test_text_past_the_cap_is_not_read(self):
        assert classify_comment("a" * cs.MAX_CLASSIFY_CHARS + " whatsapp me").is_safe
        assert not classify_comment("whatsapp me " + "a" * cs.MAX_CLASSIFY_CHARS).is_safe

    def test_deterministic(self):
        text = "Guaranteed returns, click the link"
        assert classify_comment(text) == classify_comment(text)


def _math_bold(text: str) -> str:
    """Map ASCII letters into the Mathematical Bold block (U+1D400) — a common filter dodge."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D400 + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D41A + ord(ch) - ord("a")))
        else:
            out.append(ch)
    return "".join(out)


class TestUnicodeFolding:
    @pytest.mark.parametrize("text", [
        _math_bold("WhatsApp me"),
        "ｗｈａｔｓａｐｐ ｍｅ",  # fullwidth "whatsapp me"
        "What​sApp me",                                            # zero-width space
        "Wh⁠ats‍App me",                                      # word joiner + ZWJ
        "t​.me/x",
    ])
    def test_obfuscated_scam_is_still_caught(self, text):
        assert classify_comment(text).label == LABEL_SCAM

    def test_the_math_bold_sample_really_is_non_ascii(self):
        assert not _math_bold("WhatsApp me").isascii()

    def test_normalize_drops_format_characters(self):
        assert cs.normalize_for_matching("a​b­c  d") == "abc d"


class TestOutboundContactOrLink:
    @pytest.mark.parametrize("draft", [
        "Great point, see cryptofx-pro.io",
        "Ping @fxqueen on Telegram",
        "Thanks! Call +1 555 010 2000",
        "More at Example.COM",
        "Find it on linkedin.com",
        "cryptofx dot com",
        "cryptofx[.]io",
        "cryptofx (dot) xyz",
        "https://example.com",
        "see www.example.org",
    ])
    def test_refuses_any_contact_or_link_shape(self, draft):
        assert cs.outbound_contact_or_link(draft)

    @pytest.mark.parametrize("draft", [
        "Thanks, Jane! What part of the rollout surprised you most?",
        "We used Node.js for this, e.g. the webhook layer, and LinkedIn's own API.",
        "Agreed. The data was thin, but the trend held.",
        "",
    ])
    def test_allows_an_ordinary_reply(self, draft):
        assert not cs.outbound_contact_or_link(draft)
