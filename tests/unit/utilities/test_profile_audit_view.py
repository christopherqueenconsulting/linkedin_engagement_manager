"""Profile audit card payload, follower join, report line and ignore semantics (issue #2753)."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from cqc_lem.utilities import (
    email as email_mod,
    profile_audit_job as job,
    profile_audit_view as view,
    weekly_report as wr,
)

pytestmark = pytest.mark.unit

T1 = datetime(2026, 11, 10, 12, 0)
T2 = datetime(2026, 11, 17, 12, 0)


def _audit(audit_id, period, score, created, opened=0, closed=0):
    return {"id": audit_id, "kind": "weekly", "period_key": period, "rubric_version": "1",
            "score": score, "previous_audit_id": None, "closed_count": closed,
            "opened_count": opened, "created_at": created}


def _f(check_id, status, weight=10):
    return {"check_id": check_id, "status": status, "weight": weight, "detail": "d", "fix_hint": "h"}


NEW = _audit(2, "2026-W47", 70, T2, closed=1)
OLD = _audit(1, "2026-W46", 50, T1)


def _build(actions=None, snapshots=None, latest=None, previous=None):
    return view.build_audit_view(
        [NEW, OLD],
        latest if latest is not None else [_f("custom_url", "fail"), _f("headline_outcome", "warn", 20),
                                           _f("banner_custom", "pass"), _f("icp_keywords", "unknown", 15)],
        previous if previous is not None else [{"check_id": "custom_url", "status": "fail"},
                                               {"check_id": "banner_custom", "status": "fail"}],
        actions or {}, {2: 2, 1: 3}, snapshots)


def test_trend_is_oldest_first_and_latest_findings_by_weight():
    out = _build()
    assert [p["period_key"] for p in out["trend"]] == ["2026-W46", "2026-W47"]
    assert out["latest"]["audit_id"] == 2
    assert [f["check_id"] for f in out["latest"]["findings"]][0] == "headline_outcome"
    deltas = {f["check_id"]: f["delta"] for f in out["latest"]["findings"]}
    assert deltas["banner_custom"] == "closed"
    assert deltas["custom_url"] == "still_open"
    assert deltas["headline_outcome"] == "new"


def test_open_count_leaves_out_ignored_and_done_on_this_audit():
    out = _build()
    assert out["latest"]["open_count"] == 2
    out = _build(actions={"custom_url": {"action": "ignored", "audit_id": 1},
                          "headline_outcome": {"action": "done", "audit_id": 2}})
    states = {f["check_id"]: f["state"] for f in out["latest"]["findings"]}
    assert states["custom_url"] == "ignored"
    assert states["headline_outcome"] == "done"
    assert out["latest"]["open_count"] == 0


def test_done_on_an_older_audit_reopens():
    assert view.finding_state(_f("custom_url", "fail"), {"action": "done", "audit_id": 1}, 2) == "open"


def test_unknown_is_never_open():
    assert view.finding_state(_f("icp_keywords", "unknown"), None, 2) == "unknown"
    assert view.finding_state(_f("banner_custom", "pass"), None, 2) == "passing"


def test_no_audits_gives_empty_view():
    out = view.build_audit_view([], [], None, {}, {}, [])
    assert out["latest"] is None and out["trend"] == []
    assert out["correlation"]["rows"] == []


def test_follower_join_takes_reading_before_each_audit_and_marks_missing_none():
    snaps = [{"captured_at": datetime(2026, 11, 16), "follower_count": 130, "profile_views": 40},
             {"captured_at": datetime(2026, 11, 9), "follower_count": 100, "profile_views": None}]
    rows = _build(snapshots=snaps)["correlation"]["rows"]
    assert rows[0]["follower_count"] == 100 and rows[0]["follower_change"] is None
    assert rows[1]["follower_count"] == 130 and rows[1]["follower_change"] == 30
    assert rows[0]["profile_views"] is None
    assert rows[1]["open_count"] == 2
    none = _build(snapshots=[])["correlation"]["rows"]
    assert none[0]["follower_count"] is None and none[1]["follower_change"] is None


def test_follower_join_handles_tz_aware_dates():
    audits = [_audit(1, "2026-W46", 50, datetime(2026, 11, 10, 12, tzinfo=timezone.utc))]
    snaps = [{"captured_at": datetime(2026, 11, 9), "follower_count": 5, "profile_views": 1}]
    assert view.follower_join(audits, {}, snaps)[0]["follower_count"] == 5


def test_correlation_note_claims_no_causation():
    assert "not claimed" in _build()["correlation"]["note"]


def test_report_line_score_change_and_open():
    out = _build()
    line = view.report_line(out)
    assert line["score"] == 70 and line["change"] == 20 and line["open_count"] == 2
    assert line["line"] == "Profile audit: 70/100 (+20 since the last audit), 2 open findings"


def test_report_line_none_cases():
    assert view.report_line(None) is None
    assert view.report_line({"latest": None, "trend": []}) is None
    assert view.report_line({"latest": {"score": None}, "trend": []}) is None
    single = view.build_audit_view([OLD], [_f("custom_url", "fail")], None, {}, {1: 1}, [])
    assert view.report_line(single)["line"] == "Profile audit: 50/100, 1 open finding"


def test_known_check_ids():
    assert "custom_url" in view.known_check_ids()
    assert "icp_language_gap" in view.known_check_ids()


# --- load_audit_view ----------------------------------------------------------------------------

_DB = "cqc_lem.utilities.db."


def _load(**over):
    base = {"list_profile_audits": [NEW, OLD], "get_profile_finding_actions": {},
            "read_profile_audit_findings": [_f("custom_url", "fail")],
            "get_profile_audit_open_counts": {2: 1}, "get_follower_stats": []}
    base.update(over)
    patches = [patch(_DB + k, return_value=v) for k, v in base.items()]
    for p in patches:
        p.start()
    try:
        return view.load_audit_view(1)
    finally:
        for p in patches:
            p.stop()


def test_load_builds_view():
    assert _load()["latest"]["audit_id"] == 2


def test_load_empty_history():
    assert _load(list_profile_audits=[])["latest"] is None


@pytest.mark.parametrize("name", ["list_profile_audits", "get_profile_finding_actions",
                                  "read_profile_audit_findings", "get_profile_audit_open_counts"])
def test_any_failed_read_is_none_not_empty(name):
    assert _load(**{name: None}) is None


def test_failed_previous_findings_read_is_none():
    with patch(_DB + "list_profile_audits", return_value=[NEW, OLD]), \
            patch(_DB + "get_profile_finding_actions", return_value={}), \
            patch(_DB + "read_profile_audit_findings", side_effect=[[_f("custom_url", "fail")], None]):
        assert view.load_audit_view(1) is None


def test_ignored_check_is_excluded_from_history_counts():
    with patch(_DB + "list_profile_audits", return_value=[NEW]), \
            patch(_DB + "get_profile_finding_actions",
                  return_value={"custom_url": {"action": "ignored", "audit_id": 2}}), \
            patch(_DB + "read_profile_audit_findings", return_value=[]), \
            patch(_DB + "get_profile_audit_open_counts", return_value={}) as counts, \
            patch(_DB + "get_follower_stats", return_value=[]):
        view.load_audit_view(1)
    assert counts.call_args[0][1] == ["custom_url"]


# --- job: an ignored finding never counts as opened ---------------------------------------------

def test_job_leaves_ignored_out_of_opened_count():
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile
    raw = (LinkedInProfile(full_name="Jane Doe", featured_count=0).model_dump_json(),)
    with patch(_DB + "profile_audit_exists", return_value=False), \
            patch(_DB + "get_linked_in_profile_by_user_id", return_value=raw), \
            patch(_DB + "get_latest_profile_audit", return_value={"id": 7}), \
            patch(_DB + "get_profile_audit_findings", return_value=[]), \
            patch(_DB + "get_profile_finding_actions",
                  return_value={"featured_proof": {"action": "ignored", "audit_id": 7}}), \
            patch(_DB + "insert_profile_audit", return_value=9) as ins:
        job.audit_user(1, "weekly", datetime(2026, 11, 24, tzinfo=timezone.utc))
    opened_with = ins.call_args.kwargs["opened_count"]
    with patch(_DB + "profile_audit_exists", return_value=False), \
            patch(_DB + "get_linked_in_profile_by_user_id", return_value=raw), \
            patch(_DB + "get_latest_profile_audit", return_value={"id": 7}), \
            patch(_DB + "get_profile_audit_findings", return_value=[]), \
            patch(_DB + "get_profile_finding_actions", return_value={}), \
            patch(_DB + "insert_profile_audit", return_value=9) as ins2:
        job.audit_user(1, "weekly", datetime(2026, 11, 24, tzinfo=timezone.utc))
    assert ins2.call_args.kwargs["opened_count"] == opened_with + 1


# --- weekly report line -------------------------------------------------------------------------

def test_weekly_report_loader_adds_the_audit_line():
    line = {"score": 70, "change": None, "open_count": 1, "line": "Profile audit: 70/100, 1 open finding"}
    with patch("cqc_lem.utilities.flags.flag_enabled", return_value=True), \
            patch.object(view, "load_audit_view", return_value={"x": 1}), \
            patch.object(view, "report_line", return_value=line):
        assert wr._profile_audit_line(1) == line


def test_weekly_report_line_none_when_flag_off_or_fault():
    with patch("cqc_lem.utilities.flags.flag_enabled", return_value=False):
        assert wr._profile_audit_line(1) is None
    with patch("cqc_lem.utilities.flags.flag_enabled", return_value=True), \
            patch.object(view, "load_audit_view", side_effect=RuntimeError("boom")):
        assert wr._profile_audit_line(1) is None


def test_build_weekly_report_carries_audit_and_email_shows_one_line():
    line = {"score": 70, "change": 5, "open_count": 2, "line": "Profile audit: 70/100, 2 open findings"}
    kw = dict(week_start=T1, week_end=T2, this_week_rows=[], last_week_rows=[],
              follower_snapshots=[], engager_icp_scores=[], pending_drafts=0)
    with_audit = wr.build_weekly_report(profile_audit=line, **kw)
    assert with_audit["profile_audit"] == line
    assert wr.build_weekly_report(**kw)["profile_audit"] is None
    numbers, _ = email_mod.weekly_report_lines(with_audit)
    assert numbers[-1] == line["line"]
    assert not any("Profile audit" in n for n in email_mod.weekly_report_lines(
        wr.build_weekly_report(**kw))[0])
