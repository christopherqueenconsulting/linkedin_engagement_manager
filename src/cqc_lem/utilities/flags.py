"""ONE runtime feature-flag surface (issue #651, docs/feature-flags.md).

LEM's operational toggles are env vars, so changing one means editing `/opt/lem/.env` and restarting
the stack. PostHog flags make the same toggles runtime-changeable (and %-rollout / per-user
targetable) — but a flag service is a dependency, and an engagement automation that changes
behaviour because PostHog was unreachable would be worse than one that can't be toggled at all.

So the contract here is **fail-open to the env var**, in both directions:

- No runtime personal API key (`POSTHOG_RUNTIME_API_KEY`, falling back to
  `POSTHOG_PERSONAL_API_KEY` — `posthog_keys.py` resolves it), no project key,
  `POSTHOG_FLAGS_ENABLED=false`, definitions not
  loaded, flag not defined in PostHog, evaluation inconclusive, SDK raises — every one of those
  paths returns the env var's value. A deployment that never creates a single PostHog flag behaves
  EXACTLY as it did before this module existed.
- Every registered flag keeps its env var as both its default and its fallback, so the env var
  stays the answer to "what happens if PostHog is down".

**Local evaluation only.** `only_evaluate_locally=True` on every lookup: flag definitions are polled
into the process by the SDK's background poller and evaluated in-process, so a Celery loop checking
a flag per feed post makes ZERO network requests. The consequence is a real constraint on the
registry — a flag whose release condition needs person properties the server holds cannot be decided
locally, and every worker would silently fall back to its env var. **Registered flags must use
rollout percentage / distinct-ID conditions only.**

`send_feature_flag_events=False` for the same reason: these are checked in hot loops, and a
`$feature_flag_called` event per feed post is noise nobody reads.

**Safety controls are NOT flags and never will be.** The 429 breaker, the automation pause, the
suppression tripwire and the per-day caps stay in Redis/env, where they are one authority that a
PostHog outage, a mis-targeted rollout or an errant %-ramp cannot touch.

distinct_id follows the same convention as `observability.py` — `str(user_id)`, or the `"system"`
sentinel — so a per-user flag targets the SAME PostHog person the user's own events land on.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

import posthog

from cqc_lem.utilities.logger import log_debug, log_warning
from cqc_lem.utilities.posthog_keys import runtime_api_key

# Same sentinel observability.py uses for events with no user — one PostHog person, one convention.
SYSTEM_DISTINCT_ID = "system"

# Values that read as "off" in an env var or a flag variant. Everything else is on, so a flag set to
# a real variant name (an experiment arm) counts as enabled.
_OFF_VALUES = ("0", "false", "no", "off", "disabled", "control")


@dataclass(frozen=True)
class FlagSpec:
    """One registered toggle. `key` is the PostHog flag key AND this registry's name — one
    identifier, so a rename can't leave the code and PostHog disagreeing.
    """
    key: str
    env_var: str
    default: bool
    owner: str
    description: str


# --- The registry ------------------------------------------------------------------------------
# Constants, not bare strings, at every call site: a typo'd flag name raises here instead of
# silently evaluating to False somewhere in a Celery task.
COMMENT_RESEARCH = "comment-research-enabled"
TUTORIAL_VIDEOS = "tutorial-videos-enabled"
FEED_FALLBACK_DEFAULT = "feed-fallback-when-empty-default"
COST_ROUTING = "cost-routing-enabled"
POSTHOG_SURVEYS = "posthog-surveys-enabled"
NEWSLETTER_EDITOR = "newsletter-editor-enabled"
BRAND_SHOWCASE = "brand-showcase-enabled"
VIDEO_MOTION_LINT_HOLD = "video-motion-lint-hold"
VIDEO_CAPTIONS = "video-captions-enabled"
OCCASION_NATIVE_PUBLISH = "occasion-native-publish-enabled"
ANIMATED_POST = "animated-post-enabled"
CURATED_SOURCES = "curated-sources-enabled"
COMMENT_CANDIDATE_POOL = "comment-candidate-pool-enabled"
WEEKLY_REPORT = "weekly-report-enabled"
COMMENT_CANDIDATE_CRAWL = "comment-candidate-crawl-enabled"
IMPACT_REPORT = "impact-report-enabled"
COMMENT_RAMP = "comment-ramp-enabled"
PROFILE_AUDIT = "profile-audit-enabled"
MCP_SERVER = "mcp-server-enabled"
MCP_SERVER_WRITES = "mcp-server-writes"
COMMENT_CANDIDATE_DRAFTS = "comment-candidate-drafts-enabled"
VOICE_EXEMPLARS = "voice-exemplars-enabled"
COMMENT_MULTI_CANDIDATE = "comment-multi-candidate-enabled"
AUTHENTICITY_LOOP = "authenticity-loop-enabled"

FLAGS: Dict[str, FlagSpec] = {
    spec.key: spec for spec in (
        FlagSpec(
            key=AUTHENTICITY_LOOP,
            env_var="AUTHENTICITY_LOOP",
            default=False,
            owner="content-quality",
            description=("Authenticity loop for comment and DM drafts (issue #2627, Phase 3b): "
                         "pre-text in the writer prompt, a post-check score, up to 3 regenerations "
                         "with the specific issues, then hold at PENDING and notify. Below 50 "
                         "approve and auto-approve are refused; the owner posts only through a "
                         "confirm-with-reason override that writes an audit row. OFF by default: "
                         "prompts and behaviour unchanged."),
        ),
        FlagSpec(
            key=COMMENT_MULTI_CANDIDATE,
            env_var="COMMENT_MULTI_CANDIDATE",
            default=False,
            owner="content-quality",
            description=("Draw 3 feed-comment candidates, run the deterministic gates on each and "
                         "let a cross-family judge (`lem-judge`) pick the one a busy practitioner "
                         "would plausibly type (issue #2627, Phase 3). OFF by default: one draft "
                         "per attempt, unchanged."),
        ),
        FlagSpec(
            key=VOICE_EXEMPLARS,
            env_var="VOICE_EXEMPLARS",
            default=False,
            owner="content-quality",
            description=("Ground comment and nurture-DM voice in the user's own uploaded writing: "
                         "the nearest 5 embedded exemplars go into the prompt as few-shot, and a "
                         "draft too close to one is regenerated (issue #2627, Phase 1a). Below 4 "
                         "exemplars the adjective brief is used unchanged. OFF by default."),
        ),
        FlagSpec(
            key=COMMENT_CANDIDATE_DRAFTS,
            env_var="COMMENT_CANDIDATE_DRAFTS_ENABLED",
            default=False,
            owner="engagement",
            description=("Draft one comment per pooled candidate through the claims-gated generator "
                         "and surface the drafts, with risk flags, as the `comment_candidates` lane "
                         "of Daily review (issue #2647, Phase 3). Drafting posts nothing; an approved "
                         "draft is posted only by the automated lane within its caps or by Assist. "
                         "OFF by default."),
        ),
        FlagSpec(
            key=COMMENT_RAMP,
            env_var="COMMENT_RAMP_ENABLED",
            default=False,
            owner="engagement",
            description=("The comment-volume ramp state machine, 20 -> 40 -> 70 -> 100 a day "
                         "(issue #2647, Phase 5). Steps up only on healthy reply rate, no demotion "
                         "hold, no 429 in 7 days and an ok tripwire; steps down on a breach. "
                         "Lifts the brand's comment ceiling. OFF by default."),
        ),
        FlagSpec(
            key=COMMENT_CANDIDATE_CRAWL,
            env_var="COMMENT_CANDIDATE_CRAWL_ENABLED",
            default=False,
            owner="engagement",
            description=("Nightly roster crawl that fills `comment_candidates` with source `crawl` "
                         "(issue #2647, Phase 2). Read-only on LinkedIn, weekday daytime hours in "
                         "the user's timezone only, capped per day. Also needs the pool flag. "
                         "OFF by default."),
        ),
        FlagSpec(
            key=COMMENT_CANDIDATE_POOL,
            env_var="COMMENT_CANDIDATE_POOL_ENABLED",
            default=False,
            owner="engagement",
            description=("Save every scored post the feed scan and roster pass read (past the "
                         "comment-safety and min-reactions gates) into `comment_candidates` "
                         "(issue #2635, Phase 1). OFF by default; nothing reads the pool yet."),
        ),
        FlagSpec(
            key=COMMENT_RESEARCH,
            env_var="COMMENT_RESEARCH_ENABLED",
            default=False,
            owner="content",
            description=("Per-comment web research (content_research.py). OFF by default — comments "
                         "run at high volume and are already grounded in the target post, so this "
                         "multiplies Perplexity spend. Flip on to trial richer comments."),
        ),
        FlagSpec(
            key=TUTORIAL_VIDEOS,
            env_var="TUTORIAL_VIDEOS_ENABLED",
            default=False,
            owner="marketing",
            description=("The weekly marketing tutorial producer (video_tutorials.py) and the SPA "
                         "section that embeds its output. OFF by default — it renders and publishes "
                         "public assets."),
        ),
        FlagSpec(
            key=FEED_FALLBACK_DEFAULT,
            env_var="FEED_FALLBACK_WHEN_EMPTY_DEFAULT",
            default=True,
            owner="engagement",
            description=("Default for `feed_fallback_when_empty` for users who have never SAVED an "
                         "engagement-preferences row. An explicit per-user setting always wins — "
                         "this only moves the fleet default."),
        ),
        FlagSpec(
            key=COST_ROUTING,
            env_var="COST_ROUTING_ENABLED",
            default=False,
            owner="cost",
            description=("App side of cost-aware down-routing (cost_routing.py) — writes `enabled` "
                         "into the routing policy document. The proxy's own "
                         "COST_AWARE_ROUTING_ENABLED is a SEPARATE switch and is deliberately NOT a "
                         "flag: routing_policy.py must stay stdlib-only."),
        ),
        FlagSpec(
            key=POSTHOG_SURVEYS,
            env_var="POSTHOG_SURVEYS_ENABLED",
            default=False,
            owner="growth",
            description=("PostHog Surveys own NPS + the post-quality CSAT (issue #653). ON hands "
                         "those two asks to PostHog's targeting and stands the homegrown NPS "
                         "scheduler down, so a user is never prompted twice. The bespoke asks — the "
                         "review that unlocks the extended trial, the per-issue fix CSAT — are "
                         "unaffected either way."),
        ),
        FlagSpec(
            key=NEWSLETTER_EDITOR,
            env_var="NEWSLETTER_EDITOR_ENABLED",
            default=False,
            owner="content",
            description=("Final mechanical LLM edit pass on newsletter drafts (capitalization, grammar, "
                         "punctuation, formatting) before slop-lint review. Adds one lem-medium call per "
                         "draft."),
        ),
        FlagSpec(
            key=BRAND_SHOWCASE,
            env_var="BRAND_SHOWCASE_ENABLED",
            default=False,
            owner="marketing",
            description=("Public front-page showcase of the LEM brand account's own published posts "
                         "with their stored engagement counts (issue #1299). OFF by default — it "
                         "exposes a new public /api route and renders a marketing section."),
        ),
        FlagSpec(
            key=VIDEO_MOTION_LINT_HOLD,
            env_var="VIDEO_MOTION_LINT_HOLD_ENABLED",
            default=False,
            owner="content",
            description=("ENFORCEMENT for the motion-prompt lint (issue #1277). The lint always "
                         "grades and reports; this decides whether a HARD violation buys a steered "
                         "rewrite and then holds the render. OFF = warn-only, so the credit-spend "
                         "profile is unchanged until it is flipped."),
        ),
        FlagSpec(
            key=VIDEO_CAPTIONS,
            env_var="VIDEO_CAPTIONS_ENABLED",
            default=False,
            owner="content",
            description=("Burn the post's opening line into generated video posts for LinkedIn's "
                         "muted autoplay (issue #1278). OFF by default — it re-encodes every "
                         "video post once and changes what the feed shows. Avatar-led videos need "
                         "the separate per-user `avatar_caption_overlay` opt-in on top of this."),
        ),
        FlagSpec(
            key=OCCASION_NATIVE_PUBLISH,
            env_var="OCCASION_NATIVE_PUBLISH_ENABLED",
            default=True,
            owner="content",
            description=("Automated Selenium publishing of approved occasion/milestone drafts "
                         "through LinkedIn's native 'Celebrate an occasion' composer (issue "
                         "#1088). Shipped OFF because it is the one lane that WRITES a post "
                         "through a browser and its composer chain was ungrounded; #1621 and "
                         "#1693 grounded the route live (share box, occasion type, template "
                         "chooser) and the owner authorised the flip on #1088, so it now "
                         "defaults ON. Set the env var false to silence it again; an unconfirmed "
                         "Post click is still held at `error`, never retried."),
        ),
        FlagSpec(
            key=ANIMATED_POST,
            env_var="ANIMATED_POST_ENABLED",
            default=False,
            owner="content",
            description=("Animated loop posts (utilities/animated_loop.py, docs/animated-posts.md): "
                         "a text post's gated still is also animated into a subtle cinemagraph GIF "
                         "(one standard-tier Runway render per post) and published through the "
                         "Images API in place of the still. OFF by default — it spends a render "
                         "per post and there is no evidence yet that loops out-perform stills, so "
                         "a rollout-% here is the A/B. Any loop failure ships the still."),
        ),
        FlagSpec(
            key=CURATED_SOURCES,
            env_var="CURATED_SOURCES_ENABLED",
            default=False,
            owner="content",
            description=("Curated outside sources (utilities/curated_sources.py, "
                         "docs/curated-sources.md): collect candidates from the feed, the RSS "
                         "allowlist and the government-data watchlist, and draft at most one post "
                         "in three as a reshare, re-chart or link post crediting the source. OFF "
                         "by default. Every curated draft lands PENDING and publishes only after "
                         "an owner approval — this flag never approves anything."),
        ),
        FlagSpec(
            key=WEEKLY_REPORT,
            env_var="WEEKLY_REPORT_ENABLED",
            default=False,
            owner="analytics",
            description=("The weekly 5-minute report (utilities/weekly_report.py, issue #2632): "
                         "`GET /api/user/weekly-report` answers 404 while it is off. OFF by "
                         "default and rolled out to user 1 first. Read-only: it never sends "
                         "or writes anything."),
        ),
        FlagSpec(
            key=IMPACT_REPORT,
            env_var="IMPACT_REPORT_ENABLED",
            default=False,
            owner="analytics",
            description=("The impact-report beat (utilities/impact_report_job.py, issue #2643): "
                         "emails the owner a before/after report 14 days after each release and "
                         "a monthly baseline, and posts a one-line metric-free note on the "
                         "tracker issue. OFF by default; the admin route is not gated by it."),
        ),
        FlagSpec(
            key=PROFILE_AUDIT,
            env_var="PROFILE_AUDIT_ENABLED",
            default=False,
            owner="analytics",
            description=("The weekly and monthly profile audit beats (utilities/profile_audit_job.py, "
                         "issue #2631, Phase 1): grade the cached own-profile JSON against a "
                         "deterministic rubric and store the history. Browser-free, read-only; LEM "
                         "never edits the LinkedIn profile. OFF by default."),
        ),
        FlagSpec(
            key=MCP_SERVER,
            env_var="MCP_SERVER_ENABLED",
            default=False,
            owner="platform",
            description=("The end-user MCP server at `POST /api/mcp` (`api/mcp/`, epic #2682, "
                         "docs/agent-mcp-plan.md): will gate the endpoint per user, the Agents "
                         "tab and token mint (phase P2+; the P1 endpoint is inert and reads it "
                         "nowhere). Rollout only, OFF by default: the "
                         "safety control is the `mcp:kill` Redis key / `MCP_SERVER_KILL` env, "
                         "never this flag."),
        ),
        FlagSpec(
            key=MCP_SERVER_WRITES,
            env_var="MCP_SERVER_WRITES_ENABLED",
            default=False,
            owner="platform",
            description=("The MCP server's queue-only write tools (epic #2682, phase W2). OFF by "
                         "default; when off a write tool answers `isError` \"writes not "
                         "enabled\". Nothing it creates leaves LEM before a human approves it."),
        ),
    )
}

_POLL_INTERVAL_DEFAULT = 30

_load_lock = threading.Lock()
_loaded = False
# When the last load was ATTEMPTED, so a persistently failing load can't turn every flag check in a
# feed loop into a fresh definition fetch. None = never attempted.
_last_load_attempt: Optional[float] = None


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in _OFF_VALUES


def _spec(key: str) -> FlagSpec:
    spec = FLAGS.get(key)
    if spec is None:
        raise KeyError(f"Unregistered feature flag '{key}' — add a FlagSpec to utilities/flags.py")
    return spec


def _now() -> float:
    """Indirection so the retry cooldown is testable without patching the time module itself."""
    return time.monotonic()


def _poll_interval() -> int:
    try:
        value = int((os.getenv("POSTHOG_FLAG_POLL_SECONDS") or "").strip()
                    or _POLL_INTERVAL_DEFAULT)
    except ValueError:
        return _POLL_INTERVAL_DEFAULT
    return max(value, 1)


def distinct_id(user_id: Optional[int] = None) -> str:
    """The PostHog person a flag is evaluated against — `str(user_id)`, or the `"system"` sentinel.

    Same convention `observability.py` uses for events, deliberately: a %-rollout or a per-user
    condition then targets the SAME person the user's browser and Celery events land on, instead of
    a second identity no cohort matches.
    """
    return str(user_id) if user_id is not None else SYSTEM_DISTINCT_ID


def env_default(key: str) -> bool:
    """The value this flag has with PostHog out of the picture — the fallback AND the default."""
    spec = _spec(key)
    return _bool_env(spec.env_var, spec.default)


def local_evaluation_available() -> bool:
    """Whether a PostHog lookup is even worth attempting. A personal API key is what makes LOCAL
    evaluation possible; without it the SDK would fall back to a network call per check, which this
    module never does.
    """
    if not _bool_env("POSTHOG_FLAGS_ENABLED", True):
        return False
    if not (os.getenv("POSTHOG_API_KEY") or "").strip():
        return False
    if not runtime_api_key():
        return False
    return not getattr(posthog, "disabled", False)


def _ensure_loaded() -> bool:
    """Load flag definitions once per process and start the SDK's background poller.

    The poller is what makes a flag flip visible to a long-lived Celery worker without a restart:
    definitions refresh every `POSTHOG_FLAG_POLL_SECONDS` and every subsequent lookup reads the
    refreshed set out of memory. The load itself is ONE blocking fetch at the process's first flag
    check (the SDK caps it at 10s); everything after it is in-memory.
    """
    global _loaded, _last_load_attempt
    if _loaded:
        return True
    with _load_lock:
        if _loaded:
            return True
        # A failed load retries — but no more than once per poll interval, or a PostHog outage would
        # make every flag check in a feed loop attempt its own fetch.
        now = _now()
        if _last_load_attempt is not None and now - _last_load_attempt < _poll_interval():
            return False
        _last_load_attempt = now  # lgtm[py/unused-global-variable]
        try:
            # observability.py is the ONE place posthog's module config (api key, host, disabled)
            # lives. Imported lazily HERE — not at module import — so this module can't create an
            # import cycle, and so the config is applied before setup() builds the client.
            import cqc_lem.utilities.observability  # noqa: F401

            posthog.personal_api_key = runtime_api_key()
            posthog.poll_interval = _poll_interval()
            client = posthog.setup()
            # setup() REUSES an existing client, and the first capture() may well have built one
            # before a personal key was ever set — that client would never poll. Push the settings
            # onto it directly so an already-running process still gets local evaluation.
            client.personal_api_key = posthog.personal_api_key
            client.poll_interval = posthog.poll_interval
            posthog.load_feature_flags()
            _loaded = True
            log_debug(f"PostHog feature-flag local evaluation loaded "
                      f"(poll every {posthog.poll_interval}s)", api_provider="posthog")
        except Exception as exc:
            # Never poison the process: a failed load just means every flag reads its env var
            # until the cooldown above lets the next attempt through.
            log_warning("PostHog feature-flag local evaluation unavailable — using env defaults",
                        exc=exc, api_provider="posthog")
            return False
    return True


def _coerce(value) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    return text not in _OFF_VALUES


def resolve_flag(key: str, user_id: Optional[int] = None) -> Optional[bool]:
    """PostHog's answer for this flag, or None when it has no usable one (every fallback path)."""
    if not local_evaluation_available() or not _ensure_loaded():
        return None
    try:
        value = posthog.get_feature_flag(
            key,
            distinct_id(user_id),
            # No network request, ever — this runs inside feed loops. An undecidable flag returns
            # None and the caller falls back to the env var.
            only_evaluate_locally=True,
            # A $feature_flag_called per checked feed post is volume nobody reads.
            send_feature_flag_events=False,
        )
    except Exception as exc:
        log_warning(f"Feature flag '{key}' lookup failed — using env default", exc=exc,
                    user_id=user_id, api_provider="posthog")
        return None
    return _coerce(value)


def flag_enabled(key: str, user_id: Optional[int] = None) -> bool:
    """The value of a registered toggle: PostHog's if it has one, otherwise the env var's."""
    value = resolve_flag(key, user_id)
    if value is None:
        return env_default(key)
    return value


def all_flags(user_id: Optional[int] = None) -> Dict[str, bool]:
    """Every registered flag resolved for one identity — the SPA's bootstrap payload, so the browser
    renders with the SAME values the API and the workers just used instead of flickering.
    """
    return {key: flag_enabled(key, user_id) for key in FLAGS}


def bootstrap_payload(user_id: Optional[int] = None) -> dict:
    """What `GET /api/flags` returns. `local_evaluation` is the honest half: false means every value
    below is an env default, not a PostHog decision.
    """
    resolved = all_flags(user_id)
    return {
        "distinct_id": distinct_id(user_id),
        "flags": resolved,
        "local_evaluation": local_evaluation_available() and _loaded,
    }


def registry_rows() -> list:
    """The registry as plain dicts — for docs generation and the admin/registry test."""
    return [{"key": spec.key, "env_var": spec.env_var, "default": spec.default,
             "owner": spec.owner, "description": spec.description}
            for spec in FLAGS.values()]


def reset_flag_state() -> None:
    """Drop the loaded-definitions latch AND the retry cooldown. For tests (and a future admin
    reload); the SDK's own poller is left alone.
    """
    global _loaded, _last_load_attempt
    with _load_lock:
        _loaded = False
        _last_load_attempt = None
