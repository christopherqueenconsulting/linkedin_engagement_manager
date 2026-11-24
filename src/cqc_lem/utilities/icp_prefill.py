"""A draft ICP definition for the owner to confirm, built from who they already chose (#2655).

Pure: the caller reads the roster, the leads and the scraped profile facts and hands them in. The
draft is a SUGGESTION shown in the editor — nothing here saves it, and an empty answer (``None``)
means there was no evidence to draw from, never "the ICP is nothing".
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Optional

# Leads that reached a real conversation or heat are the closest thing to "best converting".
BEST_LEAD_STAGES = ("hot", "in_conversation", "opportunity")
# A theme needs at least this many people behind it before it becomes a suggested filter.
MIN_THEME_COUNT = 1
MAX_ROLE_TITLES = 5
MAX_INDUSTRIES = 3
DRAFT_SEGMENT_ID = "draft-from-roster-and-leads"

_SLUG_RE = re.compile(r"/in/([^/?#]+)")


def _slug(value: object) -> str:
    match = _SLUG_RE.search(str(value or ""))
    return (match.group(1) if match else str(value or "")).strip().lower()


def _clean(value: object) -> str:
    text = " ".join(str(value or "").split())
    return "" if text.lower() in ("null", "none") else text


def _top(values: Iterable[str], limit: int) -> list[str]:
    """Most common values (case-insensitive), most frequent first, shown as first written."""
    counts: Counter = Counter()
    shown: dict[str, str] = {}
    for value in values:
        key = value.lower()
        counts[key] += 1
        shown.setdefault(key, value)
    return [shown[k] for k, n in counts.most_common(limit) if n >= MIN_THEME_COUNT]


def seed_profile_urls(roster: list[dict], leads: list[dict]) -> list[str]:
    """Profile URLs of ICP-category roster targets and best-stage leads, de-duplicated by slug."""
    urls: dict[str, str] = {}
    for target in roster or []:
        if target.get("category") == "icp" and target.get("profile_url"):
            urls.setdefault(_slug(target["profile_url"]), str(target["profile_url"]))
    for lead in leads or []:
        stage = lead.get("manual_stage") or lead.get("stage")
        if stage in BEST_LEAD_STAGES and lead.get("person_profile_url"):
            urls.setdefault(_slug(lead["person_profile_url"]), str(lead["person_profile_url"]))
    return list(urls.values())


def draft_icp_definition(seed_urls: list[str], facts: dict) -> Optional[dict]:
    """One draft segment from the scraped titles and industries of the seed people.

    Args:
        seed_urls: Output of `seed_profile_urls`.
        facts: `get_profile_facts` result; people never scraped are simply absent.

    Returns:
        ``{"definition": {...}, "sample_size": n}`` or None when no seed person has a title or
        industry to learn from.
    """
    wanted = {_slug(u) for u in seed_urls}
    titles: list[str] = []
    industries: list[str] = []
    used = 0
    for url, row in (facts or {}).items():
        if _slug(url) not in wanted:
            continue
        title, industry = _clean(row.get("job_title")), _clean(row.get("industry"))
        if not (title or industry):
            continue
        used += 1
        if title:
            titles.append(title)
        if industry:
            industries.append(industry)
    if not used:
        return None
    segment = {
        "id": DRAFT_SEGMENT_ID,
        "name": "Draft from your roster and best leads",
        "role_titles": _top(titles, MAX_ROLE_TITLES),
        "industries": _top(industries, MAX_INDUSTRIES),
    }
    return {"definition": {"segments": [segment], "exclusions": {}}, "sample_size": used}
