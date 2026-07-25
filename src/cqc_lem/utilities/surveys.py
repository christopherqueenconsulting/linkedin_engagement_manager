"""NPS/CSAT + review capture (issue #501) — the survey channels of the feedback->auto-work loop.

Responses are stored as ordinary `feedback` rows (source='nps' / source='review') so they flow
straight into the existing classifier + clustering + auto-FAQ pipeline; the score/rating and the
consent flag ride along in `context_json`. Only the ASK is tracked separately (`survey_prompts`),
which is what keeps each survey one-shot per user across the in-app modal and the email.

`select_survey` is deliberately a PURE function of timestamps + what has already been asked/answered,
so the "who gets asked what, when" policy is unit-testable without a DB, an email, or an LLM — same
posture as `onboarding.select_nudge`.

A review row is also the gate on the extended trial (issue #499): `db.has_review_feedback`.

Issue #502 adds a fourth, per-issue survey: the micro-CSAT asked after a fix the user reported
ships ("did this fix it?"). Its key is BUILT (`fix_csat_<issue>`) rather than listed in `_SURVEYS`,
and it is scheduled by the shipped-notice queue rather than by `select_survey`.
"""

from datetime import datetime, timedelta
from typing import Optional

from cqc_lem.utilities.db import (
    FeedbackSource, FeedbackStatus, get_latest_feedback_at, get_onboarding_state,
    get_survey_prompts_sent, get_user_subscription_info, has_review_feedback, insert_feedback,
    record_survey_prompt, update_feedback_triage,
)
from cqc_lem.utilities.logger import log_warning

# Survey keys (also the survey_prompts PK half — each is asked at most once per user).
SURVEY_NPS_DAY3 = "nps_day3"
SURVEY_NPS_TRIAL_END = "nps_trial_end"
SURVEY_REVIEW_TRIAL_END = "review_trial_end"

# The micro-CSAT that follows a shipped fix (issue #502) is per-ISSUE, so its key is built rather
# than listed: `fix_csat_<issue>` (well inside survey_prompts.survey_key's VARCHAR(32)).
FIX_CSAT_PREFIX = "fix_csat_"

# Ask for NPS this long after the user activated — early enough that the "aha" is fresh, late enough
# that they've seen automation run for a few days.
NPS_AFTER_ACTIVATION_DAYS = 3
# Trial T-3d: the last useful moment to ask, and where the review-for-extended-trial offer lands.
TRIAL_ENDING_WITHIN_DAYS = 3
# At most one survey ask per user per this many hours, whatever the beat cadence.
SURVEY_COOLDOWN_HOURS = 24
# Promoters get invited to turn that score into a review right after they answer.
PROMOTER_SCORE = 9

NPS_MIN, NPS_MAX = 0, 10
# Public review sites LEM syndicates to (marketplace/G2) are 5-star, so the review form is too.
REVIEW_MIN_RATING, REVIEW_MAX_RATING = 1, 5

_SURVEYS: dict = {
    SURVEY_NPS_DAY3: {
        "key": SURVEY_NPS_DAY3,
        "source": str(FeedbackSource.NPS),
        "subject": "Quick one: how likely are you to recommend LEM?",
        "headline": "How likely are you to recommend LEM?",
        "body": ("You've had your first posts and automated engagement go out. One number, 0–10 — "
                 "and if you have 10 seconds more, tell us why."),
        "cta_label": "Give a score",
    },
    SURVEY_NPS_TRIAL_END: {
        "key": SURVEY_NPS_TRIAL_END,
        "source": str(FeedbackSource.NPS),
        "subject": "Before your trial ends — how did we do?",
        "headline": "How likely are you to recommend LEM?",
        "body": ("Your trial wraps up in a few days. One number, 0–10, tells us whether LEM earned "
                 "its keep — and what to fix if it didn't."),
        "cta_label": "Give a score",
    },
    SURVEY_REVIEW_TRIAL_END: {
        "key": SURVEY_REVIEW_TRIAL_END,
        "source": str(FeedbackSource.REVIEW),
        "subject": "Leave a review, unlock the extended trial",
        "headline": "Write a review, keep LEM running longer",
        "body": ("Your trial ends in a few days. Leave an honest review — a rating and what would "
                 "make LEM a 10 — and you unlock the early-adopter extended trial."),
        "cta_label": "Write a review",
    },
}

# Every survey opens the same in-app modal; the key tells the SPA which form to render.
_CTA_PATH = "/?survey="


def nps_bucket(score: int) -> str:
    """Standard NPS banding, stored as the feedback row's sentiment."""
    if score >= PROMOTER_SCORE:
        return "promoter"
    if score >= 7:
        return "passive"
    return "detractor"


def review_sentiment(rating: int) -> str:
    """CSAT banding for a 1-5 review rating."""
    if rating >= 4:
        return "positive"
    return "neutral" if rating == 3 else "negative"


def _survey(key: str) -> dict:
    survey = dict(_SURVEYS[key])
    survey["cta_path"] = f"{_CTA_PATH}{key}"
    return survey


def select_survey(activated_at: Optional[datetime] = None,
                  trial_ends_at: Optional[datetime] = None,
                  nps_answered_at: Optional[datetime] = None,
                  review_answered_at: Optional[datetime] = None,
                  sent_keys=(), last_sent_at: Optional[datetime] = None,
                  now: Optional[datetime] = None) -> Optional[dict]:
    """The one survey worth asking this user right now, or None.

    Rules: each survey is asked at most once (`sent_keys`); at most one ask per
    SURVEY_COOLDOWN_HOURS (`last_sent_at`); an already-answered survey is never re-asked. At trial
    T-3d the review offer outranks NPS — it is the ask that gives the user something back."""
    now = now or datetime.now()
    if last_sent_at and now - last_sent_at < timedelta(hours=SURVEY_COOLDOWN_HOURS):
        return None

    sent = set(sent_keys or ())
    trial_ending = bool(trial_ends_at) and (
        timedelta(0) < trial_ends_at - now <= timedelta(days=TRIAL_ENDING_WITHIN_DAYS))

    if trial_ending and not review_answered_at and SURVEY_REVIEW_TRIAL_END not in sent:
        return _survey(SURVEY_REVIEW_TRIAL_END)

    if not nps_answered_at:
        if (activated_at and SURVEY_NPS_DAY3 not in sent
                and now - activated_at >= timedelta(days=NPS_AFTER_ACTIVATION_DAYS)):
            return _survey(SURVEY_NPS_DAY3)
        if trial_ending and SURVEY_NPS_TRIAL_END not in sent:
            return _survey(SURVEY_NPS_TRIAL_END)
    return None


def next_survey_for_user(user_id: int) -> Optional[dict]:
    """`select_survey` bound to a real user — used by both the beat task and the in-app modal."""
    state = get_onboarding_state(user_id) or {}
    sub = get_user_subscription_info(user_id) or {}
    sent = get_survey_prompts_sent(user_id)
    return select_survey(
        activated_at=state.get("activated_at"),
        trial_ends_at=sub.get("trial_ends_at"),
        nps_answered_at=get_latest_feedback_at(user_id, FeedbackSource.NPS),
        review_answered_at=get_latest_feedback_at(user_id, FeedbackSource.REVIEW),
        sent_keys=set(sent),
        last_sent_at=max(sent.values()) if sent else None,
    )


def survey_snapshot(user_id: int) -> dict:
    """Everything the SPA modal needs: the survey to ask (if any) and whether the review that
    unlocks the extended trial (issue #499) is already on file."""
    survey = next_survey_for_user(user_id)
    return {
        "survey": {k: survey[k] for k in ("key", "source", "headline", "body", "cta_label")}
        if survey else None,
        "review_submitted": has_review_feedback(user_id),
        "nps_min": NPS_MIN, "nps_max": NPS_MAX,
        "review_min_rating": REVIEW_MIN_RATING, "review_max_rating": REVIEW_MAX_RATING,
    }


def record_nps_response(user_id: Optional[int], score: int, why: Optional[str] = None,
                        context: Optional[dict] = None,
                        survey_key: Optional[str] = None) -> Optional[int]:
    """Persist an NPS answer as a `feedback` row (source='nps'). The free-text 'why' is the body so
    the classifier can act on it; the score rides in context_json and as the NPS band."""
    text = (why or "").strip()
    body = text or f"NPS {score}/{NPS_MAX} — no comment"
    payload = {**(context or {}), "score": int(score), "survey_key": survey_key}
    feedback_id = insert_feedback(body, user_id=user_id, source=FeedbackSource.NPS,
                                 type_hint="nps", context=payload,
                                 sentiment=nps_bucket(int(score)))
    if feedback_id and user_id:
        _record_answer(user_id, survey_key)
        _track_response(user_id, str(FeedbackSource.NPS), score=int(score))
    return feedback_id


def record_review_response(user_id: Optional[int], rating: int, improvement: Optional[str] = None,
                           testimonial: Optional[str] = None, consent_testimonial: bool = False,
                           context: Optional[dict] = None,
                           survey_key: Optional[str] = None) -> Optional[int]:
    """Persist a review as a `feedback` row (source='review'). This row IS the extended-trial gate
    (issue #499), and its body feeds the classifier + auto-FAQ like any other feedback."""
    parts = [p.strip() for p in (testimonial, improvement) if p and p.strip()]
    body = "\n\n".join(parts) or f"Review {rating}/{REVIEW_MAX_RATING} — no comment"
    payload = {**(context or {}), "rating": int(rating),
               "improvement": (improvement or "").strip() or None,
               "testimonial": (testimonial or "").strip() or None,
               "consent_testimonial": bool(consent_testimonial), "survey_key": survey_key}
    feedback_id = insert_feedback(body, user_id=user_id, source=FeedbackSource.REVIEW,
                                 type_hint="review", context=payload,
                                 sentiment=review_sentiment(int(rating)))
    if feedback_id and user_id:
        _record_answer(user_id, survey_key)
        _track_response(user_id, str(FeedbackSource.REVIEW), rating=int(rating),
                        consent_testimonial=bool(consent_testimonial))
    return feedback_id


def fix_csat_key(issue_number: int) -> str:
    """The survey_prompts key for the "did this fix it?" ask on one shipped issue (issue #502)."""
    return f"{FIX_CSAT_PREFIX}{int(issue_number)}"


def is_fix_csat_key(survey_key: Optional[str]) -> bool:
    """True for a per-issue micro-CSAT key. These are generated, not listed in `_SURVEYS`, so every
    key check has to accept them too."""
    key = (survey_key or "")
    return key.startswith(FIX_CSAT_PREFIX) and key[len(FIX_CSAT_PREFIX):].isdigit()


def _known_key(survey_key: Optional[str]) -> bool:
    return survey_key in _SURVEYS or is_fix_csat_key(survey_key)


def record_fix_csat_response(user_id: Optional[int], issue_number: int, resolved: bool,
                             comment: Optional[str] = None,
                             context: Optional[dict] = None) -> Optional[int]:
    """Persist a "did this fix it?" answer (issue #502) as a `feedback` row (source='csat').

    A "no, still broken" is left at status `new` ON PURPOSE: it re-enters the auto-work loop like any
    other report, so the cluster gets re-opened rather than the dissatisfaction being buried in an
    analytics event. A "yes" is stamped `resolved` immediately — there is nothing to classify, and
    it must not burn an LLM call on every happy answer."""
    key = fix_csat_key(issue_number)
    text = (comment or "").strip()
    body = text or (f"Confirmed fixed (issue #{int(issue_number)})" if resolved
                    else f"Still not fixed (issue #{int(issue_number)})")
    payload = {**(context or {}), "issue_number": int(issue_number), "resolved": bool(resolved),
               "survey_key": key}
    feedback_id = insert_feedback(body, user_id=user_id, source=FeedbackSource.CSAT,
                                  type_hint="fix_csat", context=payload,
                                  sentiment="positive" if resolved else "negative")
    if feedback_id and resolved:
        update_feedback_triage(feedback_id, status=FeedbackStatus.RESOLVED)
    if feedback_id and user_id:
        _record_answer(user_id, key)
        _track_response(user_id, str(FeedbackSource.CSAT), issue_number=int(issue_number),
                        resolved=bool(resolved))
    return feedback_id


def _record_answer(user_id: int, survey_key: Optional[str]) -> None:
    """Close the loop on the ask that produced this answer so nothing re-asks it."""
    if _known_key(survey_key):
        record_survey_prompt(user_id, survey_key)


def _track_response(user_id: int, source: str, **properties) -> None:
    """Analytics must never break a survey submission."""
    try:
        from cqc_lem.utilities.observability import track_survey_response
        track_survey_response(user_id, source, **properties)
    except Exception as e:
        log_warning("Could not track survey response", exc=e, user_id=user_id)


def dismiss_survey(user_id: int, survey_key: str) -> bool:
    """User closed the modal without answering — record the ask so it isn't shown or emailed again."""
    if not _known_key(survey_key):
        return False
    return record_survey_prompt(user_id, survey_key)


def send_survey_prompt(user_id: int, survey: dict) -> bool:
    """Email the survey invite and record the ask so it never goes out twice. Returns True only when
    an email actually went out."""
    from cqc_lem.utilities.notifications import notify_survey_prompt
    if not notify_survey_prompt(user_id, survey):
        return False
    record_survey_prompt(user_id, survey["key"])
    return True
