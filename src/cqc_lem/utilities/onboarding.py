"""Activation checklist + stalled-user nudges (issue #500).

"Aha" is the first AI post published AND the first automated comment/DM sent under the user's voice,
within 7 days. Step completion is DERIVED from data we already store (session cookie, engagement
preferences, posts, logs) and the first flip of each step is persisted in `onboarding_state`, so the
SPA checklist is stable, the PostHog step event fires exactly once, and the nudge clock has a start.

Nudge selection (`select_nudge`) is deliberately a PURE function of the checklist + timestamps so the
"who gets nudged, with what, when" policy is unit-testable without touching the DB, email, or an LLM.
"""

from datetime import datetime, timedelta
from typing import Optional

from cqc_lem.utilities.db import (
    OnboardingStep, ONBOARDING_STEPS, PostStatus,
    ensure_onboarding_state, get_onboarding_state, mark_onboarding_step,
    get_onboarding_nudges_sent, record_onboarding_nudge,
    get_engagement_preferences, has_engagement_preferences, has_linkedin_session,
    has_post_with_status, has_automated_engagement, get_user_subscription_info,
)
from cqc_lem.utilities.logger import log_info, log_warning

# The checklist as the SPA renders it: label + where the user goes to finish the step.
STEP_LABELS: dict = {
    OnboardingStep.LINKEDIN_CONNECTED: "Connect LinkedIn",
    OnboardingStep.VOICE_SET: "Set your voice & targeting",
    OnboardingStep.FIRST_POST_APPROVED: "Approve your first post",
    OnboardingStep.CAPS_ENABLED: "Turn on daily engagement",
    OnboardingStep.ACTIVATED: "Activated — first post live + first auto-engagement",
}
STEP_HINTS: dict = {
    OnboardingStep.LINKEDIN_CONNECTED: "Automation can't post or engage without an authorized session.",
    OnboardingStep.VOICE_SET: "Tone, topics, and who to engage with — this is what makes it sound like you.",
    OnboardingStep.FIRST_POST_APPROVED: "Approve a drafted post so your content plan starts publishing.",
    OnboardingStep.CAPS_ENABLED: "Set how many comments and DMs a day you're comfortable with.",
    OnboardingStep.ACTIVATED: "Nothing to do — this flips on its own once both have happened.",
}
STEP_PATHS: dict = {
    OnboardingStep.LINKEDIN_CONNECTED: "/account",
    OnboardingStep.VOICE_SET: "/account",
    OnboardingStep.FIRST_POST_APPROVED: "/content?tab=review",
    OnboardingStep.CAPS_ENABLED: "/account",
    OnboardingStep.ACTIVATED: "/",
}

# Nudge keys (also the onboarding_nudges PK half — each fires at most once per user).
NUDGE_CONNECT = "connect_linkedin"
NUDGE_VOICE = "set_voice"
NUDGE_FIRST_POST = "approve_post"
NUDGE_TRIAL_ENDING = "trial_ending"

# Step nudges, in checklist order: the earliest incomplete step whose grace period has elapsed wins.
_STEP_NUDGES: tuple = (
    {
        "key": NUDGE_CONNECT,
        "step": OnboardingStep.LINKEDIN_CONNECTED,
        "after_hours": 24,
        "subject": "Connect LinkedIn to switch your automation on",
        "headline": "One step left before anything can run",
        "body": ("Your account is ready, but LinkedIn isn't connected yet — so nothing can post, "
                 "comment, or send. It takes about a minute from your account page."),
        "cta_label": "Connect LinkedIn",
        "cta_path": "/account",
    },
    {
        "key": NUDGE_VOICE,
        "step": OnboardingStep.VOICE_SET,
        "after_hours": 48,
        "subject": "Tell us how you sound before we post for you",
        "headline": "Set your voice so it sounds like you",
        "body": ("LinkedIn is connected. Next: pick your tone, the topics you want to be known for, "
                 "and who's worth engaging — that's what keeps every comment and post yours."),
        "cta_label": "Set your voice",
        "cta_path": "/account",
    },
    {
        "key": NUDGE_FIRST_POST,
        "step": OnboardingStep.FIRST_POST_APPROVED,
        "after_hours": 72,
        "subject": "Your drafts are waiting on one approval",
        "headline": "Approve one post and the plan starts running",
        "body": ("We've drafted content in your voice, but nothing publishes until you approve the "
                 "first one. Review it, edit anything you'd say differently, and approve."),
        "cta_label": "Review your drafts",
        "cta_path": "/content?tab=review",
    },
)

# Sent when the trial is within this many days of ending and the user still hasn't activated.
TRIAL_ENDING_WITHIN_DAYS = 3
_TRIAL_NUDGE: dict = {
    "key": NUDGE_TRIAL_ENDING,
    "step": OnboardingStep.ACTIVATED,
    "subject": "Your trial ends soon — let's get your first post live",
    "headline": "Your trial ends in a few days",
    "body": ("You haven't seen LEM actually run yet. Finish the last setup step and your first post "
             "plus your first automated comment can go out before the trial is up."),
    "cta_label": "Finish setup",
    "cta_path": "/account",
}

# At most one onboarding nudge per user per this many hours, whatever the beat cadence.
NUDGE_COOLDOWN_HOURS = 24


def evaluate_steps(user_id: int) -> dict:
    """Live truth for each checklist step, derived from what the user has actually done."""
    prefs = get_engagement_preferences(user_id)
    configured = has_engagement_preferences(user_id)
    posted = has_post_with_status(user_id, (PostStatus.POSTED,))
    voice_set = configured and bool(
        prefs.get("tone") or prefs.get("comment_style") or prefs.get("focus_topics")
        or prefs.get("include_topics"))
    caps_enabled = configured and (int(prefs.get("max_comments_per_day") or 0) > 0
                                   or int(prefs.get("max_dms_per_day") or 0) > 0)
    return {
        OnboardingStep.LINKEDIN_CONNECTED: has_linkedin_session(user_id),
        OnboardingStep.VOICE_SET: voice_set,
        OnboardingStep.FIRST_POST_APPROVED: has_post_with_status(
            user_id, (PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.POSTED)),
        OnboardingStep.CAPS_ENABLED: caps_enabled,
        # The aha moment: a published post AND automation having engaged in the user's voice.
        OnboardingStep.ACTIVATED: posted and has_automated_engagement(user_id),
    }


def sync_onboarding_state(user_id: int) -> dict:
    """Persist newly-completed steps and emit one PostHog event per step the FIRST time it completes.
    Returns the refreshed `onboarding_state` row."""
    ensure_onboarding_state(user_id)
    steps = evaluate_steps(user_id)
    state = get_onboarding_state(user_id)
    for step in ONBOARDING_STEPS:
        if not steps.get(step) or state.get(f"{step.value}_at"):
            continue
        if mark_onboarding_step(user_id, step):
            _track_step(user_id, step, state.get("started_at"))
    return get_onboarding_state(user_id)


def _track_step(user_id: int, step: "OnboardingStep", started_at: Optional[datetime]) -> None:
    """Emit the activation-funnel event. Never fatal — analytics must not break onboarding."""
    try:
        from cqc_lem.utilities.observability import (
            track_onboarding_step, track_funnel_event,
            FUNNEL_ONBOARDING_STEP_COMPLETED, FUNNEL_ACTIVATED,
        )
        hours = None
        if started_at:
            hours = round(max(0.0, (datetime.now() - started_at).total_seconds() / 3600.0), 2)
        track_onboarding_step(user_id, str(step), hours_since_start=hours)
        # Same completion, second event: the checklist funnel (issue #503) is queried across signup
        # → activation, so it needs one stable event name for every step rather than per-step names.
        track_funnel_event(FUNNEL_ONBOARDING_STEP_COMPLETED, user_id=user_id, step=str(step),
                           hours_since_start=hours)
        if step == OnboardingStep.ACTIVATED:
            track_funnel_event(FUNNEL_ACTIVATED, user_id=user_id, hours_since_start=hours)
        log_info(f"Onboarding step completed: {step}", user_id=user_id)
    except Exception as e:
        log_warning("Could not track onboarding step", exc=e, user_id=user_id)


def select_nudge(steps: dict, started_at: Optional[datetime] = None,
                 trial_ends_at: Optional[datetime] = None, sent_keys=(),
                 last_sent_at: Optional[datetime] = None,
                 now: Optional[datetime] = None) -> Optional[dict]:
    """The next-best nudge for a stalled user, or None when there's nothing worth sending.

    Rules: activated users are never nudged; each nudge sends at most once (`sent_keys`); at most one
    nudge per NUDGE_COOLDOWN_HOURS (`last_sent_at`); a step nudge only fires once its grace period
    has elapsed since `started_at`; the earliest incomplete step wins, and the trial-ending nudge is
    the fallback once the blocking step has already been nudged."""
    now = now or datetime.now()
    if steps.get(OnboardingStep.ACTIVATED):
        return None
    if last_sent_at and now - last_sent_at < timedelta(hours=NUDGE_COOLDOWN_HOURS):
        return None

    sent = set(sent_keys or ())
    age_hours = max(0.0, (now - started_at).total_seconds() / 3600.0) if started_at else 0.0

    for nudge in _STEP_NUDGES:
        if steps.get(nudge["step"]):
            continue  # step done — check the next one in the checklist
        if age_hours >= nudge["after_hours"] and nudge["key"] not in sent:
            return dict(nudge)
        # A step that is incomplete but still inside its grace period blocks the later step nudges
        # (they'd be premature), but not the time-critical trial warning below.
        break

    if trial_ends_at and NUDGE_TRIAL_ENDING not in sent:
        remaining = trial_ends_at - now
        if timedelta(0) < remaining <= timedelta(days=TRIAL_ENDING_WITHIN_DAYS):
            return dict(_TRIAL_NUDGE)
    return None


def next_nudge_for_user(user_id: int, state: Optional[dict] = None) -> Optional[dict]:
    """`select_nudge` bound to a real user — used by both the beat task and the in-app banner."""
    state = state if state is not None else get_onboarding_state(user_id)
    steps = {step: bool(state.get(f"{step.value}_at")) for step in ONBOARDING_STEPS}
    sent = get_onboarding_nudges_sent(user_id)
    sub = get_user_subscription_info(user_id) or {}
    last_sent = max(sent.values()) if sent else None
    return select_nudge(steps, started_at=state.get("started_at"),
                        trial_ends_at=sub.get("trial_ends_at"), sent_keys=set(sent),
                        last_sent_at=last_sent)


def onboarding_snapshot(user_id: int) -> dict:
    """Everything the SPA needs: the persisted checklist plus the nudge to surface in-app."""
    state = sync_onboarding_state(user_id)
    steps = []
    for step in ONBOARDING_STEPS:
        completed_at = state.get(f"{step.value}_at")
        steps.append({
            "key": str(step),
            "label": STEP_LABELS[step],
            "hint": STEP_HINTS[step],
            "path": STEP_PATHS[step],
            "ok": bool(completed_at),
            "completed_at": completed_at.isoformat() if completed_at else None,
        })
    nudge = next_nudge_for_user(user_id, state)
    return {
        "activated": bool(state.get(f"{OnboardingStep.ACTIVATED.value}_at")),
        "started_at": state["started_at"].isoformat() if state.get("started_at") else None,
        "steps": steps,
        "nudge": {"key": nudge["key"], "headline": nudge["headline"], "body": nudge["body"],
                  "cta_label": nudge["cta_label"], "cta_path": nudge["cta_path"]} if nudge else None,
    }


def send_onboarding_nudge(user_id: int, nudge: dict) -> bool:
    """Email the nudge (copy refreshed by `lem-simple`, falling back to the static body) and record
    it so it never sends twice. Returns True only when an email actually went out."""
    from cqc_lem.utilities.notifications import notify_onboarding_nudge
    body = nudge.get("body") or ""
    try:
        from cqc_lem.utilities.ai.ai_helper import generate_onboarding_nudge_copy
        body = generate_onboarding_nudge_copy(user_id, nudge) or body
    except Exception as e:
        log_warning("Onboarding nudge copy generation failed — using default copy",
                    exc=e, user_id=user_id)
    if not notify_onboarding_nudge(user_id, {**nudge, "body": body}):
        return False
    record_onboarding_nudge(user_id, nudge["key"])
    return True
