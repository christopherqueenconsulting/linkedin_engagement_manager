from unittest.mock import patch

from cqc_lem.app import run_content_plan as rcp
from cqc_lem.utilities.db import PostStatus


def test_content_plan_terminal_log_names_queued_held_and_ready_states() -> None:
    cases = [
        (PostStatus.APPROVED, True, [], "queued", "gates_passed_and_auto_schedule_enabled"),
        (PostStatus.PENDING, True, [{"gate": "missing_asset", "demoted": True}], "held",
         "quality_gate_findings"),
        (PostStatus.PENDING, False, [], "ready", "auto_schedule_disabled"),
    ]
    for status, auto_schedule, findings, state, reason in cases:
        with patch.object(rcp, "log_info") as log:
            rcp._log_content_plan_terminal_state(1, 42, status, auto_schedule, findings)

        log.assert_called_once()
        context = log.call_args.kwargs
        assert context["terminal_state"] == state
        assert context["status"] == status.value
        assert context["reason"] == reason


def test_carousel_claim_log_records_each_kept_and_removed_number() -> None:
    before = {"slides": [{"title": "Impact", "content": "Cut costs 40% and helped 12 teams."}]}
    after = {"slides": [{"title": "Impact", "content": "Cut costs 40%."}]}
    with patch.object(rcp, "log_info") as log:
        rcp._log_carousel_claim_audit(1, 42, before, after)

    audits = [call.kwargs for call in log.call_args_list]
    assert [(audit["figure"], audit["outcome"]) for audit in audits] == [
        ("40%", "kept"), ("12", "removed")]
    assert all("context" not in audit and "slide_title" not in audit for audit in audits)


def test_carousel_claim_log_records_added_figures() -> None:
    before = {"slides": [{"title": "Impact", "content": "Cut costs 40%."}]}
    after = {"slides": [{"title": "Impact", "content": "Cut costs 60%."}]}
    with patch.object(rcp, "log_info") as log:
        rcp._log_carousel_claim_audit(1, 42, before, after)

    audits = [call.kwargs for call in log.call_args_list]
    assert [(audit["figure"], audit["outcome"], audit["reason"]) for audit in audits] == [
        ("40%", "removed", "removed_by_finalizer"),
        ("60%", "added", "introduced_by_finalizer"),
    ]


def test_carousel_claim_log_handles_none_and_partial_slides() -> None:
    assert rcp._carousel_numeric_claims(None) == []
    with patch.object(rcp, "deck_slides", return_value=[{}]):
        assert rcp._carousel_numeric_claims({"slides": [{}]}) == []
    with patch.object(rcp, "deck_slides", return_value=[{"content": "40%"}]), \
            patch.object(rcp, "numeric_claims", return_value=[{"raw": "40%"}]):
        assert rcp._carousel_numeric_claims({"slides": [{}]}) == [
            {"slot": ("unknown", None), "value": "40%"}]


def test_carousel_claim_audit_failure_warns_without_raising() -> None:
    with patch.object(rcp, "numeric_claims", side_effect=ValueError("bad claim data")), \
            patch.object(rcp, "log_warning") as warning:
        rcp._log_carousel_claim_audit(1, 42, {"slides": [{"content": "40%"}]}, {})

    warning.assert_called_once()
    assert warning.call_args.args[0] == "Could not record carousel figure audit"
