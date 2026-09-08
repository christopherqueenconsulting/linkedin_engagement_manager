"""The outbound gate's own tests, plus the three production bodies that made it necessary.

The regression corpus below is not illustrative — every string in `PRODUCTION_LEAKS` was typed into
a real person's LinkedIn inbox by this system, lifted verbatim from
`/opt/lem/logs/cqc_lem_2026_09_04.log` and the two `[link]` sends in the same 14-day window. A
change that lets any of them through has reopened the incident.

`REAL_MESSAGES` is the other half and matters just as much: a gate that refuses real copy costs
outreach silently, so several of these are also lifted from the production log — messages that DID
go out and should have.
"""

import pytest

from cqc_lem.utilities.ai.outbound_qa import (
    SURFACE_COMMENT,
    SURFACE_DM,
    VIOLATION_ASSISTANT_ASIDE,
    VIOLATION_EMPTY,
    VIOLATION_INPUT_REQUEST,
    VIOLATION_NO_CONTENT,
    VIOLATION_OVERLONG,
    VIOLATION_PLACEHOLDER,
    VIOLATION_PROMPT_LEAK,
    is_safe_to_send,
    outbound_violations,
    refusal_reason,
)

# Sent to real LinkedIn contacts. None of these may ever pass again.
PRODUCTION_LEAKS = (
    ("To assist you effectively, I need the actual message history JSON to analyze the "
     "conversation context. Please provide the message history so I can proceed with evaluating "
     "the new message and generating a response accordingly."),
    ("No worries if the timing didn't work out. I've put together a quick recap here if it "
     "helps: [link]"),
    "If the timing's off, no worries. Here's a quick recap: [link]",
)

# Real outbound copy from the same log. A refusal here is a false positive that costs a send.
REAL_MESSAGES = (
    ("Hey Dan, saw you checked my profile. What caught your eye? If it's AI ops, happy to talk "
     "about what I'm working on — off the clock, no sales talk."),
    ("Saw you checked my profile, Tiffany. What caught your eye? If it's AI ops, happy to share "
     "insights. No pitch."),
    "Happy work anniversary, Jay!",
    "Congrats on the new role, Lucia!",
    ("Victor, great teamwork—appreciate your sharpness. Need anything later? Just ping me."),
    # Ordinary English that earlier drafts of this gate refused. Each one is a pattern boundary.
    "I need to see the traces before I can say. Happy to look.",
    "I can't make Thursday. Does Friday work for you?",
    "Absolutely brilliant framing of the ops problem here.",
    "Sure enough, the retry storm was the cause.",
    "Of course the cache was cold. Classic.",
    "Absolutely, that matches what we saw last quarter.",
    "Here is the deck I mentioned, hope it helps.",
    "Great post. Step 2 is where most teams stall, in my experience.",
    'We log it as {"role": "user"} and it works fine.',
    "Why I cannot ship on Fridays: every deploy needs a human awake.",
    "Let me know what you need and I will send it over.",
)


@pytest.mark.parametrize("body", PRODUCTION_LEAKS)
def test_production_leaks_are_refused_on_both_surfaces(body):
    """The incident corpus. A pass here means the 2026-09-04 send could happen again."""
    assert not is_safe_to_send(body, surface=SURFACE_DM)
    assert not is_safe_to_send(body, surface=SURFACE_COMMENT)


def test_the_incident_dm_names_why_it_was_refused():
    """The refusal has to be diagnosable from one log line, so it names checks, not just 'bad'."""
    violations = outbound_violations(PRODUCTION_LEAKS[0], surface=SURFACE_DM)
    assert VIOLATION_INPUT_REQUEST in violations
    reason = refusal_reason(PRODUCTION_LEAKS[0], surface=SURFACE_DM)
    assert VIOLATION_INPUT_REQUEST in reason
    assert "To assist you effectively" in reason


@pytest.mark.parametrize("body", REAL_MESSAGES)
def test_real_messages_still_send(body):
    """False positives are silent lost outreach — this half of the corpus guards against that."""
    assert is_safe_to_send(body, surface=SURFACE_DM), outbound_violations(body)
    assert is_safe_to_send(body, surface=SURFACE_COMMENT), outbound_violations(body)


@pytest.mark.parametrize("body,expected", [
    ("Certainly, below is the comment you asked for.", VIOLATION_ASSISTANT_ASIDE),
    ("As an AI, I do not have opinions on that.", VIOLATION_ASSISTANT_ASIDE),
    ("I am unable to complete this without more information.", VIOLATION_ASSISTANT_ASIDE),
    ("I cannot assist with that request.", VIOLATION_ASSISTANT_ASIDE),
    ("Here is the generated comment: nice post!", VIOLATION_ASSISTANT_ASIDE),
    ("I don't have the conversation history. Please share the transcript.",
     VIOLATION_INPUT_REQUEST),
    ("Step 1: Parse the JSON string of message history.", VIOLATION_PROMPT_LEAK),
    ("### Main Focus: offer value", VIOLATION_PROMPT_LEAK),
    ("Reply below:\n```json\n{}\n```", VIOLATION_PROMPT_LEAK),
    ("<insert message history here>", VIOLATION_PROMPT_LEAK),
    ("Hi {first_name}, saw you checked my profile and wanted to reach out.",
     VIOLATION_PLACEHOLDER),
    ("Here's the recap I promised you earlier today: [link]", VIOLATION_PLACEHOLDER),
    ("Congrats on the new role at [company], really well deserved.", VIOLATION_PLACEHOLDER),
])
def test_each_check_fires_on_its_own_shape(body, expected):
    assert expected in outbound_violations(body, surface=SURFACE_DM)


def test_empty_and_contentless_bodies():
    assert outbound_violations("", surface=SURFACE_DM) == [VIOLATION_EMPTY]
    assert outbound_violations("   \n ", surface=SURFACE_DM) == [VIOLATION_EMPTY]
    assert outbound_violations("...", surface=SURFACE_DM) == [VIOLATION_NO_CONTENT]
    assert outbound_violations("—", surface=SURFACE_COMMENT) == [VIOLATION_NO_CONTENT]


def test_there_is_no_minimum_length():
    """There is deliberately no minimum length.

    A floor that looks safe costs real copy: an early draft refused "Congrats Jane!" at 15 chars.
    Shortness is not the failure this module exists for.
    """
    for terse in ("Congrats Jane!", "Exactly this.", "Agreed.", "Same here."):
        assert is_safe_to_send(terse, surface=SURFACE_DM), terse
        assert is_safe_to_send(terse, surface=SURFACE_COMMENT), terse


def test_a_dm_far_over_its_budget_is_refused_but_a_comment_is_not():
    """A DM far over its budget is refused; a comment is not.

    `build_dm_from_template` writes to 300 chars, so a DM several times that is the model dumping
    its reasoning. A comment has no such budget.
    """
    dump = "This is a perfectly ordinary sentence about AI operations. " * 20
    assert VIOLATION_OVERLONG in outbound_violations(dump, surface=SURFACE_DM)
    assert is_safe_to_send(dump, surface=SURFACE_COMMENT)


def test_all_violations_are_reported_not_just_the_first():
    """One log line has to explain the whole body, so the checks do not short-circuit."""
    body = "Sure, here is the rewritten message: Hi {first_name}, please provide the JSON."
    violations = outbound_violations(body, surface=SURFACE_DM)
    assert {VIOLATION_ASSISTANT_ASIDE, VIOLATION_PLACEHOLDER,
            VIOLATION_INPUT_REQUEST} <= set(violations)


def test_safe_body_has_no_refusal_reason():
    assert refusal_reason(REAL_MESSAGES[0], surface=SURFACE_DM) is None


def test_none_is_refused_rather_than_raising():
    """Callers hand this whatever an LLM returned, which is sometimes None."""
    assert outbound_violations(None, surface=SURFACE_DM) == [VIOLATION_EMPTY]
    assert not is_safe_to_send(None, surface=SURFACE_DM)


def test_an_unknown_surface_still_runs_every_shared_check():
    """A new surface gets no budget cap, but never silently gets no gate."""
    assert is_safe_to_send("Exactly this.", surface="something-new")
    assert not is_safe_to_send(PRODUCTION_LEAKS[0], surface="something-new")
