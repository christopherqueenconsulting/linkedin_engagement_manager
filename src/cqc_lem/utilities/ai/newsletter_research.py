"""Research layer for newsletter editions: ONE web-grounded call per edition that gathers current,
factual findings (recent stats with rough dates, real examples, trends, credible contrarian data)
for the edition's subject + blueprint, so the writer weaves specifics instead of vague claims.

Routing: prefer the LiteLLM proxy alias `lem-research` (Perplexity Sonar, see .litellm/config.yaml)
via the shared client; fall back to the direct `search_with_perplexity` helper when the proxy route
is unavailable. Every failure path degrades to empty findings — generation NEVER breaks because
research did. Toggle with NEWSLETTER_RESEARCH_ENABLED (default on)."""

import os

from cqc_lem.utilities.logger import log_debug, log_warning

_EMPTY: dict = {"findings": "", "sources": []}

_RESEARCH_SYSTEM = (
    "You are a research assistant for a professional newsletter author. Gather CURRENT, FACTUAL, "
    "citable information on the given subject. Return concise findings the author can weave into "
    "prose: specific statistics WITH their approximate date and source name, real named examples "
    "with outcomes, notable recent developments, and any credible data that challenges common "
    "assumptions. Never invent facts or numbers. If little credible material exists, say so briefly."
)


def _research_enabled() -> bool:
    return os.environ.get("NEWSLETTER_RESEARCH_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off")


def _build_research_query(subject: str, blueprint: dict = None,
                          newsletter_description: str = None, prefs: dict = None) -> str:
    from cqc_lem.utilities.ai.newsletter_blueprint import normalize_format
    parts = [f"Current facts, statistics, and real examples for a newsletter edition about: {subject.strip()}."]
    angle = (blueprint or {}).get("angle")
    if angle:
        parts.append(f"The edition's angle: {str(angle).strip()}.")
    fmt = normalize_format((blueprint or {}).get("format"))
    if fmt == "roundup":
        parts.append("Focus on notable developments from the last few weeks.")
    elif fmt in ("case_study", "teardown"):
        parts.append("Prioritize real named examples with concrete outcomes and numbers.")
    elif fmt == "contrarian":
        parts.append("Include credible data that challenges the conventional wisdom on this subject.")
    desc = (newsletter_description or "").strip()
    if desc:
        parts.append(f"The newsletter's audience and promise: {desc[:300]}.")
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


def research_newsletter_topic(subject: str, blueprint: dict = None,
                              newsletter_description: str = None, prefs: dict = None,
                              max_sources: int = 5) -> dict:
    """One research call for one edition. Returns {'findings': str, 'sources': [{'url': ...}]};
    empty findings on toggle-off, missing key, or any failure — callers always generate regardless."""
    subject = (subject or "").strip()
    if not subject or not _research_enabled():
        return dict(_EMPTY)
    query = _build_research_query(subject, blueprint, newsletter_description, prefs)
    try:
        result = _research_via_litellm(query, max_sources)
        log_debug("Newsletter research via lem-research succeeded", ai_model="lem-research")
        return result
    except Exception as exc:
        log_warning("Newsletter research via LiteLLM failed; trying direct Perplexity", exc=exc,
                    api_provider="litellm")
    try:
        from cqc_lem.utilities.ai.tools import search_with_perplexity
        raw = search_with_perplexity(query, max_sources=max_sources)
        return {"findings": (raw.get("answer") or "").strip(), "sources": raw.get("sources") or []}
    except Exception as exc:
        log_warning("Newsletter research unavailable; generating without research", exc=exc,
                    api_provider="perplexity")
        return dict(_EMPTY)
