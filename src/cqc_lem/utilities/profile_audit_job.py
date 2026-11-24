"""The weekly and monthly profile-audit beats' worker (issue #2631, Phase 1).

Browser-free: it grades the profile JSON already cached by the scrape, never opens a session. One
audit per user per period (`2026-W48` / `2026-11`); the UNIQUE key in `profile_audits` makes a re-run
a no-op. Off unless `PROFILE_AUDIT_ENABLED`. LEM never edits the LinkedIn profile.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.logger import log_info, log_warning
from cqc_lem.utilities.profile_audit import (
    PROFILE_RUBRIC_VERSION,
    audit_profile,
    diff_audits,
    score_results,
)
from cqc_lem.utilities.profile_audit_llm import llm_audit_profile, mine_icp_terms

# A cached profile older than this is not graded: it no longer describes the live page.
PROFILE_MAX_AGE_DAYS = 45


def period_key(kind: str, now: datetime) -> str:
    """`2026-W48` for weekly, `2026-11` for monthly."""
    if kind == "monthly":
        return f"{now.year}-{now.month:02d}"
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _load_profile(user_id: int) -> Optional[LinkedInProfile]:
    from cqc_lem.utilities.db import get_linked_in_profile_by_user_id
    raw = get_linked_in_profile_by_user_id(user_id, PROFILE_MAX_AGE_DAYS)
    if not raw:
        return None
    payload = raw[0] if isinstance(raw, tuple) else raw
    try:
        return LinkedInProfile.model_validate_json(payload)
    except Exception as exc:
        log_warning("Profile audit: stored profile would not parse", exc=exc, user_id=user_id)
        return None


def _icp_vocabulary(user_id: int) -> Tuple[List[str], List[str]]:
    """(mined terms, stored ICP post texts). Any read fault is an empty vocabulary, never a failure."""
    from cqc_lem.utilities.db import get_engagement_preferences, get_icp_post_texts
    try:
        definition = (get_engagement_preferences(user_id) or {}).get("icp_definition")
        texts = get_icp_post_texts(user_id) or []
        return mine_icp_terms(definition, texts), texts
    except Exception as exc:
        log_warning("Profile audit: ICP vocabulary unreadable", exc=exc, user_id=user_id)
        return [], []


def audit_user(user_id: int, kind: str, now: datetime) -> str:
    """Audit one user for the period. Returns 'audited', 'exists', 'no_profile' or 'failed'."""
    from cqc_lem.utilities.db import (
        get_latest_profile_audit,
        get_profile_audit_findings,
        get_profile_finding_actions,
        insert_profile_audit,
        profile_audit_exists,
    )
    key = period_key(kind, now)
    if profile_audit_exists(user_id, kind, key):
        return "exists"
    profile = _load_profile(user_id)
    if profile is None:
        return "no_profile"
    terms, post_texts = _icp_vocabulary(user_id)
    results = audit_profile(profile, terms)
    previous = get_latest_profile_audit(user_id)
    prev_findings = get_profile_audit_findings(previous["id"]) if previous else None
    delta = diff_audits(prev_findings, results)
    findings = [{"check_id": r.check_id, "status": r.status, "weight": r.weight, "detail": r.detail,
                 "fix_hint": r.fix_hint} for r in results]
    if kind == "monthly":
        llm_finding = llm_audit_profile(profile, terms, post_texts, user_id)
        if llm_finding:
            findings.append(llm_finding)
    # An ignored finding is the user's call to stop hearing about it: it never counts as opened.
    actions = get_profile_finding_actions(user_id) or {}
    ignored = {c for c, a in actions.items() if a["action"] == "ignored"}
    delta["opened"] = [c for c in delta["opened"] if c not in ignored]
    audit_id = insert_profile_audit(
        user_id, kind, key, PROFILE_RUBRIC_VERSION, score_results(results), findings,
        previous_audit_id=previous["id"] if previous else None,
        closed_count=len(delta["closed"]), opened_count=len(delta["opened"]))
    return "audited" if audit_id else "failed"


def run_profile_audits(kind: str, now: Optional[datetime] = None) -> Dict[str, int]:
    """Audit every active, flag-enabled user with a stored profile. Counts per outcome."""
    from cqc_lem.utilities.db import get_active_user_ids
    from cqc_lem.utilities.flags import PROFILE_AUDIT, flag_enabled
    stamp = now or datetime.now(timezone.utc)
    counts = {"audited": 0, "exists": 0, "no_profile": 0, "failed": 0, "skipped": 0}
    for user_id in get_active_user_ids():
        if not flag_enabled(PROFILE_AUDIT, user_id):
            counts["skipped"] += 1
            continue
        try:
            counts[audit_user(user_id, kind, stamp)] += 1
        except Exception as exc:
            log_warning("Profile audit failed for a user", exc=exc, user_id=user_id)
            counts["failed"] += 1
    if counts["audited"]:
        log_info(f"Profile audit ({kind}): {counts}", task_name="auto_profile_audit")
    return counts
