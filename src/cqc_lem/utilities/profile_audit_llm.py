"""Monthly LLM deep audit of the user's headline and About (issue #2754, Phase 3 of #2631).

Compares the stored profile text with buyer vocabulary the user already stores: the ICP definition's
pains, trigger phrases and role titles, plus the posts the roster scan kept from ICP targets. The
output is a list of mismatches with the exact phrases. It is REPORT-ONLY: nothing here writes to
LinkedIn or to the stored profile JSON, and every failure reads as "no LLM finding", never as an
error, so the rubric-only audit stands.
"""

import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence

from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.logger import log_debug, log_warning

LLM_FINDING_ID = "icp_language_gap"
MAX_TERMS = 12
MAX_POSTS_IN_PROMPT = 12
POST_CHARS = 400
MIN_POST_TEXTS = 2  # a phrase must recur across this many posts to count as vocabulary
MAX_MISMATCHES = 5
_WORD_RE = re.compile(r"[a-z][a-z0-9+#-]{3,}")
_STOP = frozenset(
    "that this with from have your what when they their about would there which will more "
    "just been were than into also very some other like over most make only them being "
    "while after because people think every could should still where here these those".split())

PROFILE_AUDIT_SYSTEM = (
    "You review a LinkedIn profile for buyer-language fit. You are given the profile's headline "
    "and About, plus phrases and post excerpts written by the user's ideal buyers. Find buyer "
    "phrases the profile does NOT use but should, or profile wording buyers never use. Quote "
    "phrases exactly as written in the inputs; never invent a phrase. Never rewrite the profile. "
    'Reply with JSON only: {"mismatches": [{"buyer_phrase": "...", "profile_phrase": "..." or '
    'null, "note": "<=140 chars"}]} with at most 5 items, or {"mismatches": []}.')


def mine_icp_terms(definition: Optional[Dict[str, Any]],
                   post_texts: Optional[Sequence[str]]) -> List[str]:
    """Stored buyer vocabulary: the ICP definition's phrases first, then words recurring in posts.

    Deterministic and offline. An empty list means nothing is stored, which the rubric reads as
    `unknown`.
    """
    terms: List[str] = []
    seen = set()

    def _add(value: str) -> None:
        key = " ".join(str(value or "").lower().split())
        if key and key not in seen and len(terms) < MAX_TERMS:
            seen.add(key)
            terms.append(key)

    segments = (definition or {}).get("segments") if isinstance(definition, dict) else None
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        for field in ("trigger_phrases", "pains", "role_titles"):
            for value in segment.get(field) or []:
                _add(value)
    counts: Counter = Counter()
    for text in post_texts or []:
        counts.update({w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP})
    for word, n in counts.most_common():
        if n < MIN_POST_TEXTS:
            break
        _add(word)
    return terms


def _verbatim(phrase: Any, source: str) -> bool:
    return isinstance(phrase, str) and bool(phrase.strip()) and phrase.lower().strip() in source


def parse_mismatches(raw: str, profile_text: str, buyer_text: str) -> List[Dict[str, str]]:
    """Valid mismatches only: a quoted phrase the model made up is dropped, not repaired."""
    try:
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except (ValueError, TypeError):
        return []
    items = data.get("mismatches") if isinstance(data, dict) else None
    profile_l, buyer_l = profile_text.lower(), buyer_text.lower()
    out: List[Dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not _verbatim(item.get("buyer_phrase"), buyer_l):
            continue
        own = item.get("profile_phrase")
        out.append({"buyer_phrase": item["buyer_phrase"].strip(),
                    "profile_phrase": own.strip() if _verbatim(own, profile_l) else "",
                    "note": str(item.get("note") or "")[:140]})
        if len(out) >= MAX_MISMATCHES:
            break
    return out


def llm_audit_profile(profile: LinkedInProfile, terms: Sequence[str],
                      post_texts: Sequence[str], user_id: Optional[int] = None) -> Optional[Dict[str, str]]:
    """One finding dict for the monthly audit, or None (nothing to compare, nothing found, a fault).

    The finding is a `profile_audit_findings` row with weight 0, so it never moves the score.
    """
    headline, about = (profile.headline or "").strip(), (profile.about or "").strip()
    if not (headline or about) or not (terms or post_texts):
        log_debug("Profile LLM audit skipped: no profile text or no stored ICP vocabulary", user_id=user_id)
        return None
    excerpts = [t[:POST_CHARS] for t in list(post_texts)[:MAX_POSTS_IN_PROMPT]]
    buyer_text = "\n".join(list(terms) + excerpts)
    profile_text = f"{headline}\n{about}"
    user = (f"Headline:\n{headline or '(none)'}\n\nAbout:\n{about or '(none)'}\n\n"
            f"Buyer phrases:\n{chr(10).join(terms) or '(none)'}\n\n"
            f"Buyer post excerpts:\n{chr(10).join('- ' + e for e in excerpts) or '(none)'}")
    try:
        from cqc_lem.utilities.ai.ai_helper import _call_llm
        response = _call_llm(
            model="lem-medium",
            messages=[{"role": "system", "content": PROFILE_AUDIT_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.2, max_tokens=500)
        mismatches = parse_mismatches(response.choices[0].message.content or "", profile_text, buyer_text)
    except Exception as exc:
        log_warning("Profile LLM audit failed (rubric-only audit kept)", exc=exc,
                    ai_model="lem-medium", user_id=user_id)
        return None
    if not mismatches:
        return None
    detail = "; ".join(
        f"'{m['buyer_phrase']}'" + (f" vs '{m['profile_phrase']}'" if m["profile_phrase"] else " missing")
        for m in mismatches)
    return {"check_id": LLM_FINDING_ID, "status": "warn", "weight": 0, "detail": detail[:255],
            "fix_hint": (mismatches[0]["note"] or "Use your buyers' own words in headline and About.")}
