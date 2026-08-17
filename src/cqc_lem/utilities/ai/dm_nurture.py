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

    Routed through `_call_llm` so its tokens/latency land in PostHog with every other LLM call.
    """
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
    the neutral 'keep it human' brief rather than to a pitch.
    """
    return _NURTURE_GUIDANCE.get(str(intent), _NURTURE_GUIDANCE[str(ReplyIntent.NEUTRAL)])


def nurture_delay_hours(intent: str) -> int:
    """How long to hold the drafted next message before its send slot."""
    return _NURTURE_DELAY_HOURS.get(str(intent), _NURTURE_DELAY_HOURS[str(ReplyIntent.NEUTRAL)])


# ── Who the recipient actually is (issue #1625) ───────────────────────────────────────────────
# The draft used to know their first name and nothing else, so a short or neutral reply left it
# with nothing to be specific about. Everything below is read from data LEM ALREADY HOLDS — no
# profile visit is made to write a draft, because a Chrome session per draft is a cost/account
# risk the draft does not justify. Two sources, both free:
#
#   1. `profiles` — the by-URL scrape cache, read through `db.get_profile_facts`, the same reader
#      the nightly lead scorer uses for ICP fit. Someone we have never scraped simply isn't in it.
#   2. The `dm_followups` row that opened the sequence — its `event_type` IS why we messaged them
#      first, and the caller already has it, so it costs no read at all.
#
# Missing is the NORMAL case, not a fault: the resolver returns whatever it found and the prompt
# renders only the fields present. A draft is never dropped for want of context.

# `event_type` -> what actually put us in this person's inbox. Kept in step with the enum
# `dm_followups.event_type` declares (tests/unit/utilities/test_dm_event_vocabulary.py owns that
# list); an event type with no phrase here contributes nothing rather than a guess.
#
# Every phrase must say what the SOURCE actually observed, in the right direction. The prompt hands
# these to the model as ground truth, so a phrase that overstates the relationship is exactly the
# fabrication this whole feature is gated against — #968 already had to rewrite the 'collaboration'
# DEFAULT TEMPLATE for thanking someone for a project neither party may have worked on, and the same
# wording must not come back in through the prompt. Two that read backwards if you skim the name:
#   'connection_accepted' — the source is `accept_connection_request`, i.e. THEY invited US and the
#       user accepted; nothing here says our invite was accepted.
#   'collaboration' — LinkedIn exposes no collaboration event, so the source is
#       `get_recent_collaborators`, which walks the MENTIONS feed.
_THREAD_ORIGINS: "dict[str, str]" = {
    "connection_accepted": "they sent you a connection invitation and you accepted it",
    "recommendation_received": "they wrote you a LinkedIn recommendation",
    "collaboration": "they mentioned you in a post or a comment of theirs",
    "profile_viewer": "they viewed your profile",
    "funnel": "you commented on their post first, then messaged them",
    "job_change": "they started a new role",
    "promotion": "they were promoted",
    "work_anniversary": "they hit a work anniversary",
    "birthday": "it was their birthday",
    "education": "they finished a course or degree",
    "in_the_news": "they were mentioned in the news",
    "manual": "you started this thread yourself",
    # 'nurture' means this IS the nurture sequence continuing; the original trigger is not on the
    # row, so saying anything here would be an invention.
}

# Values `get_profile_facts` can hand back that mean "we don't know". The column is
# `JSON_UNQUOTE(JSON_EXTRACT(data, ...))`, and a Pydantic `None` serializes as JSON `null`, which
# comes back as the four-character STRING 'null' — putting "their title is null" in a prompt.
_UNKNOWN_FACTS = {"", "none", "null"}

_RECIPIENT_FIELD_LABELS: "list[tuple[str, str]]" = [
    ("first_name", "First name"),
    ("job_title", "Their title"),
    ("company_name", "Their company"),
    ("industry", "Their industry"),
    ("thread_origin", "Why this conversation exists"),
]


def thread_origin(event_type: str = None) -> "str | None":
    """Plain-language reason this DM thread was opened, from the follow-up row's `event_type`.

    None for an event type with no phrase (including `nurture`, which is the sequence itself) —
    the prompt then omits the line instead of guessing why we are in their inbox.
    """
    return _THREAD_ORIGINS.get(str(event_type or "").strip().lower())


def recipient_context(profile_url: str = None, first_name: str = None, event_type: str = None,
                      user_id: int = None) -> dict:
    """What we know about the person we are drafting to, from stored data only (issue #1625).

    Returns a dict of the fields we actually have — any of `first_name`, `job_title`,
    `company_name`, `industry`, `thread_origin` — and `{}` when we know nothing about them, which
    is the pre-#1625 behaviour and still drafts a message.

    Never raises and never navigates: a profile we have not scraped is a quieter draft, not a
    dropped one.
    """
    context: dict = {}
    name = str(first_name or "").strip()
    if name:
        context["first_name"] = name

    facts: dict = {}
    if profile_url:
        try:
            from cqc_lem.utilities.db import get_profile_facts
            rows = get_profile_facts([profile_url]) or {}
            facts = next((row for row in rows.values() if row), {})
        except Exception as e:
            # A read fault, not an absent profile — the latter is an empty dict from a clean read.
            log_warning("Could not read stored profile facts for a nurture draft", exc=e,
                        user_id=user_id, action_type="dm")

    for field in ("job_title", "company_name", "industry"):
        value = str(facts.get(field) or "").strip()
        if value.lower() not in _UNKNOWN_FACTS:
            context[field] = value

    origin = thread_origin(event_type)
    if origin:
        context["thread_origin"] = origin

    if not facts:
        # Expected for anyone we have never scraped — DEBUG, never a warning (see utilities/CLAUDE.md).
        log_debug(f"Nurture recipient context: no stored profile for {profile_url or 'an unknown URL'}",
                  user_id=user_id, action_type="dm")
    return context


def format_recipient_context(context: dict = None) -> str:
    """Render `recipient_context()` as the prompt block, or `""` when there is nothing to say.

    The heading is doing real work: it tells the model these are observations we already made, not
    licence to infer their team size, budget, stack or problem from a job title.
    """
    lines = [f"- {label}: {context[key]}"
             for key, label in _RECIPIENT_FIELD_LABELS if str((context or {}).get(key) or "").strip()]
    if not lines:
        return ""
    return ("Who I am writing to — everything I know about them, and nothing more. Use it to be "
            "specific and relevant. Do NOT recite it back to them, and do NOT infer anything from "
            "it (team size, budget, tools, goals, or what they need):\n"
            + "\n".join(lines) + "\n\n")
