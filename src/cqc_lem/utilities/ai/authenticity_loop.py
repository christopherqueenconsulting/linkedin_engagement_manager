"""Authenticity loop for comment and DM drafts (#2627 Phase 3, issue #2751).

Behind `AUTHENTICITY_LOOP` (default off): the writer prompt carries `PRE_TEXT`, each draft is scored
0-100 by the judge, and a draft below `AUTHENTICITY_FLOOR` is regenerated with the specific issues,
up to `MAX_LOOPS` times. A draft that never reaches the floor is HELD by the caller, never posted.
Scoring fails OPEN: an unreadable judge never blocks a draft.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from cqc_lem.utilities.ai.authenticity_rubric import AI_SLOP_TELLS, AUTHENTICITY_JUDGE_CRITERIA
from cqc_lem.utilities.flags import AUTHENTICITY_LOOP, flag_enabled
from cqc_lem.utilities.logger import log_debug

AUTHENTICITY_FLOOR = 50
MAX_LOOPS = 3
JUDGE_TIER = "lem-judge"
_JUDGE_MAX_TOKENS = 600

PRE_TEXT = (
    "\nAuthenticity: write as the person would type it, not as an assistant. Name one concrete "
    "detail from the thread or their own experience instead of a general claim, use plain words, "
    "and keep it short. Avoid: " + "; ".join(AI_SLOP_TELLS) + ".\n"
)

_SYSTEM = (
    AUTHENTICITY_JUDGE_CRITERIA + " The draft is a short LinkedIn comment or direct message, so "
    "judge it as that: generic praise, template shape and filler score low; a specific, plain, "
    "human reply scores high. Reply with ONLY a JSON object: {\"score\": <0-100 integer>, "
    "\"issues\": [\"<specific problem to fix>\", ...]} with at most 4 short issues."
)


@dataclass
class LoopResult:
    """Outcome of `run_loop`: the best draft, its score, the issues and whether it cleared the floor."""

    text: Optional[str]
    score: Optional[int]
    issues: list = field(default_factory=list)
    passed: bool = True
    loops: int = 0


def enabled(user_id: Optional[int] = None) -> bool:
    """Whether the authenticity loop is on for this user."""
    return flag_enabled(AUTHENTICITY_LOOP, user_id)


def pre_text(user_id: Optional[int] = None) -> str:
    """The prompt pre-text, or "" when the flag is off so the off-path prompt is unchanged."""
    return PRE_TEXT if enabled(user_id) else ""


def below_floor(score: Optional[int]) -> bool:
    """True only for a known score under the floor; an unscored draft is never blocked."""
    return score is not None and int(score) < AUTHENTICITY_FLOOR


def fix_directive(issues: list) -> str:
    """The regeneration directive naming the judge's specific issues."""
    return ("\nYour last draft read as generic or machine-written. Fix exactly these: "
            + "; ".join(issues or ["make it more specific and personal"]) + ".\n")


def _parse(raw: Optional[str]) -> Optional[tuple]:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        score = max(0, min(100, int(data.get("score"))))
    except (ValueError, TypeError, AttributeError):
        return None
    issues = [str(i).strip() for i in (data.get("issues") or []) if str(i).strip()][:4]
    return score, issues


def score_draft(text: str, call_llm: Callable, user_id: Optional[int] = None) -> Optional[tuple]:
    """(score, issues) for a draft, or None when the judge failed or was unreadable (fail open)."""
    try:
        resp = call_llm(model=JUDGE_TIER, temperature=0, max_tokens=_JUDGE_MAX_TOKENS,
                        messages=[{"role": "system", "content": _SYSTEM},
                                  {"role": "user", "content": f"Draft:\n{str(text)[:1500]}"}])
        parsed = _parse(resp.choices[0].message.content)
    except Exception as exc:
        log_debug(f"Authenticity judge call failed: {type(exc).__name__}", user_id=user_id)
        return None
    if parsed is None:
        log_debug("Authenticity judge reply unreadable — draft not blocked", user_id=user_id)
    return parsed


def run_loop(draft: Callable[[str], Optional[str]], call_llm: Callable,
             user_id: Optional[int] = None, first: Optional[str] = None) -> LoopResult:
    """Score `draft`, regenerating with the issues up to `MAX_LOOPS` times while below the floor.

    `draft(directive)` returns the next text or None; `first` is an already-drafted first attempt.
    The result is the last draft with its score; `passed` is False only when every attempt scored
    below the floor. An unscored draft passes.
    """
    text = first if first is not None else draft("")
    result = LoopResult(text=text, score=None, passed=True)
    for loop in range(MAX_LOOPS + 1):
        if not text:
            return LoopResult(text=None, score=None, passed=False, loops=loop)
        scored = score_draft(text, call_llm, user_id)
        if scored is None:
            return LoopResult(text=text, score=None, passed=True, loops=loop)
        score, issues = scored
        result = LoopResult(text=text, score=score, issues=issues,
                            passed=not below_floor(score), loops=loop)
        if result.passed or loop == MAX_LOOPS:
            return result
        text = draft(fix_directive(issues)) or text
    return result
