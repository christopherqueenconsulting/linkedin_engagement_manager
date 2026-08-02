"""The affiliate / ambassador program's ONE decision core (issue #737).

Everything policy-shaped about the program lives here and nowhere else: who may be enrolled, what a
referral link looks like, how many trial days a reward is worth, and — the part with legal teeth —
whether a piece of content is affiliate promotion and whether it carries its FTC disclosure. `db.py`
owns the rows, `api/main.py` owns the HTTP, this module owns the rules.

Two things the issue insists are kept apart, and this module keeps them apart by never mixing them
in one function:

- **(A) affiliate STATUS** — enrolled by default, one click out. It grants a referral link and trial
  time. `enrollment_default()`, `is_eligible()`, `referral_bonus_days()`.
- **(B) promotional CONTENT from the user's own LinkedIn account** — strictly opt-IN, recorded with a
  timestamp and a consent version. `promo_content_allowed()` is the ONLY gate that answers it, and it
  reads the stored consent — there is deliberately no env var, no fleet default and no "enrolled
  implies consented" shortcut, because the account is the user's professional identity.

FTC 16 CFR Part 255: extra trial time IS compensation, so an affiliate who promotes LEM must
disclose the material connection *in the promotional content itself*. `disclosure_report()` is the
deterministic check the publish path runs — same shape as the #625 slop lint and the #617 comment
contract, so it composes with the gates already in the pipeline rather than becoming a second kind
of verdict. It is deliberately a REPORT, not a rewrite: `apply_disclosure()` (generation time) makes
sure the disclosure is there in the first place, and the publish gate's job is only to refuse
content that somehow arrived without it.
"""

from __future__ import annotations

import re
from typing import Optional

from cqc_lem.utilities.env_constants import (AFFILIATE_DEFAULT_ENROLLED, AFFILIATE_DISCLOSURE_TEXT,
                                             AFFILIATE_ENROLLMENT_BONUS_DAYS,
                                             AFFILIATE_MAX_REWARD_DAYS,
                                             AFFILIATE_PROGRAM_ENABLED,
                                             AFFILIATE_PROMO_CONSENT_VERSION,
                                             AFFILIATE_REFERRAL_BONUS_DAYS,
                                             AFFILIATE_REQUIRE_COMPANY_PAGE)
from cqc_lem.utilities.db import AffiliateRewardKind, AffiliateStatus, ReferralStatus
from cqc_lem.utilities.marketing.attribution import (REFERRAL_PARAM, is_owned_link, referral_code,
                                                     referral_url)

# The stored values come from `db.py`'s enums, per CLAUDE.md — spelled once here so policy code can
# compare without importing the DB layer at every call site.
STATUS_ENROLLED = str(AffiliateStatus.ENROLLED)
STATUS_OPTED_OUT = str(AffiliateStatus.OPTED_OUT)
# Not a stored status — `ineligible` is computed from the current eligibility rule, so a user who
# adds a company page tomorrow becomes eligible without a backfill.
STATUS_INELIGIBLE = "ineligible"

REWARD_ENROLLMENT = str(AffiliateRewardKind.ENROLLMENT)
REWARD_REFERRAL = str(AffiliateRewardKind.REFERRAL)
REWARD_REVOKED = str(AffiliateRewardKind.REVOKED)

REFERRAL_PENDING = str(ReferralStatus.PENDING)
REFERRAL_CONVERTED = str(ReferralStatus.CONVERTED)
REFERRAL_REJECTED = str(ReferralStatus.REJECTED)

REJECT_SELF_REFERRAL = "self_referral"
REJECT_DUPLICATE = "duplicate"
REJECT_UNKNOWN_REFERRER = "unknown_referrer"
REJECT_NOT_ENROLLED = "referrer_not_enrolled"

# What counts as a disclosure. The configured sentence is the one LEM writes, but a user editing
# their own draft may reword it, and the FTC cares about substance, not our exact string — so a
# recognised hashtag or an explicit compensated-relationship phrase counts too. Kept deliberately
# tight: "partner" or "link" alone are not disclosures.
_DISCLOSURE_HASHTAGS = ("#ad", "#sponsored", "#affiliate", "#paidpartnership")
_DISCLOSURE_PHRASES = (
    "affiliate link",
    "affiliate of",
    "i'm an affiliate",
    "i am an affiliate",
    "affiliate program",
    "material connection",
    "paid partnership",
    "i receive free",
    "i get free",
    "i earn free",
    "compensated",
)
_WORD_BOUNDARY = re.compile(r"[a-z0-9_#']+")


def program_enabled() -> bool:
    """Whether this deployment runs the program at all. A dev/CI environment that mints no referral
    links reads exactly like the pre-#737 product."""
    return bool(AFFILIATE_PROGRAM_ENABLED)


def enrollment_default() -> str:
    """The status a brand-new user starts at. Default-on is the owner's intent AND the low-risk half
    of the split — it is (A), not (B)."""
    return STATUS_ENROLLED if (program_enabled() and AFFILIATE_DEFAULT_ENROLLED) else STATUS_OPTED_OUT


def is_eligible(has_company_page: bool = True) -> bool:
    """Whether a user may hold affiliate status. The company-page boundary is opt-in per environment
    (`AFFILIATE_REQUIRE_COMPANY_PAGE`); with it off, every user is eligible. Eligibility is evaluated
    LIVE rather than frozen at signup, so adding or removing a page moves the user immediately."""
    if not program_enabled():
        return False
    return bool(has_company_page) if AFFILIATE_REQUIRE_COMPANY_PAGE else True


def code_for_user(user_id: Optional[int]) -> str:
    """The referral code stamped on a member's link. It is #658's `ref` value (the user id), NOT a
    new identifier: the capture side already resolves a person by it, and a second code space would
    mean two ways to answer the same question."""
    return referral_code(user_id)


def link_for_user(user_id: Optional[int], base: Optional[str] = None) -> str:
    """A member's shareable referral URL, or "" when no signup URL is configured."""
    return referral_url(user_id, base=base)


def parse_referrer_id(ref: Optional[str]) -> Optional[int]:
    """The referrer's user id off an inbound `ref` value, or None when it isn't one. Anything
    non-numeric or non-positive is somebody's typo (or a probe), never a referrer."""
    try:
        value = int(str(ref or "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# --- reward sizing --------------------------------------------------------------------------------
# Trial days are the currency, which is why the cap matters: a trial day is not free — it is a day of
# LLM + proxy COGS against zero MRR (docs/cost-performance-margin-plan.md). The two rewards are
# deliberately different KINDS, and the difference is what makes opt-out honest rather than a dark
# pattern: the enrollment bonus is granted for HOLDING status and revoked when status ends (the user
# returns to the standard trial), while a referral bonus was EARNED by driving a real activation and
# is never clawed back.

def enrollment_bonus_days() -> int:
    return max(0, int(AFFILIATE_ENROLLMENT_BONUS_DAYS))


def referral_bonus_days() -> int:
    return max(0, int(AFFILIATE_REFERRAL_BONUS_DAYS))


def max_reward_days() -> int:
    return max(0, int(AFFILIATE_MAX_REWARD_DAYS))


def grantable_days(already_granted: int, requested: int) -> int:
    """How many of `requested` days may actually be granted under the per-user cap. Returns 0 rather
    than a negative number when the user is already at or over the ceiling, so a caller can treat
    "nothing to grant" and "capped out" the same way."""
    cap = max_reward_days()
    remaining = cap - max(0, int(already_granted))
    return max(0, min(int(requested), remaining))


# --- (B) promotional content ---------------------------------------------------------------------

def promo_consent_version() -> str:
    return str(AFFILIATE_PROMO_CONSENT_VERSION or "v1")


def promo_content_allowed(enrollment: Optional[dict]) -> bool:
    """Whether LEM may publish promotional content about LEM from this user's own account.

    Requires ALL of: the program on, the user still enrolled, the opt-in flag set, AND a recorded
    consent timestamp. The timestamp is not decoration — a flag with no consent record is a row that
    was set by something other than the user saying yes, and that is exactly the case this must
    refuse."""
    if not program_enabled() or not isinstance(enrollment, dict):
        return False
    if str(enrollment.get("status") or "") != STATUS_ENROLLED:
        return False
    if not bool(enrollment.get("promo_content_opt_in")):
        return False
    return enrollment.get("promo_consent_at") is not None


# --- FTC disclosure -------------------------------------------------------------------------------

def disclosure_text() -> str:
    """The disclosure LEM stamps on affiliate content. Empty means this deployment has no disclosure
    configured, which `disclosure_report` treats as "affiliate content cannot be published here" —
    never as "no disclosure needed"."""
    return str(AFFILIATE_DISCLOSURE_TEXT or "").strip()


def _normalized(text: Optional[str]) -> str:
    return " ".join(_WORD_BOUNDARY.findall(str(text or "").lower()))


def has_disclosure(text: Optional[str]) -> bool:
    """Whether `text` already discloses the material connection — the configured sentence, one of the
    recognised hashtags, or an explicit compensated-relationship phrase."""
    normalized = _normalized(text)
    if not normalized:
        return False
    configured = _normalized(disclosure_text())
    if configured and configured in normalized:
        return True
    tokens = set(normalized.split())
    if any(tag in tokens for tag in _DISCLOSURE_HASHTAGS):
        return True
    return any(phrase in normalized for phrase in _DISCLOSURE_PHRASES)


def carries_referral_link(text: Optional[str], user_id: Optional[int] = None) -> bool:
    """Whether `text` publishes a LEM referral link — an owned-domain URL carrying `ref=`.

    This is the operative definition of affiliate content, and it is the right one: the material
    connection is created by the link that earns the user trial time, not by the topic. When
    `user_id` is given only THAT member's code counts, so one user's post quoting somebody else's
    link is not silently attributed to them."""
    body = str(text or "")
    if not body:
        return False
    wanted = code_for_user(user_id) if user_id is not None else None
    for match in re.finditer(r"https?://[^\s<>\"')\]]+", body):
        link = match.group(0).rstrip(".,;:!?)]")
        if not is_owned_link(link):
            continue
        found = re.search(rf"[?&]{re.escape(REFERRAL_PARAM)}=([^&#\s]+)", link)
        if not found:
            continue
        if wanted is None or found.group(1).strip() == wanted:
            return True
    return False


def is_affiliate_content(text: Optional[str], user_id: Optional[int] = None,
                         tagged: bool = False) -> bool:
    """Whether `text` is affiliate promotion. `tagged=True` is for content LEM generated as (B)
    promotional content, which is affiliate promotion whether or not a link survived the #392
    body/first-comment split."""
    return bool(tagged) or carries_referral_link(text, user_id=user_id)


def disclosure_report(text: Optional[str], user_id: Optional[int] = None,
                      tagged: bool = False) -> dict:
    """Deterministic FTC verdict for one piece of content, ready for a publish gate.

    `{is_affiliate, has_disclosure, ok, reason}`. Non-affiliate content is `ok` and untouched — the
    program does not put a disclosure obligation on the 99% of posts that never mention LEM. Affiliate
    content is `ok` only when the disclosure is present; a deployment with `AFFILIATE_DISCLOSURE_TEXT`
    blanked cannot publish affiliate content at all (`reason='no_disclosure_configured'`), because
    the failure mode of guessing is publishing an undisclosed paid endorsement."""
    affiliate = is_affiliate_content(text, user_id=user_id, tagged=tagged)
    disclosed = has_disclosure(text)
    if not affiliate:
        return {"is_affiliate": False, "has_disclosure": disclosed, "ok": True, "reason": None}
    if not disclosure_text():
        return {"is_affiliate": True, "has_disclosure": disclosed, "ok": False,
                "reason": "no_disclosure_configured"}
    if not disclosed:
        return {"is_affiliate": True, "has_disclosure": False, "ok": False,
                "reason": "missing_ftc_disclosure"}
    return {"is_affiliate": True, "has_disclosure": True, "ok": True, "reason": None}


def apply_disclosure(text: Optional[str], user_id: Optional[int] = None,
                     tagged: bool = False) -> str:
    """`text` with the disclosure appended when it is affiliate content that lacks one.

    This is the generation-time half of "it cannot be left to the user to remember": affiliate copy
    leaves the writer already disclosed, so the publish gate is a backstop rather than the mechanism.
    A no-op on non-affiliate content, on content already disclosed, and when no disclosure sentence
    is configured (there is nothing honest to append — `disclosure_report` blocks that case instead
    of letting it slip through here)."""
    body = str(text or "")
    if not is_affiliate_content(body, user_id=user_id, tagged=tagged) or has_disclosure(body):
        return body
    sentence = disclosure_text()
    if not sentence:
        return body
    return f"{body.rstrip()}\n\n{sentence}" if body.strip() else sentence


# --- orchestration ---------------------------------------------------------------------------------
# The three moments the program actually moves: a user signs up (enrol + attribute), a referred user
# ACTIVATES (convert + pay the referrer), and a user flips a toggle. They live here rather than in
# `api/main.py` because two of the three are triggered from outside the API — activation comes from
# the onboarding evaluator — and a second copy of "what a referral is worth" is exactly the parallel
# path this module exists to prevent.

def _company_page(user_id: int) -> bool:
    """Whether the user has a LinkedIn company page — only consulted when the eligibility boundary
    is switched on, so the common configuration costs no query."""
    if not AFFILIATE_REQUIRE_COMPANY_PAGE:
        return True
    from cqc_lem.utilities.db import get_company_linked_in_url_for_user
    try:
        return bool(get_company_linked_in_url_for_user(user_id))
    except Exception:
        return False


def enroll_user(user_id: int, grant_bonus: bool = True) -> dict:
    """Enrol a user in (A), and pay the enrollment bonus if one is configured (it is 0 by default —
    the reward is per-referral). Idempotent: an existing row is left alone (an opted-out user is NOT
    re-enrolled by a page load) and the bonus grant is a no-op once paid.

    `affiliate_enrolled` is emitted on the call that actually CREATED the row, not on the one that
    paid a bonus: with no join bonus there is no grant to hang it on, and enrollment is exactly the
    thing the marketing funnel needs counted.

    Returns the enrollment row, or `{}` when the program is off / the user is ineligible."""
    from cqc_lem.utilities.db import ensure_affiliate_enrollment, grant_affiliate_trial_days
    from cqc_lem.utilities.observability import AFFILIATE_ENROLLED, track_affiliate_event

    if not program_enabled() or not is_eligible(_company_page(user_id)):
        return {}
    status = enrollment_default()
    enrollment = ensure_affiliate_enrollment(user_id, status=status,
                                             referral_code=code_for_user(user_id)) or {}
    if status != STATUS_ENROLLED or enrollment.get("status") != STATUS_ENROLLED:
        return enrollment
    result: dict = {}
    if grant_bonus and enrollment_bonus_days() > 0:
        result = grant_affiliate_trial_days(user_id, enrollment_bonus_days(), REWARD_ENROLLMENT,
                                            reason="enrollment_bonus") or {}
    paid = bool(result.get("granted")) and result.get("reason") == "granted"
    if enrollment.get("created") or paid:
        track_affiliate_event(AFFILIATE_ENROLLED, user_id=user_id,
                              bonus_days=int(result.get("days") or 0),
                              trial_ends_at=str(result.get("trial_ends_at") or ""))
    return enrollment


def attribute_referral(referred_user_id: int, attribution: Optional[dict]) -> Optional[dict]:
    """Attribute a brand-new signup to the member whose link brought them, if any.

    Every rejection shape is decided HERE and stored, never guessed later: a `ref` that is not a
    number, a referrer who doesn't exist, a referrer who has opted out — and self-referral, which is
    the one that actually pays if it slips through, so it is checked before anything else. Returns
    `{referral_id, referrer_user_id, status, reason}` or None when the signup carried no `ref`."""
    from cqc_lem.utilities.db import (get_affiliate_enrollment, get_user_email,
                                      record_affiliate_referral)
    from cqc_lem.utilities.logger import log_warning
    from cqc_lem.utilities.observability import (AFFILIATE_REFERRAL_ATTRIBUTED,
                                                 AFFILIATE_REFERRAL_REJECTED, track_affiliate_event)

    if not program_enabled():
        return None
    data = attribution if isinstance(attribution, dict) else {}
    referrer_id = parse_referrer_id(data.get(REFERRAL_PARAM))
    if referrer_id is None:
        return None

    reject: Optional[str] = None
    if referrer_id == int(referred_user_id):
        reject = REJECT_SELF_REFERRAL
    else:
        enrollment = get_affiliate_enrollment(referrer_id)
        if not enrollment:
            # No enrollment row at all. Either the `ref` names nobody — or it names a real user who
            # predates the program and has not opened the Account page yet. The second one MUST NOT
            # be rejected: they are enrolled by default, their link works, and rejecting it would
            # quietly zero out every referral for the whole existing user base.
            if not get_user_email(referrer_id):
                reject = REJECT_UNKNOWN_REFERRER
            else:
                enrollment = enroll_user(referrer_id)
                if (enrollment or {}).get("status") != STATUS_ENROLLED:
                    reject = REJECT_NOT_ENROLLED
        elif enrollment.get("status") != STATUS_ENROLLED:
            reject = REJECT_NOT_ENROLLED

    status = REFERRAL_REJECTED if reject else REFERRAL_PENDING
    referral_id = record_affiliate_referral(referrer_id, referred_user_id,
                                            code_for_user(referrer_id), status=status,
                                            reject_reason=reject)
    if referral_id is None:
        # The UNIQUE key held: this signup is already attributed to somebody.
        return {"referral_id": None, "referrer_user_id": referrer_id,
                "status": REFERRAL_REJECTED, "reason": REJECT_DUPLICATE}
    if reject:
        log_warning(f"Referral rejected ({reject})", user_id=referred_user_id)
        track_affiliate_event(AFFILIATE_REFERRAL_REJECTED, user_id=referrer_id,
                              referred_user_id=referred_user_id, reason=reject)
    else:
        track_affiliate_event(AFFILIATE_REFERRAL_ATTRIBUTED, user_id=referrer_id,
                              referred_user_id=referred_user_id)
    return {"referral_id": referral_id, "referrer_user_id": referrer_id,
            "status": status, "reason": reject}


def convert_referral(referred_user_id: int) -> Optional[dict]:
    """Pay the referrer for a referral that has ACTIVATED. Called from the onboarding evaluator, not
    from signup: the issue is explicit that a referral counts on a real activated signup, so a raw
    signup (or a click) earns nothing and a farm of dormant accounts pays nobody.

    Returns the grant result, or None when there was no pending referral to convert."""
    from cqc_lem.utilities.db import convert_affiliate_referral, grant_affiliate_trial_days
    from cqc_lem.utilities.observability import (AFFILIATE_REFERRAL_CONVERTED,
                                                 AFFILIATE_REWARD_GRANTED, track_affiliate_event)

    if not program_enabled():
        return None
    referral = convert_affiliate_referral(referred_user_id)
    if not referral:
        return None
    referrer_id = int(referral["referrer_user_id"])
    track_affiliate_event(AFFILIATE_REFERRAL_CONVERTED, user_id=referrer_id,
                          referred_user_id=referred_user_id)
    result = grant_affiliate_trial_days(referrer_id, referral_bonus_days(), REWARD_REFERRAL,
                                        referral_id=int(referral["id"]), reason="referral_converted")
    if result.get("granted"):
        track_affiliate_event(AFFILIATE_REWARD_GRANTED, user_id=referrer_id,
                              days=result.get("days"), kind=REWARD_REFERRAL,
                              referred_user_id=referred_user_id)
    return {**result, "referrer_user_id": referrer_id, "referral_id": int(referral["id"])}


def set_status(user_id: int, enrolled: bool) -> dict:
    """Flip (A) and settle the enrollment bonus in the same call — opting out revokes it (back to the
    standard trial), opting back in re-grants it under the same per-user cap.

    Opting out ALSO clears (B): consent to publish promo content from the user's account cannot
    outlive the program membership it was given for."""
    from cqc_lem.utilities.db import (ensure_affiliate_enrollment, grant_affiliate_trial_days,
                                      revoke_affiliate_enrollment_bonus, set_affiliate_promo_opt_in,
                                      set_affiliate_status)
    from cqc_lem.utilities.observability import (AFFILIATE_ENROLLED, AFFILIATE_OPTED_OUT,
                                                 AFFILIATE_REWARD_GRANTED, AFFILIATE_REWARD_REVOKED,
                                                 track_affiliate_event)

    ensure_affiliate_enrollment(user_id, status=STATUS_OPTED_OUT, referral_code=code_for_user(user_id))
    enrollment = set_affiliate_status(user_id, enrolled) or {}
    if enrolled:
        result = grant_affiliate_trial_days(user_id, enrollment_bonus_days(), REWARD_ENROLLMENT,
                                            reason="enrollment_bonus") \
            if enrollment_bonus_days() > 0 else {}
        if result.get("granted") and result.get("reason") == "granted":
            track_affiliate_event(AFFILIATE_REWARD_GRANTED, user_id=user_id,
                                  days=result.get("days"), kind=REWARD_ENROLLMENT)
        track_affiliate_event(AFFILIATE_ENROLLED, user_id=user_id, source="opt_in")
        return {"enrollment": enrollment, "reward": result}
    revoked = revoke_affiliate_enrollment_bonus(user_id)
    enrollment = set_affiliate_promo_opt_in(user_id, False, promo_consent_version()) or enrollment
    if revoked.get("revoked"):
        track_affiliate_event(AFFILIATE_REWARD_REVOKED, user_id=user_id, days=revoked.get("days"))
    track_affiliate_event(AFFILIATE_OPTED_OUT, user_id=user_id)
    return {"enrollment": enrollment, "reward": revoked}


def set_promo_consent(user_id: int, enabled: bool) -> dict:
    """Record (B) consent — the only way `promo_content_opt_in` is ever set. Enabling requires the
    user to already hold affiliate status: LEM cannot publish promotion for a program they left."""
    from cqc_lem.utilities.db import get_affiliate_enrollment, set_affiliate_promo_opt_in
    from cqc_lem.utilities.observability import AFFILIATE_PROMO_CONSENT, track_affiliate_event

    current = get_affiliate_enrollment(user_id)
    if enabled and (not current or current.get("status") != STATUS_ENROLLED):
        return {"enrollment": current, "ok": False, "reason": "not_enrolled"}
    enrollment = set_affiliate_promo_opt_in(user_id, enabled, promo_consent_version())
    track_affiliate_event(AFFILIATE_PROMO_CONSENT, user_id=user_id, enabled=bool(enabled),
                          consent_version=promo_consent_version() if enabled else None)
    return {"enrollment": enrollment, "ok": True, "reason": None}


def affiliate_state(user_id: int) -> dict:
    """The whole affiliate picture for one user, as the Account section renders it: status, link,
    referral counts, days earned against the cap, and the two toggles with their consent record.

    `standard_trial_days` and `bonus_days` ride along on purpose — the opt-out copy has to be able
    to say "your trial returns to the standard N days" rather than "you will lose N days"."""
    from cqc_lem.utilities.db import (get_affiliate_enrollment, get_affiliate_referral_counts,
                                      get_affiliate_reward_totals)
    from cqc_lem.utilities.env_constants import FREE_TRIAL_DAYS

    enrollment = get_affiliate_enrollment(user_id) or {}
    eligible = is_eligible(_company_page(user_id))
    status = enrollment.get("status") or STATUS_OPTED_OUT
    if not eligible:
        status = STATUS_INELIGIBLE
    totals = get_affiliate_reward_totals(user_id)
    return {
        "program_enabled": program_enabled(),
        "eligible": eligible,
        "status": status,
        "enrolled": status == STATUS_ENROLLED,
        "referral_code": enrollment.get("referral_code") or code_for_user(user_id),
        "referral_url": link_for_user(user_id) if status == STATUS_ENROLLED else "",
        "notice_seen_at": enrollment.get("notice_seen_at"),
        "referrals": get_affiliate_referral_counts(user_id),
        "days_earned": max(0, int(totals.get("total") or 0)),
        "days_from_referrals": max(0, int(totals.get("referral") or 0)),
        "max_reward_days": max_reward_days(),
        "bonus_days": enrollment_bonus_days(),
        "referral_bonus_days": referral_bonus_days(),
        "standard_trial_days": int(FREE_TRIAL_DAYS),
        "promo_content_opt_in": bool(enrollment.get("promo_content_opt_in")),
        "promo_consent_at": enrollment.get("promo_consent_at"),
        "promo_consent_version": enrollment.get("promo_consent_version"),
        "promo_content_allowed": promo_content_allowed(enrollment),
        "disclosure_text": disclosure_text(),
    }
