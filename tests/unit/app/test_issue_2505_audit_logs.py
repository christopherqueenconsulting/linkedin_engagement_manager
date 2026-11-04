from unittest.mock import patch

from cqc_lem.app import run_content_plan as rcp
from cqc_lem.utilities.db import PostStatus


def test_content_plan_terminal_log_names_queued_held_and_ready_states():
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


def test_content_plan_failure_log_records_terminal_reason():
    with patch.object(rcp, "log_info") as log:
        rcp._log_content_plan_failure(1, 42, "generation_exception")

    context = log.call_args.kwargs
    assert context["terminal_state"] == "failed"
    assert context["status"] == PostStatus.ERROR.value
    assert context["reason"] == "generation_exception"


def test_carousel_claim_log_records_each_kept_and_removed_number():
    before = {"slides": [{"title": "Impact", "content": "Cut costs 40% and helped 12 teams."}]}
    after = {"slides": [{"title": "Impact", "content": "Cut costs 40%."}]}
    with patch.object(rcp, "log_info") as log:
        rcp._log_carousel_claim_audit(1, 42, before, after)

    audits = [call.kwargs for call in log.call_args_list]
    assert [(audit["figure"], audit["outcome"]) for audit in audits] == [
        ("40%", "kept"), ("12", "removed")]
    assert all("context" not in audit and "slide_title" not in audit for audit in audits)
