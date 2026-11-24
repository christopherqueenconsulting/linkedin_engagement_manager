"""Deterministic risk flags and a confidence score for the Daily review queue (#2625 Phase 2).

Pure text rules — no LLM call, no I/O — so a reviewer can predict exactly why an item was flagged
and the same draft always scores the same. A flag means "read this before approving", never "this
is wrong"; an item with NO flags is the only kind a bulk "approve all clean" may sweep in.

`confidence` is `100 - sum(weights)` clamped to 0..100, or None when there is nothing to score (an
empty draft is unmeasured, never confident).
"""

import re
from typing import Optional

FLAG_NUMBER = "number"
FLAG_FIRST_PERSON_CLAIM = "first_person_claim"
FLAG_NAMED_ENTITY = "named_entity"
FLAG_LINK = "link"
FLAG_MEETING_ASK = "meeting_ask"
FLAG_GATE_NEAR_MISS = "gate_near_miss"
FLAG_NEAR_DUPLICATE = "near_duplicate"
FLAG_STALE_SLOT = "stale_slot"

FLAG_WEIGHTS = {
    FLAG_NUMBER: 15, FLAG_FIRST_PERSON_CLAIM: 20, FLAG_NAMED_ENTITY: 10, FLAG_LINK: 10,
    FLAG_MEETING_ASK: 25, FLAG_GATE_NEAR_MISS: 25, FLAG_NEAR_DUPLICATE: 30, FLAG_STALE_SLOT: 10,
}
STALE_SLOT_HOURS = 48
DUPLICATE_JACCARD = 0.8

_NUMBER = re.compile(r"\d[\d,.]*\s*(?:%|percent\b|x\b|k\b|m\b|million\b|billion\b)|\$\s?\d|\b\d{2,}\b",
                     re.IGNORECASE)
_CLAIM = re.compile(r"\b(?:i|we)\s+(?:have\s+|'ve\s+|just\s+|recently\s+)?(?:built|launched|grew|grown|"
                    r"increased|reduced|saved|doubled|tripled|led|ran|closed|raised|hired|shipped|"
                    r"scaled|helped|worked|managed|founded|sold|generated|won)\b"
                    r"|\bmy (?:team|company|clients?|customers?)\b|\bwhen i was\b", re.IGNORECASE)
_LINK = re.compile(r"https?://|\bwww\.|\b[\w-]+\.(?:com|io|ai|co|org|net|app|dev)/", re.IGNORECASE)
_MEETING = re.compile(r"\b(?:book|schedule|grab|hop on|jump on|set up)\b[^.!?\n]{0,30}"
                      r"\b(?:call|meeting|chat|demo|time|15|20|30)\b|\bcalendly\b|"
                      r"\blet'?s (?:chat|talk|connect|meet)\b|\bquick (?:call|chat)\b", re.IGNORECASE)
# Two capitalised words in a row, or one that is not at a sentence start — a name, company or product.
_NAME = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]+\s+[A-Z][a-z]+\b|(?<=[a-z,]\s)[A-Z][a-zA-Z]{2,}\b")
_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def text_flags(text: Optional[str]) -> list[str]:
    """The text-only flags for one draft, in a stable order."""
    text = text or ""
    found = []
    for flag, rx in ((FLAG_NUMBER, _NUMBER), (FLAG_FIRST_PERSON_CLAIM, _CLAIM),
                     (FLAG_NAMED_ENTITY, _NAME), (FLAG_LINK, _LINK), (FLAG_MEETING_ASK, _MEETING)):
        if rx.search(text):
            found.append(flag)
    return found


def confidence_for(flags: list[str], text: Optional[str]) -> Optional[int]:
    """0-100, or None for an empty draft (unmeasured is never confident)."""
    if not (text or "").strip():
        return None
    return max(0, 100 - sum(FLAG_WEIGHTS.get(f, 10) for f in flags))


def annotate(items: list[dict]) -> list[dict]:
    """Fill `risk_flags` and `confidence` on every item in place, and return the list.

    Near-duplicate is measured across the whole queue (word-set Jaccard >= 0.8 against any other
    item with a draft), because two near-identical DMs approved together is the failure it exists for.
    """
    word_sets = [set(_words(i.get("draft_text") or "")) for i in items]
    for idx, item in enumerate(items):
        flags = text_flags(item.get("draft_text"))
        if item.get("hold_reason"):
            flags.append(FLAG_GATE_NEAR_MISS)
        if word_sets[idx] and any(j != idx and _jaccard(word_sets[idx], word_sets[j]) >= DUPLICATE_JACCARD
                                  for j in range(len(items))):
            flags.append(FLAG_NEAR_DUPLICATE)
        age = item.get("age_hours")
        if item.get("lane") == "scheduled_dms" and age is not None and age > STALE_SLOT_HOURS:
            flags.append(FLAG_STALE_SLOT)
        item["risk_flags"] = flags
        item["confidence"] = confidence_for(flags, item.get("draft_text"))
    return items
