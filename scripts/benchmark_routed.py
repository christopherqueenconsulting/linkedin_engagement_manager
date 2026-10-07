#!/usr/bin/env python3
"""Metered text-tier candidates for the model benchmark: OpenRouter or OpenAI direct (issue #2256).

`benchmark_models.py` measured only Ollama Cloud tags, so every OpenAI deployment in the text tiers
(`gpt-4o` leading `lem-complex`, the `gpt-4o-mini` fallbacks in `lem-simple` / `lem-medium` /
`lem-router`) read "never measured" in the registry. This module lets the same suites run against a
PROVIDER-QUALIFIED id such as `openai/gpt-5.4-mini`:

* **Routing.** A model id containing `/` is metered; a bare id is an Ollama tag, exactly as before.
  Ollama catalog names never contain `/`, so the two can never collide. Metered ids go to
  OpenRouter (`https://openrouter.ai/api/v1`, `OPENROUTER_API_KEY`, the default - one endpoint
  reaches OpenAI, Anthropic and Google) or, with `--text-provider openai`, to OpenAI directly with
  `OPENAI_API_KEY` (only `openai/*` ids). Both go through `AttributedOpenAI`, the one client.
  **This is benchmark plumbing only.** Production routing stays on the LiteLLM proxy with
  first-party keys; nothing here ever writes OpenRouter into `.litellm/config.yaml`.
* **Spend, bounded twice, BEFORE the first call.** Every planned completion is priced from the
  pinned cost map (`.litellm/model_prices_snapshot.json`) at its full output budget, plus the
  in-runner judge calls at the `lem-medium` alias rate. The plan is refused (exit 1) when it names
  an unpriced model, exceeds `--max-spend-usd` / `BENCHMARK_MAX_SPEND_USD` (default $2.00), or -
  on the OpenRouter route - exceeds the key's `limit_remaining` from `GET /api/v1/auth/key`. An
  `/auth/key` answer that cannot be read refuses too.
* **Spend, metered during the run.** Each attempt (a budget escalation or an empty-answer repeat is
  a separate billed call) reserves its own ceiling first and books its real usage after. A
  reservation that would cross the cap is refused and the case records "spend cap reached", so the
  cap holds even when a reasoning model needs the retries the plan did not assume.
* **Keys never leave the process.** Every error string that can reach stdout, stderr, the results
  JSON or the report passes through `redact`, which removes the configured key values.

PURE planning/pricing logic over a thin, mocked I/O layer, like the other benchmark modules.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.request
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# The media harness owns the run cap and its meter; text and media runs share both, so one
# `BENCHMARK_MAX_SPEND_USD` means the same thing on every tier.
from benchmark_media import SpendCapExceeded, SpendMeter, max_spend_usd  # noqa: E402,F401

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/auth/key"
OPENAI_BASE_URL = "https://api.openai.com/v1"
ROUTE_OPENROUTER = "openrouter"
ROUTE_OPENAI = "openai"
ROUTES = (ROUTE_OPENROUTER, ROUTE_OPENAI)
ROUTE_OLLAMA = "ollama-cloud"
KEY_ENV = {ROUTE_OPENROUTER: "OPENROUTER_API_KEY", ROUTE_OPENAI: "OPENAI_API_KEY"}
# Every credential a benchmark process can hold. `redact` strips all of them, not only the active
# route's, because a judge or PostHog error can quote a different one.
SECRET_ENVS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "OLLAMA_CLOUD_API_KEY",
               "POSTHOG_BENCHMARK_API_KEY", "POSTHOG_PERSONAL_API_KEY", "LITELLM_API_KEY",
               "LITELLM_MASTER_KEY")
JUDGE_ALIAS = "lem-medium"
# A case with no `max_tokens` is long-form at production's own (unset) budget; price it as if it
# could write this much. No shipped suite case is unbounded today.
UNBOUNDED_OUTPUT_TOKENS = 4000
# Characters per token for the INPUT estimate. English prose runs ~4; 3 over-counts on purpose so
# the planned ceiling stays a ceiling.
_CHARS_PER_TOKEN = 3
_PER_MESSAGE_OVERHEAD = 8
# The judge prompt wraps the case and the answer in a fixed rubric of about this many characters.
_JUDGE_WRAPPER_CHARS = 1200
# Families whose chat API rejects a custom temperature (reasoning models). Mirrors what the LiteLLM
# proxy does for them in production with `drop_params: true`.
_REASONING_RE = re.compile(r"^(o\d|gpt-(?:[5-9]|\d{2,}))")
_SECRET_MIN_LEN = 8


# ─────────────────────────────── pure ───────────────────────────────

def is_routed(model: str) -> bool:
    """True for a provider-qualified id (``openai/gpt-5.4-mini``), False for a bare Ollama tag."""
    return "/" in str(model or "")


def provider_of(model: str) -> str:
    """``openai/gpt-4o`` -> ``openai``; a bare tag is Ollama Cloud."""
    return str(model).split("/", 1)[0] if is_routed(model) else ROUTE_OLLAMA


def redact(text: object, secrets: Optional[list] = None) -> str:
    """``text`` with every known credential value replaced by ``[redacted]``.

    Args:
        text: Anything that is about to be printed or persisted.
        secrets: Explicit values to strip; defaults to the current values of ``SECRET_ENVS``.

    Returns:
        The redacted string. Short values are left alone - an 8-character floor keeps an empty or
        placeholder env var from blanking ordinary words.
    """
    out = str(text)
    values = secrets if secrets is not None else [os.environ.get(n) or "" for n in SECRET_ENVS]
    for value in sorted({v for v in values if v and len(v) >= _SECRET_MIN_LEN}, key=len,
                        reverse=True):
        out = out.replace(value, "[redacted]")
    return out


def route_problem(models: list, route: str) -> Optional[str]:
    """Why these metered ids cannot run on ``route``, else None."""
    if route not in ROUTES:
        return f"unknown --text-provider {route!r} (expected one of {', '.join(ROUTES)})"
    if route == ROUTE_OPENAI:
        foreign = sorted(m for m in models if is_routed(m) and provider_of(m) != "openai")
        if foreign:
            return ("--text-provider openai reaches only openai/* models; "
                    f"use the OpenRouter route for {', '.join(foreign)}")
    return None


def wire_model(model: str, route: str) -> str:
    """The id the provider expects: OpenRouter keeps the qualifier, OpenAI wants the bare id."""
    return model.split("/", 1)[1] if route == ROUTE_OPENAI and is_routed(model) else model


def wire_params(model: str, params: Optional[dict], route: str) -> dict:
    """Request params for one call, translated for the provider the way the proxy would.

    On the OpenAI route ``max_tokens`` becomes ``max_completion_tokens`` (the only budget a
    reasoning model accepts) and a non-default ``temperature`` / ``top_p`` is dropped for reasoning
    families. OpenRouter performs both translations itself, so its params pass through unchanged.

    Args:
        model: The qualified id.
        params: The case's params.
        route: ``openrouter`` or ``openai``.

    Returns:
        A new params dict.
    """
    out = dict(params or {})
    if route != ROUTE_OPENAI:
        return out
    if "max_tokens" in out:
        out["max_completion_tokens"] = out.pop("max_tokens")
    if _REASONING_RE.match(wire_model(model, route)):
        for name in ("temperature", "top_p"):
            if name in out and out[name] != 1:
                out.pop(name)
    return out


def price_spec(model: str, prices: dict) -> Optional[dict]:
    """The pinned per-token price for a qualified id, or None when it is unpriced."""
    spec = (prices or {}).get(model)
    if not isinstance(spec, dict):
        return None
    cin, cout = spec.get("input_cost_per_token"), spec.get("output_cost_per_token")
    if not isinstance(cin, (int, float)) or not isinstance(cout, (int, float)):
        return None
    return {"in": float(cin), "out": float(cout)}


def estimate_input_tokens(messages: list) -> int:
    """A deliberately generous token count for a chat prompt."""
    chars = sum(len(str((m or {}).get("content") or "")) for m in messages or [])
    return math.ceil(chars / _CHARS_PER_TOKEN) + _PER_MESSAGE_OVERHEAD * len(messages or [])


def output_budget(params: Optional[dict]) -> int:
    """The case's completion budget, or the unbounded-case ceiling."""
    for name in ("max_tokens", "max_completion_tokens"):
        value = (params or {}).get(name)
        if isinstance(value, int) and value > 0:
            return value
    return UNBOUNDED_OUTPUT_TOKENS


def call_ceiling(messages: list, params: Optional[dict], spec: dict) -> float:
    """Upper-bound USD for ONE attempt: the whole prompt plus the whole output budget."""
    return estimate_input_tokens(messages) * spec["in"] + output_budget(params) * spec["out"]


def judge_ceiling(case: dict, spec: dict, judge_tokens: int) -> float:
    """Upper-bound USD for one in-runner judge verdict on ``case``."""
    answer_tokens = output_budget(case.get("params"))
    prompt = estimate_input_tokens(case.get("messages") or []) + answer_tokens \
        + math.ceil(_JUDGE_WRAPPER_CHARS / _CHARS_PER_TOKEN)
    return prompt * spec["in"] + judge_tokens * spec["out"]


def plan_text_spend(suites: dict, targets: list, prices: dict, *, judge_enabled: bool,
                    judge_cap: int, judge_tokens: int) -> dict:
    """Price every planned call of a text-tier run before any is made.

    Args:
        suites: ``{tier: suite}`` from ``load_suites``.
        targets: ``[(tier, model, role)]`` the run will measure.
        prices: The pinned cost map's ``models``.
        judge_enabled: Whether the in-runner judge will be called.
        judge_cap: The run's judge-call cap.
        judge_tokens: The judge's per-verdict completion budget.

    Returns:
        ``{"items", "judge", "routed_usd", "total_usd", "unpriced"}``. Each item is
        ``{tier, model, role, route, calls, est_usd}``; Ollama tags are plan-metered (``$0``).
        ``total_usd`` is None when anything is unpriced - a bound nobody can compute.
    """
    items: list = []
    unpriced: list = []
    judge_costs: list = []
    for tier, model, role in targets:
        cases = (suites.get(tier) or {}).get("cases") or []
        if not is_routed(model):
            items.append({"tier": tier, "model": model, "role": role, "route": ROUTE_OLLAMA,
                          "calls": len(cases), "est_usd": 0.0})
        else:
            spec = price_spec(model, prices)
            if spec is None:
                unpriced.append(model)
            est = None if spec is None else sum(
                call_ceiling(c.get("messages") or [], c.get("params"), spec) for c in cases)
            items.append({"tier": tier, "model": model, "role": role, "route": "metered",
                          "calls": len(cases),
                          "est_usd": None if est is None else round(est, 6)})
        if judge_enabled:
            judge_costs.extend((tier, c) for c in cases if c.get("judge") is not False)
    judge = {"model": JUDGE_ALIAS, "calls": 0, "est_usd": 0.0}
    if judge_enabled and judge_costs:
        spec = price_spec(JUDGE_ALIAS, prices)
        if spec is None:
            unpriced.append(f"{JUDGE_ALIAS} (judge)")
            judge["est_usd"] = None
        else:
            ceilings = sorted((judge_ceiling(c, spec, judge_tokens) for _, c in judge_costs),
                              reverse=True)[:max(0, int(judge_cap))]
            judge = {"model": JUDGE_ALIAS, "calls": len(ceilings),
                     "est_usd": round(sum(ceilings), 6)}
    unpriced = sorted(set(unpriced))
    routed = round(sum(i["est_usd"] or 0.0 for i in items), 6)
    total = None if unpriced else round(routed + (judge["est_usd"] or 0.0), 6)
    return {"items": items, "judge": judge, "routed_usd": routed, "total_usd": total,
            "unpriced": unpriced}


def spend_refusal(plan: dict, cap: float, route: str, key_limit: Optional[dict], *,
                  check_limit: bool = True) -> Optional[str]:
    """Why the plan may not run, else None.

    Args:
        plan: From ``plan_text_spend``.
        cap: The hard per-run dollar cap.
        route: The metered route (``openrouter`` / ``openai``).
        key_limit: From ``fetch_key_limit``; required (and checked) on the OpenRouter route when
            the plan routes anything there. None means it was never read.
        check_limit: False only for a dry run's display, which never reads the key.

    Returns:
        The refusal message, or None when the run may proceed.
    """
    if plan["unpriced"]:
        return ("refusing to run: no pinned price for " + ", ".join(plan["unpriced"])
                + " in .litellm/model_prices_snapshot.json - an unpriced model has no spend bound")
    if plan["total_usd"] > cap:
        return (f"refusing to run: estimated spend ${plan['total_usd']:.2f} exceeds the "
                f"${cap:.2f} cap (--max-spend-usd / BENCHMARK_MAX_SPEND_USD)")
    if not check_limit or route != ROUTE_OPENROUTER or plan["routed_usd"] <= 0:
        return None
    if not key_limit or not key_limit.get("ok"):
        reason = (key_limit or {}).get("error") or "never read"
        return ("refusing to run: OpenRouter's key limit could not be read from /api/v1/auth/key "
                f"({reason}) - an unreadable limit is not a large one")
    remaining = key_limit.get("limit_remaining")
    if remaining is not None and plan["routed_usd"] > remaining:
        return (f"refusing to run: estimated OpenRouter spend ${plan['routed_usd']:.2f} exceeds the "
                f"key's remaining limit ${remaining:.2f}")
    return None


def effective_cap(cap: float, route: str, key_limit: Optional[dict]) -> float:
    """The meter's ceiling: the run cap, lowered to OpenRouter's remaining limit when that is less."""
    remaining = (key_limit or {}).get("limit_remaining") if route == ROUTE_OPENROUTER else None
    return min(cap, float(remaining)) if isinstance(remaining, (int, float)) else cap


def render_plan(plan: dict, cap: float, route: str, key_limit: Optional[dict]) -> str:
    """The planned-spend table printed by ``--dry-run`` and at the top of every real run."""
    lines = [f"Planned text-benchmark spend (cap ${cap:.2f}, metered route {route}):",
             "| Tier | Model | Role | Route | Calls | Est. USD (upper bound) |",
             "|---|---|---|---|---|---|"]
    for item in plan["items"]:
        est = "UNPRICED" if item["est_usd"] is None else f"${item['est_usd']:.4f}"
        lines.append(f"| {item['tier']} | `{item['model']}` | {item['role']} | {item['route']} | "
                     f"{item['calls']} | {est} |")
    judge = plan["judge"]
    if judge["calls"]:
        est = "UNPRICED" if judge["est_usd"] is None else f"${judge['est_usd']:.4f}"
        lines.append(f"| (judge) | `{judge['model']}` | in-runner judge | proxy | {judge['calls']} "
                     f"| {est} |")
    total = "n/a (unpriced model)" if plan["total_usd"] is None else f"${plan['total_usd']:.4f}"
    lines.append(f"\n**Estimated total:** {total} of a ${cap:.2f} cap "
                 f"(metered models ${plan['routed_usd']:.4f})")
    if route == ROUTE_OPENROUTER and plan["routed_usd"] > 0:
        if key_limit is None:
            lines.append("**OpenRouter limit:** not read (dry run - a real run reads "
                         "/api/v1/auth/key first and refuses when it cannot)")
        elif not key_limit.get("ok"):
            lines.append(f"**OpenRouter limit:** UNREADABLE ({key_limit.get('error')})")
        elif key_limit.get("limit_remaining") is None:
            lines.append("**OpenRouter limit:** none set on this key (the run cap still applies)")
        else:
            lines.append(f"**OpenRouter limit:** ${key_limit['limit_remaining']:.2f} remaining")
    lines.append("Ceilings assume every case answers on its first attempt at its full output "
                 "budget; a budget escalation or empty-answer repeat is metered against the same "
                 "cap during the run.")
    refusal = spend_refusal(plan, cap, route, key_limit, check_limit=key_limit is not None)
    lines.append(f"**Decision:** {refusal or 'within cap - a real run would proceed'}")
    return "\n".join(lines)


def metered_usage(model: str, prices: dict) -> dict:
    """The scorecard ``usage`` entry for a metered model: its per-token price, not an Ollama level."""
    spec = price_spec(model, prices) or {}
    return {"level": None, "label": "metered", "price_in": spec.get("in"),
            "price_out": spec.get("out")}


# ─────────────────────────────── I/O (mocked in tests) ───────────────────────────────

def fetch_key_limit(api_key: str, *, opener: Optional[Callable] = None,
                    url: str = OPENROUTER_KEY_URL) -> dict:
    """Read the OpenRouter key's spend limit. Free: this endpoint bills nothing.

    Args:
        api_key: The OpenRouter key; sent as a bearer token, never logged.
        opener: ``urllib.request.urlopen``-compatible callable, injected for tests.
        url: The key-info endpoint.

    Returns:
        ``{"ok": True, "limit", "limit_remaining", "usage"}`` or ``{"ok": False, "error"}``. A
        ``limit_remaining`` of None with ``ok`` means the key has no limit set.
    """
    try:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}",
                                                       "Accept": "application/json"})
        with (opener or urllib.request.urlopen)(request, timeout=20) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, dict):
            return {"ok": False, "error": "response carried no `data` object"}
        limit, remaining = data.get("limit"), data.get("limit_remaining")
        for name, value in (("limit", limit), ("limit_remaining", remaining)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return {"ok": False, "error": f"`{name}` is not a number"}
        if limit is not None and remaining is None:
            return {"ok": False, "error": "a limit is set but `limit_remaining` is missing"}
        return {"ok": True, "limit": limit, "limit_remaining": remaining,
                "usage": data.get("usage")}
    except Exception as exc:  # noqa: BLE001 - unreadable is a refusal reason, never a crash
        return {"ok": False, "error": redact(f"{type(exc).__name__}: {exc}", [api_key])[:200]}


def build_routed_client(base_cls: type, *, route: str, api_key: str, prices: dict, meter,
                        timeout: float = 180.0):
    """A ``ProviderClient`` subclass instance for the metered route.

    Built from the caller's ``ProviderClient`` so the retry/escalation loop is the SAME code that
    measures the Ollama models - only the transport, the param translation, the spend meter and the
    error redaction differ.

    Args:
        base_cls: ``benchmark_models.ProviderClient``.
        route: ``openrouter`` or ``openai``.
        api_key: The route's key.
        prices: The pinned cost map's ``models``.
        meter: A ``SpendMeter`` (``reserve`` / ``charge``).
        timeout: Per-request timeout in seconds.

    Returns:
        The client instance.
    """
    base_url = OPENROUTER_BASE_URL if route == ROUTE_OPENROUTER else (
        os.environ.get("OPENAI_BASE_URL") or OPENAI_BASE_URL)

    class RoutedProviderClient(base_cls):
        """Metered completions through ``AttributedOpenAI``, the one client."""

        def _openai(self):
            if self._client is None:
                from cqc_lem.utilities.ai.client import AttributedOpenAI  # noqa: WPS433 - lazy: env
                headers = ({"X-Title": "LEM model benchmark"} if route == ROUTE_OPENROUTER
                           else None)
                self._client = AttributedOpenAI(api_key=self.api_key, base_url=self.base_url,
                                                timeout=self.timeout, default_headers=headers)
            return self._client

        def _create(self, model: str, messages: list, call_params: dict):
            spec = price_spec(model, prices)
            if spec is None:
                raise RuntimeError(f"no pinned price for {model}")
            meter.reserve(call_ceiling(messages, call_params, spec))
            response = self._openai().chat.completions.create(
                model=wire_model(model, route), messages=messages,
                **wire_params(model, call_params, route))
            usage = getattr(response, "usage", None)
            pin = getattr(usage, "prompt_tokens", None)
            pout = getattr(usage, "completion_tokens", None)
            if isinstance(pin, int) and isinstance(pout, int):
                meter.charge(pin * spec["in"] + pout * spec["out"])
            else:
                meter.charge(call_ceiling(messages, call_params, spec))
            return response

        def complete(self, model: str, messages: list, params: Optional[dict] = None, *,
                     allow_budget_escalation: bool = True) -> dict:
            result = super().complete(model, messages, params,
                                      allow_budget_escalation=allow_budget_escalation)
            if result.get("error"):
                result["error"] = redact(result["error"], [self.api_key])
            return result

    return RoutedProviderClient(base_url, api_key, timeout)


class DispatchingProvider:
    """One ``complete`` for a mixed roster: bare tags to Ollama Cloud, qualified ids metered."""

    def __init__(self, ollama=None, routed=None) -> None:
        """Hold either client; a model whose route has no client records an error, never raises."""
        self.ollama = ollama
        self.routed = routed

    def complete(self, model: str, messages: list, params: Optional[dict] = None, *,
                 allow_budget_escalation: bool = True) -> dict:
        """Route ``model`` to its client and return that client's case result."""
        client = self.routed if is_routed(model) else self.ollama
        if client is None:
            return {"text": None, "error": f"no provider configured for {model}",
                    "latency_ms": None, "usage": {}}
        return client.complete(model, messages, params,
                               allow_budget_escalation=allow_budget_escalation)
