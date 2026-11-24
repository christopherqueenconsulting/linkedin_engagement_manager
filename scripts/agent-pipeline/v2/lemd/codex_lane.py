"""The Codex review lane's pure parts: the exec-policy evaluator, failure classes, family guard.

`lib/run_lane.sh` launches `codex exec` and calls this module through `codex_lane_cli.py` for every
decision it should not make in shell: which failure was that, which model family may use which
harness, and what the exec-policy rules say about a command. Everything here is pure so each rule
is a table in a test (`tests/unit/test_agent_pipeline_codex_lane.py`).

The exec-policy file (`config/codex-review.rules`) is real Starlark for Codex and plain Python call
syntax for this module, so the same text is both the sandbox and the thing the tests interrogate.
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

#: Failure classes the fallback chain reacts to. Each one falls to the next step and pauses ONLY
#: the Codex provider.
RATE_LIMIT, MODEL_NOT_FOUND, TIMEOUT, PROXY_DOWN, BAD_KEY, OTHER = (
    "rate_limit", "model_not_found", "timeout", "proxy_down", "bad_key", "other")
FALLBACK_REASONS = frozenset({RATE_LIMIT, MODEL_NOT_FOUND, TIMEOUT, PROXY_DOWN, BAD_KEY})

REVIEW_MODES = frozenset({"review", "selfreview"})

#: The Claude CLI speaks only to Claude models and the Codex CLI only to GPT models.
_CLAUDE_MODEL = re.compile(r"^(claude|sonnet|opus|haiku)", re.I)
_GPT_MODEL = re.compile(r"^(gpt|codex|o\d)", re.I)

DEFAULT_CODEX_BIN = "/home/lem/.npm-global/bin/codex"

_PATTERNS = (
    (RATE_LIMIT, re.compile(r"\b429\b|rate.?limit|too many requests|usage limit|quota", re.I)),
    (MODEL_NOT_FOUND, re.compile(r"model_not_found|model .{0,60}not (found|exist)|unknown model", re.I)),
    (BAD_KEY, re.compile(r"\b401\b|\b403\b|invalid api key|incorrect api key|unauthori[sz]ed|bad key", re.I)),
    (PROXY_DOWN, re.compile(r"connection refused|connection reset|could not connect|"
                            r"error sending request|\b50[234]\b|bad gateway|dns error", re.I)),
)


def classify_failure(rc: int, text: str) -> str:
    """Name a failed Codex run, or "" when it succeeded. Text is matched before the exit code.

    124 is coreutils `timeout`. 126/127 never reached a model and are handled by the caller before
    this is asked, so they are not a class here.
    """
    if rc == 0:
        return ""
    for name, pattern in _PATTERNS:
        if pattern.search(text or ""):
            return name
    return TIMEOUT if rc == 124 else OTHER


def harness_for_model(model: str) -> str | None:
    """The only harness allowed to carry `model`, or None for a name in neither family."""
    if _CLAUDE_MODEL.match(model or ""):
        return "claude"
    if _GPT_MODEL.match(model or ""):
        return "codex"
    return None


def family_ok(harness: str, model: str) -> bool:
    """False when a GPT model would go through the Claude CLI or a Claude model through Codex.

    Ollama tier aliases are neither family and never reach either of those two harnesses.
    """
    owner = harness_for_model(model)
    if harness == "ollama":
        return owner is None
    return owner is None or owner == harness


def resolve_binary(env: dict[str, str] | None = None) -> str | None:
    """Absolute path to an executable Codex, or None. Never searches PATH.

    A unit's PATH is minimal (the 2026-08-10 rc=127 incident), so the path is explicit: `CODEX_BIN`,
    else the npm-global install. A relative value is refused.
    """
    env = os.environ if env is None else env
    path = (env.get("CODEX_BIN") or "").strip() or DEFAULT_CODEX_BIN
    if not os.path.isabs(path):
        return None
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def parse_usage(text: str) -> tuple[int, int]:
    """(tokens_in, tokens_out) summed over every `turn.completed` event of `codex exec --json`."""
    t_in = t_out = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        usage = event.get("usage") if isinstance(event, dict) and event.get("type") == "turn.completed" else None
        if isinstance(usage, dict):
            t_in += int(usage.get("input_tokens") or 0) + int(usage.get("cached_input_tokens") or 0)
            t_out += int(usage.get("output_tokens") or 0)
    return t_in, t_out


# --- exec-policy rules ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """One `prefix_rule`: each pattern slot is a set of alternatives for that word."""

    pattern: tuple[frozenset[str], ...]
    decision: str

    def matches(self, argv: list[str]) -> bool:
        """True when `argv` starts with the pattern, word for word."""
        return len(argv) >= len(self.pattern) and all(w in alts for w, alts in zip(argv, self.pattern))


def parse_rules(text: str) -> list[Rule]:
    """Parse the rules file. Anything but a `prefix_rule(...)` call is an error: no silent skips."""
    rules: list[Rule] = []
    for node in ast.parse(text).body:
        call = node.value if isinstance(node, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "prefix_rule"):
            raise ValueError(f"line {node.lineno}: only prefix_rule(...) calls are allowed")
        kw = {k.arg: ast.literal_eval(k.value) for k in call.keywords}
        decision = kw.get("decision")
        if decision not in ("allow", "prompt", "forbidden"):
            raise ValueError(f"line {node.lineno}: bad decision {decision!r}")
        slots = tuple(frozenset([w] if isinstance(w, str) else w) for w in kw["pattern"])
        rules.append(Rule(slots, decision))
    return rules


def evaluate(rules: list[Rule], argv: list[str]) -> str:
    """`forbidden`, `allow` or `prompt` for one command. Forbidden wins; no match means prompt.

    Headless `codex exec` has no one to answer a prompt, so `prompt` is a refusal in practice.
    """
    hits = {r.decision for r in rules if r.matches(argv)}
    if "forbidden" in hits:
        return "forbidden"
    return "allow" if "allow" in hits else "prompt"


def load_rules(path: str | Path) -> list[Rule]:
    """Parse the rules file at `path`."""
    return parse_rules(Path(path).read_text(encoding="utf-8"))
