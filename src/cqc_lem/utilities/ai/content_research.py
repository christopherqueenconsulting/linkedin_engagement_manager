"""ONE research layer for every generated content type: a single web-grounded call that gathers
current, factual findings (recent stats with rough dates, real examples, trends, credible contrarian
data) for a subject + blueprint, so writers weave specifics instead of vague claims.

Routing: the direct `search_with_perplexity` helper (Perplexity's Agent API, the `fast` preset;
Sonar Chat Completions was sunset 2026-09-27, #2255) is the PRIMARY route. The proxy alias
`lem-research` (`client.responses.create`) is tried first only under `RESEARCH_VIA_PROXY`, default
OFF: LiteLLM rejects the Agent API's `"truncation": ""` and 500s (see `research_via_proxy_enabled`).
Both routes answer as a typed `output` list rather than a chat choice — `parse_agent_response` is
the ONE reader of that shape. The direct route books its own `llm_call` spend, since no proxy is
there to emit `$ai_generation`. Every failure path degrades to empty findings — generation NEVER
breaks because research did.

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
  its fallback — see utilities/flags.py.
"""

import math
import os
import time
from typing import Any, Optional

from cqc_lem.utilities.flags import COMMENT_RESEARCH, flag_enabled
from cqc_lem.utilities.logger import log_debug, log_warning
from cqc_lem.utilities.observability import llm_step

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
    back to COMMENT_RESEARCH_ENABLED whenever PostHog has no answer.
    """
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


def _field(obj: Any, name: str) -> Any:
    """`obj[name]` for a dict, `obj.name` for an SDK object, None when it carries neither."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _add_url(urls: list, candidate: Any) -> None:
    if isinstance(candidate, str) and candidate.startswith(("http://", "https://")) \
            and candidate not in urls:
        urls.append(candidate)


def parse_agent_response(response: Any, max_sources: int = 5) -> tuple[str, list]:
    """Read findings text and source URLs off a Perplexity Agent API response.

    Accepts the OpenAI SDK's parsed `Response` (the proxy path) or the raw JSON dict (the direct
    path), because both carry the same `output` list: `message` items whose `output_text` parts hold
    the answer and `url_citation` annotations, plus a `search_results` item listing what was read.
    Search results come first (they are what the answer was grounded on), then inline citations,
    then the legacy top-level `citations` / `search_results` a converted Sonar answer still carries.
    Anything unrecognised is skipped rather than raised on — the caller treats empty findings as
    "no research", never as a failure to surface.

    Args:
        response: The Agent API response, as an SDK object or a dict.
        max_sources: Upper bound on the URLs returned.

    Returns:
        `(findings, sources)` where sources is `[{"url": ...}]`, de-duplicated in reading order.
    """
    texts: list = []
    urls: list = []
    annotation_urls: list = []
    for item in _as_list(_field(response, "output")):
        kind = _field(item, "type")
        if kind == "search_results":
            for result in _as_list(_field(item, "results")):
                _add_url(urls, _field(result, "url"))
        elif kind == "message":
            for part in _as_list(_field(item, "content")):
                if _field(part, "type") not in ("output_text", "text"):
                    continue
                text = _field(part, "text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
                for note in _as_list(_field(part, "annotations")):
                    _add_url(annotation_urls, _field(note, "url"))
    findings = "\n\n".join(texts)
    if not findings:
        shortcut = _field(response, "output_text")
        findings = shortcut.strip() if isinstance(shortcut, str) else ""
    for url in annotation_urls:
        _add_url(urls, url)
    for url in _as_list(_field(response, "citations")):
        _add_url(urls, url)
    for result in _as_list(_field(response, "search_results")):
        _add_url(urls, _field(result, "url"))
    return findings, [{"url": u} for u in urls[:max(0, int(max_sources))]]


def _research_via_litellm(query: str, max_sources: int) -> dict:
    """One research call through the proxy's `/v1/responses` route.

    Args:
        query: The research query `_build_research_query` produced.
        max_sources: Upper bound on the source URLs kept.

    Returns:
        `{"findings", "sources"}`.

    Raises:
        RuntimeError: The proxy answered but the response carried no findings text.
    """
    from cqc_lem.utilities.ai.client import client
    response = client.responses.create(
        model="lem-research", instructions=_RESEARCH_SYSTEM, input=query,
        max_output_tokens=900)
    findings, sources = parse_agent_response(response, max_sources)
    if not findings:
        raise RuntimeError("Empty research response from lem-research")
    return {"findings": findings, "sources": sources}


def research_via_proxy_enabled() -> bool:
    """Whether research tries the proxy's `lem-research` alias before direct Perplexity.

    OFF by default: LiteLLM's `ResponsesAPIResponse` declares `truncation` as
    `Literal["auto", "disabled"]`, Perplexity's Agent API answers `"truncation": ""`, and the proxy
    500s on its own parse of a response Perplexity already served (and billed). Flip
    `RESEARCH_VIA_PROXY` on once a LiteLLM release accepts that answer and
    `scripts/probe_research.py --route proxy` passes repeatedly.
    """
    return _bool_env("RESEARCH_VIA_PROXY", False)


def is_known_proxy_parse_defect(exc: BaseException) -> bool:
    """True for the one proxy failure we already know about.

    That failure is LiteLLM's pydantic rejection of the Agent API's empty `truncation` field.
    Anything else through the proxy is still news.
    """
    text = str(exc)
    return "ResponsesAPIResponse" in text and "truncation" in text


#: How long one sighting of the known parse defect keeps this process off the proxy route.
PROXY_DEFECT_BACKOFF_SECONDS = 6 * 3600

# Monotonic deadline before which `research_topic` skips the proxy. Per process, never persisted:
# a restart (every deploy) re-tries the proxy once, which is how a fixed LiteLLM gets noticed.
_proxy_skip_until = 0.0


def proxy_route_open(now: Optional[float] = None) -> bool:
    """Whether `RESEARCH_VIA_PROXY` should still send this call to the proxy first.

    Every proxy call that hits the truncation defect has already been served, and billed, by
    Perplexity, and LiteLLM records it as a failed `$ai_generation`. With the flag left on, each
    research call paid for that failure before the direct route answered.
    """
    current = time.monotonic() if now is None else now
    return research_via_proxy_enabled() and current >= _proxy_skip_until


def _park_proxy_route(now: Optional[float] = None) -> None:
    """Close the proxy route for `PROXY_DEFECT_BACKOFF_SECONDS` after a known parse defect."""
    global _proxy_skip_until
    current = time.monotonic() if now is None else now
    _proxy_skip_until = current + PROXY_DEFECT_BACKOFF_SECONDS


def _usage_number(usage: Any, *path: str) -> Optional[float]:
    value: Any = usage
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _record_direct_spend(usage: Any, latency_ms: int, success: bool) -> None:
    """Book one direct research call as an `llm_call` on the `lem-research` tier.

    The direct route never touches the proxy, so no `$ai_generation` is emitted for it — this event
    and the cost-ledger accrual it feeds are its only spend record. Perplexity prices its own answer
    (`usage.cost.total_cost`, search fee included); that is booked as-is, and the per-1K estimate is
    only the fallback when it is missing. Telemetry never breaks research.
    """
    try:
        from cqc_lem.utilities.observability import (
            FEATURE_SYSTEM,
            current_llm_attribution,
            track_llm_call,
        )
        user_id, feature = current_llm_attribution()
        track_llm_call(
            model="lem-research",
            prompt_tokens=int(_usage_number(usage, "input_tokens") or 0),
            completion_tokens=int(_usage_number(usage, "output_tokens") or 0),
            latency_ms=latency_ms, success=success, user_id=user_id,
            feature=feature or FEATURE_SYSTEM,
            response_cost=_usage_number(usage, "cost", "total_cost"),
        )
    except Exception as exc:
        log_debug(f"Could not record direct research spend: {exc}", api_provider="perplexity")


def _research_direct(query: str, max_sources: int) -> dict:
    """One research call straight to Perplexity's Agent API, with its spend recorded.

    Args:
        query: The research query `_build_research_query` produced.
        max_sources: Upper bound on the source URLs kept.

    Returns:
        `{"findings", "sources"}`.

    Raises:
        Exception: Whatever `search_with_perplexity` raised; the failure is recorded first.
    """
    from cqc_lem.utilities.ai.tools import search_with_perplexity
    start = time.monotonic()
    try:
        raw = search_with_perplexity(query, max_sources=max_sources)
    except Exception:
        _record_direct_spend(None, int((time.monotonic() - start) * 1000), success=False)
        raise
    _record_direct_spend(raw.get("usage"), int((time.monotonic() - start) * 1000), success=True)
    return {"findings": (raw.get("answer") or "").strip(), "sources": raw.get("sources") or []}


@llm_step("research")
def research_topic(subject: str, content_type: str = "newsletter", blueprint: dict = None,
                   context_description: str = None, prefs: dict = None,
                   max_sources: int = 5, user_id: int = None) -> dict:
    """One research call for one piece of content. Returns {'findings': str, 'sources': [{'url':
    ...}]}; empty findings on toggle-off, missing key, or any failure — callers always generate
    regardless. `user_id` only scopes the flag lookup (issue #651) — pass it where a per-user
    rollout should be able to reach this call.
    """
    subject = (subject or "").strip()
    if not subject or not research_enabled(content_type, user_id=user_id):
        return dict(_EMPTY)
    query = _build_research_query(subject, content_type, blueprint, context_description, prefs)
    if proxy_route_open():
        try:
            result = _research_via_litellm(query, max_sources)
            log_debug(f"Content research via lem-research succeeded ({content_type})",
                      ai_model="lem-research")
            return result
        except Exception as exc:
            if is_known_proxy_parse_defect(exc):
                # The flag is on while LiteLLM still rejects Perplexity's `truncation: ""`: a
                # configuration fault, said once per backoff window rather than once per call.
                _park_proxy_route()
                log_warning("RESEARCH_VIA_PROXY is on but lem-research still hits the LiteLLM "
                            "truncation parse defect; skipping the proxy for "
                            f"{PROXY_DEFECT_BACKOFF_SECONDS // 3600}h and using direct Perplexity. "
                            "Unset RESEARCH_VIA_PROXY until scripts/probe_research.py --route proxy "
                            "passes.", api_provider="litellm")
            else:
                log_warning("Content research via LiteLLM failed; trying direct Perplexity",
                            exc=exc, api_provider="litellm")
    try:
        return _research_direct(query, max_sources)
    except Exception as exc:
        log_warning("Content research unavailable; generating without research", exc=exc,
                    api_provider="perplexity")
        return dict(_EMPTY)
