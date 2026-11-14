"""The model router: which harness and model runs this dispatch, and why.

`lane.decide()` answers "which lane" from the weekly meter and the mode. This module answers the
finer question the owner asked for in #2560: which MODEL, from how hard the work is and from the
live provider quota (`usage_signal`, phase 1). It is pure — no I/O, no clock, no environment reads
except through the `env` mapping it is handed — so every rule is a decision table in a test.

Rule order, first match wins:

1. `agent:model:*` / `agent:tier:*` labels. The owner's dial, above every other input.
2. Two-family review. Codex reviews first on every PR; a Claude Sonnet review is required in
   addition for `risk:high`, CODEOWNERS-protected paths, auth, migrations, security, live-linkedin
   and `depmajor`, so those paths see two model families. A GPT model never goes through the Claude
   CLI and a Claude model never goes through Codex: each harness talks only to its own family.
3. Code defaults: Claude Sonnet as the base, Opus when the complexity score reaches the threshold
   or the run is a retry.
4. The fallback chain, walked when a provider is paused, over budget, returns `model_not_found`,
   or the proxy is down. Every chain ends at Ollama, which is never filtered out.

An unreadable quota signal is not a reason to pick anything different: with no numbers there is
nothing to gate on, so the answer is the one a 0%-used reading would give, deterministically.

Phase 3 adds the Codex execution lane. Until then `lane.decide()` logs a Codex decision and runs
the existing path.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping

from . import usage_signal

CLAUDE, CODEX, OLLAMA = "claude", "codex", "ollama"

#: `LEM_ROUTER` values. `off` is today's routing, byte for byte; `shadow` computes and logs only.
ROUTER_MODES = ("off", "shadow", "reviews", "full")

CODE_MODES = frozenset({"start", "revise", "rebase", "fix"})
REVIEW_MODES = frozenset({"review", "selfreview"})
MECHANICAL_MODES = frozenset({"phasefix", "docfix", "depfix"})

#: Labels that make a Sonnet review mandatory on top of the Codex one.
SECOND_FAMILY_LABELS = frozenset({
    "risk:high", "risk:auth", "risk:security", "risk:migration", "risk:live-linkedin",
})

TIER2, TIER3 = "lem-agent-tier2", "lem-agent-tier3"
_TIER_LABELS = {
    "agent:tier:1": "lem-agent-tier1", "agent:tier:2": TIER2,
    "agent:tier:2-alt": "lem-agent-tier2-alt", "agent:tier:3": TIER3,
}
_MODEL_LABELS = {"agent:model:haiku": "haiku", "agent:model:sonnet": "sonnet",
                 "agent:model:opus": "opus"}

#: Defaults for every `config.env` knob. `config.env` is sourced as plain shell variables, so the
#: caller must forward them in the environment (see `v2/lane_for.py`).
DEFAULTS: dict[str, str] = {
    "LEM_ROUTER_SONNET_MODEL": "claude-sonnet-5-5",
    "LEM_ROUTER_OPUS_MODEL": "claude-opus-5-5",
    "LEM_ROUTER_HAIKU_MODEL": "claude-haiku-5-5",
    "LEM_REVIEW_MODEL": "codex-auto-review",
    "LEM_REVIEW_MODEL_ALT": "gpt-5.6-luna",
    "LEM_ROUTER_CLAUDE_MAX_PCT": "50",
    "LEM_ROUTER_CODEX_MAX_PCT": "90",
    # Complexity weights.
    "LEM_ROUTER_W_BODY": "1.0",         # per 1000 characters of issue body
    "LEM_ROUTER_W_FILES": "0.5",        # per planned file
    "LEM_ROUTER_W_CI": "2.0",           # per CI failure
    "LEM_ROUTER_W_CONFLICT": "1.5",     # per rebase conflict
    "LEM_ROUTER_W_ATTEMPT": "8.0",      # per prior attempt: a retry alone reaches the threshold
    "LEM_ROUTER_W_PRIORITY_HIGH": "2.0",
    "LEM_ROUTER_W_PRIORITY_LOW": "-1.0",
    "LEM_ROUTER_W_RISK": "3.0",         # per risk:* label
    "LEM_ROUTER_OPUS_THRESHOLD": "8.0",
}

#: `gpt-6-luna` passed both seeded-defect PRs in a screen, so it is never a valid reviewer.
EXCLUDED_MODELS = frozenset({"gpt-6-luna"})

_BUDGET_STATUSES = frozenset({"exhausted", "limited", "rejected", "blocked", "paused"})

_DEGRADED = ("REVIEW QUALITY DEGRADED: this review ran on Ollama tier 3 because the stronger "
             "reviewers were unavailable. Treat its verdict as weaker evidence.")


def _cfg(env: Mapping[str, str], key: str) -> str:
    """A knob's value, falling back to its default when unset or empty."""
    value = (env.get(key) or "").strip()
    return value or DEFAULTS[key]


def _num(env: Mapping[str, str], key: str) -> float:
    """A numeric knob; a junk value falls back to the default rather than crashing a dispatch."""
    try:
        return float(_cfg(env, key))
    except ValueError:
        return float(DEFAULTS[key])


def router_mode(env: Mapping[str, str] | None = None) -> str:
    """`LEM_ROUTER`, defaulting to `off` when unset, empty or not one of the four values."""
    env = os.environ if env is None else env
    value = (env.get("LEM_ROUTER") or "").strip().lower()
    return value if value in ROUTER_MODES else "off"


@dataclass(frozen=True)
class Inputs:
    """Everything the score and the rules read about one dispatch."""

    mode: str
    labels: frozenset[str] = frozenset()
    body_chars: int = 0
    planned_files: int = 0
    ci_failures: int = 0
    conflicts: int = 0
    attempt: int = 1
    protected_path: bool = False   # the PR touches a CODEOWNERS-protected path


@dataclass(frozen=True)
class Context:
    """The router's view of the world for one dispatch."""

    flag: str
    inputs: Inputs
    signal: usage_signal.Signal = usage_signal.UNREADABLE
    paused: frozenset[str] = frozenset()        # harnesses paused right now
    bad_models: frozenset[str] = frozenset()    # models that answered `model_not_found`
    proxy_down: bool = False
    env: Mapping[str, str] = field(default_factory=dict)


def score(inputs: Inputs, env: Mapping[str, str]) -> float:
    """Complexity score: higher is harder. Pure and monotonic in every count."""
    total = _num(env, "LEM_ROUTER_W_BODY") * max(inputs.body_chars, 0) / 1000.0
    total += _num(env, "LEM_ROUTER_W_FILES") * max(inputs.planned_files, 0)
    total += _num(env, "LEM_ROUTER_W_CI") * max(inputs.ci_failures, 0)
    total += _num(env, "LEM_ROUTER_W_CONFLICT") * max(inputs.conflicts, 0)
    total += _num(env, "LEM_ROUTER_W_ATTEMPT") * max(inputs.attempt - 1, 0)
    if "priority:high" in inputs.labels or "priority:critical" in inputs.labels:
        total += _num(env, "LEM_ROUTER_W_PRIORITY_HIGH")
    if "priority:low" in inputs.labels:
        total += _num(env, "LEM_ROUTER_W_PRIORITY_LOW")
    total += _num(env, "LEM_ROUTER_W_RISK") * sum(1 for lab in inputs.labels if lab.startswith("risk:"))
    return round(total, 2)


def requires_second_family(inputs: Inputs) -> bool:
    """True when a Sonnet review is mandatory in addition to the Codex one."""
    return (inputs.mode == "depmajor" or inputs.protected_path
            or bool(inputs.labels & SECOND_FAMILY_LABELS))


def _models(env: Mapping[str, str]) -> dict[str, str]:
    """The concrete model ids the rules choose between."""
    review = _cfg(env, "LEM_REVIEW_MODEL")
    if review in EXCLUDED_MODELS:
        review = DEFAULTS["LEM_REVIEW_MODEL"]
    return {"sonnet": _cfg(env, "LEM_ROUTER_SONNET_MODEL"), "opus": _cfg(env, "LEM_ROUTER_OPUS_MODEL"),
            "haiku": _cfg(env, "LEM_ROUTER_HAIKU_MODEL"), "review": review}


def allowed_models(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Every model `decide` can return: the closed set the tests hold it to."""
    env = {} if env is None else env
    m = _models(env)
    alt = _cfg(env, "LEM_REVIEW_MODEL_ALT")
    return frozenset({m["sonnet"], m["opus"], m["haiku"], m["review"], alt, "sonnet", "opus", "haiku",
                      *_TIER_LABELS.values()}) - EXCLUDED_MODELS


def _chain(kind: str, m: Mapping[str, str]) -> list[tuple[str, str]]:
    """A fallback chain, strongest first. Every chain ends at Ollama."""
    if kind == "code":
        return [(CLAUDE, m["opus"]), (CLAUDE, m["sonnet"]), (OLLAMA, TIER2)]
    if kind == "review":
        return [(CODEX, m["review"]), (CLAUDE, m["sonnet"]), (OLLAMA, TIER3)]
    return [(CLAUDE, m["sonnet"]), (OLLAMA, TIER3)]   # "claude_review": the required second family


def _gate(ctx: Context, harness: str, model: str) -> str:
    """Why this step cannot run, or "" when it can. Ollama is the end of every chain: never gated."""
    if harness == OLLAMA:
        return ""
    if model in ctx.bad_models:
        return "model_not_found"
    if harness in ctx.paused:
        return f"{harness}_paused"
    if harness == CODEX and ctx.proxy_down:
        return "proxy_down"
    sig = ctx.signal
    if sig.unreadable:
        return ""   # nothing to gate on: the same answer a 0% reading gives
    if harness == CLAUDE:
        limit = _num(ctx.env, "LEM_ROUTER_CLAUDE_MAX_PCT")
        if sig.claude_weekly_pct is not None and sig.claude_weekly_pct >= limit:
            return f"claude_over_budget week={sig.claude_weekly_pct:.0f}%"
        if (sig.claude_status or "").lower() in _BUDGET_STATUSES:
            return f"claude_status={sig.claude_status}"
    if harness == CODEX:
        limit = _num(ctx.env, "LEM_ROUTER_CODEX_MAX_PCT")
        for name, pct in (("5h", sig.codex_5h_pct), ("week", sig.codex_weekly_pct)):
            if pct is not None and pct >= limit:
                return f"codex_over_budget {name}={pct:.0f}%"
    return ""


def _walk(ctx: Context, chain: list[tuple[str, str]], start: int, arm: str,
          points: float) -> tuple[str, str, str]:
    """Pick the first runnable step at or after `start`, naming every step skipped on the way."""
    skipped: list[str] = []
    for harness, model in chain[start:]:
        why = _gate(ctx, harness, model)
        if not why:
            tail = f" fell_back=[{'; '.join(skipped)}]" if skipped else ""
            return harness, model, f"route arm={arm} score={points:g} {harness}/{model}{tail}"
        skipped.append(f"{harness}/{model}:{why}")
    harness, model = chain[-1]   # unreachable while Ollama ends the chain; kept as the guarantee
    return harness, model, f"route arm={arm} score={points:g} {harness}/{model} fell_back=[{'; '.join(skipped)}]"


def decide(ctx: Context) -> tuple[str, str, str]:
    """Choose `(harness, model, reason)` for one dispatch. The reason names the arm."""
    inp, env = ctx.inputs, ctx.env
    m = _models(env)
    points = score(inp, env)

    for label, model in _MODEL_LABELS.items():
        if label in inp.labels:
            return CLAUDE, m[model], f"route arm=label_model score={points:g} {CLAUDE}/{m[model]}"
    for label, tier in _TIER_LABELS.items():
        if label in inp.labels:
            return OLLAMA, tier, f"route arm=label_tier score={points:g} {OLLAMA}/{tier}"

    if inp.mode == "depmajor":
        # Its verdict arms auto-merge, so it never trades down to a cheaper family.
        return CLAUDE, m["sonnet"], f"route arm=claude_only_mode score={points:g} {CLAUDE}/{m['sonnet']}"

    if inp.mode in REVIEW_MODES:
        if inp.mode == "selfreview" and requires_second_family(inp):
            return _walk(ctx, _chain("claude_review", m), 0, "review_second_family", points)
        return _walk(ctx, _chain("review", m), 0, "review_first_family", points)

    if inp.mode in CODE_MODES:
        escalate = inp.attempt > 1 or points >= _num(env, "LEM_ROUTER_OPUS_THRESHOLD")
        arm = "code_opus_escalation" if escalate else "code_default"
        return _walk(ctx, _chain("code", m), 0 if escalate else 1, arm, points)

    # Mechanical modes (phasefix, docfix, depfix) and anything added later keep the cheap lane.
    return OLLAMA, TIER2, f"route arm=mechanical_mode score={points:g} {OLLAMA}/{TIER2}"


def degraded_marker(harness: str, model: str, mode: str) -> str:
    """The loud note for a review that fell all the way to Ollama tier 3."""
    return _DEGRADED if mode in REVIEW_MODES and harness == OLLAMA and model == TIER3 else ""


def parse_labels(raw: str) -> frozenset[str]:
    """A whitespace- or comma-separated label list as a set."""
    return frozenset(t for t in re.split(r"[\s,]+", raw or "") if t)
