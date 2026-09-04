"""The last look at a message body before it leaves LEM for a real person.

Every other content gate in this repo grades QUALITY — slop lint, the similarity gate, the
humanizer. This one grades whether the text is a MESSAGE at all. The failure it exists for is not a
mediocre DM, it is the model answering the operator instead of writing copy, and that text sailing
through every quality check because it is the right length, carries no hype word and reads as fluent
English.

That is not hypothetical. On 2026-09-04 a profile-viewer DM went to a first-degree connection
reading, in full:

    To assist you effectively, I need the actual message history JSON to analyze the conversation
    context. Please provide the message history so I can proceed with evaluating the new message
    and generating a response accordingly.

`ai_check_message_history` had been handed an empty history plus a prompt that still carried its own
authoring placeholder; the small model timed out, the OpenAI fallback answered the prompt literally,
and its reply REPLACED a perfectly good drafted DM on the way to `send_dm_now`, which types whatever
string it is given. Two more DMs in the same log window shipped a model-invented `[link]`
placeholder to real people. Nothing between the model and LinkedIn looked at the body.

So this module is deliberately NOT a flag and NOT a quality score. It is a fail-closed predicate on
the send path: a body that trips any check is refused, and refusing costs one message where sending
costs the account's credibility with a named human. Callers log the refusal and give up on that
message — never retry the same text, and never "repair" it here, because a body that reads as an
assistant aside has no correct repair.

Anchoring rule, inherited from `content_alignment.is_assistant_aside` (#1284): every pattern must
earn its place against the FALSE positive, because a hit discards real outbound copy. Patterns match
how an aside or a leak is SHAPED — an address to the operator, the prompt's own vocabulary, a
bracketed slot name — never a topic word. "Let me know what you need" is a DM; "Please provide the
message history" is not.
"""

import re
from typing import Optional

# Surfaces. A comment is public and a DM is private, but the failure mode and the verdict are the
# same on both, so the surface only tunes the length floor.
SURFACE_DM = "dm"
SURFACE_COMMENT = "comment"

VIOLATION_EMPTY = "empty_body"
VIOLATION_NO_CONTENT = "no_readable_content"
VIOLATION_OVERLONG = "over_budget"
VIOLATION_ASSISTANT_ASIDE = "assistant_aside"
VIOLATION_INPUT_REQUEST = "input_request"
VIOLATION_PROMPT_LEAK = "prompt_scaffold_leak"
VIOLATION_PLACEHOLDER = "unfilled_placeholder"

# There is deliberately NO minimum length. An early draft floored a DM at 15 characters and refused
# "Congrats Jane!" — real copy — to catch nothing the checks below miss; shortness is not the
# failure this module exists for, and every floor that looks safe costs a real message somewhere.
# What IS refused is a body with no letter or digit in it at all ("...", "—", "``"), which is a
# model returning nothing rather than a person being terse.
_HAS_READABLE_RE = re.compile(r"[A-Za-z0-9]")

# `build_dm_from_template` writes to a 300-char budget, so a DM several times that length is the
# model dumping its reasoning rather than writing a message — the same failure mode as the incident
# body, caught by size when the wording is novel. Set high enough that ordinary variation never
# trips it. A comment has no such budget and gets no cap.
_MAX_CHARS = {SURFACE_DM: 900}

# The assistant addressing the OPERATOR instead of writing the message. This is the same failure
# `content_alignment.is_assistant_aside` catches for newsletter headlines (#1284), but its pattern
# set is deliberately NOT reused here, because a headline and a message tolerate different English:
# that guard refuses any body opening "I need…" / "I cannot…", which is correct for a headline and
# wrong for a DM, where "I can't make Thursday — does Friday work?" is exactly what a human writes.
# The forms below are the ones no human sends to a LinkedIn contact.
_ASSISTANT_ASIDE_RES: tuple = (
    # The acknowledgement opener only counts WITH its assistant follow-through. "Sure! Here's the
    # rewritten message" is an aside; "Sure enough, the retry storm was the cause", "Absolutely
    # brilliant framing" and "Of course the cache was cold" are things people write, and the bare
    # opener refuses all three to catch nothing the next pattern misses.
    re.compile(r"^(?:sure|certainly|of course|absolutely|understood)\s*[!,.:]\s*"
               r"(?:here|below|i'?ll|i\s+will|i'?ve|i\s+have)\b", re.IGNORECASE),
    re.compile(r"\bas\s+an\s+ai\b", re.IGNORECASE),
    re.compile(r"\bi(?:'?m|\s+am)\s+(?:unable\s+to|not\s+able\s+to)\b", re.IGNORECASE),
    re.compile(r"\bi\s+(?:cannot|can'?t)\s+(?:assist|help|comply|proceed|complete)\b",
               re.IGNORECASE),
    re.compile(r"\bhere(?:'s| is)\s+(?:the|your|a)\s+(?:rewritten|revised|updated|generated|"
               r"suggested)\s+(?:message|comment|reply|response|version|draft)\b", re.IGNORECASE),
    re.compile(r"\b(?:let\s+me\s+know\s+if\s+you'?d\s+like\s+me\s+to|would\s+you\s+like\s+me\s+to)"
               r"\s+(?:revise|rewrite|adjust|regenerate|try\s+again)\b", re.IGNORECASE),
    re.compile(r"\b(?:could|can|would)\s+you\s+(?:please\s+)?(?:clarify|confirm)\s+(?:the|what|"
               r"which)\b", re.IGNORECASE),
)

# The model asking the OPERATOR for input it thinks is missing. This is how the 2026-09-04 DM
# arrived: the aside opened with "To assist you effectively," — no banned opener, no hype word — and
# only then asked for the JSON, so every anchored-opener guard let it through.
#
# Both patterns require an ARTIFACT word (json/history/context/transcript/…) near the request verb.
# Without that anchor, "I need to see the traces before I can say" and "happy to share more context"
# — both ordinary DM sentences — would be refused, and refusing those buys nothing.
_INPUT_ARTIFACT = (r"(?:json|message\s+history|conversation\s+(?:history|context)|transcript|"
                   r"the\s+actual\s+\w+|more\s+information|additional\s+context)")

_INPUT_REQUEST_RES: tuple = (
    # "…I need the actual message history JSON…", "I don't have the conversation context…"
    re.compile(r"\bi\s+(?:need|require|don'?t\s+have|do\s+not\s+have|am\s+missing|was\s+not\s+given)"
               r"\b[^.!?]{0,60}?" + _INPUT_ARTIFACT, re.IGNORECASE),
    # "Please provide the message history so I can proceed", "…share the JSON…"
    re.compile(r"\b(?:provide|share|paste|supply|send|give\s+me)\b[^.!?]{0,40}?" + _INPUT_ARTIFACT,
               re.IGNORECASE),
    # "so I can proceed with evaluating…" — the model narrating its own next step to the operator.
    re.compile(r"\bso\s+(?:that\s+)?i\s+can\s+(?:proceed|continue|proceed\s+with|generate|evaluate)"
               r"\b", re.IGNORECASE),
)

# The PROMPT's own vocabulary surfacing in the reply. None of these words belong in a LinkedIn DM or
# comment; every one of them is a term this repo's prompts use to address the model.
_PROMPT_LEAK_RES: tuple = (
    re.compile(r"<\s*insert\b", re.IGNORECASE),
    re.compile(r"\binsert\s+(?:the\s+)?\w+(?:\s+\w+)?\s+here\b", re.IGNORECASE),
    re.compile(r"\b(?:main_focus|new_message|message_history|profile_synthesis|event_detail)\b",
               re.IGNORECASE),
    re.compile(r"\bmessage\s+history\s+json\b", re.IGNORECASE),
    re.compile(r"```"),
    # A markdown section header addressed to the model ("### Main Focus:"). Anchored to line start
    # so a mid-sentence "#3" or a hashtag is untouched.
    re.compile(r"^\s{0,3}#{2,}\s+\S", re.MULTILINE),
    # The instruction list itself echoed back ("Step 1: Parse the JSON string…").
    re.compile(r"^\s*step\s+\d\s*:", re.IGNORECASE | re.MULTILINE),
)

# Slot names — either OURS left unrendered by `render_dm_placeholders`, or one the model invented
# because it had no real value to put there (both `[link]` DMs on 2026-09-04 were the latter).
#
# Matched against a KNOWN vocabulary rather than "any braces or brackets": a comment on an
# engineering post legitimately quotes `{"role": "user"}` or cites `[1]`, and refusing those would
# cost real comments to buy nothing.
_PLACEHOLDER_NAMES = (r"first_name|firstname|last_name|full_name|headline|blog_url|blog|event_detail"
                      r"|link|url|name|company|topic|title|date|city|role|industry|your\s+\w+")

_PLACEHOLDER_RES: tuple = (
    re.compile(r"\{\s*(?:" + _PLACEHOLDER_NAMES + r")\s*\}", re.IGNORECASE),
    re.compile(r"\[\s*(?:" + _PLACEHOLDER_NAMES + r")\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*insert\b[^\]]*\]", re.IGNORECASE),
)


def _surface_max_chars(surface: Optional[str]) -> Optional[int]:
    """Length ceiling for a surface, or None where the surface has no budget."""
    return _MAX_CHARS.get(str(surface or "").lower())


def outbound_violations(text: Optional[str], surface: str = SURFACE_DM) -> list:
    """Every reason this body must not be sent, as stable check names. Empty list == safe to send.

    Returns ALL violations rather than short-circuiting on the first, so the refusal log names
    everything wrong with the body and a single line is enough to diagnose which upstream step
    produced it.
    """
    body = str(text or "")
    stripped = body.strip()
    violations = []

    if not stripped:
        return [VIOLATION_EMPTY]
    if not _HAS_READABLE_RE.search(stripped):
        return [VIOLATION_NO_CONTENT]
    ceiling = _surface_max_chars(surface)
    if ceiling and len(stripped) > ceiling:
        violations.append(VIOLATION_OVERLONG)
    if any(pattern.search(stripped) for pattern in _ASSISTANT_ASIDE_RES):
        violations.append(VIOLATION_ASSISTANT_ASIDE)
    if any(pattern.search(stripped) for pattern in _INPUT_REQUEST_RES):
        violations.append(VIOLATION_INPUT_REQUEST)
    if any(pattern.search(body) for pattern in _PROMPT_LEAK_RES):
        violations.append(VIOLATION_PROMPT_LEAK)
    if any(pattern.search(stripped) for pattern in _PLACEHOLDER_RES):
        violations.append(VIOLATION_PLACEHOLDER)
    return violations


def is_safe_to_send(text: Optional[str], surface: str = SURFACE_DM) -> bool:
    """True when this body may be sent to a real person on `surface`."""
    return not outbound_violations(text, surface=surface)


def refusal_reason(text: Optional[str], surface: str = SURFACE_DM) -> Optional[str]:
    """A log-ready reason string, or None when the body is safe.

    The excerpt is capped and newline-flattened because this lands in a `log_warning` that
    escalates: the grouped issue key should be the CHECK names, not the body, so the body is
    context at the end of the line rather than the thing being matched on.
    """
    violations = outbound_violations(text, surface=surface)
    if not violations:
        return None
    excerpt = re.sub(r"\s+", " ", str(text or "").strip())[:120]
    return f"{', '.join(violations)} — body starts: {excerpt!r}"
