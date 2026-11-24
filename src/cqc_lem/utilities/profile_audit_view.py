"""What the Account Profile Audit card shows (issue #2753, Phase 2 of #2631).

`build_audit_view` is pure: audits, findings, the user's actions and follower snapshots in, one
payload out. `load_audit_view` does the reads and answers None when ANY read faulted, so the
endpoint can say 503 rather than render "no audits". LEM never edits the LinkedIn profile.

The follower join is a correlation display only: each audit sits beside the audience reading
nearest before it, so a reader can eyeball a change. Nothing here claims the audit caused it.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from cqc_lem.utilities.profile_audit import CHECK_WEIGHTS, FAIL, WARN, diff_audits
from cqc_lem.utilities.profile_audit_llm import LLM_FINDING_ID

OPEN_STATUSES = (WARN, FAIL)
CORRELATION_NOTE = ("Shown side by side so you can compare. A change in followers or views is not "
                    "claimed to come from the audit.")
TREND_LIMIT = 26


class _Row:
    """Adapts a findings dict to the `.check_id` / `.status` shape `diff_audits` reads."""

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.check_id = row["check_id"]
        self.status = row["status"]


def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def finding_state(finding: Mapping[str, Any], action: Optional[Mapping[str, Any]],
                  audit_id: int) -> str:
    """`ignored`, `done`, `open`, `passing` or `unknown` for one finding of audit `audit_id`.

    An ignore is permanent. A done only covers the audit it was pressed on: the next audit
    re-grades the profile, so a fix that did not land shows as open again.
    """
    if finding["status"] not in OPEN_STATUSES:
        return "unknown" if finding["status"] == "unknown" else "passing"
    if action and action["action"] == "ignored":
        return "ignored"
    if action and action["action"] == "done" and action["audit_id"] == audit_id:
        return "done"
    return "open"


def follower_join(audits_oldest_first: Sequence[Mapping[str, Any]],
                  open_counts: Mapping[int, int],
                  snapshots: Optional[Sequence[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
    """One row per audit: score, open count, and the audience reading nearest before it.

    `snapshots` is `get_follower_stats` output. A reading that is absent is None, never 0.
    """
    rows: List[Dict[str, Any]] = []
    previous_followers: Optional[int] = None
    for audit in audits_oldest_first:
        when = _naive_utc(audit.get("created_at"))
        snap = None
        if when is not None:
            snap = next((s for s in (snapshots or [])
                         if _naive_utc(s.get("captured_at")) is not None
                         and _naive_utc(s["captured_at"]) <= when), None)
        followers = snap.get("follower_count") if snap else None
        rows.append({
            "period_key": audit["period_key"],
            "score": audit["score"],
            "open_count": open_counts.get(int(audit["id"]), 0),
            "follower_count": followers,
            "profile_views": snap.get("profile_views") if snap else None,
            "follower_change": (followers - previous_followers
                                if followers is not None and previous_followers is not None else None),
        })
        if followers is not None:
            previous_followers = followers
    return rows


def build_audit_view(audits: Sequence[Mapping[str, Any]],
                     latest_findings: Sequence[Mapping[str, Any]],
                     previous_findings: Optional[Sequence[Mapping[str, Any]]],
                     actions: Mapping[str, Mapping[str, Any]],
                     open_counts: Mapping[int, int],
                     snapshots: Optional[Sequence[Mapping[str, Any]]]) -> Dict[str, Any]:
    """The card payload. `audits` is newest first; an empty list gives `latest: None`."""
    oldest_first = list(reversed(audits))
    trend = [{"period_key": a["period_key"], "kind": a["kind"], "score": a["score"],
              "opened_count": a["opened_count"], "closed_count": a["closed_count"],
              "created_at": a["created_at"].isoformat() if a.get("created_at") else None}
             for a in oldest_first]
    latest: Optional[Dict[str, Any]] = None
    if audits:
        head = audits[0]
        delta = diff_audits(previous_findings, [_Row(f) for f in latest_findings]) \
            if previous_findings is not None else {"closed": [], "opened": [], "still_open": []}
        marks = {"closed": "closed", "opened": "new", "still_open": "still_open"}
        by_check = {c: label for key, label in marks.items() for c in delta[key]}
        findings = []
        for f in latest_findings:
            state = finding_state(f, actions.get(f["check_id"]), int(head["id"]))
            findings.append({
                "check_id": f["check_id"], "status": f["status"], "weight": f["weight"],
                "detail": f["detail"], "fix_hint": f["fix_hint"], "state": state,
                "delta": by_check.get(f["check_id"]),
            })
        findings.sort(key=lambda f: (-int(f["weight"]), f["check_id"]))
        latest = {
            "audit_id": int(head["id"]), "period_key": head["period_key"], "kind": head["kind"],
            "score": head["score"], "rubric_version": head["rubric_version"],
            "created_at": head["created_at"].isoformat() if head.get("created_at") else None,
            "open_count": sum(1 for f in findings if f["state"] == "open"),
            "findings": findings,
        }
    return {
        "latest": latest,
        "trend": trend,
        "correlation": {"note": CORRELATION_NOTE,
                        "rows": follower_join(oldest_first, open_counts, snapshots)},
    }


def load_audit_view(user_id: int) -> Optional[Dict[str, Any]]:
    """Read everything and build the payload. None when any read faulted."""
    from cqc_lem.utilities.db import (
        get_follower_stats,
        get_profile_audit_open_counts,
        get_profile_finding_actions,
        list_profile_audits,
        read_profile_audit_findings,
    )
    audits = list_profile_audits(user_id, limit=TREND_LIMIT)
    actions = get_profile_finding_actions(user_id)
    if audits is None or actions is None:
        return None
    latest_findings: List[Dict[str, Any]] = []
    previous_findings: Optional[List[Dict[str, Any]]] = None
    if audits:
        latest_findings = read_profile_audit_findings(audits[0]["id"])
        if latest_findings is None:
            return None
        if len(audits) > 1:
            previous_findings = read_profile_audit_findings(audits[1]["id"])
            if previous_findings is None:
                return None
    ignored = [c for c, a in actions.items() if a["action"] == "ignored"]
    open_counts = get_profile_audit_open_counts([a["id"] for a in audits], ignored)
    if open_counts is None:
        return None
    # `get_follower_stats` answers [] on a fault, so [] reads as "no snapshots": the audience
    # columns show None, which the card names as not measured.
    oldest = _naive_utc(audits[-1]["created_at"]) if audits else None
    days = 400 if oldest is None else max(30, (datetime.now() - oldest).days + 14)
    snapshots = get_follower_stats(user_id, days=days)
    return build_audit_view(audits, latest_findings, previous_findings, actions, open_counts,
                            snapshots)


def known_check_ids() -> List[str]:
    """The check ids an action may name: the rubric's, plus the monthly LLM finding (weight 0)."""
    return [*CHECK_WEIGHTS, LLM_FINDING_ID]


def report_line(view: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The one weekly-report line: score, change since the previous audit, open findings.

    None when there is no audit, or the latest could not be scored. A score is never shown as 0
    when nothing was readable.
    """
    latest = (view or {}).get("latest")
    if not latest or latest["score"] is None:
        return None
    trend = view["trend"]
    change = None
    if len(trend) > 1 and trend[-2]["score"] is not None:
        change = latest["score"] - trend[-2]["score"]
    open_count = latest["open_count"]
    noun = "finding" if open_count == 1 else "findings"
    text = f"Profile audit: {latest['score']}/100"
    if change:
        text += f" ({change:+d} since the last audit)"
    text += f", {open_count} open {noun}"
    return {"score": latest["score"], "change": change, "open_count": open_count, "line": text}
