"""Call-ingest core (issue #2629): parsers, own-turn filter, redaction, quote verification."""

import pytest

from cqc_lem.utilities.ai import call_ingest as ci

pytestmark = pytest.mark.unit

OTTER_TXT = """Alice Rivera  0:00
We cut our churn from 8 percent to 3 percent last quarter.

Bob Stone  0:12
Wow, email me at bob@acme.com or call 415-555-0199.

Alice Rivera  0:30
I will. Also see https://acme.com/case-study for the writeup.
"""

SRT = """1
00:00:01,000 --> 00:00:04,000
Alice: We shipped the migration in two weeks.

2
00:00:04,500 --> 00:00:07,000
Bob: That is fast.
"""

VTT = """WEBVTT

00:01.000 --> 00:04.000
<v Alice>I broke production once.</v>

00:04.500 --> 00:07.000
<v Bob>Tell me more.</v>
"""


def test_parse_otter_txt_turns_and_times():
    turns = ci.parse_transcript(OTTER_TXT, ci.SOURCE_OTTER)
    assert [t["speaker"] for t in turns] == ["Alice Rivera", "Bob Stone", "Alice Rivera"]
    assert turns[0]["t0"] == 0.0 and turns[0]["t1"] == 12.0
    assert turns[1]["t0"] == 12.0
    assert "churn" in turns[0]["text"]


def test_parse_otter_hour_timestamp():
    turns = ci.parse_otter_txt("Alice  1:02:03\nhello there\n")
    assert turns[0]["t0"] == 3723.0


def test_parse_srt_and_vtt_speakers():
    srt = ci.parse_transcript(SRT, ci.SOURCE_OTTER)
    assert [t["speaker"] for t in srt] == ["Alice", "Bob"]
    assert srt[0]["text"] == "We shipped the migration in two weeks."
    assert srt[0]["t0"] == 1.0 and srt[0]["t1"] == 4.0
    vtt = ci.parse_transcript(VTT, ci.SOURCE_OTTER)
    assert [t["speaker"] for t in vtt] == ["Alice", "Bob"]
    assert vtt[0]["t0"] == 1.0


def test_parse_otter_colon_fallback():
    turns = ci.parse_otter_txt("Alice: first line\ncontinued\nBob: reply\n")
    assert turns[0]["text"] == "first line continued"
    assert turns[1]["speaker"] == "Bob"


def test_parse_wispr_all_paragraphs_are_own_voice():
    turns = ci.parse_transcript("First thought.\n\nSecond thought.", ci.SOURCE_WISPR)
    assert len(turns) == 2 and {t["speaker"] for t in turns} == {"me"}


def test_unknown_source_raises():
    with pytest.raises(ValueError):
        ci.parse_transcript("x", "zoom")


def test_own_turn_filter_selects_only_the_user():
    turns = ci.parse_transcript(OTTER_TXT, ci.SOURCE_OTTER)
    own, state = ci.select_own_turns(turns, "alice rivera")
    assert state == ci.OWN_KNOWN
    assert len(own) == 2 and all(t["speaker"] == "Alice Rivera" for t in own)


@pytest.mark.parametrize("label", [None, "", "Carol"])
def test_own_turn_filter_is_unknown_never_a_guess(label):
    turns = ci.parse_transcript(OTTER_TXT, ci.SOURCE_OTTER)
    own, state = ci.select_own_turns(turns, label)
    assert state == ci.OWN_UNKNOWN and own == []


def test_swapped_speaker_label_changes_the_output():
    """Pins the label-to-person mapping: a swapped label returns the OTHER person's words."""
    turns = ci.parse_transcript(SRT, ci.SOURCE_OTTER)
    alice, _ = ci.select_own_turns(turns, "Alice")
    swapped, _ = ci.select_own_turns(turns, "Bob")
    assert ci.transcript_text(alice) == "We shipped the migration in two weeks."
    assert "migration" not in ci.transcript_text(swapped)
    assert ci.transcript_text(alice) != ci.transcript_text(swapped)


def test_redact_emails_phones_urls_and_names():
    text = "Ask Bob Stone at bob@acme.com, 415-555-0199 or https://acme.com/x. Bob agreed."
    out, mapping = ci.redact(text, ("Bob Stone",))
    assert "bob@acme.com" not in out and "415-555-0199" not in out
    assert "https://acme.com" not in out and "Bob" not in out
    assert "[EMAIL_1]" in out and "[PHONE_1]" in out and "[URL_1]" in out
    assert out.count("[PERSON_1]") >= 1 or "[PERSON_2]" in out
    assert mapping["[EMAIL_1]"] == "bob@acme.com"


def test_redact_reuses_token_for_repeat():
    out, _ = ci.redact("a@x.com then A@X.com", ())
    assert out == "[EMAIL_1] then [EMAIL_1]"


def test_redact_turns_scrubs_other_speakers_everywhere():
    turns = ci.parse_transcript(OTTER_TXT, ci.SOURCE_OTTER)
    redacted = ci.redact_turns(turns, "Alice Rivera")
    blob = ci.transcript_text(redacted) + " ".join(str(t["speaker"]) for t in redacted)
    for secret in ("bob@acme.com", "415-555-0199", "acme.com", "Bob", "Stone"):
        assert secret not in blob
    assert redacted[0]["speaker"] == "Alice Rivera"


def test_verify_quote_exact_fuzzy_and_invented():
    transcript = "We cut our churn from 8 percent to 3 percent last quarter."
    assert ci.verify_quote("cut our churn from 8 percent to 3 percent", transcript)
    assert ci.verify_quote("We cut our CHURN from 8 percent to 3 percent last quarter!", transcript)
    assert ci.verify_quote("we cut our churn from 8 percent to 4 percent last quarter", transcript)
    assert not ci.verify_quote("we doubled revenue to 5 million dollars", transcript)
    assert not ci.verify_quote("", transcript)


def test_filter_verified_drops_every_invented_fact():
    transcript = "I broke production once. We shipped the migration in two weeks."
    candidates = [
        {"title": "real", "source_quote": "I broke production once"},
        {"title": "invented1", "source_quote": "we closed a 2 million dollar deal"},
        {"title": "invented2", "source_quote": "our team grew to 40 engineers overnight"},
        {"title": "no quote", "source_quote": ""},
    ]
    kept, dropped = ci.filter_verified(candidates, transcript)
    assert [c["title"] for c in kept] == ["real"]
    assert len(dropped) == 3


def test_kill_switch_and_default_off(monkeypatch):
    monkeypatch.delenv("CALL_INGEST_ENABLED", raising=False)
    monkeypatch.delenv("CALL_INGEST_KILL_SWITCH", raising=False)
    assert ci.call_ingest_enabled() is False
    monkeypatch.setenv("CALL_INGEST_ENABLED", "true")
    assert ci.call_ingest_enabled() is True
    monkeypatch.setenv("CALL_INGEST_KILL_SWITCH", "1")
    assert ci.call_ingest_enabled() is False
