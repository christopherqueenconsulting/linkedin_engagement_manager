"""Which lane runs this mode, and why — one decision, in one place, spoken to bash.

The routing lives here rather than in `lib/dispatch.sh` because the question changed. v1 could only
ask "has the Claude lane been failing?", so it discovered the ceiling by hitting it and its answer
was a health estimate. `spend.py` can ask "how much of the subscription is left?", which is a
different and better question — but only if something actually reads it, and until now nothing did.

The shape is deliberate:

* **The daemon probes, the action reads.** `probe_usage()` costs a real (small) `claude -p` run, so
  a per-dispatch probe would make the cost control a cost. The daemon refreshes a cache on its own
  timer; `decide()` here only ever reads it.
* **Unreadable is its own answer, never a lane.** If the meter cannot answer, this returns no
  override and says `probe_unavailable` — `lib/dispatch.sh`'s health heuristic then decides, exactly
  as it did before v2. That is the only fail-safe direction available: defaulting to Claude burns a
  subscription nobody measured, and defaulting to Ollama is the failure that prompted this module.
* **Every decision names its arm.** A surprising routing choice must be explainable after the fact
  from one log line, which is precisely what v1 could not do.

The owner's 50% weekly rule is a GLOBAL flip, not a preference: at or above it the Claude
subscription is not used at all until the window resets. Below it, the per-mode split applies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from . import route, spend

LOG = logging.getLogger("lemd.lane")

#: Modes that prefer Claude while there is weekly headroom. Implementation quality shows up most in
#: a fresh build (`start`), in acting on the owner's own words (`revise`), and in a conflict
#: resolution that has to understand both sides (`rebase`).
CLAUDE_PREFERRED = frozenset({"start", "revise", "rebase"})

#: Modes that are mechanical enough to run on the cheap lane by default. `depmajor` is NOT one of
#: them: its output is a judgement ("this major bump is safe for our usage") that arms auto-merge,
#: so it runs on Claude only — see `CLAUDE_ONLY`.
OLLAMA_ELIGIBLE = frozenset({"review", "phasefix", "docfix", "depfix"})

#: Modes that never route to Ollama, whatever the weekly headroom.
CLAUDE_ONLY = frozenset({"depmajor"})

#: `selfreview` is neither: it produces the merge gate's review evidence, so starving it does not
#: slow merges, it STOPS them (plan H4). It prefers Claude and accepts a BOUNDED wait, then falls
#: back to Ollama tier 3 with a marker loud enough to find in a log.
BOUNDED_WAIT_MODES = frozenset({"selfreview"})

#: The owner's rule, and v1's `LANE_CAPACITY_THRESHOLD`: at this much of the weekly window spent,
#: stop using the Claude subscription entirely until it resets.
WEEKLY_FLIP_PCT = 50.0


@dataclass(frozen=True)
class LaneChoice:
    """A routing decision, with the arm that produced it."""

    lane: str | None      # "claude" | "ollama" | None = no override, defer to lib/dispatch.sh
    reason: str
    tier: str | None = None
    marker: str = ""      # a loud note for the run log when quality was traded away
    model: str | None = None   # set only when the router ACTS; None leaves the launch line as is
    arm: str = ""         # the router's decision, set in shadow and active modes, empty when off

    @property
    def overrides(self) -> bool:
        """True when this decision should replace `dispatch_lane`'s."""
        return self.lane is not None


def decide(mode: str, usage: spend.Usage, *, paused_seconds: float = 0.0,
           usage_pause_minutes: int = 60, flip_pct: float = WEEKLY_FLIP_PCT,
           router: route.Context | None = None) -> LaneChoice:
    """Choose a lane for one run.

    A thin caller: the per-mode split below is the existing policy, and `route.decide` is consulted
    on top of it only when `LEM_ROUTER` is not `off`.

    Args:
        mode: The run mode.
        usage: A `/usage` reading, possibly unreadable.
        paused_seconds: How long the Claude lane has been paused, for the bounded wait.
        usage_pause_minutes: The configured pause length; the wait is bounded at 2x this.
        flip_pct: Weekly percentage at which the Claude subscription is taken out of rotation.
        router: The model router's context, or None for today's routing exactly.

    Returns:
        A `LaneChoice`. `lane=None` means "no opinion" — the caller keeps the health-based routing
        it already computed, which is the correct answer when the meter cannot answer.
    """
    choice = _policy(mode, usage, paused_seconds, usage_pause_minutes, flip_pct)
    if router is None or router.flag == "off":
        return choice
    return _apply_router(choice, router)


def _apply_router(choice: LaneChoice, ctx: route.Context) -> LaneChoice:
    """Log the router's decision, and act on it only where the flag says to.

    `shadow` never acts. `reviews` acts on review modes only. `full` acts on every mode. A Codex
    decision cannot act until the execution lane exists (phase 3), so it is logged and the existing
    path runs.
    """
    harness, model, reason = route.decide(ctx)
    LOG.info("router[%s] mode=%s %s", ctx.flag, ctx.inputs.mode, reason)
    acts = ctx.flag == "full" or (ctx.flag == "reviews" and ctx.inputs.mode in route.REVIEW_MODES)
    if not acts:
        return replace(choice, arm=f"shadow {reason}" if ctx.flag == "shadow" else f"not_acting {reason}")
    if harness == route.CODEX:
        return replace(choice, arm=f"{reason} codex_pending_phase3")
    if harness == route.CLAUDE:
        if choice.lane == "ollama" and choice.reason.startswith("weekly_flip") and "arm=label_" not in reason:
            # The meter took the subscription out of rotation. The router's quota cache is a separate
            # reading and may be unreadable (= 0% used), so it must not put a run back on Claude.
            return replace(choice, arm=f"{reason} overridden_by_weekly_flip")
        return LaneChoice(route.CLAUDE, reason, model=model, arm=reason)
    marker = route.degraded_marker(harness, model, ctx.inputs.mode) if "fell_back" in reason else ""
    return LaneChoice(route.OLLAMA, reason, tier=model, model=model, marker=marker, arm=reason)


def _policy(mode: str, usage: spend.Usage, paused_seconds: float,
            usage_pause_minutes: int, flip_pct: float) -> LaneChoice:
    """The per-mode lane policy that predates the router."""
    if mode in CLAUDE_ONLY:
        # Ahead of the probe and the weekly flip on purpose: "no opinion" would hand the run to the
        # health-based routing, and that can pick Ollama. These runs are rare (a few Dependabot
        # PRs a week), and a weaker verdict here is the one that arms auto-merge.
        return LaneChoice("claude", "claude_only_mode")

    if not usage.readable or usage.week_pct is None:
        # Say so rather than pick. The whole defect this module fixes was a lane being chosen for a
        # reason nobody could see afterwards.
        return LaneChoice(None, "probe_unavailable")

    week = usage.week_pct
    if week >= flip_pct:
        # The global flip. Not a preference and not per-mode: the subscription is out of rotation.
        return LaneChoice(
            "ollama", f"weekly_flip week={week:.0f}%>={flip_pct:.0f}% resets={usage.week_resets or 'unknown'}",
            tier="lem-agent-tier2",
        )

    if mode in CLAUDE_PREFERRED:
        return LaneChoice("claude", f"claude_preferred week={week:.0f}%")

    if mode in BOUNDED_WAIT_MODES:
        # Prefer Claude, but never wait forever for it. An unbounded wait here is a designed
        # livelock: no review evidence means no merges pipeline-wide for the whole pause.
        limit = 2 * usage_pause_minutes * 60
        if paused_seconds <= 0:
            return LaneChoice("claude", f"selfreview_prefers_claude week={week:.0f}%")
        if paused_seconds < limit:
            return LaneChoice(
                "claude", f"selfreview_waiting_for_claude ({paused_seconds:.0f}s of {limit}s)")
        return LaneChoice(
            "ollama",
            f"selfreview_wait_exhausted ({paused_seconds:.0f}s >= {limit}s) — tier3 fallback",
            tier="lem-agent-tier3",
            marker="REVIEW QUALITY DEGRADED: this adversarial review ran on Ollama tier 3 after the "
                   "Claude lane stayed paused past the bounded wait. Treat its verdict as weaker "
                   "evidence than a Claude review.",
        )

    if mode in OLLAMA_ELIGIBLE:
        return LaneChoice("ollama", f"ollama_eligible_mode week={week:.0f}%", tier="lem-agent-tier2")

    # Anything unmapped (fix, and any mode added later) keeps the existing health-based routing
    # rather than inheriting a guess from this table.
    return LaneChoice(None, f"no_preference_for_mode week={week:.0f}%")


def emit_env(choice: LaneChoice) -> str:
    """Render a decision as shell assignments for the dispatch action to eval.

    Deliberately not JSON: the consumer is a bash action, and a shell-quoting bug in a routing
    decision is a worse outcome than a slightly dull format.
    """
    lines = [f"LEM_LANE_REASON={_q(choice.reason)}"]
    if choice.lane:
        lines.append(f"LEM_LANE_OVERRIDE={_q(choice.lane)}")
        if choice.tier:
            lines.append(f"LEM_LANE_TIER={_q(choice.tier)}")
    if choice.model and choice.lane:
        lines.append(f"LEM_LANE_MODEL={_q(choice.model)}")
        lines.append(f"LEM_LANE_HARNESS={_q(choice.lane)}")
    if choice.arm:
        lines.append(f"LEM_ROUTE_ARM={_q(choice.arm)}")
    if choice.marker:
        lines.append(f"LEM_LANE_MARKER={_q(choice.marker)}")
    return "\n".join(lines)


def _q(value: str) -> str:
    """Single-quote a value for safe `eval` in bash."""
    return "'" + str(value).replace("'", "'\\''") + "'"
