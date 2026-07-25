"""Cost-aware model-cost optimization loop (docs/cost-performance-margin-plan.md §D.1(1), issue #494).

Structured like `utilities/cost_alerts.py`: PURE evaluators first (no DB, no network — these are what
the unit tests pin with synthetic cohorts), then the DB-fed collectors, publication and CLI.

The loop, once a week:

1. **Observe** — per-post quality (`engagement_rate` / engagement score + `posts.authenticity_score`)
   for the window, split into the A/B arms of every ACTIVE routing bucket.
2. **Judge** — a non-inferiority test per bucket: the cheaper tier is adopted only when the treatment
   arm's quality is statistically INDISTINGUISHABLE from control (its confidence-interval lower bound
   sits inside the equivalence margin), never merely "looks fine".
3. **Act** — promote along the cohort ramp (10% → 50% → 100% = adopted), HOLD when the data is
   inconclusive, or **auto-rollback** the moment the quality gate is breached (§D.3).
4. **Publish** — the policy document lands in Redis, where `.litellm/complexity_router.py` reads it.
   `utilities/routing_policy.py` is the single shared decision core, so router and optimizer can
   never disagree about which bucket a call belongs to.

Two independent flags gate it: `COST_ROUTING_ENABLED` (this side — writes `enabled` into the policy)
and `COST_AWARE_ROUTING_ENABLED` (the proxy side). Either one off means the router routes exactly as
it did before this existed. Only features with a real quality signal (`MEASURABLE_FEATURES`) can be
auto-experimented; everything else is reported as a human-gated recommendation, per §D.3.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Optional, Sequence

from cqc_lem.utilities.env_constants import (
    COST_ALERT_EMAIL,
    COST_ROUTING_AUTH_FLOOR,
    COST_ROUTING_AUTH_MAX_DROP,
    COST_ROUTING_CONFIDENCE_Z,
    COST_ROUTING_COOLDOWN_DAYS,
    COST_ROUTING_ENABLED,
    COST_ROUTING_INITIAL_COHORT,
    COST_ROUTING_MAX_EXPERIMENTS,
    COST_ROUTING_MAX_QUALITY_DROP,
    COST_ROUTING_MIN_SAMPLES,
    COST_ROUTING_WINDOW_DAYS,
    MARGIN_REPORT_EMAIL,
)
from cqc_lem.utilities.logger import log_error, log_info, log_warning
from cqc_lem.utilities.routing_policy import (
    ARM_CONTROL,
    ARM_TREATMENT,
    POLICY_REDIS_KEY,
    POLICY_VERSION,
    ROUTING_STATES,
    STATE_ADOPTED,
    STATE_EXPERIMENT,
    STATE_ROLLED_BACK,
    TIER_COMPLEX,
    TIER_MEDIUM,
    assign_arm,
    bucket_key,
    cheaper_tier,
    normalize_policy,
)

# ───────────────────────────── vocabulary ─────────────────────────────

VERDICT_NON_INFERIOR = "non_inferior"
VERDICT_DEGRADED = "degraded"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_INSUFFICIENT = "insufficient_data"

ACTION_PROMOTE = "promote"
ACTION_ADOPT = "adopt"
ACTION_ROLLBACK = "rollback"
ACTION_HOLD = "hold"
ACTION_START = "start"

METRIC_RATE = "engagement_rate"
METRIC_COUNT = "engagement"

# Features whose output quality is actually measurable per artifact. Posts carry both signals the
# §D.3 gate needs (engagement via `post_stats`, `posts.authenticity_score`); a comment or a DM has
# neither, so down-routing them can never be auto-adopted — it is reported for a human instead.
MEASURABLE_FEATURES = ("content",)

# Tiers worth experimenting on, expensive-first. `lem-simple` is already the floor.
CANDIDATE_TIERS = (TIER_COMPLEX, TIER_MEDIUM)

# Cohort ramp: an experiment that keeps passing widens, so a regression that only shows at scale
# still gets caught while most traffic is on the known-good tier. The top step is deliberately NOT
# 100%: a bucket with no control arm can never be measured again, and "adopted" has to stay
# revocable — the permanent holdout is what keeps auto-rollback (§D.3) alive after full rollout.
ADOPTED_COHORT_PCT = 0.9
COHORT_RAMP_TAIL = (0.5, ADOPTED_COHORT_PCT)


def default_thresholds() -> dict:
    """Thresholds from the environment (`COST_ROUTING_*`). Every caller merges over this, so a test —
    or a CLI run — can override one threshold without restating the rest."""
    return {
        "min_samples": COST_ROUTING_MIN_SAMPLES,
        "max_quality_drop": COST_ROUTING_MAX_QUALITY_DROP,
        "authenticity_max_drop": COST_ROUTING_AUTH_MAX_DROP,
        "authenticity_floor": COST_ROUTING_AUTH_FLOOR,
        "confidence_z": COST_ROUTING_CONFIDENCE_Z,
        "cooldown_days": COST_ROUTING_COOLDOWN_DAYS,
        "initial_cohort_pct": COST_ROUTING_INITIAL_COHORT,
        "max_experiments": COST_ROUTING_MAX_EXPERIMENTS,
    }


def cohort_ramp(initial_pct: float) -> tuple:
    """The ramp an experiment climbs. Steps at or below the initial cohort are dropped so a
    configured 60% start doesn't "promote" down to 50%."""
    return (float(initial_pct),) + tuple(step for step in COHORT_RAMP_TAIL if step > initial_pct)


def next_cohort_pct(current: float, initial_pct: float) -> Optional[float]:
    """The next ramp step above `current`, or None when the experiment is already at full traffic."""
    for step in cohort_ramp(initial_pct):
        if step > current + 1e-9:
            return step
    return None


# ───────────────────────────── pure evaluators ─────────────────────────────

def _engagement(row: Mapping) -> float:
    """Weighted engagement — same weighting `post_stats` and `track_post_outcome` use, restated here
    only through the shared helper so the loop can never score posts on a different scale."""
    from cqc_lem.utilities.post_stats import engagement_score
    return float(engagement_score(row.get("reactions"), row.get("comments"), row.get("reposts")))


def pick_metric(rows: Sequence[Mapping]) -> str:
    """Impression-normalized rate when EVERY row in the comparison carries impressions, else raw
    engagement. Mixing the two would compare a rate against a count; falling back to counts is safe
    because both arms cover the same window and the same cohort split."""
    rows = list(rows or [])
    if rows and all((row.get("impressions") or 0) > 0 for row in rows):
        return METRIC_RATE
    return METRIC_COUNT


def summarize_arm(rows: Optional[Sequence[Mapping]], metric: str = METRIC_COUNT) -> dict:
    """Mean/σ of the quality metric plus the median authenticity score for one arm. Posts with no
    authenticity score (pre-gate rows) are simply absent from the median rather than counted as 0 —
    an unscored post is unknown quality, not bad quality."""
    rows = list(rows or [])
    values = []
    for row in rows:
        engagement = _engagement(row)
        if metric == METRIC_RATE:
            impressions = row.get("impressions") or 0
            if impressions <= 0:
                continue
            values.append(engagement / float(impressions))
        else:
            values.append(engagement)
    authenticity = [float(row["authenticity_score"]) for row in rows
                    if row.get("authenticity_score") is not None]
    return {
        "n": len(values),
        "metric": metric,
        "mean": statistics.fmean(values) if values else 0.0,
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "authenticity_n": len(authenticity),
        "authenticity_median": statistics.median(authenticity) if authenticity else None,
    }


def compare_arms(treatment: Optional[Mapping], control: Optional[Mapping],
                 thresholds: Optional[Mapping] = None) -> dict:
    """Non-inferiority test of the cheaper tier (treatment) against the incumbent (control).

    "Statistically indistinguishable" is the plan's bar, so a plain mean comparison won't do: the
    cheaper tier is accepted only when the 95% CI lower bound on (treatment − control) sits INSIDE
    the equivalence margin (`max_quality_drop` of the control mean). A wide, underpowered interval
    therefore reads `inconclusive` — the experiment keeps running — rather than passing by default.

    The authenticity gate is separate and absolute: a median below the floor, or a drop bigger than
    `authenticity_max_drop` points, is `degraded` no matter what engagement did (§D.3).
    """
    limits = {**default_thresholds(), **(thresholds or {})}
    treatment, control = dict(treatment or {}), dict(control or {})
    t_n, c_n = int(treatment.get("n") or 0), int(control.get("n") or 0)
    detail = {"treatment": treatment, "control": control}

    if t_n < limits["min_samples"] or c_n < limits["min_samples"]:
        return {"verdict": VERDICT_INSUFFICIENT,
                "reason": f"need {limits['min_samples']} posts per arm, have "
                          f"{t_n} treatment / {c_n} control",
                **detail}

    t_auth, c_auth = treatment.get("authenticity_median"), control.get("authenticity_median")
    if t_auth is not None and t_auth < limits["authenticity_floor"]:
        return {"verdict": VERDICT_DEGRADED,
                "reason": f"treatment authenticity median {t_auth:.0f} below floor "
                          f"{limits['authenticity_floor']:.0f}",
                "authenticity_delta": (t_auth - c_auth) if c_auth is not None else None, **detail}
    if t_auth is not None and c_auth is not None and (c_auth - t_auth) > limits["authenticity_max_drop"]:
        return {"verdict": VERDICT_DEGRADED,
                "reason": f"authenticity median dropped {c_auth - t_auth:.1f} points "
                          f"(> {limits['authenticity_max_drop']:.1f})",
                "authenticity_delta": t_auth - c_auth, **detail}

    delta = float(treatment["mean"]) - float(control["mean"])
    standard_error = math.sqrt((float(treatment["stdev"]) ** 2) / t_n
                               + (float(control["stdev"]) ** 2) / c_n)
    half_width = limits["confidence_z"] * standard_error
    margin = limits["max_quality_drop"] * abs(float(control["mean"]))
    lower, upper = delta - half_width, delta + half_width
    stats = {"delta": delta, "ci_lower": lower, "ci_upper": upper, "margin": margin,
             "authenticity_delta": (t_auth - c_auth) if (t_auth is not None and c_auth is not None)
                                   else None, **detail}

    if upper < -margin:
        return {"verdict": VERDICT_DEGRADED,
                "reason": f"engagement is significantly worse (CI upper {upper:.4f} < "
                          f"-{margin:.4f})", **stats}
    if lower >= -margin:
        return {"verdict": VERDICT_NON_INFERIOR,
                "reason": f"quality held (CI lower {lower:.4f} within margin -{margin:.4f})", **stats}
    return {"verdict": VERDICT_INCONCLUSIVE,
            "reason": f"CI [{lower:.4f}, {upper:.4f}] too wide to clear margin -{margin:.4f}",
            **stats}


def evaluate_bucket(bucket: Mapping, comparison: Mapping, today: date,
                    thresholds: Optional[Mapping] = None) -> dict:
    """Turn one bucket's verdict into its next state: promote along the ramp, adopt at full traffic,
    HOLD while the data is thin, or roll back the moment the quality gate is breached.

    Rollback is unconditional — it applies to an `adopted` bucket exactly as it does to a running
    experiment, so a tier that degrades only after full rollout still snaps back to the incumbent.
    The bucket stays in the document with a cooldown so the loop doesn't immediately re-propose it.
    """
    limits = {**default_thresholds(), **(thresholds or {})}
    updated = dict(bucket)
    verdict = comparison.get("verdict")
    reason = comparison.get("reason") or ""

    if verdict == VERDICT_DEGRADED:
        updated.update(state=STATE_ROLLED_BACK, cohort_pct=0.0,
                       cooldown_until=(today + timedelta(days=limits["cooldown_days"])).isoformat(),
                       reason=reason, evaluated_on=today.isoformat())
        return {"action": ACTION_ROLLBACK, "bucket": updated, "reason": reason,
                "comparison": comparison}

    if verdict == VERDICT_NON_INFERIOR and updated.get("state") == STATE_EXPERIMENT:
        current = float(updated.get("cohort_pct") or 0.0)
        promoted = next_cohort_pct(current, limits["initial_cohort_pct"])
        adopted = promoted is None or promoted >= ADOPTED_COHORT_PCT
        updated.update(cohort_pct=promoted if promoted is not None else current, reason=reason,
                       evaluated_on=today.isoformat())
        if adopted:
            updated["state"] = STATE_ADOPTED
        return {"action": ACTION_ADOPT if adopted else ACTION_PROMOTE, "bucket": updated,
                "reason": reason, "comparison": comparison}

    updated.update(reason=reason, evaluated_on=today.isoformat())
    return {"action": ACTION_HOLD, "bucket": updated, "reason": reason, "comparison": comparison}


def split_observations(observations: Optional[Sequence[Mapping]], bucket: Mapping) -> dict:
    """Bucket every observed post into the arm its author was assigned to, ignoring posts published
    before the experiment started (they predate the routing change and would dilute both arms)."""
    started_on = bucket.get("started_on")
    arms = {ARM_TREATMENT: [], ARM_CONTROL: []}
    for row in observations or []:
        day = row.get("day")
        if started_on and day and str(day) < str(started_on):
            continue
        arms[assign_arm(row.get("user_id"), bucket)].append(row)
    return arms


def new_bucket(feature: str, from_tier: str, today: date, cohort_pct: float,
               generation: int = 1) -> dict:
    to_tier = cheaper_tier(from_tier)
    return {
        "id": f"{bucket_key(feature, from_tier)}#{generation}",
        "feature": feature,
        "from_tier": from_tier,
        "to_tier": to_tier,
        "state": STATE_EXPERIMENT,
        "cohort_pct": float(cohort_pct),
        "generation": generation,
        "started_on": today.isoformat(),
        "cooldown_until": None,
        "reason": f"opened cost/quality experiment {from_tier}→{to_tier}",
        "evaluated_on": None,
    }


def propose_buckets(observations: Optional[Sequence[Mapping]], existing: Mapping, today: date,
                    spend_by_feature: Optional[Mapping] = None,
                    thresholds: Optional[Mapping] = None) -> list:
    """Rank the cost×quality opportunities and open at most `max_experiments` of them.

    A bucket is only proposed when the feature already has enough measured posts to judge it AND its
    current authenticity median clears the floor — down-routing content that is *already* scraping
    the quality gate would be optimizing the wrong end of the funnel. Ranking prefers the feature
    with the most attributed spend (from `cost_ledger`, when it is capturing), falling back to post
    volume so the loop still ranks something before the ledger lands.
    """
    limits = {**default_thresholds(), **(thresholds or {})}
    spend = dict(spend_by_feature or {})
    active = sum(1 for b in existing.values() if b.get("state") in ROUTING_STATES)
    slots = max(0, int(limits["max_experiments"]) - active)
    if slots <= 0:
        return []

    candidates = []
    for feature in MEASURABLE_FEATURES:
        feature_rows = [row for row in (observations or [])
                        if row.get("feature", "content") == feature]
        baseline = summarize_arm(feature_rows, pick_metric(feature_rows))
        for tier in CANDIDATE_TIERS:
            key = bucket_key(feature, tier)
            previous = existing.get(key)
            if previous:
                if previous.get("state") in ROUTING_STATES:
                    continue
                cooldown = previous.get("cooldown_until")
                if cooldown and today.isoformat() < str(cooldown):
                    continue
            if baseline["n"] < limits["min_samples"]:
                continue
            median = baseline.get("authenticity_median")
            if median is not None and median < limits["authenticity_floor"]:
                continue
            generation = int((previous or {}).get("generation") or 0) + 1
            candidates.append((float(spend.get(feature) or 0.0), baseline["n"],
                               new_bucket(feature, tier, today, limits["initial_cohort_pct"],
                                          generation)))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [bucket for _, _, bucket in candidates[:slots]]


def build_routing_policy(previous: Optional[Mapping], observations: Optional[Sequence[Mapping]],
                         today: date, spend_by_feature: Optional[Mapping] = None,
                         thresholds: Optional[Mapping] = None,
                         enabled: bool = COST_ROUTING_ENABLED) -> dict:
    """Evaluate every live bucket, then open new experiments in whatever slots are left, and return
    `{"policy": ..., "changes": [...], "recommendations": [...]}`.

    `enabled` is written INTO the document, so flipping `COST_ROUTING_ENABLED` off parks every bucket
    at once without erasing the experiment state the next run needs."""
    limits = {**default_thresholds(), **(thresholds or {})}
    policy = normalize_policy(previous)
    buckets = dict(policy.get("buckets") or {})
    rows = list(observations or [])
    changes, holds = [], []

    for key, bucket in list(buckets.items()):
        if bucket.get("state") not in ROUTING_STATES:
            continue
        feature_rows = [row for row in rows if row.get("feature", "content") == bucket.get("feature")]
        arms = split_observations(feature_rows, bucket)
        metric = pick_metric(arms[ARM_TREATMENT] + arms[ARM_CONTROL])
        comparison = compare_arms(summarize_arm(arms[ARM_TREATMENT], metric),
                                  summarize_arm(arms[ARM_CONTROL], metric), limits)
        outcome = evaluate_bucket(bucket, comparison, today, limits)
        buckets[key] = outcome["bucket"]
        record = {"bucket": key, "action": outcome["action"], "reason": outcome["reason"],
                  "bucket_state": outcome["bucket"], "comparison": comparison}
        # A hold moved no traffic — it belongs in the digest, not in the "something changed" list
        # that decides whether the owner is emailed.
        (holds if outcome["action"] == ACTION_HOLD else changes).append(record)

    if enabled:
        for bucket in propose_buckets(rows, buckets, today, spend_by_feature, limits):
            key = bucket_key(bucket["feature"], bucket["from_tier"])
            buckets[key] = bucket
            changes.append({"bucket": key, "action": ACTION_START, "bucket_state": bucket,
                            "reason": bucket["reason"], "comparison": None})

    return {
        "policy": {
            "version": POLICY_VERSION,
            "enabled": bool(enabled),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "buckets": buckets,
        },
        "changes": changes,
        "holds": holds,
        "recommendations": recommend_unmeasurable(spend_by_feature),
    }


def recommend_unmeasurable(spend_by_feature: Optional[Mapping]) -> list:
    """§D.3: features that spend real money but expose no per-artifact quality signal can't clear an
    automated gate, so they are surfaced as human-gated recommendations rather than auto-routed."""
    recommendations = []
    for feature, usd in sorted((spend_by_feature or {}).items(), key=lambda kv: -float(kv[1] or 0)):
        if feature in MEASURABLE_FEATURES or float(usd or 0) <= 0:
            continue
        recommendations.append({
            "feature": feature,
            "spend_usd": round(float(usd), 4),
            "recommendation": f"{feature} spend is ${float(usd):.2f} with no per-artifact quality "
                              "signal — down-routing it needs a human product call (risk:product-decision)",
        })
    return recommendations


def render_routing_text(report: Mapping) -> str:
    """Plain-text digest — the CLI output and the plaintext alternative of the owner email."""
    policy = report.get("policy") or {}
    buckets = policy.get("buckets") or {}
    lines = [f"LEM cost-aware routing — {report.get('date')} "
             f"(enabled={policy.get('enabled')}, {len(report.get('changes') or [])} change(s))", ""]
    if not report.get("changes"):
        lines.append("No routing changes this run.")
    for change in list(report.get("changes") or []) + list(report.get("holds") or []):
        bucket = change.get("bucket_state") or {}
        state = bucket.get("state") if isinstance(bucket, Mapping) else None
        lines.append(f"[{str(change.get('action', '')).upper()}] {change.get('bucket')}"
                     f"{f' → {state}' if state else ''}")
        lines.append(f"    {change.get('reason')}")
    lines += ["", "Buckets:"]
    if not buckets:
        lines.append("  (none)")
    for key, bucket in buckets.items():
        lines.append(f"  {key}: {bucket.get('state')} → {bucket.get('to_tier')} "
                     f"@ {float(bucket.get('cohort_pct') or 0) * 100:.0f}% cohort")
    for recommendation in report.get("recommendations") or []:
        lines += ["", f"RECOMMENDATION: {recommendation.get('recommendation')}"]
    return "\n".join(lines)


def render_routing_html(report: Mapping) -> str:
    body = (render_routing_text(report)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return ("<h2>LEM cost-aware routing</h2><pre style=\"font-family:ui-monospace,monospace;"
            f"font-size:13px;line-height:1.5\">{body}</pre>")


# ─────────────────────── DB/Redis-fed collectors & publication ───────────────────────

def _today() -> date:
    return datetime.now(timezone.utc).date()


def _redis_client():
    """Redis handle for the policy document, or None when Redis is unavailable (the loop then still
    evaluates and reports — it just can't publish). Must resolve to the same instance the LiteLLM
    container reads, hence the shared `COST_ROUTING_REDIS_URL` override."""
    try:
        import redis
    except Exception:
        return None
    url = (os.getenv("COST_ROUTING_REDIS_URL") or "").strip()
    if not url.startswith("redis"):
        url = os.getenv("CELERY_BROKER_URL", "")
    if not url.startswith("redis"):
        url = f"redis://redis:{os.getenv('REDIS_PORT', '6379')}/0"
    try:
        return redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    except Exception:
        return None


def load_policy() -> dict:
    """The currently published policy, or `{}` when there is none (or Redis is unreachable). An
    unreadable policy is treated as "no experiments running" for evaluation purposes — the router
    fails open the same way, so both sides degrade to complexity-only routing together."""
    client = _redis_client()
    if client is None:
        return {}
    try:
        raw = client.get(POLICY_REDIS_KEY)
    except Exception as e:
        log_warning("Could not read routing policy from Redis", exc=e,
                    task_name="auto_weekly_cost_routing")
        return {}
    if not raw:
        return {}
    try:
        return normalize_policy(json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw))
    except Exception as e:
        log_warning("Routing policy in Redis is not valid JSON — ignoring", exc=e,
                    task_name="auto_weekly_cost_routing")
        return {}


def publish_policy(policy: Mapping) -> bool:
    """Write the policy where `.litellm/complexity_router.py` reads it. No TTL: an expired key would
    silently revert routing mid-week, and "no policy" must mean "nothing decided", not "we forgot"."""
    client = _redis_client()
    if client is None:
        log_warning("Redis unavailable — routing policy not published",
                    task_name="auto_weekly_cost_routing")
        return False
    try:
        client.set(POLICY_REDIS_KEY, json.dumps(policy))
        return True
    except Exception as e:
        log_warning("Could not publish routing policy to Redis", exc=e,
                    task_name="auto_weekly_cost_routing")
        return False


def collect_quality_observations(days: int = COST_ROUTING_WINDOW_DAYS,
                                 end: Optional[date] = None) -> list:
    """Per-post quality rows for the window, tagged with the feature they belong to. Posts are the
    `content` feature by definition — the other features have no per-artifact outcome to measure."""
    from cqc_lem.utilities.db import get_post_quality_rows

    end = end or _today()
    start = end - timedelta(days=max(int(days), 1))
    rows = get_post_quality_rows(start, end + timedelta(days=1))
    return [{**row, "feature": "content"} for row in rows]


def collect_routing_report(days: int = COST_ROUTING_WINDOW_DAYS, today: Optional[date] = None,
                           thresholds: Optional[Mapping] = None,
                           enabled: bool = COST_ROUTING_ENABLED) -> dict:
    """Read the live policy, the window's quality observations and (when the ledger is capturing) the
    per-feature spend, then build the next policy.

    While `COST_ROUTING_ENABLED` is off nothing is being routed, so the window is not scanned at all —
    the weekly run costs one Redis read/write instead of a cross-user posts+post_stats scan. It still
    republishes the parked (`enabled: false`) document so the proxy sees the flag flip, and any bucket
    left in it is judged on no data, which can only HOLD: turning the flag back on resumes exactly
    where the loop left off, and no experiment can open while it is off."""
    from cqc_lem.utilities.db import cost_ledger_available, get_cost_rollup

    today = today or _today()
    observations = collect_quality_observations(days, end=today) if enabled else []
    spend = (get_cost_rollup(today - timedelta(days=max(int(days), 1)), today, group_by="feature")
             if enabled and cost_ledger_available() else {})
    result = build_routing_policy(load_policy(), observations, today, spend, thresholds, enabled)
    return {
        "date": today.isoformat(),
        "window_days": int(days),
        "observations": len(observations),
        "spend_by_feature": spend,
        **result,
    }


def apply_routing_report(report: Optional[Mapping] = None, publish: bool = True,
                         to_email: Optional[str] = None) -> dict:
    """Publish the new policy and tell the owner what moved. Only ACTUAL changes are delivered — a
    weekly "nothing changed" email would train the owner to ignore the one that says a bucket rolled
    back. A rollback logs at ERROR so it reaches PostHog through the logger on its own."""
    from cqc_lem.utilities.email import _dispatch_email
    from cqc_lem.utilities.observability import track_routing_policy

    report = report if report is not None else collect_routing_report()
    changes = list(report.get("changes") or [])
    rollbacks = [c for c in changes if c.get("action") == ACTION_ROLLBACK]

    for change in rollbacks:
        log_error(f"Cost-aware routing rolled back {change.get('bucket')}: {change.get('reason')}",
                  task_name="auto_weekly_cost_routing")

    published = publish_policy(report.get("policy") or {}) if publish else False

    tracked = False
    try:
        track_routing_policy(report)
        tracked = True
    except Exception as e:
        log_warning("Routing policy PostHog capture failed", exc=e,
                    task_name="auto_weekly_cost_routing")

    recipient = to_email or COST_ALERT_EMAIL or MARGIN_REPORT_EMAIL
    emailed = False
    if changes and recipient:
        try:
            subject = (f"LEM routing {'ROLLBACK' if rollbacks else 'update'} — "
                       f"{len(changes)} change(s) {report.get('date')}")
            emailed = bool(_dispatch_email(recipient, subject, render_routing_html(report),
                                           text_content=render_routing_text(report),
                                           high_priority=bool(rollbacks)))
        except Exception as e:
            log_warning("Routing policy email failed", exc=e, task_name="auto_weekly_cost_routing")

    log_info(f"Cost-aware routing {report.get('date')}: {len(changes)} change(s), "
             f"{len(rollbacks)} rollback(s), {report.get('observations')} observed post(s) "
             f"(published={published}, emailed={emailed}, tracked={tracked})",
             task_name="auto_weekly_cost_routing")
    return {"published": published, "emailed": emailed, "tracked": tracked,
            "changes": len(changes), "rollbacks": len(rollbacks), "report": report}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LEM cost-aware routing optimizer")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate and print without publishing the policy or emailing")
    parser.add_argument("--apply", action="store_true",
                        help="publish the new policy to Redis and deliver changes")
    parser.add_argument("--days", type=int, default=COST_ROUTING_WINDOW_DAYS,
                        help=f"observation window in days (default {COST_ROUTING_WINDOW_DAYS})")
    args = parser.parse_args(argv)

    report = collect_routing_report(days=args.days)
    if args.apply and not args.dry_run:
        apply_routing_report(report)
    print(json.dumps(report) if args.json else render_routing_text(report))
    # Exit 2 when a bucket rolled back, so a cron/CI caller can act on it without parsing output.
    return 2 if any(c.get("action") == ACTION_ROLLBACK for c in report.get("changes") or []) else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
