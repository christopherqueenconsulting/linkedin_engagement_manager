"""Shared model-routing decision core — complexity scoring + cost-aware down-routing
(docs/cost-performance-margin-plan.md §D.1(1), issue #494).

STDLIB ONLY, and deliberately dependency-free. This module is imported from BOTH sides of a
container boundary:

* the app (`cqc_lem.utilities.routing_policy`) — builds/evaluates the policy and publishes it, and
* the LiteLLM proxy, where docker-compose mounts this exact file next to
  `.litellm/complexity_router.py` and the hook imports it as a bare `routing_policy`.

The LiteLLM image has no LEM package and no LEM dependencies, so NEVER import `cqc_lem.*`, a
third-party library, or anything with I/O here. One file, one decision — the router and the weekly
optimizer can never drift into disagreeing about which tier a call belongs in.

The policy itself is a JSON document in Redis (`lem:routing:policy`), written weekly by
`utilities/cost_routing.py` and read by the hook. Its shape:

    {
      "version": 1,
      "enabled": true,                      # master switch baked into the document
      "generated_at": "2026-07-25T12:00:00+00:00",
      "buckets": {
        "content:lem-complex": {
          "id": "content:lem-complex#2",     # bucket + generation — the A/B hash salt
          "feature": "content", "from_tier": "lem-complex", "to_tier": "lem-medium",
          "state": "experiment",             # experiment | adopted | rolled_back | hold
          "cohort_pct": 0.1,                 # share of users routed DOWN (the treatment arm)
          "generation": 2, "started_on": "2026-07-18", "cooldown_until": null
        }
      }
    }
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, Optional

# ───────────────────────────── vocabulary ─────────────────────────────

TIER_SIMPLE = "lem-simple"
TIER_MEDIUM = "lem-medium"
TIER_COMPLEX = "lem-complex"

# Cheapest → most expensive. Index order is what makes "never route UP" a one-line check.
TIER_ORDER = (TIER_SIMPLE, TIER_MEDIUM, TIER_COMPLEX)

STATE_EXPERIMENT = "experiment"
STATE_ADOPTED = "adopted"
STATE_ROLLED_BACK = "rolled_back"
STATE_HOLD = "hold"

# Only these two states move traffic. `rolled_back` and `hold` stay in the document (they carry the
# cooldown and the reason the optimizer needs next week) but route nothing.
ROUTING_STATES = frozenset({STATE_EXPERIMENT, STATE_ADOPTED})

ARM_TREATMENT = "treatment"
ARM_CONTROL = "control"

POLICY_VERSION = 1
POLICY_REDIS_KEY = "lem:routing:policy"

# Complexity signals — unchanged from the original latency-era router, just moved here so the
# decision is testable without a LiteLLM proxy.
SIMPLE_SIGNALS = ("refine", "shorten", "summarize briefly", "comma separated", "short list")
COMPLEX_SIGNALS = ("thought leadership", "viral post", "personal story", "industry news",
                   "buyer stage", "comprehensive", "step-by-step", "framework")


def cheaper_tier(tier: Optional[str]) -> Optional[str]:
    """The next tier down, or None at the bottom (or for a non-tier model)."""
    if tier not in TIER_ORDER:
        return None
    index = TIER_ORDER.index(tier)
    return TIER_ORDER[index - 1] if index > 0 else None


def bucket_key(feature: Optional[str], tier: Optional[str]) -> str:
    """Feature×tier is the optimization bucket: the same tier can be safe to down-route for comments
    and unsafe for long-form content, so they are never judged together."""
    return f"{feature or 'system'}:{tier}"


# ───────────────────────────── complexity tier ─────────────────────────────

def prompt_text(messages: Any) -> str:
    """Flatten a chat payload to lowercase text for signal matching. Non-string content (image
    parts) is skipped rather than stringified — it would add noise to the token estimate."""
    parts = []
    for message in messages or []:
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
    return " ".join(parts).lower()


def complexity_score(prompt: str) -> int:
    return (sum(1 for signal in COMPLEX_SIGNALS if signal in prompt)
            - sum(1 for signal in SIMPLE_SIGNALS if signal in prompt))


def complexity_tier(prompt: str) -> str:
    """The tier `lem-router` resolves to from prompt shape alone — the pre-cost baseline. Cost-aware
    down-routing is applied to this result, never in place of it, so a complex prompt still starts
    from the complex tier and only moves if the DATA says the cheaper tier holds up."""
    score = complexity_score(prompt)
    token_estimate = len(prompt.split())
    if score >= 2 or token_estimate > 800:
        return TIER_COMPLEX
    if score >= 1 or token_estimate > 300:
        return TIER_MEDIUM
    return TIER_SIMPLE


# ───────────────────────────── policy application ─────────────────────────────

def normalize_policy(policy: Optional[Mapping]) -> dict:
    """Coerce whatever came out of Redis into a policy dict, or `{}` when it is unusable. A policy
    written by a NEWER version is treated as unusable rather than best-guessed — routing fails open
    to complexity-only, which is always the safe direction."""
    if not isinstance(policy, Mapping):
        return {}
    try:
        version = int(policy.get("version") or 0)
    except (TypeError, ValueError):
        return {}
    if version != POLICY_VERSION:
        return {}
    buckets = policy.get("buckets")
    return {
        "version": version,
        "enabled": bool(policy.get("enabled")),
        "generated_at": policy.get("generated_at"),
        "buckets": {str(k): dict(v) for k, v in buckets.items()
                    if isinstance(v, Mapping)} if isinstance(buckets, Mapping) else {},
    }


def assign_arm(user_id: Optional[Any], bucket: Optional[Mapping]) -> str:
    """Which A/B arm a user falls in for this bucket. Deterministic on (bucket id, user), so a user
    keeps the same arm for the whole experiment — a user flipping arms mid-week would contaminate
    both sides of the comparison. The bucket id carries a generation counter, so re-running a
    previously rolled-back experiment reshuffles the cohort instead of re-testing the same users.

    Traffic with no user id (system/housekeeping calls) always lands in control: it cannot be
    attributed to a cohort, so it must not silently join the treatment.
    """
    if not isinstance(bucket, Mapping):
        return ARM_CONTROL
    try:
        pct = float(bucket.get("cohort_pct") or 0.0)
    except (TypeError, ValueError):
        return ARM_CONTROL
    if pct <= 0:
        return ARM_CONTROL
    if user_id is None or str(user_id).strip() == "":
        return ARM_CONTROL
    if pct >= 1:
        return ARM_TREATMENT
    salt = f"{bucket.get('id') or bucket_key(bucket.get('feature'), bucket.get('from_tier'))}|{user_id}"
    digest = hashlib.md5(salt.encode("utf-8")).hexdigest()  # noqa: S324 - cohort split, not a secret
    return ARM_TREATMENT if (int(digest[:8], 16) % 10_000) / 10_000.0 < pct else ARM_CONTROL


def resolve_tier(base_tier: Optional[str], feature: Optional[str], user_id: Optional[Any],
                 policy: Optional[Mapping]) -> dict:
    """Apply the cost-aware policy to a tier the complexity rules already chose.

    Returns `{"tier", "base_tier", "arm", "applied", "bucket"}`. Every failure mode — no policy, a
    disabled policy, an unknown bucket, a bucket that isn't routing, a control-arm user, a target
    that isn't strictly cheaper — resolves to the base tier untouched. Down-routing is the only
    thing this can do; it can never route a call UP or to a non-tier model.
    """
    result = {"tier": base_tier, "base_tier": base_tier, "arm": ARM_CONTROL,
              "applied": False, "bucket": None}
    if base_tier not in TIER_ORDER:
        return result
    policy = normalize_policy(policy)
    if not policy or not policy.get("enabled"):
        return result

    key = bucket_key(feature, base_tier)
    bucket = policy["buckets"].get(key)
    if not bucket or bucket.get("state") not in ROUTING_STATES:
        return result

    target = bucket.get("to_tier") or cheaper_tier(base_tier)
    if target not in TIER_ORDER or TIER_ORDER.index(target) >= TIER_ORDER.index(base_tier):
        return result

    result["bucket"] = key
    result["arm"] = assign_arm(user_id, bucket)
    if result["arm"] == ARM_TREATMENT:
        result["tier"] = target
        result["applied"] = True
    return result
