"""DM conversation reply-intent classification (issue #485).

When a lead replies to one of our DMs the sequence stops — and today the thread just goes cold.
This module is the classifier that decides what their reply MEANS so the nurture path can branch:
interested -> propose a call, objection -> address it, not-now -> light nurture later, explicit
disinterest -> stop and never message them again.

It mirrors `lead_intent.py`: a heuristic pass first, one cheap `lem-simple` call only for the
ambiguous band. DM replies are far lower volume than comments, so the LLM tier is ON by default —
but it never runs on text a decisive keyword already settled.

Env:
  DM_NURTURE_INTENT_LLM_ENABLED — allow the tier-2 LLM classification (default: true)
"""

import os
import re
from enum import StrEnum

from cqc_lem.utilities.logger import log_debug, log_warning


class ReplyIntent(StrEnum):
    """What a lead's inbound DM reply means for the next touch (issue #485)."""
    INTERESTED = 'interested'      # they want to go further -> propose a real conversation
    OBJECTION = 'objection'        # a concern/blocker -> answer it honestly, no pressure
    NOT_NOW = 'not_now'            # timing, not fit -> light value touch later
    DISINTEREST = 'disinterest'    # explicit no -> stop; never nurture this thread again
    NEUTRAL = 'neutral'            # replied, but nothing decisive -> keep it warm and human


# Text shorter than this ("ok", "👍") carries no branchable meaning; it still counts as a reply.
_MIN_TEXT_CHARS = 4
# Cap what we send to the classifier / carry into the draft prompt.
_MAX_TEXT_CHARS = 1200
# The tier-2 answer is one word — no reason to pay for more.
_LLM_MAX_TOKENS = 4

# Ordered by precedence, SAFEST FIRST: someone saying "not interested right now" matches both
# disinterest and not_now, and the only acceptable reading of that is stop.
_PATTERNS: "list[tuple[ReplyIntent, str]]" = [
    (ReplyIntent.DISINTEREST,
     r"\b(not interested|no thanks|no thank you|stop (messaging|contacting|emailing)|"
     r"(please )?(don'?t|do not) (message|contact|reach out)|unsubscribe|remove me|take me off|"
     r"(not|isn'?t|is not|aren'?t|won'?t be) a (good |great )?fit|"
     r"i'?ll pass|we'?ll pass|leave me alone|spam)\b"),
    (ReplyIntent.NOT_NOW,
     r"\b(not (right )?now|bad timing|not at (the|this) moment|maybe (later|next)|"
     r"circle back|check back|touch base (later|in)|revisit (this )?(later|in)|"
     r"next (quarter|year|month)|(later|early) (this|next) (quarter|year)|in a few (weeks|months)|"
     r"too busy|swamped|tied up|after the (holidays|launch|quarter))\b"),
    (ReplyIntent.OBJECTION,
     r"\b(too expensive|out of (our|my) budget|no budget|can'?t afford|"
     r"(we|i) already (have|use|work with|got)|(we|i)'?re already|already (have|using|working with)|"
     r"how (is|are) (this|you|yours) different|why (would|should) (we|i)|what makes you|"
     r"(not|isn'?t) sure (this|it|that|if)|(i|we) (have|has) concerns|the problem is|"
     r"(we|i) tried (that|this|something similar)|does(n'?t| not) work for)\b"),
    (ReplyIntent.INTERESTED,
     r"\b(sounds (good|great|interesting)|(i'?m|we'?re|am) interested|tell me more|"
     r"(would|i'?d|we'?d) love to|let'?s (talk|chat|connect|set (up|something)|schedule|do it)|"
     r"happy to (chat|talk|connect|jump)|book a (call|time|demo)|send (me )?(the |a )?"
     r"(link|details|info|calendar|calendly|times)|when (are|can) you (free|available)|"
     r"what (does|would) (it|this|that) cost|how much|open to (a|it|that|chatting)|"
     r"yes,? (please|let'?s|i)|sign me up|makes sense)\b"),
]

_COMPILED = [(intent, re.compile(pattern, re.I)) for intent, pattern in _PATTERNS]

_LLM_SYSTEM = (
    "You classify how a person replied to a LinkedIn direct message from a professional who reached "
    "out to them. Answer with exactly ONE of these words and nothing else:\n"
    "interested - they want to go further (a call, details, pricing, next steps)\n"
    "objection - they raised a concern, doubt, or blocker (cost, an existing vendor, skepticism)\n"
    "not_now - open in principle but the timing is wrong\n"
    "disinterest - an explicit no, or a request to stop contacting them\n"
    "neutral - a reply that is none of the above (small talk, thanks, a general remark)"
)

_LLM_ANSWERS = {str(i): i for i in ReplyIntent}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _llm_intent(text: str) -> "ReplyIntent | None":
    """Tier-2 classification for replies no keyword settled. Returns None on any failure — the
    caller then treats the reply as NEUTRAL, which keeps the thread warm without ever inventing a
    'disinterest' (the one verdict that would silently kill a live conversation).

    Routed through `_call_llm` so its tokens/latency land in PostHog with every other LLM call."""
    from cqc_lem.utilities.ai.ai_helper import _call_llm
    try:
        response = _call_llm(
            model="lem-simple",
            messages=[
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": f"Their reply:\n{text}\n\nOne word:"},
            ],
            temperature=0,
            max_tokens=_LLM_MAX_TOKENS,
        )
        answer = (response.choices[0].message.content or "").strip().lower().strip(".'\"")
        return _LLM_ANSWERS.get(answer.replace(" ", "_").replace("-", "_"))
    except Exception as e:
        log_warning("DM reply-intent LLM check failed (treating as neutral)", exc=e,
                    ai_model="lem-simple", action_type="dm")
        return None


def classify_reply_intent(text: str, use_llm: bool = True) -> dict:
    """Classify ONE inbound DM reply.

    Returns {'intent': str, 'matched': [labels], 'method': 'none'|'heuristic'|'llm'}. Never raises:
    unusable input or a failed LLM call is simply NEUTRAL, so the nurture path still drafts a human
    next message instead of dropping the thread.
    """
    cleaned = (text or "").strip()[:_MAX_TEXT_CHARS]
    if len(cleaned) < _MIN_TEXT_CHARS:
        return {"intent": str(ReplyIntent.NEUTRAL), "matched": [], "method": "none"}

    for intent, rx in _COMPILED:
        match = rx.search(cleaned)
        if match:
            log_debug(f"DM reply intent (heuristic): {intent} via '{match.group(0)}'", action_type="dm")
            return {"intent": str(intent), "matched": [match.group(0).lower()], "method": "heuristic"}

    if use_llm and _bool_env("DM_NURTURE_INTENT_LLM_ENABLED", True):
        guess = _llm_intent(cleaned)
        if guess is not None:
            log_debug(f"DM reply intent (LLM): {guess}", action_type="dm")
            return {"intent": str(guess), "matched": [], "method": "llm"}

    return {"intent": str(ReplyIntent.NEUTRAL), "matched": [], "method": "heuristic"}


def is_stop_intent(intent: str) -> bool:
    """True when the reply is an explicit no. Nothing further is drafted for that thread."""
    return str(intent) == str(ReplyIntent.DISINTEREST)


# What the next message should DO, per intent. This is the branching the whole feature exists for:
# the same "they replied" event produces a call proposal, an honest answer, or a light touch.
_NURTURE_GUIDANCE: "dict[str, str]" = {
    str(ReplyIntent.INTERESTED):
        "They are OPEN to going further. Match their energy, answer what they actually asked, and "
        "propose ONE concrete next step — a short call — with a low-friction way to say yes. Do not "
        "re-pitch; they are already in.",
    str(ReplyIntent.OBJECTION):
        "They raised a real concern. Acknowledge it plainly and take it seriously — no defensiveness, "
        "no counter-pitch. Give ONE honest, specific line that speaks to THEIR concern, and leave the "
        "door open without pushing for a meeting.",
    str(ReplyIntent.NOT_NOW):
        "The timing is wrong, not the fit. Be gracious, take the pressure off completely, offer ONE "
        "genuinely useful thing they can use now, and suggest reconnecting later. Do NOT ask for a "
        "call in this message.",
    str(ReplyIntent.NEUTRAL):
        "They replied but gave you little to work with. Be human: respond to whatever they did say, "
        "add ONE useful or curious thought, and ask ONE easy question that invites a real answer. Do "
        "not pitch and do not ask for a call yet.",
}

# Hours to wait before the drafted message is scheduled to go out, per intent. Interest decays fast;
# a "not now" needs real space or the follow-up becomes the thing they said no to.
_NURTURE_DELAY_HOURS: "dict[str, int]" = {
    str(ReplyIntent.INTERESTED): 1,
    str(ReplyIntent.OBJECTION): 4,
    str(ReplyIntent.NOT_NOW): 24 * 30,
    str(ReplyIntent.NEUTRAL): 12,
}


def nurture_guidance(intent: str) -> str:
    """Prompt guidance for drafting the next message in this thread. Unknown intents fall back to
    the neutral 'keep it human' brief rather than to a pitch."""
    return _NURTURE_GUIDANCE.get(str(intent), _NURTURE_GUIDANCE[str(ReplyIntent.NEUTRAL)])


def nurture_delay_hours(intent: str) -> int:
    """How long to hold the drafted next message before its send slot."""
    return _NURTURE_DELAY_HOURS.get(str(intent), _NURTURE_DELAY_HOURS[str(ReplyIntent.NEUTRAL)])
