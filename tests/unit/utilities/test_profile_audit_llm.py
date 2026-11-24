"""Monthly LLM profile deep audit (issue #2754): vocabulary mining, fail-open, report-only."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from cqc_lem.utilities import profile_audit_job as job, profile_audit_llm as llm
from cqc_lem.utilities.linkedin.profile import LinkedInProfile

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)
PROFILE = LinkedInProfile(full_name="Jane Doe", headline="Fractional CFO for SaaS",
                          about="I help finance teams close faster.")
DEFINITION = {"segments": [{"id": "s", "pains": ["slow month-end close"],
                            "trigger_phrases": ["audit readiness"], "role_titles": ["Controller"]}]}


def _reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_mine_terms_definition_first_then_recurring_words():
    posts = ["Reconciliation pain again", "reconciliation is slow", "unique oddity"]
    terms = llm.mine_icp_terms(DEFINITION, posts)
    assert terms[:3] == ["audit readiness", "slow month-end close", "controller"]
    assert "reconciliation" in terms and "oddity" not in terms
    assert llm.mine_icp_terms(None, None) == []
    assert llm.mine_icp_terms("bad", []) == []


def test_parse_drops_invented_phrases():
    raw = ('{"mismatches": [{"buyer_phrase": "audit readiness", "profile_phrase": "close faster", '
           '"note": "n"}, {"buyer_phrase": "made up", "profile_phrase": null}]}')
    out = llm.parse_mismatches(raw, "I help finance teams close faster.", "audit readiness")
    assert out == [{"buyer_phrase": "audit readiness", "profile_phrase": "close faster", "note": "n"}]
    assert llm.parse_mismatches("not json", "a", "b") == []


def test_llm_audit_returns_zero_weight_finding():
    reply = '{"mismatches": [{"buyer_phrase": "audit readiness", "profile_phrase": null, "note": "Add it"}]}'
    with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_reply(reply)) as call:
        finding = llm.llm_audit_profile(PROFILE, ["audit readiness"], [], 1)
    assert call.call_args.kwargs["model"] == "lem-medium"
    assert finding["check_id"] == llm.LLM_FINDING_ID and finding["weight"] == 0
    assert "audit readiness" in finding["detail"]


def test_llm_audit_fails_open_and_skips_without_vocabulary():
    with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=RuntimeError("down")):
        assert llm.llm_audit_profile(PROFILE, ["x"], [], 1) is None
    with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as call:
        assert llm.llm_audit_profile(PROFILE, [], [], 1) is None
        assert llm.llm_audit_profile(LinkedInProfile(full_name="J"), ["x"], [], 1) is None
        call.assert_not_called()
    with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_reply('{"mismatches": []}')):
        assert llm.llm_audit_profile(PROFILE, ["x"], [], 1) is None


def _run(kind, finding):
    raw = (PROFILE.model_dump_json(),)
    with patch("cqc_lem.utilities.db.profile_audit_exists", return_value=False), \
            patch("cqc_lem.utilities.db.get_linked_in_profile_by_user_id", return_value=raw), \
            patch("cqc_lem.utilities.db.get_latest_profile_audit", return_value=None), \
            patch("cqc_lem.utilities.db.get_engagement_preferences",
                  return_value={"icp_definition": DEFINITION}), \
            patch("cqc_lem.utilities.db.get_icp_post_texts", return_value=["a b"]), \
            patch.object(job, "llm_audit_profile", return_value=finding) as llm_call, \
            patch("cqc_lem.utilities.db.insert_profile_audit", return_value=5) as ins:
        assert job.audit_user(1, kind, NOW) == "audited"
    return ins.call_args.args[5], llm_call


def test_monthly_stores_llm_finding_and_resolves_icp_keywords():
    finding = {"check_id": llm.LLM_FINDING_ID, "status": "warn", "weight": 0, "detail": "d",
               "fix_hint": "h"}
    findings, _ = _run("monthly", finding)
    ids = {f["check_id"]: f for f in findings}
    assert llm.LLM_FINDING_ID in ids
    assert ids["icp_keywords"]["status"] != "unknown"


def test_weekly_never_calls_llm_and_llm_none_keeps_rubric_only():
    findings, llm_call = _run("weekly", None)
    llm_call.assert_not_called()
    assert llm.LLM_FINDING_ID not in {f["check_id"] for f in findings}
    findings, _ = _run("monthly", None)
    assert len(findings) == 7


def test_vocabulary_fault_reads_empty():
    with patch("cqc_lem.utilities.db.get_engagement_preferences", side_effect=RuntimeError("x")):
        assert job._icp_vocabulary(1) == ([], [])
