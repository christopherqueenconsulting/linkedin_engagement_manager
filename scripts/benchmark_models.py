#!/usr/bin/env python3
"""Model-tier evaluation harness (issue #721).

#716 taught the weekly model-health cron to SEE change coming — advance retirements, new Ollama
Cloud tags, a family we trail. What it could never do is say whether a candidate is any GOOD: a
swap PR was decided on a spec sheet. This is the measurement half.

Each LiteLLM tier is a CONTRACT (`tests/benchmarks/model_tiers/<tier>.json`): a fixed set of
synthetic prompts drawn from LEM's real prompt shapes, plus the properties a usable answer must
have. Every cron-detected candidate AND the tier's current champion runs the same suite, so a
recommendation is always a comparison, never an absolute claim about one model.

Two scoring layers, in this order:
  1. DETERMINISTIC (free, in-repo, the source of truth) — length caps, comma-list shape, JSON
     validity, `slop_lint.lint_report`, `content_framework.comment_contract_report`, burstiness,
     lexical self-similarity, the `LEMComplexityRouter` tier contract. A case that fails HARD here
     never spends a judge call. Every deterministic check is additionally classified by what
     PRODUCTION does with that failure (#910): a CONTRACT failure is what the call site consumes,
     a REPAIRABLE one is what a regeneration gate catches and retries. The suite scores a FIRST
     draft; production ships an n-th, so the absolute floor is on the contract rate and the
     first-draft rate is advisory (still compared against the champion).
  2. LLM JUDGE (paid, sampled, capped) — PostHog Evaluations scoring our own tagged
     `$ai_generation` events, or, when no judge provider is configured, an in-runner `lem-medium`
     judge whose verdicts are emitted as `$ai_metric`. A judge that never answers is
     `judge:timeout` — NEVER a fabricated score.

The gate is champion/challenger: a candidate is emitted as a swap recommendation only when it meets
the tier's absolute thresholds AND meets-or-beats the champion on every graded expectation. It is
deliberately a RECOMMENDATION and not a write to `.litellm/model_upgrades.yaml` — that map is the
RETIREMENT map and the reactive half of the model-health check auto-swaps whatever lands in it, so a
benchmark win must not walk itself into the live config. The report renders the exact mapping lines
for a human (or a #717-style PR) to adopt.

Split like the other ops scripts: PURE logic (suite loading, assertion evaluation, scorecard merge,
gate decision, report/leaderboard rendering) over a thin I/O layer (provider completions, PostHog
emission/eval API/HogQL polling, file writes) that the tests mock.

CLI:
  --run                    Execute the benchmark and write results JSON (--results-out). Default.
  --dry-run                Score the suites against each case's canned output. NO network at all.
  --render RESULTS.json    Pure render: write the report + update the leaderboard. No network.
  --print-suites           Validate and summarise the suites. No network.
Options:
  --models a,b             Candidate models (bare Ollama tags). Empty = champions only (baseline).
  --champions t=model,…    Override the champion per tier (default: read from --config).
  --tiers a,b              Tiers to run (default: every suite found).
  --suite-dir DIR          Suite fixtures (default tests/benchmarks/model_tiers).
  --config PATH            LiteLLM config the champions are read from (default .litellm/config.yaml).
  --out-dir DIR            Report + leaderboard directory (default docs/model-benchmarks).
  --results-out PATH       Where --run/--dry-run writes the machine-readable results.
  --recommendations-out P  Write ONLY the swap recommendations (for the cron) to this path.
  --usage-levels m=lvl,…   Ollama Cloud usage level for models whose page publishes none
                           (low|medium|high|"extra high"). Overrides the fetched value; defaults to
                           $BENCHMARK_USAGE_LEVELS so an unattended run needs no flags.
  --no-usage-levels        Skip the per-model usage-level fetch (one page request per model).
  --no-judge               Deterministic checks only.
  --max-judge-calls N      Hard cap on judge calls for the whole run (default BENCHMARK_MAX_JUDGE_CALLS).
  --run-id ID              Fix the run id (tests / reproducible dry runs).
  --today YYYY-MM-DD       Override today's date.
Exit: 0 ran / nothing recommended, 2 at least one swap recommendation, 1 error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _HERE)  # model_health_check is a sibling script, not a package module

DEFAULT_SUITE_DIR = "tests/benchmarks/model_tiers"
DEFAULT_CONFIG = ".litellm/config.yaml"
DEFAULT_OUT_DIR = "docs/model-benchmarks"
DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.
DEFAULT_APP_HOST = "https://us.posthog.com"

TIERS = ("lem-simple", "lem-medium", "lem-complex", "lem-router")

# Every benchmark event carries these, and the report renderer REFUSES anything that doesn't. The
# reports are committed to a public repo, so "this row came from prod traffic" has to be impossible
# rather than unlikely.
BENCHMARK_SOURCE = "benchmark"
BENCHMARK_DISTINCT_ID = "model-benchmark"

SCORING_POSTHOG = "posthog-evals"
SCORING_FALLBACK = "in-runner-judge"
SCORING_MIXED = "posthog-evals+in-runner-judge"
SCORING_DETERMINISTIC = "deterministic-only"
SCORING_DRY_RUN = "dry-run-canned"

JUDGE_TIMEOUT = "judge:timeout"
# Kept apart from JUDGE_TIMEOUT so the runner can tell "this answer confused the judge" (retry the
# next case) from "the judge is not reachable at all" (stop asking). Both are still unscored.
JUDGE_UNAVAILABLE = "judge:unavailable"

LEADERBOARD_BEGIN = "<!-- LEADERBOARD:BEGIN -->"
LEADERBOARD_END = "<!-- LEADERBOARD:END -->"
LEADERBOARD_MAX_ROWS = 400
LEADERBOARD_COLUMNS = ("Date", "Run", "Tier", "Model", "Role", "Contract", "First draft", "Judge",
                       "p50", "Verdict")
# The pre-#910 table, kept ONLY so a re-render can still read the rows already committed. Dropping
# them would silently evict real history at the first render after the column was added; they carry
# no contract rate, so they render `n/a` there rather than a number nobody measured.
LEADERBOARD_LEGACY_COLUMNS = ("Date", "Run", "Tier", "Model", "Role", "Deterministic", "Judge",
                              "p50", "Verdict")
LEADERBOARD_HEADER = "| " + " | ".join(LEADERBOARD_COLUMNS) + " |"
LEADERBOARD_DIVIDER = "|" + "---|" * len(LEADERBOARD_COLUMNS)

SCORECARD_COLUMNS = ("Tier", "Model", "Role", "Usage", "Cases", "Contract", "First draft", "Judge",
                     "Judged", "Timeouts", "p50", "p90")
SCORECARD_HEADER = "| " + " | ".join(SCORECARD_COLUMNS) + " |"
SCORECARD_DIVIDER = "|" + "---|" * len(SCORECARD_COLUMNS)

# Openers that mean the model narrated instead of answering. `lem-simple` outputs are pasted
# straight into LinkedIn copy, so a preamble is a correctness failure, not a style nit.
_PREAMBLE_RE = re.compile(
    r"^\s*(?:sure|certainly|of course|absolutely|here(?:'s| is| are)|below (?:is|are)|"
    r"i(?:'| a)m happy to|as an ai|great question|thanks for)\b",
    re.IGNORECASE)

_WORD_RE = re.compile(r"[a-z0-9']+")


# ─────────────────────────── env / small helpers ────────────────────────────────

def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, low: int = 0, high: int = 100000) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return max(low, min(high, int(raw))) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float, low: float = 0.0, high: float = 1.0) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return max(low, min(high, float(raw))) if raw else default
    except ValueError:
        return default


def benchmark_enabled() -> bool:
    return _env_flag("BENCHMARK_ENABLED", False)


def usage_levels_env_default() -> str:
    """`BENCHMARK_USAGE_LEVELS` — the incumbents' levels for a run nobody is watching.

    ollama.com publishes the Usage stat only on cloud-only model pages, so the locally-pullable
    incumbents' levels are a human input. On an unattended run there is no human to pass
    `--usage-levels`, and an unsupplied level renders `unknown`, which the report treats as a quota
    RISK — so the same string lives beside the API key in the runner's env instead."""
    return (os.environ.get("BENCHMARK_USAGE_LEVELS") or "").strip()


def max_judge_calls() -> int:
    """Hard ceiling on judge calls for one run. 200 covers four tiers × (one champion + up to three
    candidates) × ten cases; anything beyond that is a runaway, and the runner truncates and SAYS
    it truncated rather than quietly grading a subset."""
    return _env_int("BENCHMARK_MAX_JUDGE_CALLS", 200, low=0, high=5000)


def judge_timeout_seconds() -> int:
    return _env_int("BENCHMARK_JUDGE_TIMEOUT_SECONDS", 90, low=5, high=900)


# Reasoning models bill their chain-of-thought against `max_tokens`, and the visible answer is
# whatever is left. Every case fixture and the in-runner judge were written against non-reasoning
# models, so on `minimax-m3` / `glm-5.2` / `gpt-oss:120b` the budget is spent thinking and the reply
# arrives EMPTY with `finish_reason='length'` — which scores as "the model produced nothing" rather
# than "the harness cut it off". That is the difference between measuring a model and rejecting it
# for a harness artifact, so a truncated-before-the-answer reply is retried at a doubled budget.
# Bounded, and applied identically to champion and candidate so neither side gets a bigger budget
# than the other.

def truncation_retries() -> int:
    """How many times a completion truncated before its answer may be retried at double budget."""
    return _env_int("BENCHMARK_TRUNCATION_RETRIES", 2, low=0, high=4)


def empty_repeats() -> int:
    """How many times an EMPTY completion is re-measured at the SAME budget (#910).

    Distinct from the truncation retry above, which grows the budget. `minimax-m3` returned an empty
    completion on two `bm-20260802-20ae40` cases after exhausting the doublings; re-run at the same
    effective budget it answered and passed both. A reasoning model's single run is therefore not a
    stable measurement, and an empty-then-fine case must never read as a quality verdict — so it is
    re-measured, and the report names every case that needed it."""
    return _env_int("BENCHMARK_EMPTY_REPEATS", 1, low=0, high=3)


def judge_max_tokens() -> int:
    """Completion budget for one in-runner judge verdict. The verdict itself is ~40 tokens; the rest
    is headroom for a reasoning judge, since `lem-medium` is currently served by `gpt-oss:120b`."""
    return _env_int("BENCHMARK_JUDGE_MAX_TOKENS", 900, low=200, high=8000)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _read_text(path: str) -> str:
    with open(path) as f:
        return f.read()


# ───────────────────────────── suite loading (pure) ──────────────────────────────

class SuiteError(ValueError):
    """A suite fixture that cannot be trusted to grade a model."""


def load_suite(doc: dict, *, name: str = "<suite>") -> dict:
    """Validate one tier suite document. Raises `SuiteError` rather than grading a model against a
    malformed contract — a suite with a typo'd assertion type would silently score every candidate
    as passing, which is worse than no benchmark at all."""
    if not isinstance(doc, dict):
        raise SuiteError(f"{name}: suite must be a JSON object")
    tier = str(doc.get("tier") or "").strip()
    if tier not in TIERS:
        raise SuiteError(f"{name}: unknown tier {tier!r}")
    cases = doc.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SuiteError(f"{name}: suite has no cases")
    seen: set = set()
    for case in cases:
        if not isinstance(case, dict):
            raise SuiteError(f"{name}: case must be an object")
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            raise SuiteError(f"{name}: case is missing an id")
        if case_id in seen:
            raise SuiteError(f"{name}: duplicate case id {case_id!r}")
        seen.add(case_id)
        messages = case.get("messages")
        if not isinstance(messages, list) or not messages:
            raise SuiteError(f"{name}/{case_id}: case has no messages")
        for message in messages:
            if not isinstance(message, dict) or not str(message.get("content") or "").strip():
                raise SuiteError(f"{name}/{case_id}: malformed message")
        assertions = case.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise SuiteError(f"{name}/{case_id}: case has no assertions")
        if "budget_mirrors_production" in case and not isinstance(
                case.get("budget_mirrors_production"), bool):
            raise SuiteError(f"{name}/{case_id}: budget_mirrors_production must be true or false")
        for assertion in assertions:
            kind = (assertion or {}).get("type") if isinstance(assertion, dict) else None
            if kind not in ASSERTIONS:
                raise SuiteError(f"{name}/{case_id}: unknown assertion type {kind!r}")
            klass = (assertion or {}).get("production")
            if klass is not None and klass not in PRODUCTION_CLASSES:
                raise SuiteError(f"{name}/{case_id}: assertion {kind!r} has an unknown production "
                                 f"class {klass!r}")
    return {
        "tier": tier,
        "version": int(doc.get("version") or 1),
        "contract": str(doc.get("contract") or ""),
        "judge_rubric": str(doc.get("judge_rubric") or ""),
        "thresholds": normalize_thresholds(doc.get("thresholds")),
        "cases": cases,
    }


def normalize_thresholds(raw: Optional[dict]) -> dict:
    """A tier's floors.

    `contract_pass_rate` is the ABSOLUTE one (#910): the share of cases whose failures production
    would actually consume. `deterministic_pass_rate` — the first-draft rate over EVERY check,
    repairable ones included — is kept as an ADVISORY reading and as the champion-relative
    comparison; it is no longer a floor, because the checks it aggregates are the ones production
    regenerates against, so a 90% absolute floor there was a floor on something no model (the
    incumbents included) has ever had to clear. The judge floor stays looser, where a single rubric
    disagreement should not sink a model."""
    doc = raw if isinstance(raw, dict) else {}

    def num(key: str, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(doc.get(key, default))))
        except (TypeError, ValueError):
            return default

    try:
        min_judged = max(0, int(doc.get("min_judged", 3)))
    except (TypeError, ValueError):
        min_judged = 3
    return {"contract_pass_rate": num("contract_pass_rate", 0.9),
            "deterministic_pass_rate": num("deterministic_pass_rate", 0.9),
            "judge_pass_rate": num("judge_pass_rate", 0.8),
            "min_judged": min_judged}


def load_suites(suite_dir: str, tiers: Optional[list] = None) -> dict:
    """Read `<suite_dir>/<tier>.json` for each requested tier -> {tier: suite}."""
    wanted = [t for t in (tiers or TIERS)]
    suites: dict = {}
    for tier in wanted:
        path = os.path.join(suite_dir, f"{tier}.json")
        if not os.path.exists(path):
            if tiers:  # explicitly asked for -> a missing suite is an error, not a silent skip
                raise SuiteError(f"no suite fixture at {path}")
            continue
        suites[tier] = load_suite(json.loads(_read_text(path)), name=os.path.basename(path))
    if not suites:
        raise SuiteError(f"no suite fixtures found in {suite_dir}")
    return suites


# ──────────────────────── deterministic assertions (pure) ────────────────────────

def _tokens(text: str) -> list:
    return _WORD_RE.findall(str(text or "").lower())


def lexical_similarity(a: str, b: str) -> float:
    """Jaccard token overlap — the same deterministic measure the comment/post similarity gates fall
    back to when embeddings are unavailable. Used here on purpose: a benchmark must be reproducible
    offline, and an embedding call would make the score depend on a second model."""
    left, right = set(_tokens(a)), set(_tokens(b))
    if not left or not right:
        return 0.0
    return len(left & right) / float(len(left | right))


def _fail(detail: str) -> dict:
    return {"passes": False, "detail": detail}


def _pass(detail: str = "") -> dict:
    return {"passes": True, "detail": detail}


def _a_max_chars(spec: dict, output: str, case: dict) -> dict:
    limit = int(spec.get("value") or 0)
    n = len(output)
    return _pass(f"{n} chars") if n <= limit else _fail(f"{n} chars > {limit}")


def _a_min_chars(spec: dict, output: str, case: dict) -> dict:
    limit = int(spec.get("value") or 0)
    n = len(output)
    return _pass(f"{n} chars") if n >= limit else _fail(f"{n} chars < {limit}")


def _a_no_preamble(spec: dict, output: str, case: dict) -> dict:
    match = _PREAMBLE_RE.match(output or "")
    return _fail(f"opens with a preamble ({match.group(0).strip()!r})") if match else _pass()


def _a_comma_list(spec: dict, output: str, case: dict) -> dict:
    items = [p.strip() for p in str(output or "").split(",")]
    items = [p for p in items if p]
    low = int(spec.get("min_items") or 1)
    high = int(spec.get("max_items") or low)
    if "\n" in (output or "").strip():
        return _fail("multi-line output where a single comma-separated line was asked for")
    if not (low <= len(items) <= high):
        return _fail(f"{len(items)} comma-separated items, expected {low}-{high}")
    return _pass(f"{len(items)} items")


def _a_json_object(spec: dict, output: str, case: dict) -> dict:
    text = str(output or "").strip()
    if text.startswith("```"):
        return _fail("JSON wrapped in a markdown fence")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return _fail(f"not valid JSON ({str(exc)[:60]})")
    if not isinstance(parsed, dict):
        return _fail("valid JSON but not an object")
    missing = [k for k in (spec.get("required_keys") or []) if k not in parsed]
    return _fail(f"missing keys: {', '.join(missing)}") if missing else _pass()


def _a_slop_lint(spec: dict, output: str, case: dict) -> dict:
    from cqc_lem.utilities.ai.slop_lint import lint_report
    report = lint_report(output, spec.get("content_type") or "post",
                         spec.get("exempt_keyword"))
    if not report.get("checked"):
        # The lint is env-gated. Unchecked is NOT a pass — it is no reading, and grading it as a
        # pass would let a disabled linter flatter every candidate equally.
        return {"passes": None, "detail": "slop lint disabled for this surface"}
    hard = report.get("hard") or []
    if hard:
        return _fail("; ".join(v.get("detail", v.get("check", "")) for v in hard))
    warnings = report.get("warnings") or []
    return _pass(f"{len(warnings)} warning(s)" if warnings else "clean")


def _a_comment_contract(spec: dict, output: str, case: dict) -> dict:
    from cqc_lem.utilities.ai.content_framework import comment_contract_report
    post = (case.get("context") or {}).get("post_content") or spec.get("post_content")
    report = comment_contract_report(output, post)
    if report.get("passes"):
        return _pass("; ".join(report.get("value_adds") or []) or "contract met")
    return _fail("; ".join(report.get("failures") or ["fails the comment contract"]))


def _a_min_burstiness(spec: dict, output: str, case: dict) -> dict:
    from cqc_lem.utilities.ai.slop_lint import burstiness
    score = burstiness(output)
    if score is None:
        return {"passes": None, "detail": "too few sentences to measure burstiness"}
    floor = float(spec.get("value") or 0.0)
    return _pass(f"{score:.2f}") if score >= floor else _fail(f"burstiness {score:.2f} < {floor}")


def _a_max_similarity(spec: dict, output: str, case: dict) -> dict:
    """Templated sameness across a candidate's OWN answers. `against` names other case ids in the
    same suite, resolved by the caller into `spec['_texts']`."""
    ceiling = float(spec.get("value") or 1.0)
    worst, worst_key = 0.0, None
    for key, text in (spec.get("_texts") or {}).items():
        score = lexical_similarity(output, text)
        if score > worst:
            worst, worst_key = score, key
    if worst_key is None:
        return {"passes": None, "detail": "no comparison text available"}
    if worst > ceiling:
        return _fail(f"{worst:.2f} token overlap with {worst_key} > {ceiling}")
    return _pass(f"max {worst:.2f} vs {worst_key}")


def _a_router_tier(spec: dict, output: str, case: dict) -> dict:
    """The `LEMComplexityRouter` contract: the tier this prompt must resolve to. Deterministic and
    model-independent by design — the router's classification lives in `routing_policy`, not in the
    model — so this grades the CONTRACT (and catches a fixture that drifted from it) while the
    case's other assertions grade the candidate's completion at the router alias."""
    from cqc_lem.utilities.routing_policy import complexity_tier, prompt_text
    # `prompt_text` (not a hand-rolled join) because the router's signal matching is case- and
    # shape-sensitive: grading against a differently-flattened prompt would test a contract the
    # proxy does not actually run.
    resolved = complexity_tier(prompt_text(case.get("messages")))
    expected = str(spec.get("value") or "").strip()
    if resolved == expected:
        return _pass(f"routes to {resolved}")
    return _fail(f"routes to {resolved}, expected {expected}")


def _a_forbidden_phrases(spec: dict, output: str, case: dict) -> dict:
    lowered = str(output or "").lower()
    hits = [p for p in (spec.get("values") or []) if str(p).lower() in lowered]
    return _fail("contains " + ", ".join(repr(h) for h in hits)) if hits else _pass()


def _a_required_phrases(spec: dict, output: str, case: dict) -> dict:
    """Instruction adherence: the answer must actually be about the thing it was given. `mode:any`
    (default) accepts one hit — a grounded answer need not echo every term."""
    lowered = str(output or "").lower()
    values = [str(v) for v in (spec.get("values") or [])]
    hits = [v for v in values if v.lower() in lowered]
    if str(spec.get("mode") or "any").lower() == "all":
        missing = [v for v in values if v not in hits]
        return _fail("missing " + ", ".join(repr(m) for m in missing)) if missing else _pass()
    return _pass(f"matched {hits[0]!r}") if hits else _fail(
        "mentions none of " + ", ".join(repr(v) for v in values))


def _a_max_lines(spec: dict, output: str, case: dict) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    limit = int(spec.get("value") or 0)
    return _pass(f"{len(lines)} lines") if len(lines) <= limit else _fail(
        f"{len(lines)} non-empty lines > {limit}")


# ───────────────── what production does with a failure (#910, pure) ─────────────────
#
# The #842 run measured 40–80% deterministic pass rates for every model including both reigning
# champions, against a 90% floor — a gate that can never open. The failures were real model
# behaviour, not harness artifacts, but they are not all the same KIND of failure:
#
#   * a REPAIRABLE one is caught by a production regeneration gate and redrafted — `lint_repaired`
#     for the short surfaces, `_gated_comment` for every self-authored comment (#617, up to
#     COMMENT_GATE_MAX_ATTEMPTS, then the post is SKIPPED), `_review_generated_post` for posts
#     (one regeneration, then `evaluate_post_gates` HOLDS the draft at pending). Nothing ships.
#   * a CONTRACT one is what the call site actually consumes: a broken JSON body, a preamble
#     pasted into LinkedIn copy, an answer over LinkedIn's hard character limit, a classification
#     that came back as prose. Production has no repair for these.
#
# So the suite scores a first draft and production ships an n-th. The absolute floor belongs on the
# contract rate; the first-draft rate stays reported (and champion-relative), because needing three
# drafts is a real cost — it is just not the same thing as shipping a broken one.
PRODUCTION_CONTRACT = "contract"
PRODUCTION_REPAIRABLE = "repairable"
PRODUCTION_CLASSES = (PRODUCTION_CONTRACT, PRODUCTION_REPAIRABLE)

# Type-level defaults. A fixture may override per assertion with `"production": "..."` — whether a
# check is repairable is a property of the CALL SITE, not of the check (a craft-target `max_chars`
# on long-form is repairable; the same assertion carrying LinkedIn's hard limit is not).
REPAIRABLE_BY_DEFAULT = ("slop_lint", "comment_contract", "max_similarity", "min_burstiness")


def assertion_production_class(spec: Optional[dict]) -> str:
    """`contract` / `repairable` for one assertion. Unknown or absent -> the type's default, and an
    unrecognised type is CONTRACT: a check nobody classified must not quietly stop counting."""
    raw = str((spec or {}).get("production") or "").strip().lower()
    if raw in PRODUCTION_CLASSES:
        return raw
    return (PRODUCTION_REPAIRABLE if (spec or {}).get("type") in REPAIRABLE_BY_DEFAULT
            else PRODUCTION_CONTRACT)


ASSERTIONS: dict = {
    "max_chars": _a_max_chars,
    "min_chars": _a_min_chars,
    "no_preamble": _a_no_preamble,
    "comma_list": _a_comma_list,
    "json_object": _a_json_object,
    "slop_lint": _a_slop_lint,
    "comment_contract": _a_comment_contract,
    "min_burstiness": _a_min_burstiness,
    "max_similarity": _a_max_similarity,
    "router_tier": _a_router_tier,
    "forbidden_phrases": _a_forbidden_phrases,
    "required_phrases": _a_required_phrases,
    "max_lines": _a_max_lines,
}


def evaluate_case(case: dict, output: Optional[str],
                  peer_outputs: Optional[dict] = None) -> dict:
    """Grade ONE model output against one case. Pure.

    A missing output (provider error) is a case FAILURE with the error carried through, never an
    absent row: a model that couldn't answer is worse than one that answered badly, and dropping the
    case would quietly raise its pass rate. It is a CONTRACT failure too — the call site got
    nothing, and there is no regeneration that repairs nothing.

    Two verdicts come back per case (#910): `passes` is the first draft against every check, and
    `contract_passes` is the same draft against only the checks production has no repair for."""
    case_id = str(case.get("id"))
    if output is None:
        return {"case_id": case_id, "passes": False, "contract_passes": False,
                "error": case.get("_error") or "no output", "assertions": [],
                "failures": ["no output from the model"],
                "contract_failures": ["no output from the model"], "repairable_failures": [],
                "unscored": 0}
    results = []
    for spec in case.get("assertions") or []:
        kind = spec.get("type")
        fn = ASSERTIONS.get(kind)
        production = assertion_production_class(spec)
        if fn is None:  # unreachable via load_suite; belt-and-braces for hand-built cases
            results.append({"type": kind, "passes": False, "production": PRODUCTION_CONTRACT,
                            "detail": f"unknown assertion {kind!r}"})
            continue
        resolved = dict(spec)
        if kind == "max_similarity":
            resolved["_texts"] = {k: v for k, v in (peer_outputs or {}).items()
                                  if k in (spec.get("against") or []) and v}
        try:
            outcome = fn(resolved, str(output), case)
        except Exception as exc:  # noqa: BLE001 - one bad assertion must not sink the whole run
            outcome = _fail(f"assertion raised {type(exc).__name__}: {str(exc)[:80]}")
        results.append({"type": kind, "passes": outcome.get("passes"), "production": production,
                        "detail": outcome.get("detail", "")})
    failures = [f"{r['type']}: {r['detail']}" for r in results if r["passes"] is False]
    contract_failures = [f"{r['type']}: {r['detail']}" for r in results
                         if r["passes"] is False and r["production"] == PRODUCTION_CONTRACT]
    repairable_failures = [f"{r['type']}: {r['detail']}" for r in results
                           if r["passes"] is False and r["production"] == PRODUCTION_REPAIRABLE]
    unscored = sum(1 for r in results if r["passes"] is None)
    return {"case_id": case_id, "passes": not failures, "contract_passes": not contract_failures,
            "assertions": results, "failures": failures,
            "contract_failures": contract_failures, "repairable_failures": repairable_failures,
            "unscored": unscored}


def score_suite(suite: dict, outputs: dict) -> list:
    """Grade every case of one suite for one model. `outputs` is {case_id: text-or-None}."""
    peers = {k: v for k, v in (outputs or {}).items() if v}
    return [evaluate_case(case, (outputs or {}).get(str(case.get("id"))), peers)
            for case in suite.get("cases") or []]


# ─────────────────────────── scorecard merge (pure) ──────────────────────────────

def _rate(passed: int, total: int) -> Optional[float]:
    return round(passed / float(total), 4) if total else None


def _percentile(values: list, fraction: float) -> Optional[float]:
    ordered = sorted(v for v in values if isinstance(v, (int, float)))
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(float(ordered[index]), 1)


def merge_scorecard(tier: str, model: str, role: str, case_results: list,
                    judge_results: Optional[dict] = None,
                    timings: Optional[dict] = None) -> dict:
    """One model × one tier. Deterministic results, judge verdicts and timings are merged into the
    single row the report and the gate both read.

    Judge verdicts are three-valued: True, False, and `judge:timeout`/absent. Only the first two
    count toward `judge_pass_rate`; an unscored case is reported as unscored, so a run where the
    judge never answered can never look like a run where it approved everything.

    Deterministic results are TWO rates (#910): `contract_pass_rate` (what production would ship)
    and `deterministic_pass_rate` (the first draft against every check). Both are always reported —
    a model that clears the contract rate only by burning three drafts per case has to be visible."""
    judge = judge_results or {}
    timings = timings or {}
    total = len(case_results)
    passed = sum(1 for r in case_results if r.get("passes"))
    contract_passed = sum(1 for r in case_results if r.get("contract_passes"))
    errors = [r["case_id"] for r in case_results if r.get("error")]

    judged_pass = judged_fail = judged_timeout = 0
    for result in case_results:
        verdict = judge.get(result["case_id"])
        if verdict is None:
            continue
        if verdict.get("status") == JUDGE_TIMEOUT or verdict.get("passes") is None:
            judged_timeout += 1
        elif verdict.get("passes"):
            judged_pass += 1
        else:
            judged_fail += 1
    judged = judged_pass + judged_fail

    latencies = [timings.get(r["case_id"], {}).get("latency_ms") for r in case_results]
    latencies = [v for v in latencies if isinstance(v, (int, float))]
    tokens = sum(int(timings.get(r["case_id"], {}).get("total_tokens") or 0)
                 for r in case_results)
    escalated = [r["case_id"] for r in case_results
                 if int(timings.get(r["case_id"], {}).get("budget_escalations") or 0) > 0]
    remeasured = [r["case_id"] for r in case_results
                  if int(timings.get(r["case_id"], {}).get("repeats") or 0) > 0]
    budget_locked = [r["case_id"] for r in case_results
                     if timings.get(r["case_id"], {}).get("budget_locked")]

    return {
        "tier": tier, "model": model, "role": role,
        "cases": total,
        "deterministic_passed": passed,
        "deterministic_pass_rate": _rate(passed, total),
        "contract_passed": contract_passed,
        "contract_pass_rate": _rate(contract_passed, total),
        # Passed what production consumes, but only after a redraft it would have had to pay for.
        "repaired_cases": [r["case_id"] for r in case_results
                           if r.get("contract_passes") and not r.get("passes")],
        "errors": errors,
        "failed_cases": [{"case_id": r["case_id"], "failures": r.get("failures") or [],
                          "contract_failures": r.get("contract_failures") or [],
                          "repairable_failures": r.get("repairable_failures") or [],
                          # Carried so the report can tell "nothing arrived" apart from "something
                          # arrived that production would have shipped" — a no-output case is a
                          # measurement that never happened, not a defect (#910).
                          "error": r.get("error")}
                         for r in case_results if not r.get("passes")],
        "unscored_assertions": sum(int(r.get("unscored") or 0) for r in case_results),
        "judged": judged,
        "judge_passed": judged_pass,
        "judge_timeouts": judged_timeout,
        "judge_pass_rate": _rate(judged_pass, judged),
        "judge_notes": [{"case_id": cid, "reasoning": str(v.get("reasoning") or "")[:280]}
                        for cid, v in sorted(judge.items())
                        if v.get("passes") is False and v.get("reasoning")],
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p90_ms": _percentile(latencies, 0.9),
        "total_tokens": tokens,
        "budget_escalated_cases": escalated,
        "remeasured_cases": remeasured,
        "budget_locked_cases": budget_locked,
        "case_results": case_results,
    }


# ─────────────────────── usage level / quota delta (pure) ────────────────────────
#
# A benchmark win says a model is BETTER. It does not say what it costs. Ollama Cloud meters by the
# model's usage level, so promoting a HIGH model over a MEDIUM one raises quota burn on every call
# of that tier — issue #842's minimax-m3 / glm-5.2 question exactly. The harness therefore carries
# the level beside the scores and renders the delta in the report; it deliberately does NOT gate on
# it, because "is the extra quota worth it" is a human call, not a threshold.

USAGE_LEVEL_NAMES = {1: "Low", 2: "Medium", 3: "High", 4: "Extra high"}
# Same scale `model_health_check.parse_usage_level` reads off the model page — a test asserts the
# two agree, so an override and a scrape can never mean different things by "medium".
USAGE_LEVELS_BY_LABEL = {name.lower(): level for level, name in USAGE_LEVEL_NAMES.items()}

USAGE_UP = "up"
USAGE_DOWN = "down"
USAGE_FLAT = "flat"
USAGE_UNKNOWN = "unknown"


def usage_level(entry: Optional[dict]) -> Optional[int]:
    """The ONE place a stored level becomes a number the harness is willing to act on.

    `model_health_check.parse_usage_level` falls back to COUNTING filled pips when the page's
    wording changes, so a markup drift hands back a level like 0 or 7. Anything outside 1–4 is
    unreadable, and it has to read that way EVERYWHERE: a level the scorecard prints as `unknown`
    while the delta treats it as a real number is how an unread champion level renders a quota
    increase as a "decrease" and clears the standing spend policy on its own."""
    level = (entry or {}).get("level")
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return None
    level = int(level)
    return level if 1 <= level <= 4 else None


def usage_name(entry: Optional[dict]) -> str:
    """`{level, label}` → "High (3)". An unreadable level is `unknown`, never a guessed middle."""
    level = usage_level(entry)
    label = (" ".join(str((entry or {}).get("label") or "").split()).capitalize()
             or USAGE_LEVEL_NAMES.get(level))
    if label and level:
        return f"{label} ({level})"
    return label or "unknown"


def parse_usage_overrides(raw: Optional[str]) -> dict:
    """`model=low|medium|high|extra high` pairs.

    Needed because ollama.com publishes the Usage stat only on CLOUD-ONLY model pages: the tiers'
    incumbents (`gpt-oss:120b`, `qwen3.5:397b`, `gemma4:31b`) are also pullable locally, so their
    pages carry no level and the scrape honestly returns unknown. The level is then a human input
    from the model's cloud listing — stated on the run, never invented by the harness."""
    out: dict = {}
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        model, _, label = chunk.partition("=")
        label = " ".join(label.split()).lower()
        if not model.strip() or label not in USAGE_LEVELS_BY_LABEL:
            raise SystemExit(f"--usage-levels expects model=low|medium|high|'extra high', "
                             f"got {chunk!r}")
        out[model.strip()] = {"level": USAGE_LEVELS_BY_LABEL[label], "label": label}
    return out


def collect_usage_levels(models: list, *, fetch: Optional[Callable[[str], dict]] = None,
                         overrides: Optional[dict] = None) -> dict:
    """`model -> {level, label}` for every model in the run. Overrides win over the fetched page.

    `fetch` is injected (and omitted entirely on a dry run) so this stays the only place the harness
    would touch the network for a level; a fetch that raises leaves that model unknown rather than
    failing the benchmark."""
    overrides = overrides or {}
    levels: dict = {}
    for model in models:
        if model in overrides:
            levels[model] = dict(overrides[model])
            continue
        entry = {"level": None, "label": None}
        if fetch is not None:
            try:
                entry = fetch(model) or entry
            except Exception as exc:  # noqa: BLE001 - a level we cannot read is unknown, not fatal
                print(f"  ! usage level unavailable for {model}: {str(exc)[:120]}", file=sys.stderr)
        levels[model] = {"level": entry.get("level"), "label": entry.get("label")}
    return levels


def usage_delta(candidate: Optional[dict], champion: Optional[dict], *,
                candidate_model: str = "", champion_model: str = "") -> dict:
    """What adopting `candidate` in place of `champion` does to Ollama Cloud quota.

    `unknown` is NOT `flat`. A swap whose quota cost cannot be read is a quota RISK, so it renders
    with the same warning an increase gets — the failure mode this exists to prevent is a HIGH model
    being adopted as if it were free because nobody could see its level."""
    cand_level = usage_level(candidate)
    champ_level = usage_level(champion)
    cand_name = usage_name(candidate)
    champ_name = usage_name(champion)
    known = cand_level is not None and champ_level is not None
    if not known:
        direction, steps = USAGE_UNKNOWN, None
    elif cand_level > champ_level:
        direction, steps = USAGE_UP, cand_level - champ_level
    elif cand_level < champ_level:
        direction, steps = USAGE_DOWN, cand_level - champ_level
    else:
        direction, steps = USAGE_FLAT, 0

    champ_ref = f"champion `{champion_model}`" if champion_model else "the champion"
    if direction == USAGE_UP:
        summary = (f"⚠️ **quota increase** — {cand_name} vs {champ_ref} {champ_name} "
                   f"(+{steps} usage level{'s' if steps > 1 else ''}). Every call on this tier "
                   f"burns more Ollama Cloud quota; this is a spend decision, not a free upgrade.")
    elif direction == USAGE_DOWN:
        summary = (f"quota decrease — {cand_name} vs {champ_ref} {champ_name} "
                   f"({steps} usage level{'s' if steps < -1 else ''}).")
    elif direction == USAGE_FLAT:
        summary = f"flat on quota — {cand_name} on both sides."
    else:
        missing = [n for n, lvl in ((candidate_model or "candidate", cand_level),
                                    (champion_model or "champion", champ_level))
                   if lvl is None]
        summary = (f"⚠️ **quota delta unknown** — no usage level for {', '.join(missing)} "
                   f"(ollama.com publishes it only for cloud-only models; pass "
                   f"`--usage-levels {missing[0]}=<level>`). Treat as an increase until confirmed.")
    return {"candidate_level": cand_level, "candidate_label": cand_name,
            "champion_level": champ_level, "champion_label": champ_name,
            "direction": direction, "steps": steps, "summary": summary}


# Standing owner policy (#842 decision `2A`): a quality win that RAISES the Ollama Cloud usage level
# is adoptable only on `lem-complex` — long-form is the one tier where quality IS the product — and
# only on a STRICT judge-rate win. A tie does not buy +1 usage level on every call that tier serves.
# It lives here rather than in prose so an unattended run states the call itself instead of parking
# for the same decision twice. Still reported, never gated: the gate's `recommend` is a QUALITY
# verdict and is untouched by this — the policy only says whether that verdict can be TAKEN without
# going back to the owner.
QUOTA_INCREASE_TIERS = ("lem-complex",)

POLICY_ADOPT = "adopt"
POLICY_HOLD = "hold"


def quota_policy(tier: str, delta: Optional[dict], candidate: Optional[dict] = None,
                 champion: Optional[dict] = None) -> dict:
    """Does the standing policy let this recommendation be adopted, or does it need the owner?"""
    direction = (delta or {}).get("direction") or USAGE_UNKNOWN
    tiers = ", ".join(f"`{t}`" for t in QUOTA_INCREASE_TIERS)
    if direction in (USAGE_FLAT, USAGE_DOWN):
        return {"decision": POLICY_ADOPT,
                "reason": "no usage-level increase — the quality gate is the whole decision."}
    if direction == USAGE_UNKNOWN:
        # An unconfirmed level is treated as an increase, so it cannot clear a policy written about
        # increases: the level has to be READ before the policy can say anything true about it.
        return {"decision": POLICY_HOLD,
                "reason": ("usage level unread on at least one side, which counts as an increase — "
                           "supply it (`--usage-levels`, or `BENCHMARK_USAGE_LEVELS` on an "
                           "unattended run) and re-render before adopting.")}
    if tier not in QUOTA_INCREASE_TIERS:
        return {"decision": POLICY_HOLD,
                "reason": (f"a usage-level increase is adoptable only on {tiers}; `{tier}` would "
                           f"pay the extra quota without the long-form quality it buys.")}
    cand_judge = (candidate or {}).get("judge_pass_rate")
    champ_judge = (champion or {}).get("judge_pass_rate")
    if not isinstance(cand_judge, (int, float)) or not isinstance(champ_judge, (int, float)):
        return {"decision": POLICY_HOLD,
                "reason": ("no judge rate on both sides — a quota increase is adoptable only on a "
                           "measured quality win.")}
    if cand_judge > champ_judge:
        return {"decision": POLICY_ADOPT,
                "reason": (f"beats the champion's judge rate outright ({cand_judge:.0%} vs "
                           f"{champ_judge:.0%}) on {tiers}.")}
    return {"decision": POLICY_HOLD,
            "reason": (f"ties or trails the champion on judge rate ({cand_judge:.0%} vs "
                       f"{champ_judge:.0%}) — a tie is not worth the extra quota.")}


# ───────────────────────── champion/challenger gate (pure) ───────────────────────

VERDICT_RECOMMEND = "recommend"
VERDICT_REJECT = "reject"
VERDICT_NO_BASELINE = "no-baseline"
VERDICT_DETERMINISTIC_ONLY = "recommend-deterministic-only"


def gate_decision(candidate: dict, champion: Optional[dict], thresholds: dict) -> dict:
    """Does this candidate earn a swap recommendation on this tier?

    Three things must hold, and every one of them is reported per expectation:
      * absolute — the tier's own floors (a candidate that beats a bad champion is still bad);
      * relative — meets or beats the champion on every graded rate;
      * evidence — enough judged cases to mean anything.
    A missing champion is `no-baseline`, never a pass: "better than nothing" is not a comparison.
    Judge evidence that is missing on BOTH sides degrades to `recommend-deterministic-only`, which
    is reported but is NOT a swap recommendation — that distinction is the whole point of the gate.

    The absolute deterministic floor is on the CONTRACT rate (#910). The first-draft rate is
    returned as an ADVISORY reading rather than an expectation — advisories never change the
    verdict — but it is still compared against the champion, because a model that needs more
    redrafts than the incumbent is not an upgrade even when nothing broken ships.
    """
    expectations: list = []
    advisories: list = []
    thresholds = normalize_thresholds(thresholds)

    def expect(name: str, ok: Optional[bool], detail: str) -> None:
        expectations.append({"expectation": name, "passes": ok, "detail": detail})

    det = candidate.get("deterministic_pass_rate")
    contract = candidate.get("contract_pass_rate")
    contract_floor = thresholds["contract_pass_rate"]
    if contract is None:
        expect("contract floor", False, "no cases scored")
    else:
        expect("contract floor", contract >= contract_floor,
               f"{contract:.0%} vs floor {contract_floor:.0%}")

    advisory_floor = thresholds["deterministic_pass_rate"]
    repaired = len(candidate.get("repaired_cases") or [])
    if det is None:
        advisories.append({"expectation": "first-draft yield", "passes": None,
                           "detail": "no cases scored"})
    else:
        advisories.append({
            "expectation": "first-draft yield",
            "passes": det >= advisory_floor,
            "detail": (f"{det:.0%} vs advisory target {advisory_floor:.0%} — {repaired} case(s) "
                       "production would have redrafted rather than shipped (advisory: does not "
                       "change the verdict)")})

    if candidate.get("errors"):
        expect("provider answered every case", False,
               f"{len(candidate['errors'])} case(s) returned no output")
    else:
        expect("provider answered every case", True, "all cases answered")

    judge_rate = candidate.get("judge_pass_rate")
    judged = int(candidate.get("judged") or 0)
    judge_floor = thresholds["judge_pass_rate"]
    judge_evidence = judged >= thresholds["min_judged"]
    if not judge_evidence:
        expect("judge floor", None,
               f"{judged} judged case(s) < {thresholds['min_judged']} required — unscored")
    else:
        expect("judge floor", judge_rate is not None and judge_rate >= judge_floor,
               f"{judge_rate:.0%} vs floor {judge_floor:.0%}" if judge_rate is not None
               else "no judge verdicts")

    if champion is None:
        expect("beats champion", None, "no champion scorecard for this tier")
    else:
        champ_contract = champion.get("contract_pass_rate")
        if contract is None or champ_contract is None:
            expect("beats champion (contract)", False, "one side has no score")
        else:
            expect("beats champion (contract)", contract >= champ_contract,
                   f"{contract:.0%} vs champion {champ_contract:.0%}")
        champ_det = champion.get("deterministic_pass_rate")
        if det is None or champ_det is None:
            expect("beats champion (first-draft)", False, "one side has no score")
        else:
            expect("beats champion (first-draft)", det >= champ_det,
                   f"{det:.0%} vs champion {champ_det:.0%}")
        champ_judge = champion.get("judge_pass_rate")
        champ_evidence = int(champion.get("judged") or 0) >= thresholds["min_judged"]
        if not (judge_evidence and champ_evidence):
            expect("beats champion (judge)", None, "judge evidence missing on one side")
        else:
            expect("beats champion (judge)", (judge_rate or 0.0) >= (champ_judge or 0.0),
                   f"{(judge_rate or 0):.0%} vs champion {(champ_judge or 0):.0%}")

    if any(e["passes"] is False for e in expectations):
        verdict = VERDICT_REJECT
    elif champion is None:
        verdict = VERDICT_NO_BASELINE
    elif any(e["passes"] is None for e in expectations):
        verdict = VERDICT_DETERMINISTIC_ONLY
    else:
        verdict = VERDICT_RECOMMEND
    # Reported, never gated: a HIGH-usage candidate can still earn `recommend` on quality — what it
    # must never do is arrive without its price tag attached, or without the standing spend policy
    # (#842 `2A`) applied to that price.
    delta = usage_delta(candidate.get("usage"), (champion or {}).get("usage"),
                        candidate_model=str(candidate.get("model") or ""),
                        champion_model=str((champion or {}).get("model") or ""))
    return {"tier": candidate.get("tier"), "model": candidate.get("model"),
            "champion": (champion or {}).get("model"), "verdict": verdict,
            "expectations": expectations,
            "advisories": advisories,
            # A floor the INCUMBENT misses is a statement about the tier, not about the challenger
            # (#910). Carried on the verdict so the report says it out loud instead of leaving a
            # reader to infer it from two numbers in different tables.
            "champion_clears_contract_floor": (
                None if champion is None or champion.get("contract_pass_rate") is None
                else champion["contract_pass_rate"] >= contract_floor),
            "usage_delta": delta,
            "quota_policy": quota_policy(str(candidate.get("tier") or ""), delta,
                                         candidate, champion),
            "blockers": [e["expectation"] for e in expectations if e["passes"] is False]}


def gate_run(scorecards: list, suites: dict) -> list:
    """Run the gate for every candidate scorecard against its tier's champion."""
    champions = {c["tier"]: c for c in scorecards if c.get("role") == "champion"}
    return [gate_decision(card, champions.get(card["tier"]),
                          (suites.get(card["tier"]) or {}).get("thresholds") or {})
            for card in scorecards if card.get("role") == "candidate"]


def swap_recommendations(gates: list) -> list:
    """ONLY full `recommend` verdicts become swap recommendations. A candidate that merely cleared
    the deterministic floor, or had no champion to beat, is reported and goes no further."""
    return [{"tier": g["tier"], "model": g["model"], "champion": g["champion"],
             "usage_delta": g.get("usage_delta") or usage_delta(None, None),
             "quota_policy": g.get("quota_policy") or quota_policy(str(g.get("tier") or ""),
                                                                   g.get("usage_delta"))}
            for g in gates if g.get("verdict") == VERDICT_RECOMMEND]


def recommendation_mapping_lines(recommendations: list) -> list:
    """The exact `model_upgrades.yaml` lines a human would add — rendered, never written.

    That map is the RETIREMENT map: `model_health_check.py` auto-swaps whatever is in it into the
    live LiteLLM config on the next cron. A benchmark win is evidence for a swap, not authority to
    perform one, so this harness stops at copy-pasteable text (the same rule the #716 upgrade issue
    states in words)."""
    return [f'  "{r["champion"]}": "{r["model"]}"  # {r["tier"]}: benchmark-recommended'
            for r in recommendations if r.get("champion")]


# ───────────────────────── provenance guard (pure) ───────────────────────────────

def assert_benchmark_only(run: dict) -> None:
    """Refuse to render anything that isn't demonstrably benchmark output.

    Reports are committed to a public repo. Every scorecard must carry this run's own id and the
    benchmark source marker, and no record may carry a real person's identity — an `$ai_evaluation`
    row pulled with a mistyped HogQL filter would otherwise land a customer's LinkedIn copy in git.
    Raises `ValueError`; there is no permissive mode."""
    if not isinstance(run, dict):
        raise ValueError("benchmark report: run must be a mapping")
    if run.get("source") != BENCHMARK_SOURCE:
        raise ValueError(f"benchmark report: refusing a non-benchmark source "
                         f"{run.get('source')!r}")
    run_id = str(run.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("benchmark report: run has no run_id")
    for card in run.get("scorecards") or []:
        if str(card.get("benchmark_run_id") or "") != run_id:
            raise ValueError(f"benchmark report: scorecard {card.get('model')}/{card.get('tier')} "
                             f"is not tagged with this run_id")
        for field in ("user_id", "distinct_id", "person_id", "email"):
            value = card.get(field)
            if value not in (None, "", BENCHMARK_DISTINCT_ID):
                raise ValueError(f"benchmark report: scorecard carries a {field} "
                                 f"({value!r}) — benchmark records are anonymous")


# ─────────────────────────── report rendering (pure) ─────────────────────────────

def _fmt_rate(value: Optional[float]) -> str:
    return f"{value:.0%}" if isinstance(value, (int, float)) else "n/a"


def _fmt_ms(value: Optional[float]) -> str:
    return f"{int(value)} ms" if isinstance(value, (int, float)) else "n/a"


def report_filename(run: dict) -> str:
    return f"{run.get('date')}-{run.get('run_id')}.md"


def render_report(run: dict) -> str:
    """The per-run scorecard committed to `docs/model-benchmarks/`."""
    assert_benchmark_only(run)
    lines = [
        f"# Model-tier benchmark — {run.get('date')} (`{run.get('run_id')}`)",
        "",
        f"- **Scoring mode:** `{run.get('scoring_mode')}`"
        + (" — canned outputs, no provider or judge calls were made"
           if run.get("scoring_mode") == SCORING_DRY_RUN else ""),
        f"- **Tiers:** {', '.join(run.get('tiers') or []) or 'none'}",
        f"- **Candidates:** {', '.join(run.get('candidates') or []) or 'none (baseline run)'}",
        f"- **Judge calls spent:** {run.get('judge_calls', 0)}"
        f" (cap {run.get('judge_call_cap', 0)})",
        "",
        ("Inputs are synthetic prompt templates from `tests/benchmarks/model_tiers/`; no customer " +
         "content, credentials or production logs appear in this report."),
        "",
        "## Scorecard",
        "",
        SCORECARD_HEADER,
        SCORECARD_DIVIDER,
    ]
    for card in run.get("scorecards") or []:
        lines.append(
            f"| {card['tier']} | `{card['model']}` | {card['role']} | "
            f"{usage_name(card.get('usage'))} | {card['cases']} | "
            f"{_fmt_rate(card.get('contract_pass_rate'))} "
            f"({card.get('contract_passed', 0)}/{card.get('cases', 0)}) | "
            f"{_fmt_rate(card.get('deterministic_pass_rate'))} "
            f"({card.get('deterministic_passed', 0)}/{card.get('cases', 0)}) | "
            f"{_fmt_rate(card.get('judge_pass_rate'))} | {card.get('judged', 0)} | "
            f"{card.get('judge_timeouts', 0)} | {_fmt_ms(card.get('latency_p50_ms'))} | "
            f"{_fmt_ms(card.get('latency_p90_ms'))} |")
    lines += ["",
              ("**Contract** is the share of cases whose failures production would actually "
               "consume; **First draft** is the same cases against every check, repairable ones "
               "included. Production regenerates against the repairable ones (`lint_repaired`, the "
               "#617 comment gate, the post review gate) and skips or holds what still fails, so "
               "the gap between the two columns is redraft COST, not shipped defects. The absolute "
               "floor is on Contract; First draft is advisory and champion-relative (#910)."),
              "",
              ("**Usage** is the Ollama Cloud usage level (quota class) — `unknown` where "
               "ollama.com publishes no level for that model, which is the case for models that "
               "are also pullable locally. Supply those with `--usage-levels model=<level>`.")]

    lines += ["", "## Per-expectation detail", ""]
    for card in run.get("scorecards") or []:
        lines.append(f"### `{card['model']}` — {card['tier']} ({card['role']})")
        lines.append("")
        if card.get("errors"):
            lines.append(f"- ⚠️ no output for: {', '.join(f'`{c}`' for c in card['errors'])}")
        failed = card.get("failed_cases") or []
        if not failed:
            lines.append("- every case met every deterministic expectation")
        for entry in failed:
            contract = entry.get("contract_failures") or []
            repairable = entry.get("repairable_failures") or []
            if entry.get("error"):
                # `no output` is a CONTRACT failure — the call site got nothing — but it is not
                # something production would ship, and since #910 turns an empty completion into no
                # output this is the common path. Labelling it "production would ship this" is the
                # exact misreading the two rates exist to prevent.
                lines.append(f"- ❌ `{entry['case_id']}` — no output; nothing was measured here")
                lines.append(f"  - {entry['error']}")
            elif contract:
                lines.append(f"- ❌ `{entry['case_id']}` — production would ship this")
                for failure in contract:
                    lines.append(f"  - {failure}")
                for failure in repairable:
                    lines.append(f"  - (repairable) {failure}")
            elif repairable:
                lines.append(f"- 🔁 `{entry['case_id']}` — first draft only; production "
                             "regenerates against this and skips/holds what still fails")
                for failure in repairable:
                    lines.append(f"  - {failure}")
            else:  # a hand-built case whose failures carry no production class
                lines.append(f"- ❌ `{entry['case_id']}`")
                for failure in entry.get("failures") or []:
                    lines.append(f"  - {failure}")
        if card.get("unscored_assertions"):
            lines.append(f"- {card['unscored_assertions']} assertion(s) unscored "
                         "(not counted either way)")
        escalated = card.get("budget_escalated_cases") or []
        if escalated:
            lines.append(f"- 🧠 reasoning headroom — {len(escalated)} case(s) spent the fixture's "
                         "whole completion budget thinking and were retried at a larger one: "
                         + ", ".join(f"`{c}`" for c in escalated))
        locked = card.get("budget_locked_cases") or []
        if locked:
            lines.append(f"- 🔒 production budget — {len(locked)} case(s) spent a budget that "
                         "MIRRORS a production call site's own `max_tokens` without answering, and "
                         "were NOT retried at a larger one; a model that cannot answer inside that "
                         "budget is disqualified at that call site, not merely truncated: "
                         + ", ".join(f"`{c}`" for c in locked))
        remeasured = card.get("remeasured_cases") or []
        if remeasured:
            lines.append(f"- ⚖️ measurement variance — {len(remeasured)} case(s) came back EMPTY at "
                         "the full budget and were re-measured at that same budget: "
                         + ", ".join(f"`{c}`" for c in remeasured)
                         + ". One run of one case is a data point, not a verdict — read this "
                           "model's rates as single-measurement.")
        for note in card.get("judge_notes") or []:
            lines.append(f"- 🧑‍⚖️ `{note['case_id']}`: {note['reasoning']}")
        lines.append("")

    lines += ["## Gate verdicts", ""]
    gates = run.get("gates") or []
    if not gates:
        lines.append("No candidates in this run — the champions were measured as a baseline only.")
    for gate in gates:
        champion = f"vs champion `{gate['champion']}`" if gate.get("champion") else "no champion"
        lines += [f"### `{gate['model']}` on {gate['tier']} — **{gate['verdict']}** ({champion})",
                  ""]
        for expectation in gate.get("expectations") or []:
            mark = {True: "✅", False: "❌"}.get(expectation["passes"], "➖")
            lines.append(f"- {mark} {expectation['expectation']} — {expectation['detail']}")
        for advisory in gate.get("advisories") or []:
            mark = {True: "✅", False: "⚠️"}.get(advisory["passes"], "➖")
            lines.append(f"- {mark} {advisory['expectation']} (advisory) — {advisory['detail']}")
        if gate.get("champion_clears_contract_floor") is False:
            lines.append(f"- 🪧 the incumbent `{gate.get('champion')}` misses this tier's own "
                         "contract floor — that is a statement about the tier's fixtures, not "
                         "about this candidate; the relative half of the verdict is what stands.")
        delta = gate.get("usage_delta") or {}
        if delta.get("summary"):
            lines.append(f"- 💳 usage level — {delta['summary']}")
        policy = gate.get("quota_policy") or {}
        if gate.get("verdict") == VERDICT_RECOMMEND and policy.get("decision"):
            lines.append(f"- 🧾 standing spend policy — **{policy['decision']}**: "
                         f"{policy.get('reason', '')}")
        lines.append("")

    recommendations = run.get("recommendations") or []
    lines += ["## Swap recommendations", ""]
    if not recommendations:
        lines += [("None. No candidate met its tier's thresholds *and* beat the champion on every " +
                   "graded expectation."), ""]
    else:
        for rec in recommendations:
            lines.append(f"- **{rec['tier']}**: `{rec['champion']}` → `{rec['model']}`")
            delta = rec.get("usage_delta") or {}
            if delta.get("summary"):
                lines.append(f"  - {delta['summary']}")
            policy = rec.get("quota_policy") or {}
            if policy.get("decision"):
                lines.append(f"  - standing spend policy: **{policy['decision']}** — "
                             f"{policy.get('reason', '')}")
        if any((r.get("usage_delta") or {}).get("direction") in (USAGE_UP, USAGE_UNKNOWN)
               for r in recommendations):
            held = [r for r in recommendations
                    if (r.get("quota_policy") or {}).get("decision") == POLICY_HOLD]
            lines += ["",
                      ("At least one recommendation above raises this stack's Ollama Cloud quota "
                       "class (or its level could not be read). A quality win is not on its own an "
                       "adoption — the standing policy is that a usage-level increase is adoptable "
                       f"only on {', '.join(f'`{t}`' for t in QUOTA_INCREASE_TIERS)} and only on a "
                       "strict judge-rate win (see `docs/model-benchmarks/README.md`).")]
            if held:
                lines.append("**{} held** by that policy: {} — take these to the owner rather than "
                             "shipping them.".format(
                                 len(held),
                                 ", ".join(f"`{r['model']}` on {r['tier']}" for r in held)))
        lines += ["",
                  ("These are recommendations, not changes. `.litellm/model_upgrades.yaml` is the " +
                   "RETIREMENT map and auto-swaps into the live config, so adopting one of these is " +
                   "a deliberate edit (config change or a #717-style PR):"),
                  "", "```yaml"] + recommendation_mapping_lines(recommendations) + ["```", ""]

    lines += ["---", "",
              "Generated by `scripts/benchmark_models.py` (issue #721). Deterministic checks are "
              "the in-repo linters (`slop_lint`, the comment quality contract, the "
              "`LEMComplexityRouter` tier contract); judge scores come from "
              f"`{run.get('scoring_mode')}`."]
    return "\n".join(lines) + "\n"


def leaderboard_rows(run: dict) -> list:
    """One row per scorecard, newest-run-first when merged into the rolling table."""
    verdicts = {(g["tier"], g["model"]): g["verdict"] for g in run.get("gates") or []}
    rows = []
    for card in run.get("scorecards") or []:
        rows.append({
            "date": run.get("date"), "run_id": run.get("run_id"), "tier": card["tier"],
            "model": card["model"], "role": card["role"],
            "contract": _fmt_rate(card.get("contract_pass_rate")),
            "deterministic": _fmt_rate(card.get("deterministic_pass_rate")),
            "judge": _fmt_rate(card.get("judge_pass_rate")),
            "latency": _fmt_ms(card.get("latency_p50_ms")),
            "verdict": verdicts.get((card["tier"], card["model"]),
                                    "baseline" if card["role"] == "champion" else "—"),
        })
    return rows


def _row_markdown(row: dict) -> str:
    return (f"| {row['date']} | `{row['run_id']}` | {row['tier']} | `{row['model']}` | "
            f"{row['role']} | {row.get('contract', 'n/a')} | {row['deterministic']} | "
            f"{row['judge']} | {row['latency']} | {row['verdict']} |")


def _row_key(row: dict) -> tuple:
    return (row.get("run_id"), row.get("tier"), row.get("model"))


def _parse_row(line: str) -> Optional[dict]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    legacy = len(cells) == len(LEADERBOARD_LEGACY_COLUMNS)
    if len(cells) != len(LEADERBOARD_COLUMNS) and not legacy:
        return None
    # The table's own header and divider are pipe-delimited rows of the right width. Reading them
    # back as DATA is how a rolling table quietly grows two junk rows per render and evicts real
    # history at the row cap, so they are recognised and dropped here rather than re-emitted.
    if [c.strip("`") for c in cells] in (list(LEADERBOARD_COLUMNS),
                                         list(LEADERBOARD_LEGACY_COLUMNS)):
        return None
    if all(c and set(c.strip("`")) <= {"-", ":"} for c in cells):
        return None
    # A pre-#910 row has no contract rate. It renders `n/a` rather than borrowing the first-draft
    # number: those runs measured one rate, and printing it twice would invent a measurement.
    contract = "n/a" if legacy else cells[5]
    rest = cells[5:] if legacy else cells[6:]
    return {"date": cells[0], "run_id": cells[1].strip("`"), "tier": cells[2],
            "model": cells[3].strip("`"), "role": cells[4], "contract": contract,
            "deterministic": rest[0], "judge": rest[1], "latency": rest[2], "verdict": rest[3]}


def update_leaderboard(text: str, rows: list) -> str:
    """Merge this run's rows into the rolling table between the leaderboard markers.

    Re-running the same run id REPLACES its rows rather than appending duplicates, so a re-render
    (a failed PR retried, a `--render` of an older results file) is idempotent."""
    header = [LEADERBOARD_HEADER, LEADERBOARD_DIVIDER]
    body = str(text or "")
    if LEADERBOARD_BEGIN in body and LEADERBOARD_END in body:
        before, rest = body.split(LEADERBOARD_BEGIN, 1)
        inner, after = rest.split(LEADERBOARD_END, 1)
    else:
        before, inner, after = body.rstrip() + "\n\n", "", "\n"

    existing = [r for r in (_parse_row(line) for line in inner.splitlines()
                            if line.strip().startswith("|")) if r]
    fresh_keys = {_row_key(r) for r in rows}
    kept = [r for r in existing if _row_key(r) not in fresh_keys]
    merged = (list(rows) + kept)[:LEADERBOARD_MAX_ROWS]
    table = "\n".join(header + [_row_markdown(r) for r in merged])
    return f"{before}{LEADERBOARD_BEGIN}\n{table}\n{LEADERBOARD_END}{after}"


# ─────────────────────────── judge planning (pure) ───────────────────────────────

def judgeable_cases(case_results: list, suite: dict) -> list:
    """Which cases are worth a judge call at all.

    A case that already failed deterministically is skipped: the verdict is settled and the rubric
    would only re-confirm it at provider prices. Cases opted out with `"judge": false` are skipped
    too (the router suite's classification cases have nothing for a rubric to grade), and they are
    NOT "dropped by the cap" — keeping the two apart is what makes the cap warning meaningful."""
    by_id = {str(c.get("id")): c for c in suite.get("cases") or []}
    eligible = []
    for result in case_results:
        case = by_id.get(result["case_id"]) or {}
        if case.get("judge") is False or result.get("error"):
            continue
        if not result.get("passes"):
            continue
        eligible.append(result["case_id"])
    return eligible


def plan_judge_calls(case_results: list, suite: dict, cap: int) -> list:
    """The judgeable cases that fit inside the remaining budget, in suite order."""
    return judgeable_cases(case_results, suite)[:max(0, int(cap))]


def judge_messages(suite: dict, case: dict, output: str) -> list:
    """A SELF-CONTAINED rubric prompt. The judge (PostHog's or the fallback) only ever sees the
    event's input and output, so the tier contract is embedded here rather than referenced by repo
    path — a rubric that says "see content_framework.py" grades nothing."""
    rubric = suite.get("judge_rubric") or (
        "Judge whether the answer follows its instructions exactly, reads like a specific human "
        "practitioner rather than generic AI filler, and invents no facts, numbers or names that "
        "were not supplied in the prompt.")
    prompt = (
        f"You are grading one model answer against the contract for the '{suite.get('tier')}' "
        f"tier of a LinkedIn content system.\n\n"
        f"TIER CONTRACT: {suite.get('contract')}\n\n"
        f"RUBRIC: {rubric}\n\n"
        f"PROMPT GIVEN TO THE MODEL:\n---\n"
        + "\n".join(f"[{m.get('role')}] {m.get('content')}" for m in case.get("messages") or [])
        + f"\n---\n\nMODEL ANSWER:\n---\n{output}\n---\n\n"
        "Respond with ONLY a JSON object: {\"pass\": true|false, \"reasoning\": \"one sentence\"}. "
        "Fail the answer if it breaks the contract, adds a preamble, or states a specific fact or "
        "number that the prompt did not supply.")
    return [{"role": "system", "content": "You are a strict, terse evaluation judge. You reply "
                                          "with JSON and nothing else."},
            {"role": "user", "content": prompt}]


def parse_judge_verdict(text: Optional[str]) -> dict:
    """Parse a judge reply. An unparseable verdict is NOT a pass and NOT a fail — it is unscored,
    which is what keeps a flaky judge from silently approving a model."""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {"passes": None, "status": JUDGE_TIMEOUT, "reasoning": "unparseable judge reply"}
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            return {"passes": None, "status": JUDGE_TIMEOUT, "reasoning": "unparseable judge reply"}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("pass"), bool):
        return {"passes": None, "status": JUDGE_TIMEOUT, "reasoning": "judge reply had no verdict"}
    return {"passes": bool(parsed["pass"]), "status": "scored",
            "reasoning": str(parsed.get("reasoning") or "")[:280]}


# ───────────────── PostHog evaluation specs + retrieval (pure) ───────────────────

def evaluation_name(tier: str) -> str:
    return f"LEM benchmark — {tier}"


def evaluation_spec(suite: dict) -> dict:
    """The create-if-missing payload for one tier's LLM-judge evaluation.

    `conditions` is the load-bearing part: the filter pins scoring to events that carry a
    `benchmark_run_id`, at 100% sampling. Production `$ai_generation` events never carry that
    property, so an enabled evaluation can never bill a judge call against customer traffic."""
    tier = suite.get("tier")
    return {
        "name": evaluation_name(tier),
        "description": f"Model-tier benchmark judge for {tier} (issue #721). Scores only events "
                       "tagged with a benchmark_run_id — never production traffic.",
        "enabled": True,
        "evaluation_type": "llm_judge",
        "prompt": (f"TIER CONTRACT: {suite.get('contract')}\n\nRUBRIC: {suite.get('judge_rubric')}"
                   "\n\nPass the generation only if it satisfies the contract, adds no preamble, "
                   "and states no specific fact or number the prompt did not supply."),
        "conditions": [{
            "rollout_percentage": 100,
            "properties": [
                {"key": "benchmark_run_id", "operator": "is_set", "type": "event"},
                {"key": "lem_tier", "value": [tier], "operator": "exact", "type": "event"},
            ],
        }],
    }


def plan_evaluations(existing: dict, specs: list) -> list:
    """create / none per tier evaluation. Named-keyed: an evaluation someone edited in the UI is
    still that evaluation, and this script never overwrites a human's rubric edit."""
    plan = []
    for spec in specs:
        found = (existing or {}).get(spec["name"])
        plan.append({"action": "none" if found else "create", "name": spec["name"],
                     "id": (found or {}).get("id"), "spec": spec})
    return plan


def hogql_for_run(run_id: str, limit: int = 500) -> str:
    """The retrieval query for one run's judge verdicts. Scoped to `$ai_evaluation` events carrying
    THIS run's id — the same provenance rule `assert_benchmark_only` enforces on the way out."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(run_id or ""))
    return ("SELECT properties.benchmark_prompt_id, properties.lem_tier, properties.$ai_model, "
            "properties.$ai_evaluation_result, properties.$ai_evaluation_reasoning "
            "FROM events WHERE event = '$ai_evaluation' "
            f"AND properties.benchmark_run_id = '{safe}' "
            f"ORDER BY timestamp DESC LIMIT {int(limit)}")


def merge_judge_events(rows: list, model: str, tier: str) -> dict:
    """Fold `$ai_evaluation` rows into {case_id: verdict} for one model × tier.

    Rows for another model or tier are ignored rather than merged — one HogQL read serves the whole
    run, and mixing a candidate's verdicts into the champion's card would invert the gate."""
    verdicts: dict = {}
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            continue
        case_id, row_tier, row_model, result = row[0], row[1], row[2], row[3]
        reasoning = row[4] if len(row) > 4 else ""
        if row_model != model or row_tier != tier or not case_id:
            continue
        if isinstance(result, bool):
            passes = result
        elif isinstance(result, str):
            lowered = result.strip().lower()
            if lowered in ("true", "pass", "passed", "1"):
                passes = True
            elif lowered in ("false", "fail", "failed", "0"):
                passes = False
            else:
                continue
        else:
            continue
        verdicts[str(case_id)] = {"passes": passes, "status": "scored",
                                  "reasoning": str(reasoning or "")[:280]}
    return verdicts


def benchmark_event_properties(run_id: str, tier: str, case_id: str, model: str,
                               role: str, latency_ms: Optional[float] = None,
                               usage: Optional[dict] = None) -> dict:
    """The property bag on every emitted `$ai_generation`. `benchmark_run_id` is what the PostHog
    evaluation's condition filters on AND what `assert_benchmark_only` verifies, so it is never
    optional."""
    usage = usage or {}
    props = {
        "benchmark_run_id": run_id,
        "benchmark_prompt_id": case_id,
        "lem_tier": tier,
        "lem_benchmark_role": role,
        "source": BENCHMARK_SOURCE,
        "$ai_model": model,
        "$ai_provider": "ollama_cloud",
        "$ai_trace_id": f"{run_id}-{tier}-{case_id}-{role}",
    }
    if latency_ms is not None:
        props["$ai_latency"] = round(float(latency_ms) / 1000.0, 3)
    for key, prop in (("prompt_tokens", "$ai_input_tokens"),
                      ("completion_tokens", "$ai_output_tokens")):
        if usage.get(key) is not None:
            props[prop] = usage[key]
    return props


# ─────────────────────────── champions from config (pure) ────────────────────────

def champions_from_config(config_text: str, tiers: list) -> dict:
    """The model currently serving each tier — its FIRST Ollama Cloud deployment, which is the one
    latency-based routing reaches for first and the one a candidate would displace. Tiers whose
    primary is an OpenAI/Anthropic fallback have no Ollama champion and are reported as such."""
    from model_health_check import load_config_text, parse_deployments  # noqa: WPS433
    deployments = parse_deployments(load_config_text(config_text))
    champions: dict = {}
    for row in deployments:
        if row["is_ollama"] and row["group"] in tiers and row["group"] not in champions:
            champions[row["group"]] = row["bare"]
    return champions


def parse_champion_overrides(raw: Optional[str]) -> dict:
    out: dict = {}
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        tier, _, model = chunk.partition("=")
        if not model.strip():
            raise SystemExit(f"--champions expects tier=model pairs, got {chunk!r}")
        out[tier.strip()] = model.strip()
    return out


def parse_models(raw: Optional[str]) -> list:
    return [m.strip() for m in str(raw or "").split(",") if m.strip()]


# ─────────────────────────────── I/O (mocked in tests) ───────────────────────────

class ProviderClient:
    """Completions straight against Ollama Cloud.

    Deliberately NOT through the LiteLLM proxy: the proxy routes by tier ALIAS, and a candidate that
    isn't in the config has no alias to be reached by. Champions go the same way so both sides of a
    comparison are measured on identical plumbing."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 180.0) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self._client = None

    def _openai(self):
        if self._client is None:
            import openai
            self._client = openai.OpenAI(base_url=self.base_url, api_key=self.api_key,
                                         timeout=self.timeout)
        return self._client

    def complete(self, model: str, messages: list, params: Optional[dict] = None, *,
                 allow_budget_escalation: bool = True) -> dict:
        """One case's completion.

        `allow_budget_escalation=False` is for a case whose `max_tokens` deliberately MIRRORS a
        production call site (#910): `lem-simple`'s relevance check is `max_tokens=3` because
        `ai_helper.py`'s is, and a model that cannot answer inside that budget is disqualified at
        that call site rather than merely truncated. Doubling it there would score an answer LEM
        could never actually run. The empty-completion re-measurement still applies — it re-asks at
        the SAME budget, so it measures variance rather than buying headroom."""
        call_params = dict(params or {})
        budget = call_params.get("max_tokens")
        bounded = isinstance(budget, int) and budget > 0
        escalations = 0
        repeats = 0
        budget_locked = False
        allowed = truncation_retries() if bounded and allow_budget_escalation else 0
        while True:
            # Timed PER ATTEMPT, not across the retries. A discarded attempt is harness waste that
            # production never pays (long-form calls set no `max_tokens` at all), so charging its
            # wall-clock to the model would inflate p50/p90 for exactly the reasoning models this
            # retry exists to measure — and those numbers go into the rolling leaderboard forever.
            started = time.time()
            try:
                response = self._openai().chat.completions.create(
                    model=model, messages=messages, **call_params)
            except Exception as exc:  # noqa: BLE001 - a provider failure is a case result, not a crash
                return {"text": None, "error": str(exc)[:200], "budget_escalations": escalations,
                        "repeats": repeats, "budget_locked": budget_locked,
                        "latency_ms": (time.time() - started) * 1000.0, "usage": {}}
            choice = response.choices[0]
            text = (choice.message.content or "").strip()
            truncated = getattr(choice, "finish_reason", None) == "length"
            # Only an EMPTY reply is retried. A truncated-but-present answer is the model's real
            # output at this budget, and re-rolling it would quietly hand the verbose models a
            # second chance the concise ones never needed.
            if text:
                break
            if truncated and escalations < allowed:
                escalations += 1
                call_params["max_tokens"] = budget * (2 ** escalations)
                print(f"  {model} spent its whole {budget * (2 ** (escalations - 1))}-token budget "
                      f"before answering — retrying at {call_params['max_tokens']}",
                      file=sys.stderr)
                continue
            # Recorded only if the case ENDS with no answer: a production-budget case whose repeat
            # answered was measured fine, and naming it would read as a failure it did not have.
            budget_locked = bool(truncated and bounded and not allow_budget_escalation)
            if repeats >= empty_repeats():
                break
            repeats += 1
            print(f"  {model} returned an empty answer — re-measuring at the same budget "
                  f"({repeats}/{empty_repeats()})", file=sys.stderr)
        usage = getattr(response, "usage", None)
        return {
            # An answer that never arrived is NO OUTPUT, not a zero-length answer: grading "" would
            # render as `min_chars: 0 chars < 700`, which reads as a quality verdict on a model that
            # was never measured.
            "text": text or None,
            "error": None if text else (
                f"empty completion after {escalations} budget escalation(s) and {repeats} "
                f"re-measurement(s)"
                + (" (this case's max_tokens mirrors a production call site, so the budget was "
                   "never grown)" if budget_locked else "")),
            "budget_escalations": escalations,
            "repeats": repeats,
            "budget_locked": bool(budget_locked and not text),
            "latency_ms": (time.time() - started) * 1000.0,
            "usage": {"prompt_tokens": getattr(usage, "prompt_tokens", None),
                      "completion_tokens": getattr(usage, "completion_tokens", None),
                      "total_tokens": getattr(usage, "total_tokens", None)},
        }


class PostHogEvals:
    """The PostHog LLM-analytics surface this harness needs: emit tagged generations, ensure the
    per-tier judge evaluations exist, trigger them per event, and read the verdicts back.

    Every method degrades: no personal API key, an unreachable project or a 4xx leaves the run on
    the in-runner fallback judge rather than failing it."""

    def __init__(self, api_key: str, project_id: str, app_host: str = DEFAULT_APP_HOST) -> None:
        self.project_id = project_id
        self.app_host = app_host.rstrip("/")
        self._base = f"{self.app_host}/api/projects/{project_id}"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        import requests
        response = requests.request(method, f"{self._base}{path}", headers=self._headers,
                                    timeout=30, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def list_evaluations(self) -> dict:
        payload = self._request("GET", "/llm_analytics/evaluations/?limit=100")
        return {item.get("name"): {"id": item.get("id"), "enabled": item.get("enabled")}
                for item in payload.get("results") or [] if item.get("name")}

    def create_evaluation(self, spec: dict) -> Optional[str]:
        return self._request("POST", "/llm_analytics/evaluations/", json=spec).get("id")

    def run_evaluation(self, evaluation_id: str, event_id: str) -> Optional[str]:
        payload = self._request("POST", f"/llm_analytics/evaluations/{evaluation_id}/run/",
                                json={"event_id": event_id})
        return payload.get("workflow_id") or payload.get("id")

    def query(self, hogql: str) -> list:
        payload = self._request("POST", "/query/",
                                json={"query": {"kind": "HogQLQuery", "query": hogql}})
        return payload.get("results") or []


def emit_generation(run_id: str, tier: str, case: dict, model: str, role: str,
                    completion: dict) -> Optional[str]:
    """Emit ONE tagged `$ai_generation`. Independent of the prod LiteLLM→PostHog callback (#647) on
    purpose: this harness calls the provider directly, so nothing else would record these.

    Returns the event uuid (what the manual eval run is keyed by), or None if PostHog is not
    configured — in which case the run falls back to the in-runner judge."""
    try:
        import posthog
        import uuid
    except Exception:  # noqa: BLE001 - pragma: no cover - stdlib+dep import guard
        return None
    if not (os.environ.get("POSTHOG_API_KEY") or "").strip():
        return None
    event_id = str(uuid.uuid4())
    properties = benchmark_event_properties(run_id, tier, str(case.get("id")), model, role,
                                            completion.get("latency_ms"),
                                            completion.get("usage"))
    properties["$ai_input"] = case.get("messages")
    properties["$ai_output_choices"] = [{"role": "assistant",
                                         "content": completion.get("text") or ""}]
    try:
        posthog.api_key = os.environ["POSTHOG_API_KEY"]
        posthog.host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
        # The event uuid is what the manual eval-run endpoint is keyed by, so an SDK that won't
        # accept one leaves this generation UNJUDGEABLE — say so by returning None (the run then
        # uses the fallback judge) rather than handing back an id PostHog never stored.
        try:
            posthog.capture(distinct_id=BENCHMARK_DISTINCT_ID, event="$ai_generation",
                            properties=properties, uuid=event_id)
        except TypeError:
            posthog.capture(distinct_id=BENCHMARK_DISTINCT_ID, event="$ai_generation",
                            properties=properties)
            return None
        return event_id
    except Exception as exc:  # noqa: BLE001 - emission is best-effort
        print(f"  ! PostHog emission failed for {case.get('id')}: {str(exc)[:160]}",
              file=sys.stderr)
        return None


def emit_metric(run_id: str, tier: str, case_id: str, model: str, verdict: dict) -> None:
    """Fallback-mode judge verdicts still reach PostHog — as `$ai_metric`, so the dashboards work
    even when the native evaluation path was unavailable."""
    try:
        import posthog
    except Exception:  # noqa: BLE001 - pragma: no cover
        return
    if not (os.environ.get("POSTHOG_API_KEY") or "").strip():
        return
    try:
        posthog.api_key = os.environ["POSTHOG_API_KEY"]
        posthog.host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
        posthog.capture(distinct_id=BENCHMARK_DISTINCT_ID, event="$ai_metric", properties={
            "benchmark_run_id": run_id, "benchmark_prompt_id": case_id, "lem_tier": tier,
            "source": BENCHMARK_SOURCE, "$ai_model": model,
            "$ai_metric_name": "benchmark_judge",
            "$ai_metric_value": verdict.get("passes"),
        })
    except Exception as exc:  # noqa: BLE001
        print(f"  ! PostHog metric emission failed for {case_id}: {str(exc)[:160]}",
              file=sys.stderr)


def fallback_judge(suite: dict, case: dict, output: str) -> dict:
    """The degraded judge: `lem-medium` through LEM's own client. Used when PostHog has no judge
    provider configured (Ollama Cloud is not one) or the evaluation API is unreachable.

    Reports `judge_calls` — the number of completions this verdict actually cost. A truncation retry
    is a real billed call, so the run's cap has to count it; counting one verdict as one call would
    let `BENCHMARK_MAX_JUDGE_CALLS` be overrun by the retry factor without ever saying so."""
    calls = 0
    try:
        from cqc_lem.utilities.ai.client import client
        budget = judge_max_tokens()
        for attempt in range(truncation_retries() + 1):
            calls += 1
            response = client.chat.completions.create(
                model="lem-medium", messages=judge_messages(suite, case, output),
                temperature=0, max_tokens=budget * (2 ** attempt))
            choice = response.choices[0]
            text = str(choice.message.content or "").strip()
            if text or getattr(choice, "finish_reason", None) != "length":
                return dict(parse_judge_verdict(text), judge_calls=calls)
        # Unscored, but NOT `judge:unavailable` — that flag stops the runner asking for the rest of
        # the run, and a judge that reasoned past its budget on ONE answer is still reachable.
        return {"passes": None, "status": JUDGE_TIMEOUT, "judge_calls": calls,
                "reasoning": "judge reasoned past its token budget before reaching a verdict"}
    except Exception as exc:  # noqa: BLE001 - an unavailable judge is unscored, never a pass
        return {"passes": None, "status": JUDGE_UNAVAILABLE, "judge_calls": max(calls, 1),
                "reasoning": f"fallback judge unavailable: {str(exc)[:120]}"}


def flush_events() -> None:
    """Drain posthog-python's background batch queue.

    The manual eval-run endpoint is keyed by the generation's event uuid, so triggering it while
    that event is still sitting in the client's queue asks PostHog to score something it has never
    seen. Best-effort: a client that was never configured has nothing to flush."""
    try:
        import posthog
        posthog.flush()
    except Exception:  # noqa: BLE001 - flushing is best-effort, never a run failure
        pass


def poll_judge_results(evals: "PostHogEvals", run_id: str, expected: int,
                       timeout_seconds: int, sleep: Callable[[float], None] = time.sleep) -> list:
    """Scoring is async (seconds). Poll HogQL until every expected verdict has landed or the budget
    runs out — whatever is missing at that point is reported as `judge:timeout`, never inferred."""
    deadline = time.time() + max(1, timeout_seconds)
    rows: list = []
    while True:
        try:
            rows = evals.query(hogql_for_run(run_id))
        except Exception as exc:  # noqa: BLE001
            print(f"  ! judge result query failed: {str(exc)[:160]}", file=sys.stderr)
            return rows
        if len(rows) >= expected or time.time() >= deadline:
            return rows
        sleep(5)


# ───────────────────────────────── orchestration ─────────────────────────────────

def _case_by_id(suite: dict, case_id: str) -> dict:
    return next((c for c in suite.get("cases") or [] if str(c.get("id")) == case_id), {})


def _canned(case: dict) -> dict:
    return case.get("canned") if isinstance(case.get("canned"), dict) else {}


def run_benchmark(suites: dict, models: list, champions: dict, *, run_id: str, today: str,
                  provider: Optional[ProviderClient] = None,
                  evals: Optional["PostHogEvals"] = None,
                  judge_cap: int = 0, dry_run: bool = False,
                  judge_enabled: bool = True,
                  usage_levels: Optional[dict] = None) -> dict:
    """Run every (model, tier) pair and merge the results into one run document.

    `dry_run` scores each suite against the case's committed `canned` output and verdict: a full
    report with zero network calls, labelled as such so canned scores can never be mistaken for
    measurements."""
    targets: list = []
    for tier in sorted(suites):
        champion = champions.get(tier)
        if champion:
            targets.append((tier, champion, "champion"))
        for model in models:
            if model == champion:
                # Benchmarking a model against ITSELF ties every expectation, which the gate reads
                # as meets-or-beats and would emit as a `X -> X` swap recommendation straight into
                # the retirement-map block. It is not a comparison; skip the tier.
                print(f"  {model} already serves {tier} — skipping it as a candidate there",
                      file=sys.stderr)
                continue
            targets.append((tier, model, "candidate"))

    scoring_mode = SCORING_DRY_RUN if dry_run else (
        SCORING_DETERMINISTIC if not judge_enabled else
        SCORING_POSTHOG if evals is not None else SCORING_FALLBACK)

    evaluation_ids: dict = {}
    if evals is not None and judge_enabled and not dry_run:
        try:
            existing = evals.list_evaluations()
            for tier, suite in sorted(suites.items()):
                spec = evaluation_spec(suite)
                decision = plan_evaluations(existing, [spec])[0]
                evaluation_ids[tier] = (decision["id"] if decision["action"] == "none"
                                        else evals.create_evaluation(spec))
        except Exception as exc:  # noqa: BLE001 - degrade to the fallback judge
            print(f"  ! PostHog evaluations unavailable ({str(exc)[:160]}) — "
                  "falling back to the in-runner judge", file=sys.stderr)
            evals, evaluation_ids = None, {}
            scoring_mode = SCORING_FALLBACK

    scorecards: list = []
    judge_budget = max(0, int(judge_cap))
    judge_spent = 0
    fallback_down = False  # the in-runner judge answered "unreachable" — stop asking it
    pending: list = []  # (tier, model, role, case_id, event_id) awaiting PostHog verdicts
    awaiting: list = []  # (card, suite, outputs, judge_results) whose verdicts PostHog still owes

    for tier, model, role in targets:
        suite = suites[tier]
        outputs: dict = {}
        timings: dict = {}
        errors: dict = {}
        for case in suite["cases"]:
            case_id = str(case.get("id"))
            if dry_run:
                completion = {"text": _canned(case).get("output"), "error": None,
                              "latency_ms": _canned(case).get("latency_ms"), "usage": {}}
            elif provider is None:
                completion = {"text": None, "error": "no provider client configured",
                              "latency_ms": None, "usage": {}}
            else:
                completion = provider.complete(
                    model, case["messages"], case.get("params"),
                    allow_budget_escalation=not case.get("budget_mirrors_production"))
            outputs[case_id] = completion.get("text")
            errors[case_id] = completion.get("error")
            timings[case_id] = {"latency_ms": completion.get("latency_ms"),
                                "total_tokens": (completion.get("usage") or {}).get("total_tokens"),
                                "budget_escalations": completion.get("budget_escalations") or 0,
                                "repeats": completion.get("repeats") or 0,
                                "budget_locked": bool(completion.get("budget_locked"))}
            if not dry_run and completion.get("text") and evals is not None:
                event_id = emit_generation(run_id, tier, case, model, role, completion)
                if event_id:
                    pending.append([tier, model, role, case_id, event_id])

        case_results = score_suite(suite, outputs)
        for result in case_results:
            if errors.get(result["case_id"]):
                result["error"] = errors[result["case_id"]]

        judge_results: dict = {}
        if judge_enabled:
            if evals is not None and not dry_run:
                flush_events()
            planned = plan_judge_calls(case_results, suite, judge_budget - judge_spent)
            skipped = len(judgeable_cases(case_results, suite)) - len(planned)
            if skipped > 0:
                print(f"  judge cap reached — {skipped} case(s) unscored for {model}/{tier}",
                      file=sys.stderr)
            for case_id in planned:
                # `planned` was sized one call per case, but a truncation-retried verdict costs
                # more than one — so the cap is re-checked here rather than only between models.
                if judge_spent >= judge_budget:
                    print(f"  judge cap reached mid-suite — {model}/{tier} stopped at "
                          f"{judge_spent} call(s)", file=sys.stderr)
                    break
                case = _case_by_id(suite, case_id)
                if dry_run:
                    canned = _canned(case)
                    judge_results[case_id] = (
                        {"passes": bool(canned["judge_pass"]), "status": "scored",
                         "reasoning": str(canned.get("judge_reasoning") or "")}
                        if isinstance(canned.get("judge_pass"), bool)
                        else {"passes": None, "status": JUDGE_TIMEOUT,
                              "reasoning": "no canned judge verdict"})
                elif evals is not None:
                    event = next((p for p in pending
                                  if p[0] == tier and p[1] == model and p[3] == case_id), None)
                    if event and evaluation_ids.get(tier):
                        try:
                            evals.run_evaluation(evaluation_ids[tier], event[4])
                        except Exception as exc:  # noqa: BLE001
                            print(f"  ! eval trigger failed for {case_id}: {str(exc)[:120]}",
                                  file=sys.stderr)
                    judge_results[case_id] = {"passes": None, "status": JUDGE_TIMEOUT,
                                              "reasoning": "awaiting PostHog verdict"}
                elif fallback_down:
                    # The judge answered the FIRST case with "I am not reachable". Re-asking it
                    # forty more times costs the openai client's retry budget per case and cannot
                    # produce a verdict, so the rest of the run is unscored deliberately.
                    judge_results[case_id] = {"passes": None, "status": JUDGE_UNAVAILABLE,
                                              "reasoning": "fallback judge unavailable"}
                    continue
                else:
                    verdict = fallback_judge(suite, case, outputs.get(case_id) or "")
                    judge_results[case_id] = verdict
                    if verdict.get("status") == JUDGE_UNAVAILABLE:
                        fallback_down = True
                        print(f"  ! in-runner judge unreachable ({verdict.get('reasoning')}) — "
                              "the remaining cases are reported unscored", file=sys.stderr)
                    else:
                        emit_metric(run_id, tier, case_id, model, verdict)
                judge_spent += int((judge_results.get(case_id) or {}).get("judge_calls") or 1)

        card = merge_scorecard(tier, model, role, case_results, judge_results, timings)
        card["benchmark_run_id"] = run_id
        card["usage"] = dict((usage_levels or {}).get(model) or {"level": None, "label": None})
        scorecards.append(card)
        if evals is not None and judge_enabled and not dry_run and judge_results:
            awaiting.append((card, suite, outputs, judge_results))

    if awaiting and judge_spent:
        rows = poll_judge_results(evals, run_id, judge_spent, judge_timeout_seconds())
        posthog_scored = fallback_used = False
        for card, suite, outputs, judge_results in awaiting:
            # UPDATE, never replace: a case PostHog never scored has to stay in the dict as a
            # timeout. Rebuilding the card from the returned rows alone drops it entirely, and a
            # partial read would then render as a full-marks judge pass on a cherry-picked subset.
            judge_results.update(merge_judge_events(rows, card["model"], card["tier"]))
            posthog_scored = posthog_scored or any(v.get("passes") is not None
                                                   for v in judge_results.values())
            # PostHog accepted the events but scored none of them — the documented degraded case
            # (its judge needs a provider key of its own; it cannot judge via Ollama Cloud). Spend
            # what is left of the cap on the in-runner judge rather than reporting a run whose
            # every case is unscorable, which the gate can only ever read as no evidence at all.
            for case_id in sorted(judge_results):
                if judge_results[case_id].get("passes") is not None or fallback_down:
                    continue
                if judge_spent >= judge_budget:
                    break
                replacement = fallback_judge(suite, _case_by_id(suite, case_id),
                                             outputs.get(case_id) or "")
                judge_spent += int(replacement.get("judge_calls") or 1)
                if replacement.get("status") == JUDGE_UNAVAILABLE:
                    fallback_down = True
                if replacement.get("passes") is None:
                    continue
                judge_results[case_id] = replacement
                emit_metric(run_id, card["tier"], case_id, card["model"], replacement)
                fallback_used = True
            merged = merge_scorecard(card["tier"], card["model"], card["role"],
                                     card["case_results"], judge_results, {})
            for key in ("judged", "judge_passed", "judge_timeouts", "judge_pass_rate",
                        "judge_notes"):
                card[key] = merged[key]
        if fallback_used:
            scoring_mode = SCORING_MIXED if posthog_scored else SCORING_FALLBACK

    gates = gate_run(scorecards, suites)
    recommendations = swap_recommendations(gates)
    return {
        "source": BENCHMARK_SOURCE,
        "run_id": run_id,
        "date": today,
        "scoring_mode": scoring_mode,
        "tiers": sorted(suites),
        "candidates": list(models),
        "champions": {t: champions.get(t) for t in sorted(suites)},
        "usage_levels": dict(usage_levels or {}),
        "judge_calls": judge_spent,
        "judge_call_cap": judge_budget,
        "scorecards": scorecards,
        "gates": gates,
        "recommendations": recommendations,
    }


def write_report(run: dict, out_dir: str) -> str:
    """Render + write the per-run report and update the rolling leaderboard. Returns the report
    path. `render_report` runs the provenance guard first, so a tainted run never reaches disk."""
    body = render_report(run)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, report_filename(run))
    with open(path, "w") as f:
        f.write(body)
    readme = os.path.join(out_dir, "README.md")
    existing = _read_text(readme) if os.path.exists(readme) else ""
    with open(readme, "w") as f:
        f.write(update_leaderboard(existing, leaderboard_rows(run)))
    return path


# ─────────────────────────────────── CLI ─────────────────────────────────────────

def _run_id(today: str) -> str:
    return f"bm-{today.replace('-', '')}-{os.urandom(3).hex()}"


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--render", metavar="RESULTS")
    ap.add_argument("--print-suites", action="store_true")
    ap.add_argument("--models", default="")
    ap.add_argument("--champions", default="")
    ap.add_argument("--tiers", default="")
    ap.add_argument("--suite-dir", default=DEFAULT_SUITE_DIR)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--results-out", default=None)
    ap.add_argument("--recommendations-out", default=None)
    ap.add_argument("--usage-levels", default="")
    ap.add_argument("--no-usage-levels", action="store_true")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--max-judge-calls", type=int, default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--today", default=None)
    args = ap.parse_args(argv)

    today = args.today or _today()
    tiers = parse_models(args.tiers) or None

    try:
        suites = load_suites(args.suite_dir, tiers)
    except (SuiteError, ValueError) as exc:
        print(f"suite error: {exc}", file=sys.stderr)
        return 1

    if args.print_suites:
        for tier, suite in sorted(suites.items()):
            judged = sum(1 for c in suite["cases"] if c.get("judge") is not False)
            print(f"{tier}: {len(suite['cases'])} cases ({judged} judgeable), "
                  f"thresholds={suite['thresholds']}")
            for case in suite["cases"]:
                # `~` marks a check production REPAIRS rather than ships — those are outside the
                # absolute contract floor (#910), so a calibration review can see the split.
                kinds = ", ".join(
                    a["type"] + ("~" if assertion_production_class(a) == PRODUCTION_REPAIRABLE
                                 else "")
                    for a in case["assertions"])
                locked = " [production budget]" if case.get("budget_mirrors_production") else ""
                print(f"  - {case['id']}: {kinds}{locked}")
        return 0

    if args.render:
        run = json.loads(_read_text(args.render))
        try:
            path = write_report(run, args.out_dir)
        except ValueError as exc:
            print(f"refusing to render: {exc}", file=sys.stderr)
            return 1
        print(f"report -> {path}")
        return 2 if run.get("recommendations") else 0

    if not args.dry_run and not benchmark_enabled():
        print("BENCHMARK_ENABLED is not set — nothing to do (use --dry-run for an offline proof)")
        return 0

    models = parse_models(args.models)
    champions = parse_champion_overrides(args.champions)
    if not champions:
        try:
            champions = champions_from_config(_read_text(args.config), list(suites))
        except OSError as exc:
            print(f"could not read {args.config}: {exc}", file=sys.stderr)
            return 1

    provider = evals = None
    if not args.dry_run:
        base_url = os.environ.get("OLLAMA_CLOUD_URL", "")
        api_key = os.environ.get("OLLAMA_CLOUD_API_KEY", "")
        if not base_url or not api_key:
            print("OLLAMA_CLOUD_URL / OLLAMA_CLOUD_API_KEY must be set to run the benchmark",
                  file=sys.stderr)
            return 1
        provider = ProviderClient(base_url, api_key)
        personal_key = (os.environ.get("POSTHOG_PERSONAL_API_KEY") or "").strip()
        project_key = (os.environ.get("POSTHOG_API_KEY") or "").strip()
        # BOTH keys or neither: the personal key reads the evaluation API, the project key emits the
        # generations it scores. One without the other would score nothing and report every case as
        # a timeout, so it degrades to the in-runner judge instead — the documented fallback.
        if personal_key and project_key and not args.no_judge:
            evals = PostHogEvals(personal_key,
                                 os.environ.get("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID),
                                 os.environ.get("POSTHOG_APP_HOST", DEFAULT_APP_HOST))
        elif personal_key and not project_key and not args.no_judge:
            print("  POSTHOG_API_KEY is unset — PostHog evaluations cannot score events that were "
                  "never emitted; using the in-runner judge", file=sys.stderr)

    # Every model the run will score, champions included — a delta needs both sides.
    measured = sorted(set(models) | {m for m in champions.values() if m})
    fetch = None
    if not args.dry_run and not args.no_usage_levels:
        from model_health_check import fetch_usage_level  # noqa: WPS433 - sibling script
        fetch = fetch_usage_level
    usage_levels = collect_usage_levels(
        measured, fetch=fetch,
        overrides=parse_usage_overrides(args.usage_levels or usage_levels_env_default()))

    cap = args.max_judge_calls if args.max_judge_calls is not None else max_judge_calls()
    run = run_benchmark(suites, models, champions,
                        run_id=args.run_id or _run_id(today), today=today,
                        provider=provider, evals=evals, judge_cap=cap,
                        dry_run=bool(args.dry_run), judge_enabled=not args.no_judge,
                        usage_levels=usage_levels)

    if args.results_out:
        with open(args.results_out, "w") as f:
            json.dump(run, f, indent=2)
        print(f"results -> {args.results_out}")
    if args.recommendations_out:
        with open(args.recommendations_out, "w") as f:
            json.dump(run["recommendations"], f, indent=2)

    try:
        path = write_report(run, args.out_dir)
    except ValueError as exc:
        print(f"refusing to render: {exc}", file=sys.stderr)
        return 1
    print(f"report -> {path}")
    for rec in run["recommendations"]:
        print(f"RECOMMEND [{rec['tier']}] {rec['champion']} -> {rec['model']}")
    if not run["recommendations"]:
        print("no swap recommendations")
    return 2 if run["recommendations"] else 0


if __name__ == "__main__":
    sys.exit(main())
