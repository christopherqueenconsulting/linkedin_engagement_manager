"""ONE research layer for every generated content type: a single web-grounded call that gathers
current, factual findings (recent stats with rough dates, real examples, trends, credible contrarian
data) for a subject + blueprint, so writers weave specifics instead of vague claims.

Routing: prefer the LiteLLM proxy alias `lem-research` (Perplexity Sonar, see .litellm/config.yaml)
via the shared client; fall back to the direct `search_with_perplexity` helper when the proxy route
is unavailable. Every failure path degrades to empty findings — generation NEVER breaks because
research did.

COST POLICY (per-type toggles, all under the CONTENT_RESEARCH_ENABLED master switch):
- newsletter — NEWSLETTER_RESEARCH_ENABLED, default ON. Weekly cadence; one call per edition is cheap
  and long-form depth needs real numbers.
- post — POST_RESEARCH_ENABLED, default ON. A handful per day; grounding posts in current facts is
  worth one search call each.
- comment — COMMENT_RESEARCH_ENABLED, default OFF. Comments run at HIGH volume (many per day per
  user) and are already grounded in their primary source: the TARGET POST. Firing a Perplexity
  search per comment would multiply API spend for marginal value, so comment research is opt-in.
  This is the one type ALSO carried by a runtime feature flag (issue #651): it is the expensive,
  reversible toggle worth trialling on a cohort without a deploy. The env var stays its default and
  its fallback — see utilities/flags.py."""

import os

from cqc_lem.utilities.flags import COMMENT_RESEARCH, flag_enabled
from cqc_lem.utilities.logger import log_debug, log_warning

_EMPTY: dict = {"findings": "", "sources": []}

# (env var, default) per content type. Unknown types inherit the conservative comment default.
_TYPE_TOGGLES: dict = {
    "newsletter": ("NEWSLETTER_RESEARCH_ENABLED", True),
    "post": ("POST_RESEARCH_ENABLED", True),
    "comment": ("COMMENT_RESEARCH_ENABLED", False),
}

_RESEARCH_SYSTEM = (
    "You are a research assistant for a professional LinkedIn author. Gather CURRENT, FACTUAL, "
    "citable information on the given subject. Return concise findings the author can weave into "
    "prose: specific statistics WITH their approximate date and source name, real named examples "
    "with outcomes, notable recent developments, and any credible data that challenges common "
    "assumptions. Never invent facts or numbers. If little credible material exists, say so briefly."
)

_SUBJECT_LINE = {
    "newsletter": "Current facts, statistics, and real examples for a newsletter edition about: {subject}.",
    "post": "Current facts, statistics, and real examples for a short LinkedIn post about: {subject}.",
    "comment": ("Current facts, statistics, and real examples relevant to a short LinkedIn comment "
                "replying to a post about: {subject}."),
}

# Format keys (across ALL menus) that shift the research emphasis. The newsletter strings are the
# original per-format focuses; the post/comment archetypes map onto the same three emphases.
_RECENT_FOCUS_FORMATS = {"roundup", "industry_observation"}
_EXAMPLE_FOCUS_FORMATS = {"case_study", "teardown", "case_snapshot", "storyteller"}
_CONTRARIAN_FOCUS_FORMATS = {"contrarian", "contrarian_take", "myth_vs_reality", "respectful_contrarian"}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def research_enabled(content_type: str, user_id: int = None) -> bool:
    """Master switch AND the per-type toggle must both allow research. The comment toggle is
    additionally flag-controlled (issue #651) and can therefore be trialled per-user; the flag falls
    back to COMMENT_RESEARCH_ENABLED whenever PostHog has no answer."""
    if not _bool_env("CONTENT_RESEARCH_ENABLED", True):
        return False
    env_name, default = _TYPE_TOGGLES.get(content_type, _TYPE_TOGGLES["comment"])
    # Unknown types already inherit the comment toggle, so they inherit its flag too.
    if env_name == _TYPE_TOGGLES["comment"][0]:
        return flag_enabled(COMMENT_RESEARCH, user_id=user_id)
    return _bool_env(env_name, default)


def _build_research_query(subject: str, content_type: str, blueprint: dict = None,
                          context_description: str = None, prefs: dict = None) -> str:
    from cqc_lem.utilities.ai.content_framework import normalize_key
    template = _SUBJECT_LINE.get(content_type, _SUBJECT_LINE["post"])
    parts = [template.format(subject=subject.strip())]
    angle = (blueprint or {}).get("angle")
    if angle:
        parts.append(f"The piece's angle: {str(angle).strip()}.")
    fmt = normalize_key(content_type, "format", (blueprint or {}).get("format"))
    if fmt in _RECENT_FOCUS_FORMATS:
        parts.append("Focus on notable developments from the last few weeks.")
    elif fmt in _EXAMPLE_FOCUS_FORMATS:
        parts.append("Prioritize real named examples with concrete outcomes and numbers.")
    elif fmt in _CONTRARIAN_FOCUS_FORMATS:
        parts.append("Include credible data that challenges the conventional wisdom on this subject.")
    desc = (context_description or "").strip()
    if desc:
        parts.append(f"The audience and promise: {desc[:300]}.")
    focus = (prefs or {}).get("focus_topics")
    if focus:
        focus_str = ", ".join(str(t) for t in focus) if isinstance(focus, (list, tuple)) else str(focus)
        if focus_str.strip():
            parts.append(f"Audience focus areas: {focus_str[:200]}.")
    parts.append("Return: 3-6 specific recent statistics (each with its approximate date and source "
                 "name), 2-3 real examples, and 1-2 notable trends or contrarian data points.")
    return " ".join(parts)


def _research_via_litellm(query: str, max_sources: int) -> dict:
    from cqc_lem.utilities.ai.client import client
    response = client.chat.completions.create(
        model="lem-research",
        messages=[{"role": "system", "content": _RESEARCH_SYSTEM},
                  {"role": "user", "content": query}],
        temperature=0.2, max_tokens=900)
    findings = (response.choices[0].message.content or "").strip()
    if not findings:
        raise RuntimeError("Empty research response from lem-research")
    # Perplexity via LiteLLM surfaces citations as an extra response field when available.
    citations = getattr(response, "citations", None) or []
    sources = [{"url": u} for u in list(citations)[:max_sources] if isinstance(u, str)]
    return {"findings": findings, "sources": sources}


def research_topic(subject: str, content_type: str = "newsletter", blueprint: dict = None,
                   context_description: str = None, prefs: dict = None,
                   max_sources: int = 5, user_id: int = None) -> dict:
    """One research call for one piece of content. Returns {'findings': str, 'sources': [{'url':
    ...}]}; empty findings on toggle-off, missing key, or any failure — callers always generate
    regardless. `user_id` only scopes the flag lookup (issue #651) — pass it where a per-user
    rollout should be able to reach this call."""
    subject = (subject or "").strip()
    if not subject or not research_enabled(content_type, user_id=user_id):
        return dict(_EMPTY)
    query = _build_research_query(subject, content_type, blueprint, context_description, prefs)
    try:
        result = _research_via_litellm(query, max_sources)
        log_debug(f"Content research via lem-research succeeded ({content_type})",
                  ai_model="lem-research")
        return result
    except Exception as exc:
        log_warning("Content research via LiteLLM failed; trying direct Perplexity", exc=exc,
                    api_provider="litellm")
    try:
        from cqc_lem.utilities.ai.tools import search_with_perplexity
        raw = search_with_perplexity(query, max_sources=max_sources)
        return {"findings": (raw.get("answer") or "").strip(), "sources": raw.get("sources") or []}
    except Exception as exc:
        log_warning("Content research unavailable; generating without research", exc=exc,
                    api_provider="perplexity")
        return dict(_EMPTY)
