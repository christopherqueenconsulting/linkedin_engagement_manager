"""ONE experiment surface — PostHog Experiments (issue #652, docs/experiments.md).

LEM hand-rolled experimentation twice. The cost/quality loop (`cost_routing.py`) grew its own
cohort hashing, its own non-inferiority test and its own weekly readout; the #396 media A/B harness
grew its own shipped-variant table and its own winner ranking. Both work, and neither can answer
"is this difference real" with anything a statistician would sign — and nothing rendered anywhere a
human looks. PostHog Experiments already has the multivariate flag, the exposure model, both stats
engines and the readout, so this module is the ADAPTER, not a third implementation:

* **Assignment** — `resolve_variant()` reads a PostHog MULTIVARIATE flag by local evaluation, so a
  cohort is reproducible from the flag instead of from a hash nobody can inspect.
* **Exposure** — `$feature_flag_called` (what PostHog's experiment engine reads to decide which
  variant a person was in), emitted ONCE per (experiment, person, variant) per process. flags.py
  deliberately suppresses these for hot boolean toggles; an experiment without exposures has no
  readout at all, so here they are emitted deliberately and deduped instead.
* **Outcomes** — the metric events already exist (`post_outcome`, `comment_outcome`,
  `$ai_generation`). PostHog attributes them to a variant BY PERSON via the exposure, so no new
  event is needed; `experiment_properties()` additionally stamps `$feature/<key>` where the caller
  already has the user in hand, which is what makes a property-based readout possible too.

**An unresolvable experiment is the CONTROL arm.** No PostHog key, definitions not loaded, flag not
defined, evaluation inconclusive, `EXPERIMENTS_ENABLED=false`, SDK raises — every one of those paths
returns the spec's control variant, which is by construction the behaviour that shipped before the
experiment existed. That is why there are no env-var fallbacks here (flags.py's contract): a toggle
has a configured default worth honouring, an experiment arm does not.

`_raw_variant()` keeps "PostHog said control" and "PostHog said nothing" apart, because
`experiment_properties()` must NOT stamp `$feature/x=control` on a metric event when nobody was
enrolled — a fabricated control arm is worse than a missing one, it makes the readout look
populated.

The local-evaluation bootstrap (personal key, background poller, retry cooldown) lives in flags.py —
this module reuses it (lazily, see `_flag_surface`) rather than starting a second poller in the same
worker. That constrains every registered flag the same way flags.py's registry is constrained: local
evaluation cannot decide a release condition needing server-held person properties, so experiment
flags must use rollout percentage / distinct-ID conditions only, or every worker silently reads
control.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Sequence

import posthog

from cqc_lem.utilities.logger import log_debug, log_warning
from cqc_lem.utilities.routing_policy import ARM_CONTROL, ARM_TREATMENT, SYSTEM_USER_ID

# Every experiment's control arm is named the same thing the routing policy already calls it, so the
# arm vocabulary is ONE word across the flag, the policy document and the analysis.
CONTROL = ARM_CONTROL

# How PostHog decided the arm.
ASSIGNMENT_FLAG = "flag"
# The artifact itself decided it (the #396 media harness: whichever combo actually shipped). There is
# no flag to read — the adapter's job is to report the arm that already happened.
ASSIGNMENT_SHIPPED = "shipped"


@dataclass(frozen=True)
class ExperimentSpec:
    """One experiment. `key` is the PostHog feature-flag key AND the experiment key AND this
    registry's name — one identifier, so a rename can't leave the code, the flag and the experiment
    readout disagreeing. `variants[0]` is the control by definition."""
    key: str
    variants: tuple
    owner: str
    metric_events: tuple
    description: str
    assignment: str = ASSIGNMENT_FLAG
    # Share of enrolled traffic the flag should start the treatment on. Provisioned by
    # scripts/posthog_experiments.py; PostHog owns it from then on.
    rollout_pct: float = 0.5

    @property
    def control(self) -> str:
        return self.variants[0]


# --- The registry ------------------------------------------------------------------------------
# Constants, not bare strings, at every call site: a typo'd key raises here instead of silently
# resolving to the control arm somewhere in a Celery task.
COST_ROUTING_ARM = "cost-routing-arm"
COMMENT_CONTRACT_PROMPT = "comment-contract-prompt"
POST_MEDIA_VARIANT = "post-media-variant"

# The pilot prompt variant (G2). Named for what it changes, not "variant-b", so the readout says
# which prompt won.
COMMENT_CONTRACT_AUTHOR_QUESTION = "author-question"

EXPERIMENTS: Dict[str, ExperimentSpec] = {
    spec.key: spec for spec in (
        ExperimentSpec(
            key=COST_ROUTING_ARM,
            variants=(ARM_CONTROL, ARM_TREATMENT),
            owner="cost",
            metric_events=("post_outcome", "$ai_generation"),
            description=("Cost-aware down-routing cohort (docs/cost-performance-margin-plan.md "
                         "§D.1.1). Treatment runs one model tier cheaper. Success = engagement rate "
                         "holding (post_outcome) while $ai_generation cost falls. The arm is "
                         "resolved app-side and handed to the LiteLLM router INSIDE the policy "
                         "document — routing_policy.py must stay stdlib-only, so it can never "
                         "import this module."),
            # Matches COST_ROUTING_INITIAL_COHORT's 10% default: the ramp starts small because the
            # treatment arm is spending real quality, not just money.
            rollout_pct=0.1,
        ),
        ExperimentSpec(
            key=COMMENT_CONTRACT_PROMPT,
            variants=(CONTROL, COMMENT_CONTRACT_AUTHOR_QUESTION),
            owner="content",
            metric_events=("comment_outcome",),
            description=("Pilot LLM prompt experiment (G2): the feed-comment quality contract's "
                         "closing ask. Control offers four value-adds and any open question; "
                         "treatment REQUIRES a question only the post's author could answer. "
                         "Metric = author-reply rate (D4) from the #628 T+24h outcome sweep. At "
                         "current single-user volume this is underpowered by design — see the "
                         "small-sample caveat in docs/experiments.md."),
        ),
        ExperimentSpec(
            key=POST_MEDIA_VARIANT,
            # Data-defined: the arms are whichever media combos actually shipped, so the variant list
            # is derived from generate_variants.DEFAULT_COMBOS at provisioning time rather than
            # frozen here.
            variants=(CONTROL,),
            owner="content",
            metric_events=("post_outcome",),
            description=("Adapter for the #396 media A/B harness: the combo a post SHIPPED becomes "
                         "its exposure, so post_outcome engagement can be read per variant in "
                         "PostHog beside post_stats.select_variant_winners' own ranking."),
            assignment=ASSIGNMENT_SHIPPED,
        ),
    )
}

# PostHog variant keys are slugs. A shipped media combo key is not ("…/flux-dev|gen4_turbo|1:1"), so
# it is slugified — with a stable digest suffix when the slug had to be truncated, so two long combos
# can never collapse into one arm.
_SLUG_MAX = 60
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# Exposure dedup. One entry per (experiment, distinct_id, variant) actually emitted. Bounded so a
# multi-user worker can't grow it without limit; on overflow it is cleared, which re-emits at worst
# one duplicate exposure per person (PostHog's readout counts a person once regardless).
_EXPOSURE_CACHE_MAX = 5000
_exposed: set = set()
_exposure_lock = threading.Lock()

# Latched lazy import of the flag surface — see `_flag_surface`.
_flags_module = None
_flags_unavailable = False

_OFF_VALUES = ("0", "false", "no", "off", "disabled")


def spec(key: str) -> ExperimentSpec:
    found = EXPERIMENTS.get(key)
    if found is None:
        raise KeyError(f"Unregistered experiment '{key}' — add an ExperimentSpec to "
                       "utilities/experiments.py")
    return found


def enabled() -> bool:
    """The one kill switch. Off means every experiment reads as its control arm and no exposure is
    emitted — the pre-experiment behaviour, everywhere, immediately."""
    raw = (os.environ.get("EXPERIMENTS_ENABLED") or "").strip().lower()
    return raw not in _OFF_VALUES if raw else True


def distinct_id(user_id: Optional[int] = None) -> str:
    """Same convention as observability.py and flags.py, so an exposure lands on the SAME PostHog
    person the metric events do — which is the whole mechanism by which PostHog attributes an
    outcome to an arm. The "system" sentinel is taken from routing_policy, the one stdlib-only copy
    every side of the cost experiment already shares."""
    return str(user_id) if user_id is not None else SYSTEM_USER_ID


def variant_slug(value: Optional[str]) -> str:
    """A PostHog-safe variant key for an arm whose name comes from data (a shipped media combo)."""
    slug = _SLUG_STRIP.sub("-", str(value or "").strip().lower()).strip("-")
    if not slug:
        return CONTROL
    if len(slug) <= _SLUG_MAX:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:6]
    return f"{slug[:_SLUG_MAX - 7].rstrip('-')}-{digest}"


def _flag_surface():
    """flags.py (#651), or None when it cannot be loaded.

    Imported lazily and latched: this module holds no import-time dependency on the flag surface, and
    a failure is reported ONCE rather than per feed comment — `_raw_variant` runs in hot loops, and a
    warning per LLM call would be the noise that hides the next real one."""
    global _flags_module, _flags_unavailable
    if _flags_module is None and not _flags_unavailable:
        try:
            from cqc_lem.utilities import flags
            _flags_module = flags
        except Exception as exc:
            _flags_unavailable = True  # lgtm[py/unused-global-variable]
            log_warning("Feature-flag surface unavailable — every experiment reads its control arm",
                        exc=exc, api_provider="posthog")
    return _flags_module


def _local_evaluation_ready() -> bool:
    """Whether a PostHog lookup is worth attempting.

    flags.py owns the ONE local-evaluation bootstrap — personal API key, the SDK's background
    definition poller and the failed-load cooldown. Experiments reuse it instead of starting a second
    poller in the same process, so a flag flip and an experiment ramp become visible to a worker on
    the same tick. Every not-ready path is the control arm."""
    flags = _flag_surface()
    if flags is None:
        return False
    try:
        return flags.local_evaluation_available() and flags._ensure_loaded()
    except Exception:
        # flags._ensure_loaded() already swallows and logs its own load failures; anything reaching
        # here is unexpected, and the answer is still "use the control arm".
        return False


def enrollment_available() -> bool:
    """Whether PostHog can enrol anyone at all right now.

    For callers that would have to do REAL WORK to produce a cohort — `cost_routing` lists every
    active user before it can ask for their arms — so an experiment plane that is off costs a DB scan
    it can never use. A per-call resolution doesn't need this: `_raw_variant` checks the same thing."""
    return enabled() and _local_evaluation_ready()


def _raw_variant(key: str, user_id: Optional[int] = None) -> Optional[str]:
    """PostHog's answer for this experiment, or None when it has no usable one.

    None is not the control arm: it means nobody was enrolled, which callers must be able to tell
    apart (see `experiment_properties`). A value PostHog returns that is not one of the spec's
    variants is also None — a flag reconfigured behind the code's back must not silently become an
    arm the code has no branch for."""
    experiment = spec(key)
    if experiment.assignment != ASSIGNMENT_FLAG or not enabled():
        return None
    if not _local_evaluation_ready():
        return None
    try:
        value = posthog.get_feature_flag(
            key,
            distinct_id(user_id),
            # No network request, ever — this is checked per feed comment and per LLM call.
            only_evaluate_locally=True,
            # Exposure is emitted explicitly (and deduped) by `resolve_variant`, so the SDK's own
            # per-call event would only duplicate it.
            send_feature_flag_events=False,
        )
    except Exception as exc:
        log_warning(f"Experiment '{key}' lookup failed — using the control arm", exc=exc,
                    user_id=user_id, api_provider="posthog")
        return None
    if value is None or isinstance(value, bool):
        # A boolean answer means the flag exists but is not multivariate — it carries no arm.
        return None
    text = str(value).strip()
    if text not in experiment.variants:
        log_warning(f"Experiment '{key}' returned unknown variant '{text}' — using the control arm",
                    user_id=user_id, api_provider="posthog")
        return None
    return text


def resolve_variant(key: str, user_id: Optional[int] = None, track: bool = True) -> str:
    """The arm this user is in — always one of the spec's variants, control when PostHog has no
    answer. Emits the exposure PostHog's readout is built on (once per person per variant per
    process) unless `track=False`, which is for callers that only need to know the arm to LABEL
    something that already happened."""
    variant = _raw_variant(key, user_id)
    if variant is None:
        return spec(key).control
    if track:
        track_exposure(key, variant, user_id)
    return variant


def is_treatment(key: str, user_id: Optional[int] = None, track: bool = True) -> bool:
    """Convenience for the two-arm experiments (`control`/`treatment`)."""
    return resolve_variant(key, user_id, track=track) == ARM_TREATMENT


def assignments(user_ids: Optional[Iterable], key: str) -> dict:
    """`{user_id: variant}` for the users PostHog HAS an answer for — users it has no opinion on are
    absent rather than defaulted, so a caller (the routing policy) can fall back to its own
    assignment for them instead of quietly parking everyone in control.

    Exposure is emitted for each enrolled user: this is the enrolment moment for a cohort that is
    decided in bulk once a week rather than per request."""
    resolved = {}
    for user_id in user_ids or []:
        if user_id is None:
            continue
        variant = _raw_variant(key, user_id)
        if variant is None:
            continue
        resolved[user_id] = variant
        track_exposure(key, variant, user_id)
    return resolved


def experiment_properties(user_id: Optional[int] = None,
                          keys: Optional[Sequence[str]] = None,
                          extra: Optional[Mapping[str, str]] = None) -> dict:
    """`{"$feature/<key>": <variant>}` for a metric event.

    PostHog attributes an outcome to an arm BY PERSON via the exposure event, so this is additive —
    it makes a property-based readout (and a HogQL breakdown) possible without a join. Only
    experiments PostHog actually enrolled this person in appear: stamping `control` on an event from
    an unenrolled person would inflate the control arm with traffic that was never in the test.

    `extra` carries arms the caller already knows (a shipped media variant), which have no flag to
    read — those are slugified here so the caller never has to know PostHog's variant-key rules. The
    kill switch covers those too: `EXPERIMENTS_ENABLED=false` must mean NO event carries an arm, or
    a switched-off experiment still renders a populated property breakdown."""
    if not enabled():
        return {}
    props = {}
    for key in keys or ():
        variant = _raw_variant(key, user_id)
        if variant is not None:
            props[f"$feature/{key}"] = variant
    for key, variant in (extra or {}).items():
        if variant:
            props[f"$feature/{key}"] = variant_slug(variant)
    return props


def track_exposure(key: str, variant: str, user_id: Optional[int] = None, **extra) -> bool:
    """Emit ONE `$feature_flag_called` exposure, deduped per (experiment, person, variant).

    Returns True when this call emitted it. Never raises: an experiment that cannot be measured must
    still not break the code path it is measuring."""
    identity = distinct_id(user_id)
    token = (key, identity, str(variant))
    with _exposure_lock:
        if token in _exposed:
            return False
        if len(_exposed) >= _EXPOSURE_CACHE_MAX:
            _exposed.clear()
        _exposed.add(token)
    try:
        from cqc_lem.utilities.observability import track_experiment_exposure
        track_experiment_exposure(key, variant, user_id=user_id, **extra)
        log_debug(f"Experiment '{key}' exposure: {identity} → {variant}", user_id=user_id,
                  api_provider="posthog")
        return True
    except Exception as exc:
        log_warning(f"Experiment '{key}' exposure capture failed", exc=exc, user_id=user_id,
                    api_provider="posthog")
        return False


def track_shipped_variant(key: str, shipped_key: Optional[str], user_id: Optional[int] = None,
                          **extra) -> Optional[str]:
    """Exposure for an `ASSIGNMENT_SHIPPED` experiment — the #396 harness adapter. The arm is
    whatever combo shipped, so it is slugified into a PostHog variant key and reported as-is.
    Returns the variant key, or None when there was nothing to report."""
    if spec(key).assignment != ASSIGNMENT_SHIPPED or not enabled():
        return None
    if not shipped_key:
        return None
    variant = variant_slug(shipped_key)
    track_exposure(key, variant, user_id, shipped_key=str(shipped_key), **extra)
    return variant


def registry_rows() -> list:
    """The registry as plain dicts — for docs, the provisioning script and the registry test."""
    return [{"key": s.key, "variants": list(s.variants), "control": s.control, "owner": s.owner,
             "assignment": s.assignment, "rollout_pct": s.rollout_pct,
             "metric_events": list(s.metric_events), "description": s.description}
            for s in EXPERIMENTS.values()]


def reset_exposure_cache() -> None:
    """Drop the exposure dedup set AND the latched flag-surface import. For tests, and for a
    long-lived process that legitimately wants to re-emit enrolment (a re-provisioned experiment)."""
    global _flags_module, _flags_unavailable
    with _exposure_lock:
        _exposed.clear()
    _flags_module = None
    _flags_unavailable = False
