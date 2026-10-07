#!/usr/bin/env python3
"""ONE live research call, to prove `lem-research` still answers with citations (issue #2255).

`research_topic` fails OPEN by design: when Perplexity stops answering, content still generates,
just without findings. That makes a broken research route invisible from the outside, which is how
`lem-research` sat on a sunset API (Sonar Chat Completions, ended 2026-09-27) without anything
failing. This probe is the outside check. It calls the production function for the chosen route
directly - no fail-open wrapper - and exits non-zero unless the answer carries findings text AND at
least one citation URL.

Routes:
  proxy   (default) `content_research._research_via_litellm`: the app's `AttributedOpenAI` client,
          `client.responses.create(model="lem-research")`, through the LiteLLM proxy. Needs the
          app's proxy env (a prod-image sidecar on the compose network has it).
  direct  `tools.search_with_perplexity`: Perplexity's Agent API straight, with PERPLEXITY_API_KEY.

Usage:
  python scripts/probe_research.py [--route proxy|direct] [--subject TEXT] [--max-sources N]

Costs one research call (about $0.003 on the `fast` preset). Prints no credentials.
Exit: 0 findings + >=1 citation, 1 anything else.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, Optional

# An operator probe, not a production lane: its verdict is the printed PASS/FAIL. Run from a
# prod-image sidecar it holds the real POSTHOG_API_KEY, so mute telemetry before cqc_lem is imported
# or a failed probe files a production error-tracking issue (#1661).
os.environ.setdefault("LEM_TELEMETRY_MUTED", "1")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

DEFAULT_SUBJECT = "how small businesses are using AI agents for back-office operations"
ROUTES = ("proxy", "direct")


def _call(route: str, query: str, max_sources: int) -> dict:
    """Run one research call on ``route`` and return ``{"findings", "sources"}``."""
    if route == "direct":
        from cqc_lem.utilities.ai.tools import search_with_perplexity
        raw = search_with_perplexity(query, max_sources=max_sources)
        return {"findings": (raw.get("answer") or "").strip(), "sources": raw.get("sources") or []}
    from cqc_lem.utilities.ai.content_research import _research_via_litellm
    return _research_via_litellm(query, max_sources)


def _redact(text: str) -> str:
    """Strip any credential this probe could have read out of an error message."""
    for name in ("PERPLEXITY_API_KEY", "LITELLM_API_KEY", "LITELLM_MASTER_KEY", "OPENAI_API_KEY"):
        secret = os.environ.get(name) or ""
        if len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return text


def probe(route: str, subject: str, max_sources: int,
          call: Optional[Callable[[str, str, int], dict]] = None,
          out: Callable[[str], None] = print) -> int:
    """Run the probe and report it.

    Args:
        route: ``proxy`` or ``direct``.
        subject: What to research.
        max_sources: Upper bound on the citation URLs kept.
        call: Injected for tests; defaults to the production route.
        out: Where report lines go.

    Returns:
        The process exit code: 0 on findings plus at least one citation, else 1.
    """
    from cqc_lem.utilities.ai.content_research import _build_research_query
    query = _build_research_query(subject, "post")
    started = time.time()
    try:
        result = (call or _call)(route, query, max_sources)
    except Exception as exc:  # noqa: BLE001 - the probe reports a failure, it never raises one
        out(f"FAIL route={route}: {type(exc).__name__}: {_redact(str(exc))[:400]}")
        return 1
    elapsed = time.time() - started
    findings = str(result.get("findings") or "")
    urls = [s.get("url") for s in result.get("sources") or [] if isinstance(s, dict)]
    out(f"route={route} latency={elapsed:.1f}s findings_chars={len(findings)} citations={len(urls)}")
    out("--- findings (first 600 chars) ---")
    out(findings[:600] or "(empty)")
    out("--- citations ---")
    for url in urls:
        out(f"  {url}")
    if not urls:
        out("  (none)")
    if findings and urls:
        out("PASS: non-empty research answer with at least one citation URL")
        return 0
    out("FAIL: " + ("no findings text" if not findings else "no citation URL in the response"))
    return 1


def main(argv: Optional[list] = None) -> int:
    """CLI entry point; see the module docstring."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--route", choices=ROUTES, default="proxy")
    ap.add_argument("--subject", default=DEFAULT_SUBJECT)
    ap.add_argument("--max-sources", type=int, default=5)
    args = ap.parse_args(argv)
    return probe(args.route, args.subject, args.max_sources)


if __name__ == "__main__":
    sys.exit(main())
