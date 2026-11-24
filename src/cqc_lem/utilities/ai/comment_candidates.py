"""Multi-candidate comment selection with a cross-family judge (#2627 Phase 3, issue #2751).

With `COMMENT_MULTI_CANDIDATE` on, the comment writer draws `CANDIDATE_COUNT` drafts, the
deterministic gates grade each, and the judge picks one of the survivors. The judge runs on the
`lem-judge` alias, whose family differs from the writer tier's (config test), so it never grades its
own style. Every failure path fails OPEN to the first survivor: a judge hiccup never skips a comment.
"""
import json
import re
from typing import Callable, Optional

from cqc_lem.utilities.flags import COMMENT_MULTI_CANDIDATE, flag_enabled
from cqc_lem.utilities.logger import log_debug

CANDIDATE_COUNT = 3
JUDGE_TIER = "lem-judge"
_JUDGE_MAX_TOKENS = 600

_JUDGE_SYSTEM = (
    "You pick between candidate LinkedIn comments written for the same post. Choose the ONE a busy "
    "practitioner would plausibly type: specific to the post, plain, unpolished, no filler praise, "
    "no template shape. Reply with ONLY a JSON object: {\"pick\": <candidate number>, "
    "\"reason\": \"<one short sentence>\"}."
)


def enabled(user_id: Optional[int] = None) -> bool:
    """Whether multi-candidate selection is on for this user."""
    return flag_enabled(COMMENT_MULTI_CANDIDATE, user_id)


def _parse_pick(raw: Optional[str], count: int) -> Optional[tuple]:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        pick = int(data.get("pick"))
    except (ValueError, TypeError, AttributeError):
        return None
    if not 1 <= pick <= count:
        return None
    return pick - 1, str(data.get("reason") or "").strip()


def judge_pick(candidates: list, post_content: str, call_llm: Callable,
               user_id: Optional[int] = None) -> int:
    """Index of the candidate the judge prefers; 0 when there is one or the judge is unreadable."""
    if len(candidates) < 2:
        return 0
    listing = "\n\n".join(f"Candidate {i}:\n{c}" for i, c in enumerate(candidates, 1))
    try:
        resp = call_llm(model=JUDGE_TIER, temperature=0, max_tokens=_JUDGE_MAX_TOKENS,
                        messages=[{"role": "system", "content": _JUDGE_SYSTEM},
                                  {"role": "user",
                                   "content": f"Post:\n{str(post_content or '')[:1500]}\n\n{listing}"}])
        parsed = _parse_pick(resp.choices[0].message.content, len(candidates))
    except Exception as exc:
        log_debug(f"Comment judge call failed: {type(exc).__name__}", user_id=user_id,
                  action_type="comment")
        return 0
    if parsed is None:
        log_debug("Comment judge reply unreadable — keeping the first candidate", user_id=user_id,
                  action_type="comment")
        return 0
    index, reason = parsed
    log_debug(f"Comment judge picked candidate {index + 1} of {len(candidates)}: {reason}",
              user_id=user_id, action_type="comment")
    return index


def select_candidate(draw: Callable[[], Optional[str]], is_clean: Callable[[str], bool],
                     pick: Callable[[list], int]) -> Optional[str]:
    """Draw `CANDIDATE_COUNT` drafts and return the judge's choice among the gate-clean ones.

    Returns the first draft when none (or only one) is clean, so the caller's normal gate path runs
    on it unchanged. None only when the writer produced nothing at all.
    """
    drafts = [d for d in (draw() for _ in range(CANDIDATE_COUNT)) if d]
    if not drafts:
        return None
    clean = [d for d in drafts if is_clean(d)]
    if len(clean) < 2:
        return clean[0] if clean else drafts[0]
    return clean[pick(clean)]
