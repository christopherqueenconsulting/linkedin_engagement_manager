"""Inbound buying-intent detection (issue #483).

Someone asking "how much?", "do you help with X?", or "DM me" in a comment/reply/DM is the warmest
lead we get. This module is the classifier the existing read paths call on text they ALREADY have —
it never scrapes anything itself.

Two tiers, in cost order:
1. A heuristic keyword/regex pass. STRONG families (pricing, hiring us, contact-me, stated need,
   referral ask, demo/trial) are decisive on their own; WEAK families are merely suggestive.
2. For the ambiguous band only — weak signal but no strong one — ONE cheap `lem-simple` call.
   Text with no signal at all never reaches the LLM, which matters because comments and replies run
   at high volume (same cost logic as COMMENT_RESEARCH_ENABLED in content_research.py).

Env:
  LEAD_INTENT_LLM_ENABLED  — allow the tier-2 LLM check for ambiguous text (default: true)
  LEAD_INTENT_MIN_SCORE    — score at/above which text is flagged a lead (default: 40)
"""

import os
import re

from cqc_lem.utilities.logger import log_debug, log_warning

# Weight per matched family. One STRONG family alone clears the default threshold; WEAK families
# only accumulate toward the ambiguous band that earns an LLM check.
_STRONG_WEIGHT = 40
_WEAK_WEIGHT = 15
_MAX_SCORE = 100
# What the LLM's "yes" is worth — above the default threshold, but below a strong keyword hit so the
# operator's queue still ranks explicit asks first.
_LLM_SCORE = 55
# Text shorter than this ("nice!", "🔥") can't carry intent; skip it before any matching.
_MIN_TEXT_CHARS = 8
# Cap what we send to the classifier / store as evidence.
_MAX_TEXT_CHARS = 1200
# The tier-2 answer is one word ('yes'/'no') — no reason to pay for more.
_LLM_MAX_TOKENS = 3

_STRONG_PATTERNS: "list[tuple[str, str]]" = [
    ("pricing", r"\b(how much|pricing|price list|prices?|what (does|would) (it|this|that) cost|"
                r"cost(s)? (per|to|for)|rates?|quote|retainer|budget for)\b"),
    ("engage_us", r"\b(do you (offer|sell|provide|have|do|build|handle|work with|help)|"
                  r"can you (help|do|build|make|handle|take)|could you (help|do|build)|"
                  r"are you (taking|accepting)\b[^.?!]{0,20}\b(clients|customers|work|projects))\b"),
    ("contact_me", r"\b(dm me|dm'?d you|send me a dm|message me|pm me|email me|reach out|"
                   r"get in touch|contact me|let'?s (talk|chat|connect|set (up|something)|schedule)|"
                   r"book a (call|demo|time)|hop on a call|calendly)\b"),
    ("stated_need", r"\b(i need|we need|i'?m looking for|we'?re looking for|looking for someone|"
                    r"need help with|need this|in the market for|shopping for|trying to find)\b"),
    ("referral", r"\b(who do you recommend|any recommendations|recommend (someone|anyone|a\b)|"
                 r"referral|know anyone who)\b"),
    ("demo_trial", r"\b(free trial|start a trial|book a demo|see a demo|sign up for|how do i "
                   r"(start|sign up|get started)|get started with)\b"),
]

_WEAK_PATTERNS: "list[tuple[str, str]]" = [
    ("curiosity", r"\b(interested in|tell me more|more info(rmation)?|learn more|send (me )?"
                  r"(the )?(link|details|info)|curious (about|how|what))\b"),
    ("buying_context", r"\b(my (team|company|agency|clients)|our (team|company|agency|clients)|"
                       r"we'?re (currently|about to|planning to|evaluating))\b"),
    ("direct_question", r"\byou(r)?\b[^?]{0,120}\?"),
]

_COMPILED_STRONG = [(label, re.compile(pattern, re.I)) for label, pattern in _STRONG_PATTERNS]
_COMPILED_WEAK = [(label, re.compile(pattern, re.I)) for label, pattern in _WEAK_PATTERNS]

_NO_SIGNAL: dict = {"is_lead": False, "score": 0, "matched": [], "method": "none"}

_LLM_SYSTEM = (
    "You classify whether a LinkedIn comment or message is INBOUND BUYING INTENT — the person is "
    "asking about the author's services, pricing, availability, or otherwise raising their hand to "
    "start a business conversation. Praise, agreement, general discussion, job seeking, and "
    "someone pitching THEIR OWN services are NOT buying intent. Answer with only 'yes' or 'no'."
)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _min_score() -> int:
    try:
        return int(os.environ.get("LEAD_INTENT_MIN_SCORE") or _STRONG_WEIGHT)
    except ValueError:
        return _STRONG_WEIGHT


def _llm_says_lead(text: str) -> bool:
    """Tier-2 check for the ambiguous band. Fails CLOSED (False) — a classifier hiccup must not
    flood the operator's leads inbox with noise; a genuine lead usually carries a strong keyword.

    Routed through `_call_llm` so this high-volume classifier's tokens/latency land in PostHog with
    every other LLM call.
    """
    from cqc_lem.utilities.ai.ai_helper import _call_llm
    try:
        response = _call_llm(
            model="lem-simple",
            messages=[
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": f"Message:\n{text}\n\nIs this inbound buying intent? "
                                            f"Answer yes or no."},
            ],
            temperature=0,
            max_tokens=_LLM_MAX_TOKENS,
        )
        answer = (response.choices[0].message.content or "").strip().lower()
        return answer.startswith("y")
    except Exception as e:
        log_warning("Lead-intent LLM check failed (treating as not a lead)", exc=e, ai_model="lem-simple")
        return False


def detect_lead_signals(text: str, use_llm: bool = True) -> dict:
    """Classify one piece of inbound text (a comment, reply, or DM body) for buying intent.

    Returns {'is_lead': bool, 'score': 0-100, 'matched': [family labels], 'method': 'none'|
    'heuristic'|'llm'}. Never raises — an unusable input or a failed LLM call is simply "not a lead".
    """
    cleaned = (text or "").strip()[:_MAX_TEXT_CHARS]
    if len(cleaned) < _MIN_TEXT_CHARS:
        return dict(_NO_SIGNAL)

    matched = [label for label, rx in _COMPILED_STRONG if rx.search(cleaned)]
    weak = [label for label, rx in _COMPILED_WEAK if rx.search(cleaned)]
    score = min(_MAX_SCORE, len(matched) * _STRONG_WEIGHT + len(weak) * _WEAK_WEIGHT)
    matched += weak
    threshold = _min_score()

    if score >= threshold:
        log_debug(f"Lead intent (heuristic, score {score}): {matched}", action_type="engaged")
        return {"is_lead": True, "score": score, "matched": matched, "method": "heuristic"}

    # Ambiguous band: something suggestive fired but nothing decisive. Only these pay for an LLM call.
    if score > 0 and use_llm and _bool_env("LEAD_INTENT_LLM_ENABLED", True) and _llm_says_lead(cleaned):
        log_debug(f"Lead intent (LLM) on weak signals {matched}", action_type="engaged")
        return {"is_lead": True, "score": max(score, _LLM_SCORE), "matched": matched, "method": "llm"}

    return {"is_lead": False, "score": score, "matched": matched, "method": "heuristic"}
